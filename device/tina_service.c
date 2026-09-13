#define _GNU_SOURCE
#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64

/*
 * SynCap adapter for the small Allwinner/Tina stereo recorder.
 *
 * The vendor qgapp process owns both sensors, the IMU and the audio device. It
 * is deliberately started once. qgapp writes two-second MCAP segments into a
 * tmpfs ring and publishes the two raw HEVC previews on TCP ports 9100 and
 * 9101. A capture is therefore a transfer policy for already-running qgapp,
 * not another recorder process. If qgapp exits, the Tina camera stack cannot
 * be reopened reliably without a reboot, so the service fails closed instead
 * of repeatedly starting new producers.
 *
 * This file has no third-party dependencies and is suitable for a static musl
 * build. The HTTP implementation intentionally supports only the small subset
 * needed by the SynCap client and closes every connection after one request.
 */

#include <arpa/inet.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <net/if.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/select.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/ioctl.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#ifndef PATH_MAX
#define PATH_MAX 4096
#endif

#ifndef O_NOFOLLOW
/* Darwin hides O_NOFOLLOW under this strict POSIX feature set. Host tests
 * retain lstat/fstat checks; Tina/Linux uses the real no-follow flag. */
#define O_NOFOLLOW 0
#endif

#define SERVICE_VERSION "0.2.5"
#define HTTP_LIMIT (32U * 1024U)
#define IO_BLOCK (64U * 1024U)
#define DEFAULT_PORT 8080
#define DEFAULT_MIN_FREE (512ULL * 1024ULL * 1024ULL)
#define ESTIMATED_BYTES_PER_SECOND 1500000ULL
#define SEGMENT_SECONDS 2
#define INTEGRITY_STABLE_SEGMENTS 2
#define STOP_WAIT_SECONDS 8
#define STOP_DRAIN_SECONDS 45
#define EXPORT_STALL_SECONDS 15
#define MCAP_MAGIC_SIZE 8
#define MAX_INCOMPLETE_SEGMENTS 64
#define QUARANTINE_RESERVE_BYTES 4096
#define WIFI_COMMAND_OUTPUT_LIMIT (512U * 1024U)
#define MAX_WIFI_NETWORKS 64
#define WIFI_SCAN_CACHE_MAX_AGE_MS 15000ULL

static const unsigned char MCAP_MAGIC[MCAP_MAGIC_SIZE] = {
    0x89, 'M', 'C', 'A', 'P', '0', '\r', '\n'
};

struct string_buf {
    char *data;
    size_t len;
    size_t cap;
};

struct config {
    char ring_dir[PATH_MAX];
    char qgapp_path[PATH_MAX];
    char qgapp_pidfile[PATH_MAX];
    char qgapp_log[PATH_MAX];
    char wifi_path[PATH_MAX];
    char iw_path[PATH_MAX];
    char storage_candidates[4][PATH_MAX];
    size_t storage_candidate_count;
    uint16_t port;
    uint64_t minimum_free_bytes;
    bool dry_run;
    bool allow_unmounted_storage;
    bool allow_missing_preview_listeners;
};

struct export_file;

struct integrity_counters {
    long long left_drops;
    long long right_drops;
    long long imu_gaps;
    long long venc_resets;
    long long queue_drops;
    long long audio_timeouts;
    long long audio_backsteps;
    long long audio_gaps;
    long long frame_backsteps;
    long long frame_counter_drops[2];
};

struct integrity_observation {
    struct integrity_counters counters;
    long long audio_frames;
    long long fsync_marks;
    long long imu_frame_counter_last;
};

struct integrity_baseline_state {
    bool observed;
    bool established;
    unsigned stable_segments;
    uint64_t generation;
    struct integrity_observation last;
};

struct capture_state {
    bool active;
    bool finalizing;
    bool failed;
    char stop_segment[NAME_MAX + 1];
    char failure_reason[192];
    char capture_id[96];
    char session_id[96];
    char name[256];
    char session_dir[PATH_MAX];
    char storage_root[PATH_MAX];
    char created_at[64];
    uint64_t started_mono_ns;
    uint64_t failed_duration_ms;
    unsigned long segments_moved;
    struct export_file *files;
    size_t file_count;
    size_t file_capacity;
    uint64_t total_size;
    bool integrity_baseline_valid;
    struct integrity_counters integrity_baseline;
    struct integrity_observation integrity_last;
};

struct storage_info {
    bool mounted;
    bool writable;
    bool can_capture;
    uint64_t total_bytes;
    uint64_t free_bytes;
    const char *reason;
    char root[PATH_MAX];
};

struct http_request {
    char method[12];
    char path[2048];
    char host[256];
    char range[256];
    char body[HTTP_LIMIT + 1];
    size_t body_len;
};

struct sync_cache {
    bool observed;
    bool valid;
    bool hardware_trigger;
    unsigned long fsync_marks;
    long long frame_backstep;
    long long left_drops;
    long long right_drops;
};

static struct config g_cfg;
static struct capture_state g_capture;
static volatile sig_atomic_t g_running = 1;
static pid_t g_qgapp_pid = -1;
static int g_server_fd = -1;
static unsigned long g_idle_clean_segments_discarded = 0;
static struct sync_cache g_sync;
static bool g_ring_quarantined = false;
static char g_ring_quarantine_reason[256];
static bool g_producer_intentionally_stopped = false;
static uint64_t g_last_terminal_segment_ns = 0;
static uint64_t g_qgapp_tracking_started_ns = 0;
static bool g_qgapp_has_clean_segment = false;
static struct integrity_baseline_state g_integrity_baseline;

static void quarantine_ring(const char *reason);

struct incomplete_segment_observation {
    char name[NAME_MAX + 1];
    uint64_t first_seen_ns;
};

static struct incomplete_segment_observation g_incomplete_segments[MAX_INCOMPLETE_SEGMENTS];

static void log_line(const char *level, const char *fmt, ...) {
    char timestamp[32];
    time_t now = time(NULL);
    struct tm local;
    va_list args;

    localtime_r(&now, &local);
    strftime(timestamp, sizeof(timestamp), "%Y-%m-%dT%H:%M:%S", &local);
    fprintf(stderr, "%s %s ", timestamp, level);
    va_start(args, fmt);
    vfprintf(stderr, fmt, args);
    va_end(args);
    fputc('\n', stderr);
    fflush(stderr);
}

static uint64_t monotonic_ns(void) {
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0) return 0;
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void iso_now(char *output, size_t size) {
    time_t now = time(NULL);
    struct tm utc;
    gmtime_r(&now, &utc);
    strftime(output, size, "%Y-%m-%dT%H:%M:%SZ", &utc);
}

static void id_stamp(char *output, size_t size) {
    time_t now = time(NULL);
    struct tm local;
    localtime_r(&now, &local);
    strftime(output, size, "%Y%m%d_%H%M%S", &local);
}

static bool safe_copy(char *dst, size_t dst_size, const char *src) {
    size_t length = strlen(src);
    if (length >= dst_size) return false;
    memcpy(dst, src, length + 1);
    return true;
}

static bool path_join(char *out, size_t size, const char *left, const char *right) {
    int written = snprintf(out, size, "%s%s%s", left,
                           left[0] && left[strlen(left) - 1] == '/' ? "" : "/", right);
    return written >= 0 && (size_t)written < size;
}

static bool fsync_directory(const char *path) {
    int fd;
    int saved;
    fd = open(path, O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    if (fd < 0) return false;
    if (fsync(fd) == 0) {
        close(fd);
        return true;
    }
    saved = errno;
#ifdef __linux__
    if (saved == EINVAL || saved == ENOTSUP) {
        if (syncfs(fd) == 0) {
            close(fd);
            return true;
        }
        saved = errno;
    }
#else
    if (saved == EINVAL || saved == ENOTSUP) {
        close(fd);
        return true;
    }
#endif
    close(fd);
    errno = saved;
    return false;
}

static bool fsync_parent_directory(const char *path) {
    char parent[PATH_MAX];
    char *slash;
    if (!safe_copy(parent, sizeof(parent), path)) {
        errno = ENAMETOOLONG;
        return false;
    }
    slash = strrchr(parent, '/');
    if (!slash) return fsync_directory(".");
    if (slash == parent) slash[1] = '\0';
    else *slash = '\0';
    return fsync_directory(parent);
}

static bool ensure_plain_directory(const char *path, mode_t mode, bool *created) {
    struct stat st;
    if (created) *created = false;
    if (mkdir(path, mode) == 0) {
        if (created) *created = true;
        return fsync_parent_directory(path);
    }
    if (errno != EEXIST || lstat(path, &st) != 0 || !S_ISDIR(st.st_mode)) {
        if (errno == EEXIST) errno = ENOTDIR;
        return false;
    }
    return true;
}

static bool file_exists(const char *path) {
    struct stat st;
    return lstat(path, &st) == 0 && S_ISREG(st.st_mode);
}

static uint64_t incomplete_segment_age_ns(const char *name) {
    uint64_t now = monotonic_ns();
    size_t index;
    for (index = 0; index < MAX_INCOMPLETE_SEGMENTS; ++index) {
        if (strcmp(g_incomplete_segments[index].name, name) == 0)
            return now - g_incomplete_segments[index].first_seen_ns;
    }
    for (index = 0; index < MAX_INCOMPLETE_SEGMENTS; ++index) {
        if (!g_incomplete_segments[index].name[0]) {
            safe_copy(g_incomplete_segments[index].name,
                      sizeof(g_incomplete_segments[index].name), name);
            g_incomplete_segments[index].first_seen_ns = now;
            return 0;
        }
    }
    /* More incomplete entries than the tmpfs should reasonably hold is itself
     * unsafe.  Fail closed instead of silently forgetting an older segment. */
    return UINT64_MAX;
}

static void clear_incomplete_segment(const char *name) {
    size_t index;
    for (index = 0; index < MAX_INCOMPLETE_SEGMENTS; ++index) {
        if (strcmp(g_incomplete_segments[index].name, name) == 0) {
            memset(&g_incomplete_segments[index], 0, sizeof(g_incomplete_segments[index]));
            return;
        }
    }
}

static bool process_alive(pid_t pid) {
    char path[64];
    char stat_line[512];
    char *closing_paren;
    FILE *stat_file;
    if (pid <= 1) return false;
    if (kill(pid, 0) != 0 && errno != EPERM) return false;
#ifdef __linux__
    snprintf(path, sizeof(path), "/proc/%ld/stat", (long)pid);
    stat_file = fopen(path, "r");
    if (!stat_file) return false;
    if (!fgets(stat_line, sizeof(stat_line), stat_file)) {
        fclose(stat_file);
        return false;
    }
    fclose(stat_file);
    closing_paren = strrchr(stat_line, ')');
    if (closing_paren && closing_paren[1] == ' ' && closing_paren[2] == 'Z') return false;
#else
    (void)path;
    (void)stat_line;
    (void)closing_paren;
    (void)stat_file;
#endif
    return true;
}

static bool qgapp_comm_matches(pid_t pid) {
#ifdef __linux__
    {
        char path[64];
        char comm[64];
        FILE *file;
        snprintf(path, sizeof(path), "/proc/%ld/comm", (long)pid);
        file = fopen(path, "r");
        if (!file) return false;
        if (!fgets(comm, sizeof(comm), file)) {
            fclose(file);
            return false;
        }
        fclose(file);
        comm[strcspn(comm, "\r\n")] = '\0';
        return strcmp(comm, "qgapp") == 0 && process_alive(pid);
    }
#else
    (void)pid;
    return false;
#endif
}

static bool qgapp_arguments_match(pid_t pid) {
#ifdef __linux__
    static const char *fixed_arguments[] = {"1600", "1200", "30", "4000", "2"};
    char path[64];
    char command[PATH_MAX + 128];
    const char *cursor;
    const char *end;
    int fd;
    ssize_t count;
    size_t index;
    snprintf(path, sizeof(path), "/proc/%ld/cmdline", (long)pid);
    fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return false;
    count = read(fd, command, sizeof(command));
    close(fd);
    if (count <= 0 || (size_t)count >= sizeof(command) || command[count - 1] != '\0') return false;
    cursor = command;
    end = command + count;
    if (strcmp(cursor, g_cfg.qgapp_path) != 0) return false;
    cursor += strlen(cursor) + 1;
    if (cursor >= end || strcmp(cursor, g_cfg.ring_dir) != 0) return false;
    cursor += strlen(cursor) + 1;
    for (index = 0; index < sizeof(fixed_arguments) / sizeof(fixed_arguments[0]); ++index) {
        if (cursor >= end || strcmp(cursor, fixed_arguments[index]) != 0) return false;
        cursor += strlen(cursor) + 1;
    }
    return cursor == end;
#else
    (void)pid;
    return false;
#endif
}

static bool qgapp_identity_matches(pid_t pid) {
    if (!process_alive(pid)) return false;
    /* Host tests intentionally use a shell fixture. Production adoption and
     * signalling are restricted to one exact vendor command line. */
    if (strcmp(g_cfg.qgapp_path, "/usr/bin/qgapp") != 0) return true;
    return qgapp_comm_matches(pid) && qgapp_arguments_match(pid);
}

static bool sb_reserve(struct string_buf *buf, size_t extra) {
    size_t needed = buf->len + extra + 1;
    size_t next;
    char *grown;
    if (needed <= buf->cap) return true;
    next = buf->cap ? buf->cap : 1024;
    while (next < needed) {
        if (next > SIZE_MAX / 2) return false;
        next *= 2;
    }
    grown = realloc(buf->data, next);
    if (!grown) return false;
    buf->data = grown;
    buf->cap = next;
    return true;
}

static bool sb_append_n(struct string_buf *buf, const char *text, size_t length) {
    if (!sb_reserve(buf, length)) return false;
    memcpy(buf->data + buf->len, text, length);
    buf->len += length;
    buf->data[buf->len] = '\0';
    return true;
}

static bool sb_append(struct string_buf *buf, const char *text) {
    return sb_append_n(buf, text, strlen(text));
}

static bool sb_printf(struct string_buf *buf, const char *fmt, ...) {
    va_list args;
    va_list copy;
    int needed;
    va_start(args, fmt);
    va_copy(copy, args);
    needed = vsnprintf(NULL, 0, fmt, copy);
    va_end(copy);
    if (needed < 0 || !sb_reserve(buf, (size_t)needed)) {
        va_end(args);
        return false;
    }
    vsnprintf(buf->data + buf->len, buf->cap - buf->len, fmt, args);
    va_end(args);
    buf->len += (size_t)needed;
    return true;
}

static bool sb_json_string(struct string_buf *buf, const char *text) {
    const unsigned char *cursor = (const unsigned char *)text;
    if (!sb_append(buf, "\"")) return false;
    while (*cursor) {
        char escaped[7];
        switch (*cursor) {
            case '\"': if (!sb_append(buf, "\\\"")) return false; break;
            case '\\': if (!sb_append(buf, "\\\\")) return false; break;
            case '\b': if (!sb_append(buf, "\\b")) return false; break;
            case '\f': if (!sb_append(buf, "\\f")) return false; break;
            case '\n': if (!sb_append(buf, "\\n")) return false; break;
            case '\r': if (!sb_append(buf, "\\r")) return false; break;
            case '\t': if (!sb_append(buf, "\\t")) return false; break;
            default:
                if (*cursor < 0x20) {
                    snprintf(escaped, sizeof(escaped), "\\u%04x", *cursor);
                    if (!sb_append(buf, escaped)) return false;
                } else if (!sb_append_n(buf, (const char *)cursor, 1)) {
                    return false;
                }
        }
        ++cursor;
    }
    return sb_append(buf, "\"");
}

static void sb_free(struct string_buf *buf) {
    free(buf->data);
    memset(buf, 0, sizeof(*buf));
}

static bool write_all(int fd, const void *data, size_t length) {
    const unsigned char *cursor = data;
    while (length) {
        ssize_t written = write(fd, cursor, length);
        if (written < 0) {
            if (errno == EINTR) continue;
            return false;
        }
        if (written == 0) return false;
        cursor += written;
        length -= (size_t)written;
    }
    return true;
}

static bool read_small_file(const char *path, struct string_buf *buf, size_t limit) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    char block[4096];
    struct stat st;
    ssize_t count;
    if (fd < 0) return false;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode)) {
        close(fd);
        errno = EINVAL;
        return false;
    }
    while ((count = read(fd, block, sizeof(block))) > 0) {
        if (buf->len + (size_t)count > limit || !sb_append_n(buf, block, (size_t)count)) {
            close(fd);
            errno = EFBIG;
            return false;
        }
    }
    close(fd);
    if (count != 0) return false;
    /* A successfully read empty file is still a valid C string.  qgapp can
     * leave a zero-length sidecar behind when it is interrupted, so callers
     * must never receive { data = NULL, len = 0 } on success. */
    return buf->data != NULL || sb_append_n(buf, "", 0);
}

/* Compact SHA-256 implementation used to make exports independently verifiable. */
struct sha256_ctx {
    uint32_t state[8];
    uint64_t bits;
    unsigned char block[64];
    size_t used;
};

static uint32_t rotr32(uint32_t value, unsigned amount) {
    return (value >> amount) | (value << (32U - amount));
}

static void sha256_transform(struct sha256_ctx *ctx, const unsigned char block[64]) {
    static const uint32_t k[64] = {
        0x428a2f98U,0x71374491U,0xb5c0fbcfU,0xe9b5dba5U,0x3956c25bU,0x59f111f1U,0x923f82a4U,0xab1c5ed5U,
        0xd807aa98U,0x12835b01U,0x243185beU,0x550c7dc3U,0x72be5d74U,0x80deb1feU,0x9bdc06a7U,0xc19bf174U,
        0xe49b69c1U,0xefbe4786U,0x0fc19dc6U,0x240ca1ccU,0x2de92c6fU,0x4a7484aaU,0x5cb0a9dcU,0x76f988daU,
        0x983e5152U,0xa831c66dU,0xb00327c8U,0xbf597fc7U,0xc6e00bf3U,0xd5a79147U,0x06ca6351U,0x14292967U,
        0x27b70a85U,0x2e1b2138U,0x4d2c6dfcU,0x53380d13U,0x650a7354U,0x766a0abbU,0x81c2c92eU,0x92722c85U,
        0xa2bfe8a1U,0xa81a664bU,0xc24b8b70U,0xc76c51a3U,0xd192e819U,0xd6990624U,0xf40e3585U,0x106aa070U,
        0x19a4c116U,0x1e376c08U,0x2748774cU,0x34b0bcb5U,0x391c0cb3U,0x4ed8aa4aU,0x5b9cca4fU,0x682e6ff3U,
        0x748f82eeU,0x78a5636fU,0x84c87814U,0x8cc70208U,0x90befffaU,0xa4506cebU,0xbef9a3f7U,0xc67178f2U
    };
    uint32_t words[64];
    uint32_t a,b,c,d,e,f,g,h;
    unsigned i;
    for (i = 0; i < 16; ++i) {
        words[i] = ((uint32_t)block[i * 4] << 24) |
                   ((uint32_t)block[i * 4 + 1] << 16) |
                   ((uint32_t)block[i * 4 + 2] << 8) |
                   (uint32_t)block[i * 4 + 3];
    }
    for (i = 16; i < 64; ++i) {
        uint32_t s0 = rotr32(words[i - 15], 7) ^ rotr32(words[i - 15], 18) ^ (words[i - 15] >> 3);
        uint32_t s1 = rotr32(words[i - 2], 17) ^ rotr32(words[i - 2], 19) ^ (words[i - 2] >> 10);
        words[i] = words[i - 16] + s0 + words[i - 7] + s1;
    }
    a=ctx->state[0]; b=ctx->state[1]; c=ctx->state[2]; d=ctx->state[3];
    e=ctx->state[4]; f=ctx->state[5]; g=ctx->state[6]; h=ctx->state[7];
    for (i = 0; i < 64; ++i) {
        uint32_t s1 = rotr32(e,6) ^ rotr32(e,11) ^ rotr32(e,25);
        uint32_t choice = (e & f) ^ ((~e) & g);
        uint32_t temp1 = h + s1 + choice + k[i] + words[i];
        uint32_t s0 = rotr32(a,2) ^ rotr32(a,13) ^ rotr32(a,22);
        uint32_t majority = (a & b) ^ (a & c) ^ (b & c);
        uint32_t temp2 = s0 + majority;
        h=g; g=f; f=e; e=d+temp1; d=c; c=b; b=a; a=temp1+temp2;
    }
    ctx->state[0]+=a; ctx->state[1]+=b; ctx->state[2]+=c; ctx->state[3]+=d;
    ctx->state[4]+=e; ctx->state[5]+=f; ctx->state[6]+=g; ctx->state[7]+=h;
}

static void sha256_init(struct sha256_ctx *ctx) {
    static const uint32_t initial[8] = {
        0x6a09e667U,0xbb67ae85U,0x3c6ef372U,0xa54ff53aU,
        0x510e527fU,0x9b05688cU,0x1f83d9abU,0x5be0cd19U
    };
    memcpy(ctx->state, initial, sizeof(initial));
    ctx->bits = 0;
    ctx->used = 0;
}

static void sha256_update(struct sha256_ctx *ctx, const unsigned char *data, size_t length) {
    ctx->bits += (uint64_t)length * 8ULL;
    while (length) {
        size_t space = sizeof(ctx->block) - ctx->used;
        size_t take = length < space ? length : space;
        memcpy(ctx->block + ctx->used, data, take);
        ctx->used += take;
        data += take;
        length -= take;
        if (ctx->used == sizeof(ctx->block)) {
            sha256_transform(ctx, ctx->block);
            ctx->used = 0;
        }
    }
}

static void sha256_final(struct sha256_ctx *ctx, unsigned char digest[32]) {
    unsigned i;
    ctx->block[ctx->used++] = 0x80;
    if (ctx->used > 56) {
        memset(ctx->block + ctx->used, 0, 64 - ctx->used);
        sha256_transform(ctx, ctx->block);
        ctx->used = 0;
    }
    memset(ctx->block + ctx->used, 0, 56 - ctx->used);
    for (i = 0; i < 8; ++i) ctx->block[63 - i] = (unsigned char)(ctx->bits >> (i * 8));
    sha256_transform(ctx, ctx->block);
    for (i = 0; i < 8; ++i) {
        digest[i * 4] = (unsigned char)(ctx->state[i] >> 24);
        digest[i * 4 + 1] = (unsigned char)(ctx->state[i] >> 16);
        digest[i * 4 + 2] = (unsigned char)(ctx->state[i] >> 8);
        digest[i * 4 + 3] = (unsigned char)ctx->state[i];
    }
}

static bool has_suffix(const char *text, const char *suffix) {
    size_t text_len = strlen(text);
    size_t suffix_len = strlen(suffix);
    return text_len >= suffix_len && strcmp(text + text_len - suffix_len, suffix) == 0;
}

static bool is_safe_component(const char *value) {
    const unsigned char *cursor = (const unsigned char *)value;
    if (!*cursor) return false;
    while (*cursor) {
        if (!(isalnum(*cursor) || *cursor == '_' || *cursor == '-' || *cursor == '.')) return false;
        ++cursor;
    }
    return strcmp(value, ".") != 0 && strcmp(value, "..") != 0;
}

static bool mcap_is_closed(const char *path) {
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    struct stat st;
    unsigned char tail[MCAP_MAGIC_SIZE];
    ssize_t count;
    if (fd < 0) return false;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < MCAP_MAGIC_SIZE ||
        lseek(fd, st.st_size - MCAP_MAGIC_SIZE, SEEK_SET) < 0) {
        close(fd);
        return false;
    }
    count = read(fd, tail, sizeof(tail));
    close(fd);
    return count == MCAP_MAGIC_SIZE && memcmp(tail, MCAP_MAGIC, MCAP_MAGIC_SIZE) == 0;
}

enum segment_status_state {
    SEGMENT_STATUS_PENDING,
    SEGMENT_STATUS_CLEAN,
    SEGMENT_STATUS_INTEGRITY_FAILED,
    SEGMENT_STATUS_FAILED
};

static long long json_get_integer(const char *json, const char *key, long long fallback);
static bool json_get_boolean(const char *json, const char *key, bool fallback);

static bool json_object_copy(const char *json, const char *key, char *out, size_t out_size) {
    char needle[128];
    const char *cursor;
    const char *start;
    unsigned depth = 0;
    bool in_string = false;
    bool escaped = false;
    int written = snprintf(needle, sizeof(needle), "\"%s\"", key);
    if (written < 0 || (size_t)written >= sizeof(needle)) return false;
    cursor = strstr(json, needle);
    if (!cursor) return false;
    cursor += strlen(needle);
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor++ != ':') return false;
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor != '{') return false;
    start = cursor;
    for (; *cursor; ++cursor) {
        char ch = *cursor;
        if (in_string) {
            if (escaped) escaped = false;
            else if (ch == '\\') escaped = true;
            else if (ch == '"') in_string = false;
            continue;
        }
        if (ch == '"') in_string = true;
        else if (ch == '{') ++depth;
        else if (ch == '}') {
            size_t length;
            if (!depth) return false;
            --depth;
            if (depth) continue;
            length = (size_t)(cursor - start + 1);
            if (length >= out_size) return false;
            memcpy(out, start, length);
            out[length] = '\0';
            return true;
        }
    }
    return false;
}

static bool integrity_counters_equal(const struct integrity_counters *left,
                                     const struct integrity_counters *right) {
    return left->left_drops == right->left_drops &&
           left->right_drops == right->right_drops &&
           left->imu_gaps == right->imu_gaps &&
           left->venc_resets == right->venc_resets &&
           left->queue_drops == right->queue_drops &&
           left->audio_timeouts == right->audio_timeouts &&
           left->audio_backsteps == right->audio_backsteps &&
           left->audio_gaps == right->audio_gaps &&
           left->frame_backsteps == right->frame_backsteps &&
           left->frame_counter_drops[0] == right->frame_counter_drops[0] &&
           left->frame_counter_drops[1] == right->frame_counter_drops[1];
}

static bool integrity_progress_advanced(const struct integrity_observation *current,
                                        const struct integrity_observation *previous) {
    return current->audio_frames > previous->audio_frames &&
           current->fsync_marks > previous->fsync_marks &&
           current->imu_frame_counter_last > previous->imu_frame_counter_last;
}

static bool integrity_counters_are_startup_clean(const struct integrity_counters *counters) {
    return counters->left_drops == 0 && counters->right_drops == 0 &&
           counters->imu_gaps == 0 && counters->venc_resets == 0 &&
           counters->queue_drops == 0 && counters->audio_timeouts == 0 &&
           counters->audio_backsteps == 0 && counters->audio_gaps == 0 &&
           counters->frame_backsteps == 0 &&
           counters->frame_counter_drops[0] == 0 &&
           counters->frame_counter_drops[1] == 0;
}

static bool parse_segment_integrity(const char *json,
                                    struct integrity_observation *observation) {
    char drops_object[512];
    char audio_object[512];
    char sync_object[4096];
    const char *drops;
    const char *array;
    long long imu_first;
    memset(observation, 0, sizeof(*observation));
    if (!json_object_copy(json, "drops", drops_object, sizeof(drops_object)) ||
        !json_object_copy(json, "audio", audio_object, sizeof(audio_object)) ||
        !json_object_copy(json, "sync", sync_object, sizeof(sync_object))) return false;
    drops = strstr(sync_object, "\"fc_drops\"");
    array = drops ? strchr(drops, '[') : NULL;
    imu_first = json_get_integer(sync_object, "imu_fc_first", -1);
    observation->counters.left_drops = json_get_integer(drops_object, "left", -1);
    observation->counters.right_drops = json_get_integer(drops_object, "right", -1);
    observation->counters.imu_gaps = json_get_integer(drops_object, "imu_gaps", -1);
    observation->counters.venc_resets = json_get_integer(drops_object, "venc_resets", -1);
    observation->counters.queue_drops = json_get_integer(drops_object, "q_drops", -1);
    observation->audio_frames = json_get_integer(audio_object, "frames", 0);
    observation->counters.audio_timeouts = json_get_integer(audio_object, "timeouts", -1);
    observation->counters.audio_backsteps = json_get_integer(audio_object, "backsteps", -1);
    observation->counters.audio_gaps = json_get_integer(audio_object, "gaps", -1);
    observation->fsync_marks = json_get_integer(sync_object, "fsync_marks", 0);
    observation->imu_frame_counter_last = json_get_integer(sync_object, "imu_fc_last", -1);
    observation->counters.frame_backsteps =
        json_get_integer(sync_object, "frame_backstep", -1);
    observation->counters.frame_counter_drops[0] = -1;
    observation->counters.frame_counter_drops[1] = -1;
    if (array) {
        (void)sscanf(array, "[ %lld , %lld",
                     &observation->counters.frame_counter_drops[0],
                     &observation->counters.frame_counter_drops[1]);
    }
    return observation->counters.left_drops >= 0 &&
           observation->counters.right_drops >= 0 &&
           observation->counters.imu_gaps >= 0 &&
           observation->counters.venc_resets >= 0 &&
           observation->counters.queue_drops >= 0 &&
           json_get_boolean(audio_object, "enabled", false) &&
           observation->audio_frames > 0 &&
           observation->counters.audio_timeouts >= 0 &&
           observation->counters.audio_backsteps >= 0 &&
           observation->counters.audio_gaps >= 0 &&
           json_get_boolean(sync_object, "hw_trigger", false) &&
           observation->fsync_marks > 0 &&
           observation->counters.frame_backsteps >= 0 &&
           imu_first >= 0 && observation->imu_frame_counter_last > imu_first &&
           observation->counters.frame_counter_drops[0] >= 0 &&
           observation->counters.frame_counter_drops[1] >= 0;
}

static bool json_root_object_complete(const char *json, size_t length) {
    size_t index = 0;
    unsigned object_depth = 0;
    unsigned array_depth = 0;
    bool in_string = false;
    bool escaped = false;
    while (index < length && isspace((unsigned char)json[index])) ++index;
    if (index == length || json[index] != '{') return false;
    for (; index < length; ++index) {
        char ch = json[index];
        if (in_string) {
            if (escaped) escaped = false;
            else if (ch == '\\') escaped = true;
            else if (ch == '"') in_string = false;
            continue;
        }
        if (ch == '"') in_string = true;
        else if (ch == '{') ++object_depth;
        else if (ch == '[') ++array_depth;
        else if (ch == '}') {
            if (!object_depth) return false;
            --object_depth;
        } else if (ch == ']') {
            if (!array_depth) return false;
            --array_depth;
        }
        if (!in_string && object_depth == 0 && array_depth == 0) {
            ++index;
            while (index < length && isspace((unsigned char)json[index])) ++index;
            return index == length;
        }
    }
    return false;
}

static enum segment_status_state segment_status_state(
    const char *path, struct integrity_observation *observation) {
    struct string_buf status = {0};
    struct integrity_observation parsed;
    enum segment_status_state state = SEGMENT_STATUS_PENDING;
    size_t length;
    if (!read_small_file(path, &status, 1024U * 1024U)) goto done;
    length = status.len;
    while (length && isspace((unsigned char)status.data[length - 1])) --length;
    /* qgapp creates/writes sidecars non-atomically.  Empty or truncated JSON
     * means "still publishing", not a failed capture. */
    if (!length || status.data[length - 1] != '}' ||
        !json_root_object_complete(status.data, length)) goto done;
    if (strstr(status.data, "\"state\": \"clean\"") != NULL ||
        strstr(status.data, "\"state\":\"clean\"") != NULL) {
        state = parse_segment_integrity(status.data, &parsed)
                    ? SEGMENT_STATUS_CLEAN
                    : SEGMENT_STATUS_INTEGRITY_FAILED;
        if (state == SEGMENT_STATUS_CLEAN && observation) *observation = parsed;
    } else if (strstr(status.data, "\"state\": \"failed\"") != NULL ||
               strstr(status.data, "\"state\":\"failed\"") != NULL) {
        state = SEGMENT_STATUS_FAILED;
    }
done:
    sb_free(&status);
    return state;
}

static void reset_integrity_baseline(void) {
    memset(&g_integrity_baseline, 0, sizeof(g_integrity_baseline));
    g_qgapp_has_clean_segment = false;
}

static void invalidate_integrity_baseline(void) {
    uint64_t generation = g_integrity_baseline.generation + 1;
    memset(&g_integrity_baseline, 0, sizeof(g_integrity_baseline));
    g_integrity_baseline.generation = generation;
    g_qgapp_has_clean_segment = false;
}

static bool observe_idle_integrity(const struct integrity_observation *observation) {
    bool counters_stable = g_integrity_baseline.observed &&
        integrity_counters_equal(&observation->counters,
                                 &g_integrity_baseline.last.counters);
    bool progress_advanced = g_integrity_baseline.observed &&
        integrity_progress_advanced(observation, &g_integrity_baseline.last);
    if (!counters_stable || !progress_advanced) {
        uint64_t generation = g_integrity_baseline.generation + 1;
        memset(&g_integrity_baseline, 0, sizeof(g_integrity_baseline));
        g_integrity_baseline.observed = true;
        g_integrity_baseline.stable_segments = 1;
        g_integrity_baseline.generation = generation;
        g_integrity_baseline.last = *observation;
        g_qgapp_has_clean_segment = false;
        return false;
    }
    g_integrity_baseline.last = *observation;
    if (g_integrity_baseline.stable_segments < INTEGRITY_STABLE_SEGMENTS)
        ++g_integrity_baseline.stable_segments;
    g_integrity_baseline.established =
        g_integrity_baseline.stable_segments >= INTEGRITY_STABLE_SEGMENTS;
    g_qgapp_has_clean_segment = g_integrity_baseline.established;
    return g_integrity_baseline.established;
}

static bool capture_accepts_integrity(const struct integrity_observation *observation) {
    if (!g_capture.integrity_baseline_valid ||
        !integrity_counters_equal(&observation->counters,
                                  &g_capture.integrity_baseline) ||
        !integrity_progress_advanced(observation, &g_capture.integrity_last)) return false;
    g_capture.integrity_last = *observation;
    return true;
}

static bool copy_file_atomic(const char *source, const char *destination,
                             uint64_t *size_out, char sha_out[65]) {
    char temporary[PATH_MAX];
    unsigned char block[IO_BLOCK];
    unsigned char digest[32];
    static const char digits[] = "0123456789abcdef";
    struct sha256_ctx hash;
    uint64_t total = 0;
    unsigned index;
    int source_fd = -1;
    int dest_fd = -1;
    ssize_t count;
    bool ok = false;
    int written;
    int saved_error;

    written = snprintf(temporary, sizeof(temporary), "%s.part.%ld", destination, (long)getpid());
    if (written < 0 || (size_t)written >= sizeof(temporary)) {
        errno = ENAMETOOLONG;
        return false;
    }
    source_fd = open(source, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (source_fd < 0) goto done;
    {
        struct stat source_st;
        if (fstat(source_fd, &source_st) != 0 || !S_ISREG(source_st.st_mode)) {
            errno = EINVAL;
            goto done;
        }
    }
    dest_fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0640);
    if (dest_fd < 0) goto done;
    sha256_init(&hash);
    while ((count = read(source_fd, block, sizeof(block))) > 0) {
        if (!write_all(dest_fd, block, (size_t)count)) goto done;
        sha256_update(&hash, block, (size_t)count);
        total += (uint64_t)count;
    }
    if (count < 0 || fsync(dest_fd) != 0) goto done;
    /* FAT creates a new directory entry on rename. Sync the still-open file
     * again to persist its size/start cluster at that new entry before the
     * tmpfs source can be retired; syncing only the parent leaves zero-byte
     * files on disk until the kernel's later inode writeback. */
    if (rename(temporary, destination) != 0 || fsync(dest_fd) != 0 ||
        !fsync_parent_directory(destination)) goto done;
    if (close(dest_fd) != 0) {
        dest_fd = -1;
        goto done;
    }
    dest_fd = -1;
    sha256_final(&hash, digest);
    for (index = 0; index < 32; ++index) {
        sha_out[index * 2] = digits[digest[index] >> 4];
        sha_out[index * 2 + 1] = digits[digest[index] & 15];
    }
    sha_out[64] = '\0';
    *size_out = total;
    ok = true;
done:
    saved_error = errno;
    if (source_fd >= 0) close(source_fd);
    if (dest_fd >= 0) close(dest_fd);
    if (!ok) {
        unlink(temporary);
        errno = saved_error;
    }
    return ok;
}

static bool read_pidfile(const char *path, pid_t *pid) {
    FILE *file = fopen(path, "r");
    long value;
    if (!file) return false;
    if (fscanf(file, "%ld", &value) != 1 || value <= 1 || value > INT_MAX) {
        fclose(file);
        return false;
    }
    fclose(file);
    *pid = (pid_t)value;
    return true;
}

static void write_pidfile(const char *path, pid_t pid) {
    char temporary[PATH_MAX];
    FILE *file;
    int written = snprintf(temporary, sizeof(temporary), "%s.tmp", path);
    if (written < 0 || (size_t)written >= sizeof(temporary)) return;
    file = fopen(temporary, "w");
    if (!file) return;
    fprintf(file, "%ld\n", (long)pid);
    if (fclose(file) == 0) rename(temporary, path);
    else unlink(temporary);
}

#ifdef __linux__
static pid_t find_qgapp_process(void) {
    DIR *proc = opendir("/proc");
    struct dirent *entry;
    pid_t matched = -1;
    unsigned qgapp_count = 0;
    if (!proc) return -1;
    while ((entry = readdir(proc)) != NULL) {
        char comm_path[PATH_MAX];
        char comm[64] = {0};
        char *end;
        long value;
        FILE *file;
        if (!isdigit((unsigned char)entry->d_name[0])) continue;
        value = strtol(entry->d_name, &end, 10);
        if (*end || value <= 1 || value > INT_MAX) continue;
        snprintf(comm_path, sizeof(comm_path), "/proc/%s/comm", entry->d_name);
        file = fopen(comm_path, "r");
        if (!file) continue;
        if (fgets(comm, sizeof(comm), file)) {
            comm[strcspn(comm, "\r\n")] = '\0';
            if (strcmp(comm, "qgapp") == 0 && process_alive((pid_t)value)) {
                ++qgapp_count;
                if (qgapp_arguments_match((pid_t)value)) matched = (pid_t)value;
            }
        }
        fclose(file);
    }
    closedir(proc);
    if (qgapp_count == 0) return -1;
    return qgapp_count == 1 && matched > 1 ? matched : (pid_t)-2;
}

static unsigned signal_all_qgapp_processes(int signal_number) {
    DIR *proc = opendir("/proc");
    struct dirent *entry;
    unsigned signalled = 0;
    if (!proc) return 0;
    while ((entry = readdir(proc)) != NULL) {
        char *end;
        long value;
        if (!isdigit((unsigned char)entry->d_name[0])) continue;
        value = strtol(entry->d_name, &end, 10);
        if (*end || value <= 1 || value > INT_MAX || !qgapp_comm_matches((pid_t)value)) continue;
        if (kill((pid_t)value, signal_number) == 0) ++signalled;
    }
    closedir(proc);
    return signalled;
}
#else
static pid_t find_qgapp_process(void) { return -1; }
static unsigned signal_all_qgapp_processes(int signal_number) {
    (void)signal_number;
    return 0;
}
#endif

#ifdef __linux__
static bool proc_tcp_has_listener(const char *path, uint16_t port) {
    FILE *file = fopen(path, "r");
    char line[512];
    if (!file) return false;
    while (fgets(line, sizeof(line), file)) {
        char local[160];
        char remote[160];
        char state[8];
        char *colon;
        unsigned long parsed_port;
        if (sscanf(line, " %*d: %159s %159s %7s", local, remote, state) != 3) continue;
        colon = strrchr(local, ':');
        if (!colon) continue;
        parsed_port = strtoul(colon + 1, NULL, 16);
        if (parsed_port == port && strcmp(state, "0A") == 0) {
            fclose(file);
            return true;
        }
    }
    fclose(file);
    return false;
}
#endif

/* This check is read-only: raw HEVC listeners may allow only one consumer. */
static bool tcp_port_listening(uint16_t port) {
#ifdef __linux__
    return proc_tcp_has_listener("/proc/net/tcp", port) ||
           proc_tcp_has_listener("/proc/net/tcp6", port);
#else
    (void)port;
    return false;
#endif
}

static bool qgapp_running(void) {
    if (g_cfg.dry_run) return true;
    return qgapp_identity_matches(g_qgapp_pid) &&
           tcp_port_listening(9100) && tcp_port_listening(9101);
}

static bool qgapp_ready_for_capture(void) {
    if (g_cfg.dry_run) return true;
    if (!qgapp_identity_matches(g_qgapp_pid) || !g_qgapp_has_clean_segment) return false;
    return g_cfg.allow_missing_preview_listeners ||
           (tcp_port_listening(9100) && tcp_port_listening(9101));
}

static bool ensure_qgapp(bool start_if_missing) {
    pid_t pid;
    int log_fd;

    if (g_cfg.dry_run) return true;
    if (strcmp(g_cfg.qgapp_path, "/usr/bin/qgapp") != 0 &&
        read_pidfile(g_cfg.qgapp_pidfile, &pid) && qgapp_identity_matches(pid)) {
        g_qgapp_pid = pid;
        g_last_terminal_segment_ns = monotonic_ns();
        g_qgapp_tracking_started_ns = g_last_terminal_segment_ns;
        reset_integrity_baseline();
        log_line("INFO", "adopting qgapp pid %ld from pidfile", (long)pid);
        return true;
    }
    pid = find_qgapp_process();
    if (pid == (pid_t)-2) {
        quarantine_ring("conflicting qgapp process detected; reboot is required");
        log_line("ERROR", "%s", g_ring_quarantine_reason);
        return false;
    }
    if (qgapp_identity_matches(pid)) {
        g_qgapp_pid = pid;
        g_last_terminal_segment_ns = monotonic_ns();
        g_qgapp_tracking_started_ns = g_last_terminal_segment_ns;
        reset_integrity_baseline();
        write_pidfile(g_cfg.qgapp_pidfile, pid);
        log_line("INFO", "adopting existing qgapp pid %ld", (long)pid);
        return true;
    }
    if (!start_if_missing) return false;
    if (!ensure_plain_directory(g_cfg.ring_dir, 0750, NULL)) {
        log_line("ERROR", "cannot create ring %s: %s", g_cfg.ring_dir, strerror(errno));
        return false;
    }
    pid = fork();
    if (pid < 0) {
        log_line("ERROR", "cannot fork qgapp: %s", strerror(errno));
        return false;
    }
    if (pid == 0) {
        log_fd = open(g_cfg.qgapp_log, O_WRONLY | O_CREAT | O_APPEND, 0640);
        if (log_fd >= 0) {
            dup2(log_fd, STDOUT_FILENO);
            dup2(log_fd, STDERR_FILENO);
            if (log_fd > STDERR_FILENO) close(log_fd);
        }
        setenv("C06_STREAM", "1", 1);
        execl(g_cfg.qgapp_path, g_cfg.qgapp_path, g_cfg.ring_dir,
              "1600", "1200", "30", "4000", "2", (char *)NULL);
        _exit(127);
    }
    g_qgapp_pid = pid;
    g_last_terminal_segment_ns = monotonic_ns();
    g_qgapp_tracking_started_ns = g_last_terminal_segment_ns;
    reset_integrity_baseline();
    write_pidfile(g_cfg.qgapp_pidfile, pid);
    log_line("INFO", "started persistent qgapp pid %ld", (long)pid);
    return true;
}

#ifdef __linux__
static void decode_mount_token(char *text) {
    char *read_cursor = text;
    char *write_cursor = text;
    while (*read_cursor) {
        if (read_cursor[0] == '\\' && isdigit((unsigned char)read_cursor[1]) &&
            isdigit((unsigned char)read_cursor[2]) && isdigit((unsigned char)read_cursor[3])) {
            int value = (read_cursor[1] - '0') * 64 + (read_cursor[2] - '0') * 8 + (read_cursor[3] - '0');
            *write_cursor++ = (char)value;
            read_cursor += 4;
        } else {
            *write_cursor++ = *read_cursor++;
        }
    }
    *write_cursor = '\0';
}
#endif

static bool path_is_real_mount(const char *path, bool *mount_writable) {
#ifdef __linux__
    FILE *mounts = fopen("/proc/self/mounts", "r");
    char line[2048];
    bool found = false;
    if (!mounts) mounts = fopen("/proc/mounts", "r");
    if (!mounts) return false;
    while (fgets(line, sizeof(line), mounts)) {
        char mountpoint[PATH_MAX];
        char options[512];
        char padded_options[520];
        if (sscanf(line, "%*s %4095s %*s %511s", mountpoint, options) != 2) continue;
        decode_mount_token(mountpoint);
        if (strcmp(path, mountpoint) == 0) {
            found = true;
            snprintf(padded_options, sizeof(padded_options), ",%s,", options);
            *mount_writable = strstr(padded_options, ",ro,") == NULL;
            break;
        }
    }
    fclose(mounts);
    return found;
#else
    (void)path; (void)mount_writable;
    return false;
#endif
}

static bool probe_path_writable(const char *path) {
    char probe[PATH_MAX];
    int fd;
    int written = snprintf(probe, sizeof(probe), "%s/.syncap-write-test-%ld", path, (long)getpid());
    if (written < 0 || (size_t)written >= sizeof(probe)) return false;
    fd = open(probe, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return false;
    close(fd);
    unlink(probe);
    return true;
}

static struct storage_info inspect_storage(bool verify_write) {
    struct storage_info info;
    size_t index;
    memset(&info, 0, sizeof(info));
    info.reason = "not_available";
    for (index = 0; index < g_cfg.storage_candidate_count; ++index) {
        const char *candidate = g_cfg.storage_candidates[index];
        struct stat st;
        struct statvfs fs;
        bool mounted;
        bool mount_writable = true;
        if (lstat(candidate, &st) != 0 || !S_ISDIR(st.st_mode)) continue;
        mounted = g_cfg.allow_unmounted_storage || path_is_real_mount(candidate, &mount_writable);
        if (!mounted) {
            if (info.reason && strcmp(info.reason, "not_available") == 0) info.reason = "not_mounted";
            continue;
        }
        info.mounted = true;
        safe_copy(info.root, sizeof(info.root), candidate);
        if (statvfs(candidate, &fs) != 0) {
            info.reason = "stat_failed";
            return info;
        }
        info.total_bytes = (uint64_t)fs.f_blocks * (uint64_t)fs.f_frsize;
        info.free_bytes = (uint64_t)fs.f_bavail * (uint64_t)fs.f_frsize;
        info.writable = mount_writable && access(candidate, W_OK) == 0;
        if (info.writable && verify_write) info.writable = probe_path_writable(candidate);
        if (!info.writable) {
            info.reason = "not_writable";
            return info;
        }
        if (info.free_bytes < g_cfg.minimum_free_bytes) {
            info.reason = "insufficient_free_space";
            return info;
        }
        info.can_capture = true;
        info.reason = NULL;
        return info;
    }
    return info;
}

static bool append_storage_json(struct string_buf *json, const struct storage_info *storage) {
    return sb_printf(json,
        "{\"target\":\"usb\",\"canCapture\":%s,"
        "\"capturePolicy\":{\"minimumFreeBytes\":%llu,\"estimatedBytesPerSecond\":%llu},"
        "\"internal\":{\"available\":false,\"mounted\":false,\"label\":\"Internal\","
        "\"totalBytes\":0,\"freeBytes\":0,\"canCapture\":false,\"reason\":\"not_available\"},"
        "\"usb\":{\"available\":%s,\"mounted\":%s,\"label\":\"SDCARD\","
        "\"mediaType\":\"sd\",\"displayName\":\"SD 卡\","
        "\"totalBytes\":%llu,\"freeBytes\":%llu,\"canCapture\":%s,\"reason\":",
        storage->can_capture ? "true" : "false",
        (unsigned long long)g_cfg.minimum_free_bytes,
        (unsigned long long)ESTIMATED_BYTES_PER_SECOND,
        storage->mounted ? "true" : "false", storage->mounted ? "true" : "false",
        (unsigned long long)storage->total_bytes, (unsigned long long)storage->free_bytes,
        storage->can_capture ? "true" : "false") &&
        (storage->reason ? sb_json_string(json, storage->reason) : sb_append(json, "null")) &&
        sb_append(json, "}}");
}

static unsigned hex_value(char ch) {
    if (ch >= '0' && ch <= '9') return (unsigned)(ch - '0');
    if (ch >= 'a' && ch <= 'f') return (unsigned)(ch - 'a' + 10);
    if (ch >= 'A' && ch <= 'F') return (unsigned)(ch - 'A' + 10);
    return 16;
}

static bool append_utf8(char *out, size_t out_size, size_t *used, unsigned value) {
    unsigned char bytes[4];
    size_t count;
    if (value <= 0x7f) { bytes[0] = (unsigned char)value; count = 1; }
    else if (value <= 0x7ff) {
        bytes[0] = 0xc0 | (unsigned char)(value >> 6);
        bytes[1] = 0x80 | (unsigned char)(value & 0x3f); count = 2;
    } else {
        bytes[0] = 0xe0 | (unsigned char)(value >> 12);
        bytes[1] = 0x80 | (unsigned char)((value >> 6) & 0x3f);
        bytes[2] = 0x80 | (unsigned char)(value & 0x3f); count = 3;
    }
    if (*used + count >= out_size) return false;
    memcpy(out + *used, bytes, count);
    *used += count;
    return true;
}

/* Extracts the string values used by our small request bodies. */
static bool json_get_string(const char *json, const char *key, char *out, size_t out_size) {
    char needle[128];
    const char *cursor;
    size_t used = 0;
    int written = snprintf(needle, sizeof(needle), "\"%s\"", key);
    if (written < 0 || (size_t)written >= sizeof(needle)) return false;
    cursor = strstr(json, needle);
    if (!cursor) return false;
    cursor += strlen(needle);
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor++ != ':') return false;
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor++ != '\"') return false;
    while (*cursor && *cursor != '\"') {
        unsigned char ch = (unsigned char)*cursor++;
        if (ch == '\\') {
            ch = (unsigned char)*cursor++;
            switch (ch) {
                case '\"': case '\\': case '/': break;
                case 'b': ch = '\b'; break;
                case 'f': ch = '\f'; break;
                case 'n': ch = '\n'; break;
                case 'r': ch = '\r'; break;
                case 't': ch = '\t'; break;
                case 'u': {
                    unsigned value = 0;
                    unsigned i;
                    for (i = 0; i < 4; ++i) {
                        if (!cursor[i]) return false;
                        unsigned digit = hex_value(cursor[i]);
                        if (digit > 15) return false;
                        value = value * 16 + digit;
                    }
                    cursor += 4;
                    if (!append_utf8(out, out_size, &used, value)) return false;
                    continue;
                }
                default: return false;
            }
        }
        if (used + 1 >= out_size) return false;
        out[used++] = (char)ch;
    }
    if (*cursor != '\"') return false;
    out[used] = '\0';
    return true;
}

static long long json_get_integer(const char *json, const char *key, long long fallback) {
    char needle[128];
    const char *cursor;
    char *end;
    long long value;
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    cursor = strstr(json, needle);
    if (!cursor) return fallback;
    cursor += strlen(needle);
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor++ != ':') return fallback;
    while (isspace((unsigned char)*cursor)) ++cursor;
    value = strtoll(cursor, &end, 10);
    return end == cursor ? fallback : value;
}

static bool json_get_boolean(const char *json, const char *key, bool fallback) {
    char needle[128];
    const char *cursor;
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    cursor = strstr(json, needle);
    if (!cursor) return fallback;
    cursor += strlen(needle);
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor++ != ':') return fallback;
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (strncmp(cursor, "true", 4) == 0) return true;
    if (strncmp(cursor, "false", 5) == 0) return false;
    return fallback;
}

static void update_sync_cache(const char *status_path) {
    struct string_buf status = {0};
    const char *drops;
    const char *open;
    long long left = -1;
    long long right = -1;
    long long fsync;
    long long backstep;
    bool hardware;
    if (!read_small_file(status_path, &status, 1024U * 1024U)) {
        sb_free(&status);
        return;
    }
    hardware = json_get_boolean(status.data, "hw_trigger", false);
    fsync = json_get_integer(status.data, "fsync_marks", 0);
    backstep = json_get_integer(status.data, "frame_backstep", -1);
    drops = strstr(status.data, "\"fc_drops\"");
    open = drops ? strchr(drops, '[') : NULL;
    if (open) sscanf(open, "[ %lld , %lld", &left, &right);
    /* Vendor counters are cumulative for the lifetime of qgapp.  Startup can
     * add a few timestamp backsteps before the first stable segment, so judge
     * counter health by deltas between consecutive segments instead of
     * requiring lifetime totals to remain zero forever.  These counters do
     * not measure exposure timestamps or camera-to-camera skew. */
    g_sync.valid = g_sync.observed && hardware && g_sync.hardware_trigger &&
                   fsync > (long long)g_sync.fsync_marks &&
                   backstep >= 0 && backstep == g_sync.frame_backstep &&
                   left >= 0 && left == g_sync.left_drops &&
                   right >= 0 && right == g_sync.right_drops;
    g_sync.observed = true;
    g_sync.hardware_trigger = hardware;
    g_sync.fsync_marks = fsync > 0 ? (unsigned long)fsync : 0;
    g_sync.frame_backstep = backstep;
    g_sync.left_drops = left;
    g_sync.right_drops = right;
    sb_free(&status);
}

struct export_file {
    char name[NAME_MAX + 1];
    uint64_t size;
    char sha256[65];
};

static void reset_capture(void) {
    free(g_capture.files);
    memset(&g_capture, 0, sizeof(g_capture));
}

static uint64_t capture_elapsed_ms(void) {
    return g_capture.failed ? g_capture.failed_duration_ms
                            : (monotonic_ns() - g_capture.started_mono_ns) / 1000000ULL;
}

static bool remember_capture_file(const char *name, uint64_t size, const char *sha256) {
    struct export_file *grown;
    size_t index;
    for (index = 0; index < g_capture.file_count; ++index) {
        if (strcmp(g_capture.files[index].name, name) == 0) {
            g_capture.total_size -= g_capture.files[index].size;
            g_capture.files[index].size = size;
            safe_copy(g_capture.files[index].sha256, sizeof(g_capture.files[index].sha256), sha256);
            g_capture.total_size += size;
            return true;
        }
    }
    if (g_capture.file_count == g_capture.file_capacity) {
        size_t capacity = g_capture.file_capacity ? g_capture.file_capacity * 2 : 32;
        grown = realloc(g_capture.files, capacity * sizeof(*grown));
        if (!grown) return false;
        g_capture.files = grown;
        g_capture.file_capacity = capacity;
    }
    memset(&g_capture.files[g_capture.file_count], 0, sizeof(*g_capture.files));
    safe_copy(g_capture.files[g_capture.file_count].name,
              sizeof(g_capture.files[g_capture.file_count].name), name);
    g_capture.files[g_capture.file_count].size = size;
    safe_copy(g_capture.files[g_capture.file_count].sha256,
              sizeof(g_capture.files[g_capture.file_count].sha256), sha256);
    ++g_capture.file_count;
    g_capture.total_size += size;
    return true;
}

static int export_file_compare(const void *left, const void *right) {
    const struct export_file *a = left;
    const struct export_file *b = right;
    return strcmp(a->name, b->name);
}

static bool append_file_array(struct string_buf *json, const struct export_file *files,
                              size_t count, const char *session_id, bool include_urls) {
    size_t index;
    if (!sb_append(json, "[")) return false;
    for (index = 0; index < count; ++index) {
        if (index && !sb_append(json, ",")) return false;
        if (!sb_append(json, "{\"name\":" ) || !sb_json_string(json, files[index].name) ||
            !sb_printf(json, ",\"sizeBytes\":%llu,\"sha256\":\"%s\"",
                       (unsigned long long)files[index].size, files[index].sha256)) return false;
        if (include_urls) {
            if (!sb_append(json, ",\"url\":\"/v1/sessions/") ||
                !sb_append(json, session_id) || !sb_append(json, "/files/") ||
                !sb_append(json, files[index].name) || !sb_append(json, "\"")) return false;
        }
        if (!sb_append(json, "}")) return false;
    }
    return sb_append(json, "]");
}

static bool write_atomic_text(const char *path, const char *content, size_t length) {
    char temporary[PATH_MAX];
    int fd;
    int written = snprintf(temporary, sizeof(temporary), "%s.tmp.%ld", path, (long)getpid());
    if (written < 0 || (size_t)written >= sizeof(temporary)) return false;
    fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0640);
    if (fd < 0) return false;
    if (!write_all(fd, content, length) || fsync(fd) != 0) {
        close(fd);
        unlink(temporary);
        return false;
    }
    /* Persist the replacement FAT entry, not just the temporary filename. */
    if (rename(temporary, path) != 0 || fsync(fd) != 0 || !fsync_parent_directory(path)) {
        int saved = errno;
        close(fd);
        unlink(temporary);
        errno = saved;
        return false;
    }
    if (close(fd) != 0) {
        unlink(temporary);
        return false;
    }
    return true;
}

static bool build_session_manifest(struct string_buf *json, const char *state,
                                   uint64_t duration_ms, const struct export_file *files,
                                   size_t file_count, uint64_t total_size) {
    char completed_at[64];
    iso_now(completed_at, sizeof(completed_at));
    if (!sb_append(json, "{\"schema\":\"syncap.session/1.0\",\"id\":") ||
        !sb_json_string(json, g_capture.session_id) ||
        !sb_append(json, ",\"captureId\":") || !sb_json_string(json, g_capture.capture_id) ||
        !sb_append(json, ",\"name\":") || !sb_json_string(json, g_capture.name) ||
        !sb_append(json, ",\"state\":") || !sb_json_string(json, state) ||
        !sb_append(json, ",\"createdAt\":") || !sb_json_string(json, g_capture.created_at) ||
        !sb_append(json, ",\"startedAt\":") || !sb_json_string(json, g_capture.created_at) ||
        !sb_printf(json, ",\"startedAtDeviceTimeNs\":\"%llu\",\"durationMs\":%llu",
                   (unsigned long long)g_capture.started_mono_ns,
                   (unsigned long long)duration_ms)) return false;
    if (strcmp(state, "recording") != 0) {
        if (!sb_append(json, ",\"completedAt\":") || !sb_json_string(json, completed_at)) return false;
    }
    if (strcmp(state, "failed") == 0) {
        const char *reason = g_capture.failure_reason[0]
                                 ? g_capture.failure_reason : g_ring_quarantine_reason;
        if (!sb_append(json, ",\"recoveryRequired\":true,\"rebootRequired\":true,\"failureReason\":") ||
            !sb_json_string(json, reason)) return false;
    }
    if (!sb_append(json,
        ",\"sources\":[\"left\",\"right\",\"imu0\",\"audio0\"],"
        "\"recording\":{\"container\":\"segmented-mcap\",\"segmentSeconds\":2,"
        "\"videoCodec\":\"h265\",\"videoWidth\":1600,\"videoHeight\":1200,"
        "\"frameRate\":29.4118,\"copyOnly\":true,\"includes\":[\"left\",\"right\",\"/ego/imu\",\"/audio\"]},"
        "\"synchronization\":{\"cameraMethod\":\"hardware_trigger\","
        "\"ptp\":{\"supported\":false,\"requiredForCapture\":false,\"reason\":\"not_required\"}},"
        "\"storage\":{\"target\":\"usb\",\"label\":\"SDCARD\"},\"sizeBytes\":")) return false;
    if (!sb_printf(json, "%llu,\"fileCount\":%lu,\"files\":",
                   (unsigned long long)total_size, (unsigned long)file_count) ||
        !append_file_array(json, files, file_count, g_capture.session_id, false) ||
        !sb_append(json, "}")) return false;
    return true;
}

static bool write_session_state(const char *state, uint64_t duration_ms,
                                const struct export_file *files, size_t file_count,
                                uint64_t total_size) {
    struct string_buf json = {0};
    char path[PATH_MAX];
    bool ok = build_session_manifest(&json, state, duration_ms, files, file_count, total_size) &&
              path_join(path, sizeof(path), g_capture.session_dir, "session.json") &&
              write_atomic_text(path, json.data, json.len);
    sb_free(&json);
    return ok;
}

static bool ring_quarantine_path(char *path, size_t size) {
    return path_join(path, size, g_cfg.ring_dir, ".syncap-quarantine");
}

static bool ring_active_path(char *path, size_t size) {
    return path_join(path, size, g_cfg.ring_dir, ".syncap-active.json");
}

static bool ring_reserve_path(char *path, size_t size) {
    return path_join(path, size, g_cfg.ring_dir, ".syncap-quarantine.reserve");
}

static bool prepare_quarantine_reserve(void) {
    char path[PATH_MAX];
    unsigned char block[QUARANTINE_RESERVE_BYTES] = {0};
    struct stat st;
    int fd;
    if (g_ring_quarantined || !ring_reserve_path(path, sizeof(path))) return true;
    if (lstat(path, &st) == 0) {
        if (!S_ISREG(st.st_mode)) {
            errno = EINVAL;
            return false;
        }
        if (st.st_size == QUARANTINE_RESERVE_BYTES) return true;
        if (unlink(path) != 0 || !fsync_directory(g_cfg.ring_dir)) return false;
    } else if (errno != ENOENT) return false;
    fd = open(path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (fd < 0) return false;
    if (!write_all(fd, block, sizeof(block)) || fsync(fd) != 0) {
        int saved = errno;
        close(fd);
        unlink(path);
        errno = saved;
        return false;
    }
    if (close(fd) != 0) {
        int saved = errno;
        unlink(path);
        errno = saved;
        return false;
    }
    return fsync_directory(g_cfg.ring_dir);
}

static void release_quarantine_reserve(void) {
    char path[PATH_MAX];
    if (ring_reserve_path(path, sizeof(path)) && unlink(path) == 0)
        (void)fsync_directory(g_cfg.ring_dir);
}

static bool write_active_capture_marker(void) {
    struct string_buf json = {0};
    char path[PATH_MAX];
    bool ok = ring_active_path(path, sizeof(path)) &&
              sb_append(&json, "{\"captureId\":") &&
              sb_json_string(&json, g_capture.capture_id) &&
              sb_append(&json, ",\"sessionId\":") &&
              sb_json_string(&json, g_capture.session_id) &&
              sb_append(&json, ",\"startedAt\":") &&
              sb_json_string(&json, g_capture.created_at) &&
              sb_append(&json, "}") &&
              write_atomic_text(path, json.data, json.len);
    sb_free(&json);
    return ok;
}

static bool clear_active_capture_marker(void) {
    char path[PATH_MAX];
    if (!ring_active_path(path, sizeof(path))) return false;
    if (unlink(path) != 0 && errno != ENOENT) return false;
    if (!fsync_directory(g_cfg.ring_dir)) return false;
    return prepare_quarantine_reserve();
}

static void stop_producer_for_recovery(void) {
    pid_t pid = g_qgapp_pid;
    unsigned stopped = 0;
    if (g_producer_intentionally_stopped) return;
    if (!qgapp_identity_matches(pid)) {
        if (!read_pidfile(g_cfg.qgapp_pidfile, &pid) || !qgapp_identity_matches(pid))
            pid = find_qgapp_process();
    }
    if (!g_cfg.dry_run) {
        if (strcmp(g_cfg.qgapp_path, "/usr/bin/qgapp") == 0) {
            stopped = signal_all_qgapp_processes(SIGKILL);
        } else if (qgapp_identity_matches(pid) && kill(pid, SIGKILL) == 0) {
            stopped = 1;
        }
        if (stopped)
            log_line("ERROR", "SIGKILL sent to %u qgapp producer(s) to bound ring growth", stopped);
        else
            log_line("WARN", "no verified qgapp producer remained to stop");
    }
    g_qgapp_pid = pid;
    g_producer_intentionally_stopped = true;
}

static void pause_producer_for_recovery(void) {
    if (g_cfg.dry_run) return;
    if (strcmp(g_cfg.qgapp_path, "/usr/bin/qgapp") == 0) {
        (void)signal_all_qgapp_processes(SIGSTOP);
    } else if (qgapp_identity_matches(g_qgapp_pid)) {
        (void)kill(g_qgapp_pid, SIGSTOP);
    }
}

static void quarantine_ring(const char *reason) {
    char marker[PATH_MAX];
    if (!g_ring_quarantined) {
        safe_copy(g_ring_quarantine_reason, sizeof(g_ring_quarantine_reason), reason);
    }
    g_ring_quarantined = true;
    /* Stop writes before releasing the reserved page, otherwise a full-ring
     * producer can consume the marker space between unlink and commit. */
    pause_producer_for_recovery();
    release_quarantine_reserve();
    if (!ring_quarantine_path(marker, sizeof(marker)) ||
        !write_atomic_text(marker, g_ring_quarantine_reason,
                           strlen(g_ring_quarantine_reason))) {
        log_line("ERROR", "could not persist ring quarantine marker: %s", strerror(errno));
    }
    stop_producer_for_recovery();
}

static void load_ring_quarantine(void) {
    char marker[PATH_MAX];
    struct string_buf reason = {0};
    if (!ring_quarantine_path(marker, sizeof(marker)) || !file_exists(marker)) return;
    g_ring_quarantined = true;
    if (read_small_file(marker, &reason, sizeof(g_ring_quarantine_reason) - 1) && reason.len) {
        safe_copy(g_ring_quarantine_reason, sizeof(g_ring_quarantine_reason), reason.data);
    } else {
        safe_copy(g_ring_quarantine_reason, sizeof(g_ring_quarantine_reason),
                  "captured ring data requires manual recovery");
    }
    sb_free(&reason);
    stop_producer_for_recovery();
    log_line("WARN", "ring cleanup held for recovery: %s", g_ring_quarantine_reason);
}

static void recover_active_ring_marker(void) {
    char marker[PATH_MAX];
    if (!ring_active_path(marker, sizeof(marker)) || !file_exists(marker)) return;
    quarantine_ring("capture was interrupted; ring data was retained for recovery");
    log_line("WARN", "%s", g_ring_quarantine_reason);
}

static void fail_active_capture(const char *reason) {
    if (!g_capture.active || g_capture.failed) return;
    g_capture.failed_duration_ms = capture_elapsed_ms();
    g_capture.failed = true;
    safe_copy(g_capture.failure_reason, sizeof(g_capture.failure_reason), reason);
    quarantine_ring(reason);
    qsort(g_capture.files, g_capture.file_count, sizeof(*g_capture.files), export_file_compare);
    write_session_state("failed", capture_elapsed_ms(), g_capture.files, g_capture.file_count,
                        g_capture.total_size);
    log_line("ERROR", "%s", reason);
}

static bool write_export_manifest(const struct export_file *files, size_t file_count,
                                  uint64_t total_size) {
    struct string_buf json = {0};
    char path[PATH_MAX];
    bool ok = sb_append(&json, "{\"sessionId\":") &&
              sb_json_string(&json, g_capture.session_id) &&
              sb_printf(&json, ",\"rangeSupported\":true,\"totalBytes\":%llu,\"manifestUrl\":\"/v1/sessions/%s/manifest\",\"files\":",
                        (unsigned long long)total_size, g_capture.session_id) &&
              append_file_array(&json, files, file_count, g_capture.session_id, true) &&
              sb_append(&json, "}") &&
              path_join(path, sizeof(path), g_capture.session_dir, ".syncap-export.json") &&
              write_atomic_text(path, json.data, json.len);
    sb_free(&json);
    return ok;
}

static bool retire_ring_pair(const char *mcap_path, const char *status_path) {
    static unsigned long sequence = 0;
    char retired_mcap[PATH_MAX];
    char retired_status[PATH_MAX];
    struct stat st;
    unsigned attempt;
    bool removed = true;
    int failure_errno = 0;
    for (attempt = 0; attempt < 32; ++attempt) {
        unsigned long token = ++sequence;
        int first = snprintf(retired_mcap, sizeof(retired_mcap),
                             "%s.syncap-retired.%ld.%lu", mcap_path, (long)getpid(), token);
        int second = snprintf(retired_status, sizeof(retired_status),
                              "%s.syncap-retired.%ld.%lu", status_path, (long)getpid(), token);
        if (first < 0 || second < 0 || (size_t)first >= sizeof(retired_mcap) ||
            (size_t)second >= sizeof(retired_status)) {
            errno = ENAMETOOLONG;
            return false;
        }
        if (lstat(retired_mcap, &st) != 0 && errno == ENOENT &&
            lstat(retired_status, &st) != 0 && errno == ENOENT) break;
    }
    if (attempt == 32) {
        errno = EEXIST;
        return false;
    }
    if (rename(mcap_path, retired_mcap) != 0) return false;
    if (rename(status_path, retired_status) != 0) {
        int saved = errno;
        (void)rename(retired_mcap, mcap_path);
        (void)fsync_directory(g_cfg.ring_dir);
        errno = saved;
        return false;
    }
    if (!fsync_directory(g_cfg.ring_dir)) return false;
    if (unlink(retired_mcap) != 0 && errno != ENOENT) {
        removed = false;
        failure_errno = errno;
    }
    if (unlink(retired_status) != 0 && errno != ENOENT) {
        removed = false;
        if (!failure_errno) failure_errno = errno;
    }
    if (!fsync_directory(g_cfg.ring_dir)) {
        removed = false;
        if (!failure_errno) failure_errno = errno;
    }
    if (!removed) errno = failure_errno ? failure_errno : EIO;
    return removed;
}

static bool copy_closed_segment(const char *mcap_path, const char *status_path,
                                const char *mcap_name, const char *status_name) {
    char mcap_destination[PATH_MAX];
    char status_destination[PATH_MAX];
    char mcap_sha[65];
    char status_sha[65];
    uint64_t mcap_size;
    uint64_t status_size;
    if (!path_join(mcap_destination, sizeof(mcap_destination), g_capture.session_dir, mcap_name) ||
        !path_join(status_destination, sizeof(status_destination), g_capture.session_dir, status_name)) return false;

    /* Keep both source files until both durable destination files exist. */
    if (!copy_file_atomic(mcap_path, mcap_destination, &mcap_size, mcap_sha) ||
        !copy_file_atomic(status_path, status_destination, &status_size, status_sha) ||
        !remember_capture_file(mcap_name, mcap_size, mcap_sha) ||
        !remember_capture_file(status_name, status_size, status_sha) ||
        !write_session_state("recording",
                             (monotonic_ns() - g_capture.started_mono_ns) / 1000000ULL,
                             g_capture.files, g_capture.file_count,
                             g_capture.total_size) ||
        !write_export_manifest(g_capture.files, g_capture.file_count,
                               g_capture.total_size)) return false;
    if (!retire_ring_pair(mcap_path, status_path)) return false;
    ++g_capture.segments_moved;
    return true;
}

static void fail_for_stale_ring_entry(const char *name, const char *kind) {
    char reason[256];
    snprintf(reason, sizeof(reason), "%s %s remained incomplete", kind, name);
    if (g_capture.active && !g_capture.failed) fail_active_capture(reason);
    else quarantine_ring(reason);
}

static void check_stale_open_segments(void) {
    DIR *directory;
    struct dirent *entry;
    if (g_ring_quarantined) return;
    directory = opendir(g_cfg.ring_dir);
    if (!directory) return;
    while ((entry = readdir(directory)) != NULL) {
        char mcap_path[PATH_MAX];
        char status_path[PATH_MAX];
        struct stat st;
        uint64_t age;
        if (!has_suffix(entry->d_name, ".mcap") || !is_safe_component(entry->d_name) ||
            !path_join(mcap_path, sizeof(mcap_path), g_cfg.ring_dir, entry->d_name) ||
            lstat(mcap_path, &st) != 0 || !S_ISREG(st.st_mode) ||
            snprintf(status_path, sizeof(status_path), "%s.status.json", mcap_path) < 0 ||
            file_exists(status_path)) continue;
        age = incomplete_segment_age_ns(entry->d_name);
        if (age >= (SEGMENT_SECONDS + 2) * 1000000000ULL) {
            fail_for_stale_ring_entry(entry->d_name, "open segment");
            break;
        }
    }
    closedir(directory);
}

struct ring_status_entry {
    char name[NAME_MAX + 1];
};

static bool ring_status_sequence(const char *name, size_t *prefix_length_out,
                                 unsigned long long *sequence_out) {
    static const char suffix[] = ".mcap.status.json";
    size_t name_length = strlen(name);
    size_t suffix_length = strlen(suffix);
    const char *end;
    const char *digits;
    char *parsed_end;
    unsigned long long sequence;
    if (name_length < suffix_length) return false;
    end = name + name_length - suffix_length;
    digits = end;
    while (digits > name && isdigit((unsigned char)digits[-1])) --digits;
    if (digits == end || digits == name || digits[-1] != '-') return false;
    sequence = strtoull(digits, &parsed_end, 10);
    if (parsed_end != end) return false;
    *prefix_length_out = (size_t)(digits - name);
    *sequence_out = sequence;
    return true;
}

static int ring_status_entry_compare(const void *left, const void *right) {
    const struct ring_status_entry *left_entry = left;
    const struct ring_status_entry *right_entry = right;
    size_t left_prefix;
    size_t right_prefix;
    unsigned long long left_sequence;
    unsigned long long right_sequence;
    if (ring_status_sequence(left_entry->name, &left_prefix, &left_sequence) &&
        ring_status_sequence(right_entry->name, &right_prefix, &right_sequence) &&
        left_prefix == right_prefix &&
        strncmp(left_entry->name, right_entry->name, left_prefix) == 0) {
        if (left_sequence < right_sequence) return -1;
        if (left_sequence > right_sequence) return 1;
    }
    return strcmp(left_entry->name, right_entry->name);
}

static void process_closed_segments(void) {
    static const char suffix[] = ".mcap.status.json";
    DIR *directory = opendir(g_cfg.ring_dir);
    struct dirent *entry;
    struct ring_status_entry *entries = NULL;
    size_t entry_count = 0;
    size_t entry_capacity = 0;
    size_t index;
    bool allocation_failed = false;
    bool saw_terminal = false;
    if (!directory) return;
    while ((entry = readdir(directory)) != NULL) {
        struct ring_status_entry *grown;
        if (!has_suffix(entry->d_name, suffix) || !is_safe_component(entry->d_name)) continue;
        if (entry_count == entry_capacity) {
            size_t capacity = entry_capacity ? entry_capacity * 2 : 16;
            grown = realloc(entries, capacity * sizeof(*grown));
            if (!grown) {
                allocation_failed = true;
                break;
            }
            entries = grown;
            entry_capacity = capacity;
        }
        if (!safe_copy(entries[entry_count].name, sizeof(entries[entry_count].name),
                       entry->d_name)) continue;
        ++entry_count;
    }
    closedir(directory);
    if (allocation_failed) {
        free(entries);
        if (g_capture.active && !g_capture.failed)
            fail_active_capture("cannot enumerate recorder segments");
        else
            quarantine_ring("cannot enumerate recorder segments");
        return;
    }
    qsort(entries, entry_count, sizeof(*entries), ring_status_entry_compare);
    for (index = 0; index < entry_count; ++index) {
        char status_path[PATH_MAX];
        char mcap_path[PATH_MAX];
        char mcap_name[NAME_MAX + 1];
        struct integrity_observation integrity;
        enum segment_status_state status_state;
        bool integrity_evidence_valid;
        bool tracked_producer;
        bool valid_footer;
        uint64_t incomplete_age_ns;
        size_t status_length;
        size_t mcap_length;
        const char *status_name = entries[index].name;
        if (g_capture.finalizing && g_capture.stop_segment[0]) {
            struct ring_status_entry boundary;
            int length = snprintf(boundary.name, sizeof(boundary.name), "%s.status.json",
                                  g_capture.stop_segment);
            if (length < 0 || (size_t)length >= sizeof(boundary.name) ||
                ring_status_entry_compare(&entries[index], &boundary) > 0) break;
        }
        status_length = strlen(status_name);
        mcap_length = status_length - strlen(".status.json");
        if (mcap_length >= sizeof(mcap_name)) continue;
        memcpy(mcap_name, status_name, mcap_length);
        mcap_name[mcap_length] = '\0';
        if (!path_join(status_path, sizeof(status_path), g_cfg.ring_dir, status_name) ||
            !path_join(mcap_path, sizeof(mcap_path), g_cfg.ring_dir, mcap_name)) continue;
        clear_incomplete_segment(mcap_name);
        status_state = segment_status_state(status_path, &integrity);
        integrity_evidence_valid = status_state == SEGMENT_STATUS_CLEAN;
        if (status_state == SEGMENT_STATUS_PENDING) {
            incomplete_age_ns = incomplete_segment_age_ns(status_name);
            if (incomplete_age_ns >= (SEGMENT_SECONDS + 2) * 1000000000ULL) {
                fail_for_stale_ring_entry(status_name, "segment status");
            }
            break;
        }
        valid_footer = mcap_is_closed(mcap_path);
        /* A balanced root object plus a closed MCAP is a complete publish.
         * If the status wins the write race, allow the MCAP a short settling
         * window before treating the terminal pair as corrupt. */
        if ((status_state == SEGMENT_STATUS_CLEAN ||
             status_state == SEGMENT_STATUS_INTEGRITY_FAILED) && !valid_footer) {
            incomplete_age_ns = incomplete_segment_age_ns(status_name);
            if (incomplete_age_ns < 2ULL * 1000000000ULL) break;
        } else {
            clear_incomplete_segment(status_name);
        }
        if (valid_footer) {
            saw_terminal = true;
            g_last_terminal_segment_ns = monotonic_ns();
        }
        tracked_producer = qgapp_identity_matches(g_qgapp_pid);
        if (status_state == SEGMENT_STATUS_CLEAN && valid_footer) {
            if (g_capture.active && !g_capture.failed) {
                if (!capture_accepts_integrity(&integrity))
                    status_state = SEGMENT_STATUS_INTEGRITY_FAILED;
            } else if (!g_capture.active && tracked_producer) {
                if (!observe_idle_integrity(&integrity))
                    status_state = SEGMENT_STATUS_INTEGRITY_FAILED;
            } else if (!integrity_counters_are_startup_clean(&integrity.counters)) {
                status_state = SEGMENT_STATUS_INTEGRITY_FAILED;
            } else {
                g_qgapp_has_clean_segment = true;
            }
        } else if (status_state == SEGMENT_STATUS_INTEGRITY_FAILED && valid_footer &&
                   !g_capture.active && tracked_producer) {
            invalidate_integrity_baseline();
        }
        if (integrity_evidence_valid && valid_footer) {
            update_sync_cache(status_path);
        }
        if (g_capture.active && !g_capture.failed) {
            if (status_state != SEGMENT_STATUS_CLEAN || !valid_footer) {
                char reason[256];
                snprintf(reason, sizeof(reason), "terminal segment %s is %s",
                         mcap_name,
                         !valid_footer
                             ? "missing a valid MCAP footer"
                             : (status_state == SEGMENT_STATUS_INTEGRITY_FAILED
                                    ? "failed integrity checks"
                                    : "explicitly marked failed"));
                fail_active_capture(reason);
                continue;
            }
            errno = EIO;
            if (!copy_closed_segment(mcap_path, status_path, mcap_name, status_name)) {
                int copy_error = errno;
                char reason[256];
                snprintf(reason, sizeof(reason), "SD segment transfer failed: %s", strerror(copy_error));
                fail_active_capture(reason);
            }
            /* Give the control socket a turn between durable SD transfers.
             * A backlog must not turn one poll into an unbounded batch. */
            break;
        } else if (!g_capture.active && !g_ring_quarantined) {
            /* qgapp may report a structurally complete first segment as clean
             * while its IMU counters are still warming up.  This is idle
             * pre-roll, not captured data: retire it without stopping the
             * producer, but keep capture readiness false until a subsequent
             * segment passes every strict integrity check.  Startup residue is
             * still preserved because no tracked producer exists then. */
            if (status_state == SEGMENT_STATUS_INTEGRITY_FAILED && valid_footer &&
                tracked_producer) {
                if (!retire_ring_pair(mcap_path, status_path)) {
                    char reason[256];
                    snprintf(reason, sizeof(reason), "cannot retire idle warm-up segment %s: %s",
                             mcap_name, strerror(errno));
                    quarantine_ring(reason);
                } else {
                    log_line("WARN", "discarded idle warm-up segment %s while counters stabilize",
                             mcap_name);
                }
                continue;
            }
            /* Corrupt terminal data may be the tail of an interrupted capture.
             * Preserve it and fail closed; only a proven-clean idle pair can be
             * discarded. Empty/truncated sidecars remain pending above. */
            if (status_state != SEGMENT_STATUS_CLEAN || !valid_footer) {
                char reason[256];
                snprintf(reason, sizeof(reason), "idle terminal segment %s is %s",
                         mcap_name,
                         !valid_footer
                             ? "missing a valid MCAP footer"
                             : (status_state == SEGMENT_STATUS_INTEGRITY_FAILED
                                    ? "failed integrity checks"
                                    : "explicitly marked failed"));
                quarantine_ring(reason);
                continue;
            }
            if (!retire_ring_pair(mcap_path, status_path)) {
                char reason[256];
                snprintf(reason, sizeof(reason), "cannot retire idle segment %s: %s",
                         mcap_name, strerror(errno));
                quarantine_ring(reason);
            } else {
                ++g_idle_clean_segments_discarded;
            }
        }
    }
    free(entries);
    check_stale_open_segments();
    /* Time spent copying a boundary observed in this pass is not evidence
     * that the producer stalled. Re-observe the ring on the next pass. */
    if (!saw_terminal && !g_cfg.dry_run && !g_ring_quarantined && qgapp_identity_matches(g_qgapp_pid) &&
        g_last_terminal_segment_ns &&
        monotonic_ns() - g_last_terminal_segment_ns >= (SEGMENT_SECONDS + 6) * 1000000000ULL) {
        const char *reason = "qgapp published no complete segment boundary for 8 seconds";
        if (g_capture.active && !g_capture.failed) fail_active_capture(reason);
        else quarantine_ring(reason);
    }
}

static bool find_pending_sidecar(char *name, size_t name_size) {
    DIR *directory = opendir(g_cfg.ring_dir);
    struct dirent *entry;
    bool found = false;
    if (!directory) return false;
    while ((entry = readdir(directory)) != NULL) {
        char status_path[PATH_MAX];
        if (!has_suffix(entry->d_name, ".mcap.status.json") ||
            !is_safe_component(entry->d_name) ||
            !path_join(status_path, sizeof(status_path), g_cfg.ring_dir, entry->d_name)) continue;
        if (segment_status_state(status_path, NULL) == SEGMENT_STATUS_PENDING) {
            found = safe_copy(name, name_size, entry->d_name);
            break;
        }
    }
    closedir(directory);
    return found;
}

static bool capture_has_file(const char *name) {
    size_t index;
    for (index = 0; index < g_capture.file_count; ++index) {
        if (strcmp(g_capture.files[index].name, name) == 0) return true;
    }
    return false;
}

/* Include the latest existing segment even in the short interval between
 * closing it and opening the next one. Older queued SD transfers are not a
 * substitute for that stop boundary. */
static bool find_stop_segment(char *name, size_t name_size) {
    DIR *directory = opendir(g_cfg.ring_dir);
    struct dirent *entry;
    struct ring_status_entry latest = {{0}};
    if (!directory) return false;
    while ((entry = readdir(directory)) != NULL) {
        char path[PATH_MAX];
        struct stat st;
        struct ring_status_entry candidate;
        int length;
        if (!has_suffix(entry->d_name, ".mcap") || !is_safe_component(entry->d_name) ||
            !path_join(path, sizeof(path), g_cfg.ring_dir, entry->d_name) ||
            lstat(path, &st) != 0 || !S_ISREG(st.st_mode)) continue;
        length = snprintf(candidate.name, sizeof(candidate.name), "%s.status.json", entry->d_name);
        if (length < 0 || (size_t)length >= sizeof(candidate.name)) continue;
        if (!latest.name[0] || ring_status_entry_compare(&candidate, &latest) > 0) {
            latest = candidate;
            safe_copy(name, name_size, entry->d_name);
        }
    }
    closedir(directory);
    return latest.name[0] != '\0';
}

static bool begin_capture(const char *name, struct string_buf *response,
                          int *http_status, const char **error_code, char *error_message,
                          size_t error_message_size) {
    struct storage_info storage = inspect_storage(true);
    char syncap_root[PATH_MAX];
    char stamp[32];
    char pending_sidecar[NAME_MAX + 1];
    struct integrity_counters request_integrity_counters;
    struct integrity_observation capture_boundary_integrity;
    unsigned long suffix;
    unsigned long idle_baseline;
    uint64_t request_integrity_generation = 0;
    uint64_t boundary_deadline;
    bool session_created = false;
    if (g_ring_quarantined) {
        *http_status = 409;
        *error_code = "capture.recovery_required";
        safe_copy(error_message, error_message_size,
                  "ring contains quarantined capture data; recover it or reboot before a new capture");
        return false;
    }
    if (g_capture.active) {
        *http_status = 409;
        *error_code = "capture.already_running";
        safe_copy(error_message, error_message_size, "a capture is already recording");
        return false;
    }
    if (!storage.can_capture) {
        *http_status = 409;
        *error_code = "storage.insufficient";
        snprintf(error_message, error_message_size, "SD card cannot capture: %s",
                 storage.reason ? storage.reason : "unavailable");
        return false;
    }
    if (!qgapp_ready_for_capture()) {
        *http_status = 409;
        *error_code = "camera.unavailable";
        if (qgapp_identity_matches(g_qgapp_pid) && !g_qgapp_has_clean_segment) {
            safe_copy(error_message, error_message_size,
                      "camera recorder is warming up; wait for a clean segment boundary");
        } else {
            safe_copy(error_message, error_message_size,
                      qgapp_identity_matches(g_qgapp_pid)
                          ? "qgapp is alive but preview listeners are unavailable; reboot is required"
                          : "persistent qgapp is not running");
        }
        return false;
    }

    /*
     * Align start to a segment boundary. The segment open when this request
     * arrives contains pre-roll, so it closes and is discarded while idle.
     */
    memset(&request_integrity_counters, 0, sizeof(request_integrity_counters));
    memset(&capture_boundary_integrity, 0, sizeof(capture_boundary_integrity));
    if (!g_cfg.dry_run) {
        request_integrity_generation = g_integrity_baseline.generation;
        request_integrity_counters = g_integrity_baseline.last.counters;
    }
    process_closed_segments();
    if (!g_cfg.dry_run) {
        idle_baseline = g_idle_clean_segments_discarded;
        boundary_deadline = monotonic_ns() + (SEGMENT_SECONDS + 4) * 1000000000ULL;
        while (g_idle_clean_segments_discarded == idle_baseline &&
               g_integrity_baseline.generation == request_integrity_generation &&
               g_integrity_baseline.established && monotonic_ns() < boundary_deadline) {
            struct timespec delay = {0, 50000000L};
            nanosleep(&delay, NULL);
            process_closed_segments();
        }
        if (g_integrity_baseline.generation != request_integrity_generation ||
            !g_integrity_baseline.established ||
            !integrity_counters_equal(&request_integrity_counters,
                                      &g_integrity_baseline.last.counters)) {
            *http_status = 409;
            *error_code = "camera.unavailable";
            safe_copy(error_message, error_message_size,
                      "camera integrity counters changed while aligning capture; wait for them to stabilize");
            return false;
        }
        if (g_idle_clean_segments_discarded == idle_baseline) {
            *http_status = 409;
            *error_code = "camera.segment_boundary_timeout";
            safe_copy(error_message, error_message_size,
                      "qgapp did not publish a clean MCAP boundary");
            return false;
        }
        capture_boundary_integrity = g_integrity_baseline.last;
    }
    if (find_pending_sidecar(pending_sidecar, sizeof(pending_sidecar))) {
        char reason[256];
        snprintf(reason, sizeof(reason),
                 "incomplete pre-capture segment %s requires recovery",
                 pending_sidecar);
        quarantine_ring(reason);
        *http_status = 409;
        *error_code = "capture.recovery_required";
        safe_copy(error_message, error_message_size,
                  "incomplete recorder data was retained; reboot is required before capture");
        return false;
    }
    reset_capture();
    g_capture.integrity_baseline_valid = true;
    g_capture.integrity_baseline = capture_boundary_integrity.counters;
    g_capture.integrity_last = capture_boundary_integrity;
    g_capture.active = true;
    g_capture.started_mono_ns = monotonic_ns();
    iso_now(g_capture.created_at, sizeof(g_capture.created_at));
    id_stamp(stamp, sizeof(stamp));
    suffix = (unsigned long)((g_capture.started_mono_ns ^ (uint64_t)getpid()) & 0xffffffUL);
    snprintf(g_capture.session_id, sizeof(g_capture.session_id), "ses_%s_%06lx", stamp, suffix);
    snprintf(g_capture.capture_id, sizeof(g_capture.capture_id), "cap_%s_%06lx", stamp, suffix);
    safe_copy(g_capture.name, sizeof(g_capture.name), name && name[0] ? name : "Tina stereo capture");
    safe_copy(g_capture.storage_root, sizeof(g_capture.storage_root), storage.root);
    if (!path_join(syncap_root, sizeof(syncap_root), storage.root, "SynCap") ||
        !path_join(g_capture.session_dir, sizeof(g_capture.session_dir), syncap_root, g_capture.session_id) ||
        !ensure_plain_directory(syncap_root, 0750, NULL) ||
        !write_active_capture_marker() ||
        !ensure_plain_directory(g_capture.session_dir, 0750, &session_created) ||
        !session_created ||
        !write_session_state("recording", 0, NULL, 0, 0)) {
        int saved = errno ? errno : EIO;
        (void)clear_active_capture_marker();
        if (session_created) (void)rmdir(g_capture.session_dir);
        snprintf(error_message, error_message_size, "cannot create SD session: %s", strerror(saved));
        *http_status = 500;
        *error_code = "storage.write_failed";
        reset_capture();
        errno = saved;
        return false;
    }
    return sb_append(response, "{\"captureId\":") && sb_json_string(response, g_capture.capture_id) &&
           sb_append(response, ",\"sessionId\":") && sb_json_string(response, g_capture.session_id) &&
           sb_printf(response, ",\"state\":\"recording\",\"elapsedMs\":0,\"startedAtDeviceTimeNs\":\"%llu\","
                              "\"storage\":{\"target\":\"usb\",\"label\":\"SDCARD\"}}",
                     (unsigned long long)g_capture.started_mono_ns);
}

static bool finalize_capture(const char *capture_id, struct string_buf *response,
                             int *http_status, const char **error_code, char *error_message,
                             size_t error_message_size) {
    unsigned long baseline;
    uint64_t deadline;
    uint64_t drain_deadline;
    uint64_t duration_ms;
    size_t file_count;
    uint64_t total_size;
    size_t mcap_count = 0;
    size_t status_count = 0;
    size_t index;
    char completed_capture[96];
    char completed_session[96];
    char completed_name[256];
    char created_at[64];
    char stop_segment[NAME_MAX + 1] = {0};
    bool have_stop_segment;
    bool stop_closed = false;
    const char *timeout_reason = "qgapp did not close the current MCAP segment before the deadline";

    if (!g_capture.active || strcmp(g_capture.capture_id, capture_id) != 0) {
        *http_status = 404;
        *error_code = "capture.not_found";
        safe_copy(error_message, error_message_size, "capture is not active");
        return false;
    }
    if (g_capture.failed) {
        quarantine_ring(g_capture.failure_reason[0]
                            ? g_capture.failure_reason
                            : "failed capture contains ring data requiring recovery");
        duration_ms = capture_elapsed_ms();
        qsort(g_capture.files, g_capture.file_count, sizeof(*g_capture.files), export_file_compare);
        write_session_state("failed", duration_ms, g_capture.files, g_capture.file_count,
                            g_capture.total_size);
        *http_status = 500;
        *error_code = "capture.interrupted";
        safe_copy(error_message, error_message_size,
                  g_capture.failure_reason[0] ? g_capture.failure_reason : "qgapp exited during capture");
        reset_capture();
        return false;
    }
    g_capture.finalizing = true;
    baseline = g_capture.segments_moved;
    have_stop_segment = find_stop_segment(stop_segment, sizeof(stop_segment));
    if (have_stop_segment)
        safe_copy(g_capture.stop_segment, sizeof(g_capture.stop_segment), stop_segment);
    deadline = monotonic_ns() + STOP_WAIT_SECONDS * 1000000000ULL;
    drain_deadline = monotonic_ns() + STOP_DRAIN_SECONDS * 1000000000ULL;
    for (;;) {
        process_closed_segments();
        if (g_capture.failed) break;
        if (!g_cfg.dry_run && !qgapp_identity_matches(g_qgapp_pid)) {
            fail_active_capture("qgapp exited while capture stop was waiting for the final segment");
            break;
        }
        if ((have_stop_segment && capture_has_file(stop_segment)) ||
            (!have_stop_segment && g_capture.segments_moved > baseline)) break;
        if (!have_stop_segment && find_stop_segment(stop_segment, sizeof(stop_segment))) {
            have_stop_segment = true;
            safe_copy(g_capture.stop_segment, sizeof(g_capture.stop_segment), stop_segment);
        }
        if (have_stop_segment) {
            char mcap_path[PATH_MAX];
            char status_path[PATH_MAX];
            if (path_join(mcap_path, sizeof(mcap_path), g_cfg.ring_dir, stop_segment) &&
                snprintf(status_path, sizeof(status_path), "%s.status.json", mcap_path) > 0) {
                stop_closed = mcap_is_closed(mcap_path) &&
                              segment_status_state(status_path, NULL) != SEGMENT_STATUS_PENDING;
            }
        }
        /* Recheck the boundary after each copy: it may have closed during a
         * slow fsync and was absent from that pass's directory snapshot.
         * Already-closed data waits on storage, not on the camera. */
        if (!stop_closed && monotonic_ns() >= deadline) break;
        if (monotonic_ns() >= drain_deadline) {
            timeout_reason = "SD card did not finish saving the closed capture boundary before the deadline";
            break;
        }
        if (stop_closed) continue;
        struct timespec delay = {0, 100000000L};
        nanosleep(&delay, NULL);
    }
    duration_ms = capture_elapsed_ms();
    if (g_capture.failed) {
        qsort(g_capture.files, g_capture.file_count, sizeof(*g_capture.files), export_file_compare);
        write_session_state("failed", duration_ms, g_capture.files, g_capture.file_count,
                            g_capture.total_size);
        *http_status = 500;
        *error_code = "capture.interrupted";
        safe_copy(error_message, error_message_size, g_capture.failure_reason);
        reset_capture();
        return false;
    }
    if ((have_stop_segment && !capture_has_file(stop_segment)) ||
        (!have_stop_segment && g_capture.segments_moved == baseline)) {
        quarantine_ring(timeout_reason);
        qsort(g_capture.files, g_capture.file_count, sizeof(*g_capture.files), export_file_compare);
        write_session_state("failed", duration_ms, g_capture.files, g_capture.file_count,
                            g_capture.total_size);
        *http_status = 500;
        *error_code = "capture.finalization_failed";
        safe_copy(error_message, error_message_size, timeout_reason);
        reset_capture();
        return false;
    }
    qsort(g_capture.files, g_capture.file_count, sizeof(*g_capture.files), export_file_compare);
    file_count = g_capture.file_count;
    total_size = g_capture.total_size;
    for (index = 0; index < file_count; ++index) {
        if (has_suffix(g_capture.files[index].name, ".mcap.status.json")) ++status_count;
        else if (has_suffix(g_capture.files[index].name, ".mcap")) ++mcap_count;
    }
    if (mcap_count == 0 || mcap_count != status_count) {
        *http_status = 500;
        *error_code = "capture.finalization_failed";
        snprintf(error_message, error_message_size,
                 "segment integrity mismatch: %lu MCAP and %lu clean status files",
                 (unsigned long)mcap_count, (unsigned long)status_count);
        quarantine_ring(error_message);
        write_session_state("failed", duration_ms, g_capture.files, file_count, total_size);
        reset_capture();
        return false;
    }
    if (!write_session_state("complete", duration_ms, g_capture.files, file_count, total_size) ||
        !write_export_manifest(g_capture.files, file_count, total_size)) {
        *http_status = 500;
        *error_code = "capture.finalization_failed";
        safe_copy(error_message, error_message_size, "cannot commit session metadata");
        fail_active_capture(error_message);
        return false;
    }
    if (!clear_active_capture_marker()) {
        *http_status = 500;
        *error_code = "capture.finalization_failed";
        safe_copy(error_message, error_message_size,
                  "session is durable but capture recovery marker could not be cleared");
        fail_active_capture(error_message);
        return false;
    }
    safe_copy(completed_capture, sizeof(completed_capture), g_capture.capture_id);
    safe_copy(completed_session, sizeof(completed_session), g_capture.session_id);
    safe_copy(completed_name, sizeof(completed_name), g_capture.name);
    safe_copy(created_at, sizeof(created_at), g_capture.created_at);
    reset_capture();

    return sb_append(response, "{\"captureId\":") && sb_json_string(response, completed_capture) &&
           sb_append(response, ",\"sessionId\":") && sb_json_string(response, completed_session) &&
           sb_append(response, ",\"state\":\"completed\",\"session\":{\"id\":") &&
           sb_json_string(response, completed_session) && sb_append(response, ",\"name\":") &&
           sb_json_string(response, completed_name) && sb_append(response, ",\"createdAt\":") &&
           sb_json_string(response, created_at) &&
           sb_printf(response, ",\"durationMs\":%llu,\"sizeBytes\":%llu,\"status\":\"complete\","
                               "\"fileCount\":%lu}}",
                     (unsigned long long)duration_ms, (unsigned long long)total_size,
                     (unsigned long)file_count);
}

static void supervise_qgapp(void) {
    static uint64_t last_check_ns = 0;
    uint64_t now;
    pid_t discovered;
    bool exited_child = false;
    if (g_cfg.dry_run) return;
    now = monotonic_ns();
    if (last_check_ns && now - last_check_ns < 2000000000ULL) return;
    last_check_ns = now;
    if (process_alive(g_qgapp_pid) && g_qgapp_tracking_started_ns &&
        now - g_qgapp_tracking_started_ns < 2000000000ULL) return;
    if (g_qgapp_pid > 1 && waitpid(g_qgapp_pid, NULL, WNOHANG) == g_qgapp_pid)
        exited_child = true;
    if (strcmp(g_cfg.qgapp_path, "/usr/bin/qgapp") == 0) {
        discovered = find_qgapp_process();
        if (discovered == (pid_t)-2) {
            const char *reason = "conflicting qgapp process detected; capture cannot continue";
            if (g_capture.active) fail_active_capture(reason);
            else quarantine_ring(reason);
            return;
        }
        if (discovered == g_qgapp_pid && qgapp_identity_matches(g_qgapp_pid)) return;
    } else if (!exited_child && qgapp_identity_matches(g_qgapp_pid)) {
        return;
    }

    if (g_capture.active) {
        fail_active_capture("qgapp exited during capture; completed MCAP segments were retained");
        return;
    }

    if (g_ring_quarantined) return;
    /* On this firmware, reopening the ISP after qgapp exits fails with
     * /dev/video0 unavailable.  Repeated retries only produce fork storms and
     * can never restore the previews, so preserve the ring and require the
     * board to reset the camera stack. */
    quarantine_ring("qgapp exited; reboot is required to reset the Tina camera stack");
    log_line("ERROR", "%s", g_ring_quarantine_reason);
}

static double system_uptime(void) {
    FILE *file = fopen("/proc/uptime", "r");
    double value = 0;
    if (!file) return 0;
    if (fscanf(file, "%lf", &value) != 1) value = 0;
    fclose(file);
    return value;
}

static bool read_trimmed_value(const char *path, char *out, size_t out_size) {
    FILE *file = fopen(path, "r");
    if (!file) return false;
    if (!fgets(out, (int)out_size, file)) {
        fclose(file);
        return false;
    }
    fclose(file);
    out[strcspn(out, "\r\n \t")] = '\0';
    return out[0] != '\0';
}

static void device_identifier(char *out, size_t out_size) {
    static const char *paths[] = {
        "/sys/class/android_usb/android0/iSerial",
        "/sys/kernel/config/usb_gadget/g1/strings/0x409/serialnumber",
        "/etc/machine-id"
    };
    char raw[128];
    size_t path_index;
    size_t used = 0;
    safe_copy(out, out_size, "tina-stereo");
    for (path_index = 0; path_index < sizeof(paths) / sizeof(paths[0]); ++path_index) {
        const unsigned char *cursor;
        if (!read_trimmed_value(paths[path_index], raw, sizeof(raw))) continue;
        safe_copy(out, out_size, "tina-stereo-");
        used = strlen(out);
        for (cursor = (const unsigned char *)raw; *cursor && used + 1 < out_size && used < 28; ++cursor) {
            if (isalnum(*cursor)) out[used++] = (char)tolower(*cursor);
        }
        out[used] = '\0';
        if (used > strlen("tina-stereo-")) return;
    }
}

static void normalized_host(const struct http_request *request, char *out, size_t out_size) {
    char copy[256];
    char *colon;
    const unsigned char *cursor;
    if (!request->host[0] || !safe_copy(copy, sizeof(copy), request->host)) {
        safe_copy(out, out_size, "127.0.0.1");
        return;
    }
    colon = strrchr(copy, ':');
    if (colon && strchr(copy, ':') == colon) *colon = '\0';
    if (!copy[0]) {
        safe_copy(out, out_size, "127.0.0.1");
        return;
    }
    for (cursor = (const unsigned char *)copy; *cursor; ++cursor) {
        if (!(isalnum(*cursor) || *cursor == '.' || *cursor == '-' || *cursor == ':' ||
              *cursor == '[' || *cursor == ']')) {
            safe_copy(out, out_size, "127.0.0.1");
            return;
        }
    }
    safe_copy(out, out_size, copy);
}

static bool build_manifest(const struct http_request *request, struct string_buf *json) {
    char host[256];
    char device_id[64];
    normalized_host(request, host, sizeof(host));
    device_identifier(device_id, sizeof(device_id));
    return sb_append(json, "{\"protocolVersion\":\"1.0\",\"device\":{\"id\":") &&
        sb_json_string(json, device_id) &&
        sb_append(json,
            ",\"displayName\":\"SynCap 双目相机\",\"model\":\"Allwinner Tina Stereo\","
            "\"type\":\"allwinner-tina\",\"firmwareVersion\":\"" SERVICE_VERSION "\"},"
            "\"adapter\":{\"kind\":\"allwinner-tina-stereo\",\"security\":\"lan\"},"
            "\"capabilities\":[\"camera.preview\",\"capture.device\",\"session.export\","
            "\"sensor.imu\",\"sensor.audio\",\"sync.metrics\",\"sync.hardware_trigger\","
            "\"storage.sd\",\"network.wifi\",\"provisioning.bluetooth\"],"
            "\"cameras\":[{\"id\":\"left\",\"label\":\"LEFT\",\"direction\":\"设备左目\","
            "\"mountPosition\":\"stereo_left\",\"preview\":{\"transport\":\"tcp-hevc\",\"url\":\"tcp://") &&
        sb_append(json, host) &&
        sb_append(json,
            ":9100\",\"codec\":\"h265\",\"width\":1600,\"height\":1200,\"frameRate\":29.4118}},"
            "{\"id\":\"right\",\"label\":\"RIGHT\",\"direction\":\"设备右目\","
            "\"mountPosition\":\"stereo_right\",\"preview\":{\"transport\":\"tcp-hevc\",\"url\":\"tcp://") &&
        sb_append(json, host) &&
        sb_append(json,
            ":9101\",\"codec\":\"h265\",\"width\":1600,\"height\":1200,\"frameRate\":29.4118}}],"
            "\"sensors\":[{\"id\":\"imu0\",\"type\":\"imu\",\"topic\":\"/ego/imu\"},"
            "{\"id\":\"audio0\",\"type\":\"stereo_audio\",\"topic\":\"/audio\"}],"
            "\"synchronization\":{\"cameraMethod\":\"hardware_trigger\","
            "\"ptp\":{\"supported\":false,\"requiredForCapture\":false,\"reason\":\"not_required\"}}}");
}

static bool append_empty_metric(struct string_buf *json) {
    return sb_append(json, "{\"current\":null,\"mean\":null,\"p95\":null,\"min\":null,\"max\":null,\"samples\":0}");
}

static bool build_capture_status(struct string_buf *json) {
    if (!g_capture.active) {
        if (!g_ring_quarantined) return sb_append(json, "{\"state\":\"idle\"}");
        return sb_append(json,
                   "{\"state\":\"failed\",\"recoveryRequired\":true,\"rebootRequired\":true,"
                   "\"producerState\":\"intentionally_stopped\",\"failureReason\":") &&
               sb_json_string(json, g_ring_quarantine_reason) && sb_append(json, "}");
    }
    if (!sb_append(json, "{\"state\":") ||
        !sb_json_string(json, g_capture.failed ? "failed" :
                        (g_capture.finalizing ? "finalizing" : "recording")) ||
        !sb_append(json, ",\"captureId\":") || !sb_json_string(json, g_capture.capture_id) ||
        !sb_append(json, ",\"sessionId\":") || !sb_json_string(json, g_capture.session_id) ||
        !sb_append(json, ",\"name\":") || !sb_json_string(json, g_capture.name) ||
        !sb_append(json, ",\"startedAt\":") || !sb_json_string(json, g_capture.created_at) ||
        !sb_printf(json, ",\"startedAtDeviceTimeNs\":\"%llu\",\"elapsedMs\":%llu,"
                         "\"sizeBytes\":%llu,\"fileCount\":%lu",
                   (unsigned long long)g_capture.started_mono_ns,
                   (unsigned long long)capture_elapsed_ms(),
                   (unsigned long long)g_capture.total_size,
                   (unsigned long)g_capture.file_count) ||
        !sb_append(json, ",\"storage\":{\"target\":\"usb\",\"mediaType\":\"sd\"}") ) return false;
    if (g_capture.failed &&
        (!sb_append(json, ",\"failureReason\":") || !sb_json_string(json, g_capture.failure_reason))) return false;
    if (g_ring_quarantined &&
        !sb_append(json,
            ",\"recoveryRequired\":true,\"rebootRequired\":true,"
            "\"producerState\":\"intentionally_stopped\"")) return false;
    if (!sb_append(json, "}")) return false;
    return true;
}

static bool interface_ipv4(const char *interface_name, char *out, size_t out_size) {
#ifdef __linux__
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    struct ifreq request;
    struct sockaddr_in *address;
    if (fd < 0) return false;
    memset(&request, 0, sizeof(request));
    safe_copy(request.ifr_name, sizeof(request.ifr_name), interface_name);
    if (ioctl(fd, SIOCGIFADDR, &request) != 0) {
        close(fd);
        return false;
    }
    close(fd);
    address = (struct sockaddr_in *)&request.ifr_addr;
    return inet_ntop(AF_INET, &address->sin_addr, out, (socklen_t)out_size) != NULL;
#else
    (void)interface_name; (void)out; (void)out_size;
    return false;
#endif
}

static bool run_command_capture(char *const argv[], unsigned timeout_seconds,
                                struct string_buf *output, int *exit_code) {
    int descriptors[2] = {-1, -1};
    pid_t pid;
    int flags;
    int status = 0;
    bool child_done = false;
    bool pipe_done = false;
    bool ok = true;
    uint64_t deadline_ns;
    if (pipe(descriptors) != 0) return false;
    pid = fork();
    if (pid < 0) {
        close(descriptors[0]);
        close(descriptors[1]);
        return false;
    }
    if (pid == 0) {
        int null_fd;
        close(descriptors[0]);
        if (dup2(descriptors[1], STDOUT_FILENO) < 0 ||
            dup2(descriptors[1], STDERR_FILENO) < 0) _exit(126);
        if (descriptors[1] > STDERR_FILENO) close(descriptors[1]);
        null_fd = open("/dev/null", O_RDONLY | O_CLOEXEC);
        if (null_fd >= 0) {
            (void)dup2(null_fd, STDIN_FILENO);
            if (null_fd > STDERR_FILENO) close(null_fd);
        }
        execv(argv[0], argv);
        dprintf(STDERR_FILENO, "syncap_exec_errno=%d\n", errno);
        _exit(127);
    }
    close(descriptors[1]);
    descriptors[1] = -1;
    flags = fcntl(descriptors[0], F_GETFL, 0);
    if (flags < 0 || fcntl(descriptors[0], F_SETFL, flags | O_NONBLOCK) != 0) ok = false;
    deadline_ns = monotonic_ns() + (uint64_t)timeout_seconds * 1000000000ULL;
    while (ok && (!child_done || !pipe_done) && g_running) {
        char block[4096];
        ssize_t count;
        do {
            count = read(descriptors[0], block, sizeof(block));
            if (count > 0) {
                if (output->len + (size_t)count > WIFI_COMMAND_OUTPUT_LIMIT ||
                    !sb_append_n(output, block, (size_t)count)) {
                    errno = EFBIG;
                    ok = false;
                    break;
                }
            } else if (count == 0) {
                pipe_done = true;
            }
        } while (count > 0);
        if (count < 0 && errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) ok = false;
        if (!child_done) {
            pid_t waited = waitpid(pid, &status, WNOHANG);
            if (waited == pid) child_done = true;
            else if (waited < 0 && errno != EINTR) ok = false;
        }
        /* Do not charge later recorder housekeeping to an already completed
         * command. Its exit status and pipe EOF are both known at this point. */
        if (child_done && pipe_done) break;
        process_closed_segments();
        supervise_qgapp();
        if (monotonic_ns() >= deadline_ns) {
            errno = ETIMEDOUT;
            ok = false;
        }
        if (ok && (!child_done || !pipe_done)) {
            fd_set read_set;
            struct timeval delay = {0, 200000};
            FD_ZERO(&read_set);
            FD_SET(descriptors[0], &read_set);
            if (select(descriptors[0] + 1, &read_set, NULL, NULL, &delay) < 0 && errno != EINTR)
                ok = false;
        }
    }
    if (!child_done) {
        (void)kill(pid, SIGKILL);
        while (waitpid(pid, &status, 0) < 0 && errno == EINTR) {}
    }
    close(descriptors[0]);
    if (exit_code) {
        *exit_code = WIFEXITED(status) ? WEXITSTATUS(status) : 128;
    }
    return ok && child_done && WIFEXITED(status) && WEXITSTATUS(status) == 0;
}

struct wifi_network {
    char ssid[129];
    int rssi;
    bool has_wpa;
    bool has_rsn;
    bool has_psk;
    bool has_sae;
    bool age_known;
    unsigned long long age_ms;
};

static const char *wifi_security(const struct wifi_network *network) {
    if (network->has_sae && !network->has_psk) return "wpa3-sae";
    if (network->has_rsn && network->has_psk) return "wpa2-psk";
    if (network->has_wpa && network->has_psk) return "wpa-psk";
    if (network->has_rsn || network->has_wpa) return "enterprise";
    return "open";
}

static int wifi_security_rank(const struct wifi_network *network) {
    const char *security = wifi_security(network);
    if (strcmp(security, "wpa2-psk") == 0) return 3;
    if (strcmp(security, "wpa-psk") == 0) return 2;
    if (strcmp(security, "open") == 0) return 1;
    return 0;
}

static bool valid_wifi_text(const char *value, size_t minimum, size_t maximum) {
    const unsigned char *cursor = (const unsigned char *)value;
    size_t length = strlen(value);
    if (length < minimum || length > maximum) return false;
    while (*cursor) {
        if (*cursor < 0x20 || *cursor == 0x7f) return false;
        ++cursor;
    }
    return true;
}

static void remember_wifi_network(struct wifi_network *networks, size_t *count,
                                  bool *truncated, const struct wifi_network *candidate,
                                  bool cached) {
    size_t index;
    if (cached && (!candidate->age_known || candidate->age_ms > WIFI_SCAN_CACHE_MAX_AGE_MS))
        return;
    if (!valid_wifi_text(candidate->ssid, 1, 32)) return;
    for (index = 0; index < *count; ++index) {
        if (strcmp(networks[index].ssid, candidate->ssid) != 0) continue;
        if (wifi_security_rank(candidate) > wifi_security_rank(&networks[index]) ||
            (wifi_security_rank(candidate) == wifi_security_rank(&networks[index]) &&
             candidate->rssi > networks[index].rssi)) networks[index] = *candidate;
        return;
    }
    if (*count == MAX_WIFI_NETWORKS) {
        *truncated = true;
        return;
    }
    networks[(*count)++] = *candidate;
}

static int wifi_network_compare(const void *left, const void *right) {
    const struct wifi_network *a = left;
    const struct wifi_network *b = right;
    int rank = wifi_security_rank(b) - wifi_security_rank(a);
    if (rank) return rank;
    if (a->rssi != b->rssi) return b->rssi - a->rssi;
    return strcmp(a->ssid, b->ssid);
}

static bool parse_wifi_scan(char *text, struct string_buf *json, bool cached) {
    struct wifi_network networks[MAX_WIFI_NETWORKS];
    struct wifi_network current;
    size_t count = 0;
    size_t index;
    bool have_bss = false;
    bool truncated = false;
    unsigned long long max_age_ms = 0;
    unsigned long long scanned_at_ms = (unsigned long long)time(NULL) * 1000ULL;
    char *line;
    char *saveptr;
    memset(&current, 0, sizeof(current));
    current.rssi = -127;
    line = text ? strtok_r(text, "\n", &saveptr) : NULL;
    while (line) {
        while (*line == ' ' || *line == '\t') ++line;
        if (strncmp(line, "BSS ", 4) == 0) {
            if (have_bss) remember_wifi_network(networks, &count, &truncated, &current, cached);
            memset(&current, 0, sizeof(current));
            current.rssi = -127;
            have_bss = true;
        } else if (have_bss && strncmp(line, "SSID:", 5) == 0) {
            const char *value = line + 5;
            while (*value == ' ' || *value == '\t') ++value;
            safe_copy(current.ssid, sizeof(current.ssid), value);
        } else if (have_bss && strncmp(line, "signal:", 7) == 0) {
            double signal_value;
            if (sscanf(line + 7, "%lf", &signal_value) == 1) current.rssi = (int)signal_value;
        } else if (have_bss && strncmp(line, "last seen:", 10) == 0) {
            int consumed = 0;
            if (sscanf(line + 10, " %llu ms ago%n", &current.age_ms, &consumed) == 1 && consumed > 0)
                current.age_known = true;
        } else if (have_bss && strncmp(line, "WPA:", 4) == 0) {
            current.has_wpa = true;
        } else if (have_bss && strncmp(line, "RSN:", 4) == 0) {
            current.has_rsn = true;
        } else if (have_bss && strstr(line, "Authentication suites:")) {
            if (strstr(line, "PSK")) current.has_psk = true;
            if (strstr(line, "SAE")) current.has_sae = true;
        }
        line = strtok_r(NULL, "\n", &saveptr);
    }
    if (have_bss) remember_wifi_network(networks, &count, &truncated, &current, cached);
    /* A busy scan with no provably recent BSS is a failure, not an empty scan. */
    if (cached && count == 0) return false;
    qsort(networks, count, sizeof(networks[0]), wifi_network_compare);
    if (!sb_append(json, "{\"networks\":[")) return false;
    for (index = 0; index < count; ++index) {
        const char *security = wifi_security(&networks[index]);
        if (cached && networks[index].age_ms > max_age_ms) max_age_ms = networks[index].age_ms;
        if (index && !sb_append(json, ",")) return false;
        if (!sb_append(json, "{\"ssid\":") || !sb_json_string(json, networks[index].ssid) ||
            !sb_printf(json, ",\"rssi\":%d,\"security\":", networks[index].rssi) ||
            !sb_json_string(json, security) ||
            !sb_append(json, strcmp(security, "open") == 0
                                  ? ",\"secure\":false}"
                                  : ",\"secure\":true}")) return false;
    }
    if (cached) scanned_at_ms = scanned_at_ms > max_age_ms ? scanned_at_ms - max_age_ms : 0;
    if (!sb_printf(json, "],\"scannedAt\":%llu", scanned_at_ms)) return false;
    if (cached && !sb_printf(json, ",\"cached\":true,\"cacheMaxAgeMs\":%llu", max_age_ms)) return false;
    if (truncated && !sb_append(json, ",\"truncated\":true")) return false;
    return sb_append(json, "}");
}

static bool wifi_scan(struct string_buf *json) {
    struct string_buf output = {0};
    int exit_code;
    unsigned attempt;
    char *const scan_argv[] = {g_cfg.iw_path, "dev", "wlan0", "scan", NULL};
    char *const dump_argv[] = {g_cfg.iw_path, "dev", "wlan0", "scan", "dump", NULL};
    /* Scanning is read-only. Country selection remains in configure_wifi().
     * Three 5-second attempts, two 0.5-second delays and a 2-second dump stay
     * within the BLE worker's 25-second HTTP timeout. Retry only driver busy. */
    for (attempt = 0; attempt < 3 && g_running; ++attempt) {
        bool busy;
        exit_code = 0;
        if (run_command_capture(scan_argv, 5, &output, &exit_code)) {
            bool ok = parse_wifi_scan(output.data, json, false);
            sb_free(&output);
            return ok;
        }
        busy = exit_code == ((256 - EBUSY) & 255) && output.data &&
               (strstr(output.data, "(-16)") || strstr(output.data, "Resource busy"));
        if (!busy) {
            int exec_errno = 0;
            if (exit_code == 127 && output.data)
                (void)sscanf(output.data, "syncap_exec_errno=%d", &exec_errno);
            log_line("WARN", "Wi-Fi scan failed (exit %d, exec errno %d)", exit_code, exec_errno);
        }
        sb_free(&output);
        if (!busy) return false;
        if (attempt < 2) {
            struct timespec delay = {0, 500000000L};
            nanosleep(&delay, NULL);
        }
    }
    {
        bool ok;
        exit_code = 0;
        ok = g_running && run_command_capture(dump_argv, 2, &output, &exit_code) &&
             output.data && parse_wifi_scan(output.data, json, true);
        if (!ok) {
            int exec_errno = 0;
            if (exit_code == 127 && output.data)
                (void)sscanf(output.data, "syncap_exec_errno=%d", &exec_errno);
            log_line("WARN", "Wi-Fi cache failed (exit %d, exec errno %d)", exit_code, exec_errno);
        }
        sb_free(&output);
        return ok;
    }
}

static bool current_wifi_ssid(char *ssid, size_t ssid_size) {
    struct string_buf output = {0};
    char *const link_argv[] = {g_cfg.iw_path, "dev", "wlan0", "link", NULL};
    char *line;
    char *saveptr;
    int exit_code;
    bool found = false;
    ssid[0] = '\0';
    if (!run_command_capture(link_argv, 5, &output, &exit_code) || !output.data) {
        sb_free(&output);
        return false;
    }
    line = strtok_r(output.data, "\n", &saveptr);
    while (line) {
        while (*line == ' ' || *line == '\t') ++line;
        if (strncmp(line, "SSID:", 5) == 0) {
            line += 5;
            while (*line == ' ' || *line == '\t') ++line;
            found = valid_wifi_text(line, 1, 32) && safe_copy(ssid, ssid_size, line);
            break;
        }
        line = strtok_r(NULL, "\n", &saveptr);
    }
    sb_free(&output);
    return found;
}

static bool build_wifi_status(struct string_buf *json) {
    char ssid[129];
    char ip[INET_ADDRSTRLEN];
    bool linked = current_wifi_ssid(ssid, sizeof(ssid));
    bool addressed = interface_ipv4("wlan0", ip, sizeof(ip));
    if (!sb_append(json, "{\"state\":") ||
        !sb_json_string(json, linked && addressed ? "connected" : linked ? "obtaining_ip" : "disconnected"))
        return false;
    if (linked && (!sb_append(json, ",\"ssid\":") || !sb_json_string(json, ssid))) return false;
    if (addressed && (!sb_append(json, ",\"ipAddress\":") || !sb_json_string(json, ip))) return false;
    return sb_append(json, "}");
}

static bool configure_wifi(const char *body, struct string_buf *json,
                           const char **error_code, char *error_message,
                           size_t error_message_size) {
    char claim_code[16];
    char ssid[129];
    char password[128] = {0};
    char security[32] = "wpa2-psk";
    char linked_ssid[129] = {0};
    char ip[INET_ADDRSTRLEN] = {0};
    struct string_buf output = {0};
    char *country_argv[] = {g_cfg.wifi_path, "-S", "countrycode", "cCN", NULL};
    char *connect_secure_argv[] = {g_cfg.wifi_path, "-c", ssid, password, NULL};
    char *connect_open_argv[] = {g_cfg.wifi_path, "-c", ssid, NULL};
    int exit_code;
    bool command_ok;
    uint64_t deadline;
    bool linked = false;
    bool addressed = false;
    if (!json_get_string(body, "claimCode", claim_code, sizeof(claim_code)) ||
        strcmp(claim_code, "123456") != 0) {
        *error_code = "unauthorized";
        safe_copy(error_message, error_message_size, "paired BLE authorization is required");
        return false;
    }
    if (!json_get_string(body, "ssid", ssid, sizeof(ssid)) || !valid_wifi_text(ssid, 1, 32)) {
        *error_code = "wifi.invalid_ssid";
        safe_copy(error_message, error_message_size, "SSID must contain 1 to 32 bytes");
        return false;
    }
    (void)json_get_string(body, "security", security, sizeof(security));
    if (strcmp(security, "open") != 0 && strcmp(security, "wpa-psk") != 0 &&
        strcmp(security, "wpa2-psk") != 0) {
        *error_code = "wifi.unsupported_security";
        safe_copy(error_message, error_message_size, "network security is not supported");
        return false;
    }
    if (strcmp(security, "open") != 0 &&
        (!json_get_string(body, "password", password, sizeof(password)) ||
         !valid_wifi_text(password, 8, 63))) {
        *error_code = "wifi.invalid_password";
        safe_copy(error_message, error_message_size, "network password must contain 8 to 63 bytes");
        return false;
    }
    (void)run_command_capture(country_argv, 5, &output, &exit_code);
    sb_free(&output);
    command_ok = run_command_capture(strcmp(security, "open") == 0
                                         ? connect_open_argv
                                         : connect_secure_argv,
                                     60, &output, &exit_code);
    if (output.data) memset(output.data, 0, output.cap);
    sb_free(&output);
    deadline = monotonic_ns() + 20000000000ULL;
    do {
        addressed = interface_ipv4("wlan0", ip, sizeof(ip));
        linked = current_wifi_ssid(linked_ssid, sizeof(linked_ssid));
        if (linked && addressed && strcmp(linked_ssid, ssid) == 0) break;
        {
            struct timespec delay = {0, 250000000L};
            nanosleep(&delay, NULL);
        }
        process_closed_segments();
        supervise_qgapp();
    } while (monotonic_ns() < deadline);
    memset(password, 0, sizeof(password));
    if (!linked || strcmp(linked_ssid, ssid) != 0) {
        *error_code = "wifi_association_timeout";
        safe_copy(error_message, error_message_size,
                  command_ok ? "device could not associate with the selected network"
                             : "Wi-Fi manager rejected the selected network");
        return false;
    }
    if (!addressed) {
        *error_code = "dhcp_failed";
        safe_copy(error_message, error_message_size,
                  "network associated but did not provide an IP address");
        return false;
    }
    return sb_append(json, "{\"state\":\"connected\",\"ssid\":") &&
           sb_json_string(json, linked_ssid) && sb_append(json, ",\"ipAddress\":") &&
           sb_json_string(json, ip) && sb_append(json, "}");
}

static bool build_status(struct string_buf *json) {
    struct storage_info storage = inspect_storage(false);
    bool left_online = g_cfg.dry_run || tcp_port_listening(9100);
    bool right_online = g_cfg.dry_run || tcp_port_listening(9101);
    double uptime = system_uptime();
    double total_gib = (double)storage.total_bytes / (1024.0 * 1024.0 * 1024.0);
    double free_gib = (double)storage.free_bytes / (1024.0 * 1024.0 * 1024.0);
    bool producer_running = (g_cfg.dry_run || qgapp_identity_matches(g_qgapp_pid)) &&
                            !g_ring_quarantined;
    bool trigger_active = producer_running && g_sync.valid;
    char wifi_ip[INET_ADDRSTRLEN] = {0};
    bool wifi_connected = interface_ipv4("wlan0", wifi_ip, sizeof(wifi_ip));
    if (!sb_printf(json,
        "{\"v\":\"1.0\",\"deviceTimeNs\":\"%llu\",\"adapter\":\"allwinner-tina-stereo\","
        "\"cameraDemoRunning\":%s,\"cameras\":["
        "{\"id\":\"left\",\"port\":9100,\"online\":%s,\"fps\":29.4118,\"groupSkewMs\":null},"
        "{\"id\":\"right\",\"port\":9101,\"online\":%s,\"fps\":29.4118,\"groupSkewMs\":null}],\"sync\":{"
        "\"method\":\"hardware_trigger\",\"state\":\"%s\",\"hardwareTrigger\":%s,"
        "\"counterHealth\":\"%s\",\"observedTriggerCount\":",
        (unsigned long long)monotonic_ns(), qgapp_running() ? "true" : "false",
        left_online ? "true" : "false", right_online ? "true" : "false",
        !producer_running ? "producer_stopped" :
            (trigger_active ? "hardware_trigger_active" : "waiting_for_clean_segment"),
        !g_sync.observed ? "null" : (g_sync.hardware_trigger ? "true" : "false"),
        trigger_active ? "stable" : "unverified") ||
        !(g_sync.observed ? sb_printf(json, "%lu", g_sync.fsync_marks) : sb_append(json, "null")) ||
        !sb_append(json, ",\"acquisitionSkewMs\":") ||
        !append_empty_metric(json) || !sb_append(json, ",\"previewPtsSkewMs\":") ||
        !append_empty_metric(json) || !sb_append(json, ",\"previewCameraSkewMs\":") ||
        !append_empty_metric(json) ||
        !sb_append(json,
            "},\"clock\":{\"source\":\"free_running\",\"state\":\"unsupported\",\"domain\":null,"
            "\"interface\":null,\"hardwareTimestamping\":false,\"phcDevice\":null,"
            "\"ptp4lRunning\":false,\"phc2sysRunning\":false,\"grandmasterPresent\":false,"
            "\"statusReason\":\"not_required\",\"requiredForCapture\":false,"
            "\"grandmasterIdentity\":null,\"offsetFromMasterNs\":null,\"meanPathDelayNs\":null,"
            "\"lastUpdateAgeMs\":null,\"systemClockDisciplined\":false,\"sensorClockMapped\":false},") ||
        !sb_printf(json,
            "\"system\":{\"uptimeSeconds\":%.3f,\"temperatureC\":null,"
            "\"storage\":{\"totalGiB\":%.3f,\"availableGiB\":%.3f}},\"capture\":",
            uptime, total_gib, free_gib) || !build_capture_status(json) ||
        !sb_append(json, ",\"storage\":" ) || !append_storage_json(json, &storage) ||
        !sb_printf(json, ",\"wifi\":{\"state\":\"%s\",\"ssid\":null,\"ipAddress\":",
                   wifi_connected ? "connected" : "disconnected") ||
        !(wifi_connected ? sb_json_string(json, wifi_ip) : sb_append(json, "null")) ||
        !sb_append(json, "}}")) return false;
    return true;
}

static bool session_root(char *out, size_t out_size) {
    struct storage_info storage = inspect_storage(false);
    struct stat st;
    if (!storage.mounted || !storage.root[0]) return false;
    return path_join(out, out_size, storage.root, "SynCap") &&
           lstat(out, &st) == 0 && S_ISDIR(st.st_mode);
}

static bool resolve_session(const char *session_id, char *out, size_t out_size) {
    char root[PATH_MAX];
    struct stat st;
    if (strncmp(session_id, "ses_", 4) != 0 || !is_safe_component(session_id) ||
        !session_root(root, sizeof(root)) || !path_join(out, out_size, root, session_id) ||
        lstat(out, &st) != 0 || !S_ISDIR(st.st_mode)) return false;
    return true;
}

static int string_compare_desc(const void *left, const void *right) {
    const char *const *a = left;
    const char *const *b = right;
    return strcmp(*b, *a);
}

/* Validate persisted metadata before advertising it as a usable session.
 * This deliberately checks file sizes, not full hashes: the recorder shares
 * this thread, and re-hashing an entire SD card would stall segment draining. */
static const char *validate_session_document(const char *directory, const char *session_id,
                                              const struct string_buf *json, bool exporting) {
    char id[96];
    char state[32] = {0};
    const char *cursor;
    uint64_t total = 0;
    size_t count = 0;
    long long declared_size;
    long long declared_count = -1;
    if (!json->data || !json_root_object_complete(json->data, json->len) ||
        !json_get_string(json->data, exporting ? "sessionId" : "id", id, sizeof(id)) ||
        strcmp(id, session_id) != 0) return "session.metadata_corrupt";
    declared_size = json_get_integer(json->data, exporting ? "totalBytes" : "sizeBytes", -1);
    if (declared_size < 0) return "session.metadata_corrupt";
    if (!exporting) {
        if (!json_get_string(json->data, "state", state, sizeof(state)) ||
            (strcmp(state, "recording") != 0 && strcmp(state, "finalizing") != 0 &&
             strcmp(state, "complete") != 0 && strcmp(state, "failed") != 0))
            return "session.metadata_corrupt";
        declared_count = json_get_integer(json->data, "fileCount", -1);
        if (declared_count < 0) return "session.metadata_corrupt";
    }
    cursor = strstr(json->data, "\"files\"");
    if (!cursor) return "session.metadata_corrupt";
    cursor += strlen("\"files\"");
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor++ != ':') return "session.metadata_corrupt";
    while (isspace((unsigned char)*cursor)) ++cursor;
    if (*cursor++ != '[') return "session.metadata_corrupt";
    while (true) {
        const char *start;
        unsigned depth = 0;
        bool in_string = false;
        bool escaped = false;
        char entry[4096];
        char name[NAME_MAX + 1];
        char path[PATH_MAX];
        char sha[65];
        struct stat st;
        long long size;
        size_t length;
        size_t index;
        while (isspace((unsigned char)*cursor)) ++cursor;
        if (*cursor == ']') break;
        if (*cursor != '{') return "session.metadata_corrupt";
        start = cursor;
        do {
            char ch = *cursor++;
            if (!ch) return "session.metadata_corrupt";
            if (in_string) {
                if (escaped) escaped = false;
                else if (ch == '\\') escaped = true;
                else if (ch == '"') in_string = false;
            } else if (ch == '"') in_string = true;
            else if (ch == '{') ++depth;
            else if (ch == '}') --depth;
        } while (depth || in_string);
        length = (size_t)(cursor - start);
        if (length >= sizeof(entry)) return "session.metadata_corrupt";
        memcpy(entry, start, length);
        entry[length] = '\0';
        if (!json_get_string(entry, "name", name, sizeof(name)) ||
            !is_safe_component(name) ||
            (!has_suffix(name, ".mcap") && !has_suffix(name, ".mcap.status.json")) ||
            !json_get_string(entry, "sha256", sha, sizeof(sha)) || strlen(sha) != 64)
            return "session.metadata_corrupt";
        for (index = 0; index < 64; ++index)
            if (!isxdigit((unsigned char)sha[index])) return "session.metadata_corrupt";
        size = json_get_integer(entry, "sizeBytes", -1);
        if (size <= 0 || (has_suffix(name, ".mcap") && size <= MCAP_MAGIC_SIZE))
            return "session.data_corrupt";
        if (!path_join(path, sizeof(path), directory, name) || lstat(path, &st) != 0 ||
            !S_ISREG(st.st_mode) || st.st_size != size) return "session.data_corrupt";
        total += (uint64_t)size;
        ++count;
        process_closed_segments();
        supervise_qgapp();
        while (isspace((unsigned char)*cursor)) ++cursor;
        if (*cursor == ']') break;
        if (*cursor++ != ',') return "session.metadata_corrupt";
        while (isspace((unsigned char)*cursor)) ++cursor;
        if (*cursor != '{') return "session.metadata_corrupt";
    }
    if (total != (uint64_t)declared_size ||
        (!exporting && count != (uint64_t)declared_count)) return "session.metadata_corrupt";
    if ((exporting || strcmp(state, "complete") == 0) && count == 0)
        return "session.data_corrupt";
    return NULL;
}

static bool build_sessions(struct string_buf *json) {
    char root[PATH_MAX];
    DIR *directory;
    struct dirent *entry;
    char **ids = NULL;
    size_t count = 0;
    size_t capacity = 0;
    size_t index;
    if (!sb_append(json, "{\"sessions\":[")) return false;
    if (!session_root(root, sizeof(root)) || !(directory = opendir(root))) return sb_append(json, "]}");
    while ((entry = readdir(directory)) != NULL) {
        char path[PATH_MAX];
        struct stat st;
        char **grown;
        process_closed_segments();
        supervise_qgapp();
        if (strncmp(entry->d_name, "ses_", 4) != 0 || !is_safe_component(entry->d_name) ||
            !path_join(path, sizeof(path), root, entry->d_name) || lstat(path, &st) != 0 ||
            !S_ISDIR(st.st_mode)) continue;
        if (count == capacity) {
            capacity = capacity ? capacity * 2 : 16;
            grown = realloc(ids, capacity * sizeof(*ids));
            if (!grown) break;
            ids = grown;
        }
        ids[count] = strdup(entry->d_name);
        if (!ids[count]) break;
        ++count;
    }
    closedir(directory);
    qsort(ids, count, sizeof(*ids), string_compare_desc);
    for (index = 0; index < count; ++index) {
        char dir[PATH_MAX];
        char manifest_path[PATH_MAX];
        struct string_buf manifest = {0};
        char id[96] = {0};
        char name[256] = {0};
        char created_at[64] = {0};
        char state[32] = {0};
        long long duration;
        long long size;
        long long file_count;
        const char *failure_reason = NULL;
        process_closed_segments();
        supervise_qgapp();
        if (!path_join(dir, sizeof(dir), root, ids[index]) ||
            !path_join(manifest_path, sizeof(manifest_path), dir, "session.json") ||
            !read_small_file(manifest_path, &manifest, 16U * 1024U * 1024U))
            failure_reason = "session.metadata_corrupt";
        else
            failure_reason = validate_session_document(dir, ids[index], &manifest, false);
        duration = 0;
        size = 0;
        file_count = 0;
        if (manifest.data && (!failure_reason || strcmp(failure_reason, "session.data_corrupt") == 0)) {
            json_get_string(manifest.data, "id", id, sizeof(id));
            json_get_string(manifest.data, "name", name, sizeof(name));
            json_get_string(manifest.data, "createdAt", created_at, sizeof(created_at));
            json_get_string(manifest.data, "state", state, sizeof(state));
            duration = json_get_integer(manifest.data, "durationMs", 0);
            size = json_get_integer(manifest.data, "sizeBytes", 0);
            file_count = json_get_integer(manifest.data, "fileCount", 0);
        }
        if (failure_reason) safe_copy(state, sizeof(state), "failed");
        if (!name[0]) safe_copy(name, sizeof(name), ids[index]);
        if (json->data && json->len && json->data[json->len - 1] != '[') sb_append(json, ",");
        sb_append(json, "{\"id\":"); sb_json_string(json, id[0] ? id : ids[index]);
        sb_append(json, ",\"name\":"); sb_json_string(json, name);
        sb_append(json, ",\"createdAt\":"); sb_json_string(json, created_at);
        sb_printf(json, ",\"durationMs\":%lld,\"sizeBytes\":%lld,\"status\":", duration, size);
        sb_json_string(json, state);
        if (failure_reason) {
            sb_append(json, ",\"failureReason\":");
            sb_json_string(json, failure_reason);
            sb_append(json, ",\"recoveryRequired\":true,\"exportAvailable\":false");
        }
        sb_printf(json, ",\"fileCount\":%lld,\"storage\":{\"target\":\"usb\","
                        "\"mediaType\":\"sd\",\"displayName\":\"SD 卡\"}}", file_count);
        sb_free(&manifest);
    }
    for (index = 0; index < count; ++index) free(ids[index]);
    free(ids);
    return sb_append(json, "]}");
}

static bool read_session_file(const char *session_id, const char *filename,
                              char *path, size_t path_size) {
    char directory[PATH_MAX];
    struct stat st;
    if (!is_safe_component(filename) || !resolve_session(session_id, directory, sizeof(directory)) ||
        !path_join(path, path_size, directory, filename) || lstat(path, &st) != 0 ||
        !S_ISREG(st.st_mode)) return false;
    if (strcmp(filename, "session.json") == 0 || strcmp(filename, ".syncap-export.json") == 0) return true;
    return has_suffix(filename, ".mcap") || has_suffix(filename, ".mcap.status.json");
}

static bool mark_manifest_interrupted(const char *path, bool *was_interrupted) {
    struct string_buf original = {0};
    struct string_buf updated = {0};
    const char *marker;
    const char *value_start;
    const char *value_end;
    char old_state[32];
    size_t state_length;
    char completed_at[64];
    bool ok = false;
    *was_interrupted = false;
    if (!read_small_file(path, &original, 16U * 1024U * 1024U)) goto done;
    marker = strstr(original.data, "\"state\":");
    if (!marker) goto done;
    value_start = strchr(marker + strlen("\"state\":"), '\"');
    if (!value_start) goto done;
    value_end = strchr(value_start + 1, '\"');
    if (!value_end) goto done;
    state_length = (size_t)(value_end - value_start - 1);
    if (state_length >= sizeof(old_state)) goto done;
    memcpy(old_state, value_start + 1, state_length);
    old_state[state_length] = '\0';
    if (strcmp(old_state, "recording") != 0 && strcmp(old_state, "finalizing") != 0) {
        ok = true;
        goto done;
    }
    if (!sb_append_n(&updated, original.data, (size_t)(value_start - original.data)) ||
        !sb_json_string(&updated, "failed") ||
        !sb_append(&updated, value_end + 1)) goto done;
    while (updated.len && isspace((unsigned char)updated.data[updated.len - 1])) --updated.len;
    if (!updated.len || updated.data[updated.len - 1] != '}') goto done;
    --updated.len;
    updated.data[updated.len] = '\0';
    iso_now(completed_at, sizeof(completed_at));
    if (!sb_append(&updated, ",\"failureReason\":\"capture.interrupted_by_service_restart\","
                            "\"recoveryRequired\":true,\"rebootRequired\":true,\"completedAt\":") ||
        !sb_json_string(&updated, completed_at) || !sb_append(&updated, "}")) goto done;
    ok = write_atomic_text(path, updated.data, updated.len);
    if (ok) *was_interrupted = true;
done:
    sb_free(&updated);
    sb_free(&original);
    return ok;
}

static bool recover_interrupted_sessions(char *session_id, size_t session_id_size) {
    char root[PATH_MAX];
    DIR *directory;
    struct dirent *entry;
    bool recovered_any = false;
    session_id[0] = '\0';
    if (!session_root(root, sizeof(root)) || !(directory = opendir(root))) return false;
    while ((entry = readdir(directory)) != NULL) {
        char session_dir[PATH_MAX];
        char manifest[PATH_MAX];
        struct stat st;
        bool was_interrupted = false;
        if (strncmp(entry->d_name, "ses_", 4) != 0 || !is_safe_component(entry->d_name) ||
            !path_join(session_dir, sizeof(session_dir), root, entry->d_name) ||
            lstat(session_dir, &st) != 0 || !S_ISDIR(st.st_mode) ||
            !path_join(manifest, sizeof(manifest), session_dir, "session.json") ||
            !file_exists(manifest)) continue;
        if (!mark_manifest_interrupted(manifest, &was_interrupted)) {
            log_line("WARN", "could not recover interrupted session %s", entry->d_name);
        } else if (was_interrupted) {
            recovered_any = true;
            if (!session_id[0]) safe_copy(session_id, session_id_size, entry->d_name);
        }
    }
    closedir(directory);
    return recovered_any;
}

static const char *http_reason(int status) {
    switch (status) {
        case 200: return "OK";
        case 206: return "Partial Content";
        case 204: return "No Content";
        case 400: return "Bad Request";
        case 401: return "Unauthorized";
        case 403: return "Forbidden";
        case 404: return "Not Found";
        case 409: return "Conflict";
        case 413: return "Payload Too Large";
        case 416: return "Range Not Satisfiable";
        case 500: return "Internal Server Error";
        default: return "Error";
    }
}

static bool send_headers(int fd, int status, const char *content_type, uint64_t content_length,
                         const char *extra) {
    char headers[2048];
    int length = snprintf(headers, sizeof(headers),
        "HTTP/1.1 %d %s\r\n"
        "Server: SynCap-Tina/%s\r\n"
        "Content-Type: %s\r\n"
        "Content-Length: %llu\r\n"
        "Access-Control-Allow-Origin: *\r\n"
        "Access-Control-Allow-Headers: Content-Type, Range, X-SynCap-Claim\r\n"
        "Access-Control-Allow-Methods: GET, HEAD, POST, OPTIONS\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n%s\r\n",
        status, http_reason(status), SERVICE_VERSION, content_type,
        (unsigned long long)content_length, extra ? extra : "");
    return length > 0 && (size_t)length < sizeof(headers) && write_all(fd, headers, (size_t)length);
}

static bool send_json(int fd, int status, const char *json, size_t length) {
    return send_headers(fd, status, "application/json; charset=utf-8", length, NULL) &&
           (length == 0 || write_all(fd, json, length));
}

static void send_error_json(int fd, int status, const char *code, const char *message) {
    struct string_buf json = {0};
    if (sb_append(&json, "{\"error\":") && sb_json_string(&json, code) &&
        sb_append(&json, ",\"message\":") && sb_json_string(&json, message) &&
        sb_append(&json, "}")) send_json(fd, status, json.data, json.len);
    sb_free(&json);
}

static ssize_t read_request_bytes(int fd, void *buffer, size_t length, uint64_t deadline_ns) {
    while (g_running) {
        uint64_t now = monotonic_ns();
        uint64_t remaining_ns;
        struct timeval timeout;
        fd_set read_set;
        int ready;
        if (now >= deadline_ns) {
            errno = ETIMEDOUT;
            return -1;
        }
        remaining_ns = deadline_ns - now;
        if (remaining_ns > 200000000ULL) remaining_ns = 200000000ULL;
        timeout.tv_sec = (time_t)(remaining_ns / 1000000000ULL);
        timeout.tv_usec = (suseconds_t)((remaining_ns % 1000000000ULL) / 1000ULL);
        FD_ZERO(&read_set);
        FD_SET(fd, &read_set);
        ready = select(fd + 1, &read_set, NULL, NULL, &timeout);
        process_closed_segments();
        supervise_qgapp();
        if (ready > 0) return read(fd, buffer, length);
        if (ready < 0 && errno != EINTR) return -1;
    }
    errno = EINTR;
    return -1;
}

static bool parse_http_request(int fd, struct http_request *request, int *error_status) {
    char raw[HTTP_LIMIT + 8192];
    size_t used = 0;
    char *separator = NULL;
    size_t header_length;
    size_t body_offset;
    size_t already;
    size_t content_length = 0;
    char *line;
    char *saveptr;
    uint64_t deadline_ns = monotonic_ns() + 10000000000ULL;

    memset(request, 0, sizeof(*request));
    while (used + 1 < sizeof(raw)) {
        ssize_t count = read_request_bytes(fd, raw + used, sizeof(raw) - used - 1,
                                           deadline_ns);
        if (count < 0) {
            if (errno == EINTR) continue;
            *error_status = 400;
            return false;
        }
        if (count == 0) break;
        used += (size_t)count;
        raw[used] = '\0';
        separator = strstr(raw, "\r\n\r\n");
        if (separator) break;
    }
    if (!separator) {
        *error_status = used + 1 >= sizeof(raw) ? 413 : 400;
        return false;
    }
    header_length = (size_t)(separator - raw);
    body_offset = header_length + 4;
    already = used > body_offset ? used - body_offset : 0;
    if (sscanf(raw, "%11s %2047s", request->method, request->path) != 2) {
        *error_status = 400;
        return false;
    }
    *separator = '\0';
    (void)strtok_r(raw, "\r\n", &saveptr);
    while ((line = strtok_r(NULL, "\r\n", &saveptr)) != NULL) {
        char *colon = strchr(line, ':');
        char *value;
        if (!colon) continue;
        *colon = '\0';
        value = colon + 1;
        while (*value == ' ' || *value == '\t') ++value;
        if (strcasecmp(line, "Content-Length") == 0) {
            char *end;
            unsigned long parsed = strtoul(value, &end, 10);
            while (*end == ' ' || *end == '\t') ++end;
            if (*end || parsed > HTTP_LIMIT) {
                *error_status = parsed > HTTP_LIMIT ? 413 : 400;
                return false;
            }
            content_length = (size_t)parsed;
        } else if (strcasecmp(line, "Host") == 0) {
            safe_copy(request->host, sizeof(request->host), value);
        } else if (strcasecmp(line, "Range") == 0) {
            safe_copy(request->range, sizeof(request->range), value);
        }
    }
    if (already > content_length) already = content_length;
    if (already) memcpy(request->body, ((char *)separator) + 4, already);
    request->body_len = already;
    while (request->body_len < content_length) {
        ssize_t count = read_request_bytes(fd, request->body + request->body_len,
                                           content_length - request->body_len,
                                           deadline_ns);
        if (count < 0) {
            if (errno == EINTR) continue;
            *error_status = 400;
            return false;
        }
        if (count == 0) {
            *error_status = 400;
            return false;
        }
        request->body_len += (size_t)count;
    }
    request->body[request->body_len] = '\0';
    return true;
}

struct byte_range {
    uint64_t start;
    uint64_t end;
    bool partial;
};

static bool parse_byte_range(const char *header, uint64_t size, struct byte_range *range) {
    const char *spec;
    char *end;
    unsigned long long first;
    unsigned long long last;
    range->start = 0;
    range->end = size ? size - 1 : 0;
    range->partial = false;
    if (!header || !*header) return true;
    if (strncmp(header, "bytes=", 6) != 0 || size == 0) return false;
    spec = header + 6;
    if (strchr(spec, ',')) return false;
    if (*spec == '-') {
        first = strtoull(spec + 1, &end, 10);
        if (*end || first == 0) return false;
        if (first > size) first = size;
        range->start = size - first;
        range->end = size - 1;
    } else {
        first = strtoull(spec, &end, 10);
        if (end == spec || first >= size || *end != '-') return false;
        spec = end + 1;
        if (*spec) {
            last = strtoull(spec, &end, 10);
            if (*end || last < first) return false;
            if (last >= size) last = size - 1;
        } else {
            last = size - 1;
        }
        range->start = first;
        range->end = last;
    }
    range->partial = true;
    return true;
}

static bool send_download_bytes(int fd, const unsigned char *data, size_t length,
                                uint64_t *last_progress_ns,
                                uint64_t *last_housekeeping_ns) {
    while (length && g_running) {
        uint64_t now = monotonic_ns();
        ssize_t written;
        if (!*last_housekeeping_ns || now - *last_housekeeping_ns >= 200000000ULL) {
            process_closed_segments();
            supervise_qgapp();
            *last_housekeeping_ns = now;
        }
        written = send(fd, data, length, 0);
        if (written > 0) {
            data += written;
            length -= (size_t)written;
            *last_progress_ns = monotonic_ns();
            continue;
        }
        if (written == 0) return false;
        if (errno == EINTR) continue;
        if (errno != EAGAIN && errno != EWOULDBLOCK) return false;
        now = monotonic_ns();
        if (now - *last_progress_ns >= EXPORT_STALL_SECONDS * 1000000000ULL) {
            errno = ETIMEDOUT;
            return false;
        }
        {
            fd_set write_set;
            struct timeval timeout = {0, 200000};
            FD_ZERO(&write_set);
            FD_SET(fd, &write_set);
            if (select(fd + 1, NULL, &write_set, NULL, &timeout) < 0 && errno != EINTR)
                return false;
        }
    }
    return length == 0;
}

static void send_file_response(int fd, const char *path, const char *range_header, bool head_only) {
    struct stat st;
    struct byte_range range;
    char extra[512];
    int file_fd;
    uint64_t remaining;
    uint64_t last_progress_ns;
    uint64_t last_housekeeping_ns = 0;
    unsigned char block[IO_BLOCK];
    file_fd = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (file_fd < 0 || fstat(file_fd, &st) != 0 || !S_ISREG(st.st_mode)) {
        if (file_fd >= 0) close(file_fd);
        send_error_json(fd, 404, "not_found", "file not found");
        return;
    }
    if (!parse_byte_range(range_header, (uint64_t)st.st_size, &range)) {
        snprintf(extra, sizeof(extra), "Accept-Ranges: bytes\r\nContent-Range: bytes */%llu\r\n",
                 (unsigned long long)st.st_size);
        send_headers(fd, 416, "application/octet-stream", 0, extra);
        close(file_fd);
        return;
    }
    if (range.partial) {
        snprintf(extra, sizeof(extra),
                 "Accept-Ranges: bytes\r\nContent-Range: bytes %llu-%llu/%llu\r\n",
                 (unsigned long long)range.start, (unsigned long long)range.end,
                 (unsigned long long)st.st_size);
    } else {
        safe_copy(extra, sizeof(extra), "Accept-Ranges: bytes\r\n");
    }
    remaining = st.st_size == 0 ? 0 : range.end - range.start + 1;
    if (!send_headers(fd, range.partial ? 206 : 200, "application/octet-stream", remaining, extra) ||
        head_only) {
        close(file_fd);
        return;
    }
    {
        int flags = fcntl(fd, F_GETFL, 0);
        if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
            close(file_fd);
            return;
        }
    }
    if (lseek(file_fd, (off_t)range.start, SEEK_SET) < 0) {
        close(file_fd);
        return;
    }
    last_progress_ns = monotonic_ns();
    while (remaining) {
        size_t want = remaining < sizeof(block) ? (size_t)remaining : sizeof(block);
        ssize_t count = read(file_fd, block, want);
        if (count <= 0 || !send_download_bytes(fd, block, (size_t)count,
                                                &last_progress_ns,
                                                &last_housekeeping_ns)) break;
        remaining -= (uint64_t)count;
    }
    close(file_fd);
}

static bool split_session_route(const char *path, char *session_id, size_t session_size,
                                char *tail, size_t tail_size) {
    static const char prefix[] = "/v1/sessions/";
    const char *cursor;
    const char *slash;
    size_t id_length;
    if (strncmp(path, prefix, sizeof(prefix) - 1) != 0) return false;
    cursor = path + sizeof(prefix) - 1;
    slash = strchr(cursor, '/');
    if (!slash) return false;
    id_length = (size_t)(slash - cursor);
    if (id_length == 0 || id_length >= session_size) return false;
    memcpy(session_id, cursor, id_length);
    session_id[id_length] = '\0';
    return safe_copy(tail, tail_size, slash + 1);
}

static bool split_capture_stop(const char *path, char *capture_id, size_t size) {
    static const char prefix[] = "/v1/captures/";
    static const char suffix[] = "/stop";
    size_t path_length = strlen(path);
    size_t id_length;
    if (strncmp(path, prefix, sizeof(prefix) - 1) != 0 || !has_suffix(path, suffix)) return false;
    id_length = path_length - (sizeof(prefix) - 1) - (sizeof(suffix) - 1);
    if (id_length == 0 || id_length >= size) return false;
    memcpy(capture_id, path + sizeof(prefix) - 1, id_length);
    capture_id[id_length] = '\0';
    return strncmp(capture_id, "cap_", 4) == 0 && is_safe_component(capture_id);
}

static bool client_is_loopback(int fd) {
    struct sockaddr_in peer;
    socklen_t peer_size = sizeof(peer);
    uint32_t address;
    memset(&peer, 0, sizeof(peer));
    if (getpeername(fd, (struct sockaddr *)&peer, &peer_size) != 0 ||
        peer.sin_family != AF_INET) return false;
    address = ntohl(peer.sin_addr.s_addr);
    return (address >> 24) == 127U;
}

static void handle_request(int fd, const struct http_request *request) {
    struct string_buf json = {0};
    struct storage_info storage;
    char session_id[96];
    char tail[NAME_MAX + 64];
    char path[PATH_MAX];

    if (strcmp(request->method, "OPTIONS") == 0) {
        send_headers(fd, 204, "text/plain", 0, NULL);
        return;
    }
    if (strcmp(request->method, "GET") == 0 || strcmp(request->method, "HEAD") == 0) {
        bool head_only = strcmp(request->method, "HEAD") == 0;
        if (strcmp(request->path, "/health") == 0) {
            sb_printf(&json, "{\"ok\":true,\"service\":\"syncap-tina\",\"version\":\"%s\","
                             "\"qgappRunning\":%s,\"ringQuarantined\":%s,"
                             "\"producerIntentionallyStopped\":%s}", SERVICE_VERSION,
                      qgapp_running() ? "true" : "false",
                      g_ring_quarantined ? "true" : "false",
                      g_producer_intentionally_stopped ? "true" : "false");
        } else if (strcmp(request->path, "/v1/manifest") == 0) {
            build_manifest(request, &json);
        } else if (strcmp(request->path, "/v1/status") == 0) {
            build_status(&json);
        } else if (strcmp(request->path, "/v1/wifi/scan") == 0) {
            if (!wifi_scan(&json)) {
                send_error_json(fd, 500, "wifi.scan_failed",
                                "device could not scan nearby networks");
                sb_free(&json);
                return;
            }
        } else if (strcmp(request->path, "/v1/wifi/status") == 0) {
            if (!build_wifi_status(&json)) {
                send_error_json(fd, 500, "wifi.status_failed",
                                "device could not read Wi-Fi status");
                sb_free(&json);
                return;
            }
        } else if (strcmp(request->path, "/v1/storage") == 0) {
            storage = inspect_storage(false);
            append_storage_json(&json, &storage);
        } else if (strcmp(request->path, "/v1/captures/current") == 0) {
            build_capture_status(&json);
        } else if (strcmp(request->path, "/v1/sessions") == 0) {
            build_sessions(&json);
        } else if (split_session_route(request->path, session_id, sizeof(session_id), tail, sizeof(tail))) {
            if (strcmp(tail, "manifest") == 0 && read_session_file(session_id, "session.json", path, sizeof(path))) {
                char directory[PATH_MAX];
                const char *failure_reason;
                if (!read_small_file(path, &json, 4U * 1024U * 1024U)) {
                    send_error_json(fd, 500, "session.read_failed", "cannot read session manifest");
                    sb_free(&json);
                    return;
                }
                failure_reason = resolve_session(session_id, directory, sizeof(directory))
                    ? validate_session_document(directory, session_id, &json, false)
                    : "session.metadata_corrupt";
                if (failure_reason) {
                    send_error_json(fd, 409, failure_reason,
                                    "saved session metadata or data is damaged; original files were retained");
                    sb_free(&json);
                    return;
                }
            } else if (strncmp(tail, "files/", 6) == 0 &&
                       read_session_file(session_id, tail + 6, path, sizeof(path))) {
                send_file_response(fd, path, request->range, head_only);
                return;
            } else {
                send_error_json(fd, 404, "not_found", "session resource not found");
                return;
            }
        } else {
            send_error_json(fd, 404, "not_found", "endpoint not found");
            return;
        }
        if (!json.data) {
            send_error_json(fd, 500, "internal_error", "cannot build response");
        } else if (head_only) {
            send_headers(fd, 200, "application/json; charset=utf-8", json.len, NULL);
        } else {
            send_json(fd, 200, json.data, json.len);
        }
        sb_free(&json);
        return;
    }

    if (strcmp(request->method, "POST") == 0) {
        int status = 500;
        const char *error_code = "internal_error";
        char error_message[512] = "request failed";
        char value[256] = {0};
        char capture_id[96];

        if (strcmp(request->path, "/v1/wifi/configure") == 0) {
            if (!client_is_loopback(fd)) {
                send_error_json(fd, 403, "forbidden",
                                "Wi-Fi configuration is available only through local provisioning");
                return;
            }
            if (!configure_wifi(request->body, &json, &error_code,
                                error_message, sizeof(error_message))) {
                status = strcmp(error_code, "unauthorized") == 0 ? 401 :
                         strncmp(error_code, "wifi.invalid_", 13) == 0 ? 400 : 409;
                send_error_json(fd, status, error_code, error_message);
                sb_free(&json);
                return;
            }
        } else if (strcmp(request->path, "/v1/storage/configure") == 0) {
            if (!json_get_string(request->body, "target", value, sizeof(value))) {
                send_error_json(fd, 400, "invalid_request", "target is required");
                return;
            }
            if (strcmp(value, "usb") != 0 && strcmp(value, "sd") != 0) {
                send_error_json(fd, 409, "storage.unsupported_target",
                                "this device records only to its removable SD card");
                return;
            }
            storage = inspect_storage(true);
            append_storage_json(&json, &storage);
        } else if (strcmp(request->path, "/v1/captures/start") == 0) {
            json_get_string(request->body, "name", value, sizeof(value));
            if (!begin_capture(value, &json, &status, &error_code,
                               error_message, sizeof(error_message))) {
                send_error_json(fd, status, error_code, error_message);
                sb_free(&json);
                return;
            }
        } else if (split_capture_stop(request->path, capture_id, sizeof(capture_id))) {
            if (!finalize_capture(capture_id, &json, &status, &error_code,
                                  error_message, sizeof(error_message))) {
                send_error_json(fd, status, error_code, error_message);
                sb_free(&json);
                return;
            }
        } else if (split_session_route(request->path, session_id, sizeof(session_id), tail, sizeof(tail)) &&
                   strcmp(tail, "prepare-export") == 0 &&
                   read_session_file(session_id, ".syncap-export.json", path, sizeof(path))) {
            char directory[PATH_MAX];
            const char *failure_reason;
            if (!read_small_file(path, &json, 16U * 1024U * 1024U)) {
                send_error_json(fd, 500, "session.read_failed", "cannot read export manifest");
                sb_free(&json);
                return;
            }
            failure_reason = resolve_session(session_id, directory, sizeof(directory))
                ? validate_session_document(directory, session_id, &json, true)
                : "session.metadata_corrupt";
            if (failure_reason) {
                send_error_json(fd, 409, failure_reason,
                                "saved export metadata or files are damaged; original files were retained");
                sb_free(&json);
                return;
            }
        } else {
            send_error_json(fd, 404, "not_found", "endpoint not found");
            return;
        }
        if (!json.data) send_error_json(fd, 500, "internal_error", "cannot build response");
        else send_json(fd, 200, json.data, json.len);
        sb_free(&json);
        return;
    }
    send_error_json(fd, 400, "invalid_method", "unsupported HTTP method");
}

static void stop_service(int signal_number) {
    (void)signal_number;
    g_running = 0;
}

static void usage(FILE *output, const char *program) {
    fprintf(output,
        "Usage: %s [options]\n"
        "  --port N                     HTTP port (default 8080)\n"
        "  --ring PATH                  qgapp rolling directory\n"
        "  --storage PATH               SD mount candidate (repeatable)\n"
        "  --qgapp PATH                 vendor recorder executable\n"
        "  --qgapp-pidfile PATH         persistent qgapp pidfile\n"
        "  --qgapp-log PATH             vendor recorder log\n"
        "  --wifi-command PATH          vendor Wi-Fi manager (default /bin/wifi)\n"
        "  --iw-command PATH            iw executable (default /usr/sbin/iw)\n"
        "  --min-free-bytes N           capture threshold\n"
        "  --dry-run                    do not start qgapp (host tests)\n"
        "  --allow-unmounted-storage    QA only; accept a normal directory\n"
        "  --allow-missing-preview-listeners  QA only; skip 9100/9101 readiness\n",
        program);
}

static bool parse_u64(const char *text, uint64_t *value) {
    char *end;
    unsigned long long parsed;
    errno = 0;
    parsed = strtoull(text, &end, 10);
    if (errno || end == text || *end) return false;
    *value = (uint64_t)parsed;
    return true;
}

static bool configure(int argc, char **argv) {
    const char *env_storage;
    const char *env_allow;
    int index;
    memset(&g_cfg, 0, sizeof(g_cfg));
    safe_copy(g_cfg.ring_dir, sizeof(g_cfg.ring_dir), "/tmp/syncap-ring");
    safe_copy(g_cfg.qgapp_path, sizeof(g_cfg.qgapp_path), "/usr/bin/qgapp");
    safe_copy(g_cfg.qgapp_pidfile, sizeof(g_cfg.qgapp_pidfile), "/var/run/syncap-qgapp.pid");
    safe_copy(g_cfg.qgapp_log, sizeof(g_cfg.qgapp_log), "/dev/null");
    safe_copy(g_cfg.wifi_path, sizeof(g_cfg.wifi_path), "/bin/wifi");
    safe_copy(g_cfg.iw_path, sizeof(g_cfg.iw_path), "/usr/sbin/iw");
    safe_copy(g_cfg.storage_candidates[0], sizeof(g_cfg.storage_candidates[0]), "/rom/mnt/SDCARD");
    safe_copy(g_cfg.storage_candidates[1], sizeof(g_cfg.storage_candidates[1]), "/mnt/extsd");
    g_cfg.storage_candidate_count = 2;
    g_cfg.port = DEFAULT_PORT;
    g_cfg.minimum_free_bytes = DEFAULT_MIN_FREE;

    env_storage = getenv("SYNCAP_TINA_STORAGE_ROOT");
    if (env_storage && *env_storage) {
        if (!safe_copy(g_cfg.storage_candidates[0], sizeof(g_cfg.storage_candidates[0]), env_storage)) return false;
        g_cfg.storage_candidate_count = 1;
    }
    env_allow = getenv("SYNCAP_TINA_ALLOW_UNMOUNTED_STORAGE");
    if (env_allow && strcmp(env_allow, "1") == 0) g_cfg.allow_unmounted_storage = true;

    for (index = 1; index < argc; ++index) {
        const char *option = argv[index];
        const char *value = index + 1 < argc ? argv[index + 1] : NULL;
        if (strcmp(option, "--help") == 0 || strcmp(option, "-h") == 0) {
            usage(stdout, argv[0]);
            exit(0);
        } else if (strcmp(option, "--dry-run") == 0) {
            g_cfg.dry_run = true;
        } else if (strcmp(option, "--allow-unmounted-storage") == 0) {
            g_cfg.allow_unmounted_storage = true;
        } else if (strcmp(option, "--allow-missing-preview-listeners") == 0) {
            g_cfg.allow_missing_preview_listeners = true;
        } else if (strcmp(option, "--port") == 0 && value) {
            uint64_t parsed;
            if (!parse_u64(value, &parsed) || parsed == 0 || parsed > 65535) return false;
            g_cfg.port = (uint16_t)parsed;
            ++index;
        } else if (strcmp(option, "--min-free-bytes") == 0 && value) {
            if (!parse_u64(value, &g_cfg.minimum_free_bytes)) return false;
            ++index;
        } else if (strcmp(option, "--ring") == 0 && value) {
            if (!safe_copy(g_cfg.ring_dir, sizeof(g_cfg.ring_dir), value)) return false;
            ++index;
        } else if (strcmp(option, "--qgapp") == 0 && value) {
            if (!safe_copy(g_cfg.qgapp_path, sizeof(g_cfg.qgapp_path), value)) return false;
            ++index;
        } else if (strcmp(option, "--qgapp-pidfile") == 0 && value) {
            if (!safe_copy(g_cfg.qgapp_pidfile, sizeof(g_cfg.qgapp_pidfile), value)) return false;
            ++index;
        } else if (strcmp(option, "--qgapp-log") == 0 && value) {
            if (!safe_copy(g_cfg.qgapp_log, sizeof(g_cfg.qgapp_log), value)) return false;
            ++index;
        } else if (strcmp(option, "--wifi-command") == 0 && value) {
            if (!safe_copy(g_cfg.wifi_path, sizeof(g_cfg.wifi_path), value)) return false;
            ++index;
        } else if (strcmp(option, "--iw-command") == 0 && value) {
            if (!safe_copy(g_cfg.iw_path, sizeof(g_cfg.iw_path), value)) return false;
            ++index;
        } else if (strcmp(option, "--storage") == 0 && value) {
            if (g_cfg.storage_candidate_count == 2 &&
                strcmp(g_cfg.storage_candidates[0], "/rom/mnt/SDCARD") == 0) {
                g_cfg.storage_candidate_count = 0;
            }
            if (g_cfg.storage_candidate_count >= 4 ||
                !safe_copy(g_cfg.storage_candidates[g_cfg.storage_candidate_count],
                           sizeof(g_cfg.storage_candidates[0]), value)) return false;
            ++g_cfg.storage_candidate_count;
            ++index;
        } else {
            fprintf(stderr, "unknown or incomplete option: %s\n", option);
            return false;
        }
    }
    return g_cfg.storage_candidate_count > 0;
}

static bool set_cloexec(int fd) {
    int fd_flags;
    fd_flags = fcntl(fd, F_GETFD);
    return fd_flags >= 0 && fcntl(fd, F_SETFD, fd_flags | FD_CLOEXEC) == 0;
}

static int create_server(uint16_t port) {
    int fd = socket(AF_INET, SOCK_STREAM, 0);
    int yes = 1;
    struct sockaddr_in address;
    if (fd < 0) return -1;
    if (!set_cloexec(fd)) {
        int saved = errno;
        close(fd);
        errno = saved;
        return -1;
    }
    setsockopt(fd, SOL_SOCKET, SO_REUSEADDR, &yes, sizeof(yes));
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_ANY);
    address.sin_port = htons(port);
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0 || listen(fd, 8) != 0) {
        close(fd);
        return -1;
    }
    return fd;
}

int main(int argc, char **argv) {
    struct sigaction action;
    char recovered_session[96];
    if (!configure(argc, argv)) {
        usage(stderr, argv[0]);
        return 2;
    }
    if (!ensure_plain_directory(g_cfg.ring_dir, 0750, NULL)) {
        log_line("ERROR", "cannot create ring directory %s: %s", g_cfg.ring_dir, strerror(errno));
        return 1;
    }
    memset(&action, 0, sizeof(action));
    action.sa_handler = stop_service;
    sigemptyset(&action.sa_mask);
    sigaction(SIGINT, &action, NULL);
    sigaction(SIGTERM, &action, NULL);
    signal(SIGPIPE, SIG_IGN);

    /* Bind before touching capture state so a second service instance cannot
     * mark the first instance's live session as interrupted. */
    g_server_fd = create_server(g_cfg.port);
    if (g_server_fd < 0) {
        log_line("ERROR", "cannot listen on port %u: %s", g_cfg.port, strerror(errno));
        return 1;
    }
    if (!prepare_quarantine_reserve())
        log_line("WARN", "cannot reserve quarantine marker space: %s", strerror(errno));
    load_ring_quarantine();
    if (!g_ring_quarantined) recover_active_ring_marker();
    if (recover_interrupted_sessions(recovered_session, sizeof(recovered_session)) &&
        !g_ring_quarantined) {
        char reason[256];
        snprintf(reason, sizeof(reason),
                 "interrupted session %s has ring data retained for recovery",
                 recovered_session[0] ? recovered_session : "unknown");
        quarantine_ring(reason);
    }
    if (!g_ring_quarantined) {
        bool adopted_qgapp = ensure_qgapp(false);
        process_closed_segments();
        if (!g_ring_quarantined && !adopted_qgapp && !ensure_qgapp(true))
            log_line("WARN", "API will remain available but qgapp is unavailable");
    }
    log_line("INFO", "SynCap Tina service %s listening on 0.0.0.0:%u", SERVICE_VERSION, g_cfg.port);
    while (g_running) {
        fd_set read_set;
        struct timeval timeout = {0, 200000};
        unsigned long moved_before = g_capture.segments_moved;
        int ready;
        process_closed_segments();
        if (g_capture.active && g_capture.segments_moved != moved_before)
            timeout.tv_usec = 0;
        supervise_qgapp();
        FD_ZERO(&read_set);
        FD_SET(g_server_fd, &read_set);
        ready = select(g_server_fd + 1, &read_set, NULL, NULL, &timeout);
        if (ready < 0) {
            if (errno == EINTR) continue;
            log_line("ERROR", "select failed: %s", strerror(errno));
            break;
        }
        if (ready > 0 && FD_ISSET(g_server_fd, &read_set)) {
            int client = accept(g_server_fd, NULL, NULL);
            if (client >= 0) {
                struct timeval io_timeout = {10, 0};
                struct http_request request;
                int error_status = 400;
                if (!set_cloexec(client)) {
                    close(client);
                    continue;
                }
                setsockopt(client, SOL_SOCKET, SO_RCVTIMEO, &io_timeout, sizeof(io_timeout));
                setsockopt(client, SOL_SOCKET, SO_SNDTIMEO, &io_timeout, sizeof(io_timeout));
                if (parse_http_request(client, &request, &error_status)) handle_request(client, &request);
                else send_error_json(client, error_status, "invalid_request", "malformed HTTP request");
                close(client);
            }
        }
    }
    close(g_server_fd);
    g_server_fd = -1;
    log_line("INFO", "service stopped; persistent qgapp was left running");
    return 0;
}
