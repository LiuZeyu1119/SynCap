#define _POSIX_C_SOURCE 200809L

#include <arpa/inet.h>
#include <ctype.h>
#include <dbus/dbus.h>
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <netinet/in.h>
#include <poll.h>
#include <pthread.h>
#include <signal.h>
#include <stdbool.h>
#include <stdatomic.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <time.h>
#include <unistd.h>

#define BLUEZ_SERVICE "org.bluez"
#define ADAPTER_IFACE "org.bluez.Adapter1"
#define AGENT_MANAGER_IFACE "org.bluez.AgentManager1"
#define AGENT_IFACE "org.bluez.Agent1"
#define GATT_MANAGER_IFACE "org.bluez.GattManager1"
#define GATT_SERVICE_IFACE "org.bluez.GattService1"
#define GATT_CHARACTERISTIC_IFACE "org.bluez.GattCharacteristic1"
#define LE_ADVERTISING_MANAGER_IFACE "org.bluez.LEAdvertisingManager1"
#define LE_ADVERTISEMENT_IFACE "org.bluez.LEAdvertisement1"
#define DBUS_PROPERTIES_IFACE "org.freedesktop.DBus.Properties"
#define DBUS_OBJECT_MANAGER_IFACE "org.freedesktop.DBus.ObjectManager"

#define ROOT_PATH "/org/syncap/tina/ble"
#define SERVICE_PATH ROOT_PATH "/service0"
#define COMMAND_PATH SERVICE_PATH "/char0"
#define STATUS_PATH SERVICE_PATH "/char1"
#define ADVERTISEMENT_PATH ROOT_PATH "/advertisement0"
#define AGENT_PATH ROOT_PATH "/agent0"

#define SYNCAP_SERVICE_UUID "8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1"
#define SYNCAP_COMMAND_UUID "8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1"
#define SYNCAP_STATUS_UUID "8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1"
#define FIXED_CLAIM_CODE "123456"

#define BLE_HEADER_SIZE 16U
#define BLE_MAX_FRAGMENTS 128U
#define BLE_MAX_PAYLOAD 4096U
#define BLE_MAX_STATUS 480U
#define BLE_FRAGMENT_TTL_MS 15000ULL
#define HTTP_MAX_RESPONSE (64U * 1024U)
#define RFCOMM_CHANNEL 7U
#define RFCOMM_RESPONSE_WAIT_MS 95000ULL

#ifndef AF_BLUETOOTH
#define AF_BLUETOOTH 31
#endif
#ifndef BTPROTO_RFCOMM
#define BTPROTO_RFCOMM 3
#endif
#ifndef SOL_BLUETOOTH
#define SOL_BLUETOOTH 274
#endif
#ifndef BT_SECURITY
#define BT_SECURITY 4
#endif
#ifndef BT_SECURITY_MEDIUM
#define BT_SECURITY_MEDIUM 2
#endif
#ifndef SOL_RFCOMM
#define SOL_RFCOMM 18
#endif
#ifndef RFCOMM_LM
#define RFCOMM_LM 0x03
#endif
#ifndef RFCOMM_LM_AUTH
#define RFCOMM_LM_AUTH 0x0002
#endif
#ifndef RFCOMM_LM_ENCRYPT
#define RFCOMM_LM_ENCRYPT 0x0004
#endif

struct syncap_sockaddr_rc {
    sa_family_t family;
    unsigned char address[6];
    uint8_t channel;
};

struct syncap_bt_security {
    uint8_t level;
    uint8_t key_size;
};

enum object_kind {
    OBJECT_ROOT,
    OBJECT_SERVICE,
    OBJECT_COMMAND,
    OBJECT_STATUS,
    OBJECT_ADVERTISEMENT,
    OBJECT_AGENT,
};

enum command_kind {
    COMMAND_WIFI_SCAN,
    COMMAND_WIFI_CONFIGURE,
    COMMAND_DEVICE_STATUS,
};

enum command_result {
    COMMAND_ACCEPTED,
    COMMAND_WAITING,
    COMMAND_INVALID_LENGTH,
    COMMAND_INVALID_FORMAT,
    COMMAND_UNAUTHORIZED,
    COMMAND_UNSUPPORTED,
    COMMAND_BUSY,
    COMMAND_INTERNAL_ERROR,
};

struct object_context {
    enum object_kind kind;
};

struct fragment_part {
    unsigned char *data;
    size_t length;
};

struct reassembly_state {
    bool active;
    uint32_t message_id;
    uint16_t count;
    uint16_t received;
    size_t total_length;
    uint64_t started_ms;
    struct fragment_part parts[BLE_MAX_FRAGMENTS];
};

struct worker_request {
    enum command_kind kind;
    char *payload;
    size_t payload_length;
};

struct byte_buf {
    char *data;
    size_t length;
    size_t capacity;
};

struct fixed_buf {
    char data[BLE_MAX_STATUS + 1U];
    size_t length;
};

static DBusConnection *g_bus;
static volatile sig_atomic_t g_signal_stop;
static atomic_bool g_stop = ATOMIC_VAR_INIT(false);
static atomic_bool g_dispatch_stop = ATOMIC_VAR_INIT(false);
static atomic_bool g_bluez_lost = ATOMIC_VAR_INIT(false);
static atomic_bool g_rfcomm_stop = ATOMIC_VAR_INIT(false);
static int g_rfcomm_listen_fd = -1;
static char g_adapter_path[128] = "/org/bluez/hci0";
static char g_local_name[64] = "SynCap-Tina";
static unsigned g_http_port = 8080;
static pthread_mutex_t g_state_lock = PTHREAD_MUTEX_INITIALIZER;
static bool g_worker_active;
static char g_status[BLE_MAX_STATUS + 1U] = "{\"state\":\"ready\"}";
static size_t g_status_length = sizeof("{\"state\":\"ready\"}") - 1U;
static struct reassembly_state g_reassembly;

static struct object_context g_root_context = {OBJECT_ROOT};
static struct object_context g_service_context = {OBJECT_SERVICE};
static struct object_context g_command_context = {OBJECT_COMMAND};
static struct object_context g_status_context = {OBJECT_STATUS};
static struct object_context g_advertisement_context = {OBJECT_ADVERTISEMENT};
static struct object_context g_agent_context = {OBJECT_AGENT};

static void secure_zero(void *memory, size_t length) {
    volatile unsigned char *cursor = memory;
    while (length--) *cursor++ = 0;
}

static void log_line(const char *level, const char *message) {
    fprintf(stderr, "syncap-tina-ble[%s]: %s\n", level, message);
    fflush(stderr);
}

static uint64_t monotonic_ms(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value) != 0) return 0;
    return (uint64_t)value.tv_sec * 1000ULL + (uint64_t)value.tv_nsec / 1000000ULL;
}

static const char *ready_marker_path(void) {
    const char *path = getenv("SYNCAP_TINA_BLE_READY_FILE");
    return path && path[0] == '/' && strlen(path) < PATH_MAX - 32U ? path : NULL;
}

static void clear_ready_marker(void) {
    const char *path = ready_marker_path();
    if (path) (void)unlink(path);
}

static bool publish_ready_marker(void) {
    const char *path = ready_marker_path();
    char temporary[PATH_MAX];
    char contents[32];
    size_t remaining;
    const char *cursor;
    int fd;
    int count;
    if (!path) return getenv("SYNCAP_TINA_BLE_READY_FILE") == NULL;
    count = snprintf(temporary, sizeof(temporary), "%s.tmp.%ld", path, (long)getpid());
    if (count < 0 || (size_t)count >= sizeof(temporary)) return false;
    count = snprintf(contents, sizeof(contents), "%ld\n", (long)getpid());
    if (count < 0 || (size_t)count >= sizeof(contents)) return false;
    (void)unlink(temporary);
    fd = open(temporary, O_WRONLY | O_CREAT | O_EXCL, 0600);
    if (fd < 0) return false;
    (void)fcntl(fd, F_SETFD, FD_CLOEXEC);
    cursor = contents;
    remaining = (size_t)count;
    while (remaining) {
        ssize_t written = write(fd, cursor, remaining);
        if (written > 0) {
            cursor += written;
            remaining -= (size_t)written;
        } else if (written < 0 && errno == EINTR) {
            continue;
        } else {
            close(fd);
            unlink(temporary);
            return false;
        }
    }
    if (fsync(fd) != 0) {
        close(fd);
        unlink(temporary);
        return false;
    }
    if (close(fd) != 0 || rename(temporary, path) != 0) {
        unlink(temporary);
        return false;
    }
    return true;
}

static bool stop_requested(void) {
    return atomic_load_explicit(&g_stop, memory_order_relaxed);
}

static bool dispatch_stop_requested(void) {
    return atomic_load_explicit(&g_dispatch_stop, memory_order_relaxed);
}

static bool rfcomm_stop_requested(void) {
    return atomic_load_explicit(&g_rfcomm_stop, memory_order_relaxed);
}

static bool bluez_was_lost(void) {
    return atomic_load_explicit(&g_bluez_lost, memory_order_relaxed);
}

static long long realtime_ms(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_REALTIME, &value) != 0) return 0;
    return (long long)value.tv_sec * 1000LL + value.tv_nsec / 1000000L;
}

static bool byte_buf_reserve(struct byte_buf *buffer, size_t extra) {
    size_t needed;
    size_t next;
    char *grown;
    if (extra > HTTP_MAX_RESPONSE || buffer->length > HTTP_MAX_RESPONSE - extra) return false;
    needed = buffer->length + extra + 1U;
    if (needed <= buffer->capacity) return true;
    next = buffer->capacity ? buffer->capacity : 4096U;
    while (next < needed) {
        if (next > HTTP_MAX_RESPONSE / 2U) next = HTTP_MAX_RESPONSE + 1U;
        else next *= 2U;
        if (next > HTTP_MAX_RESPONSE + 1U) return false;
    }
    grown = realloc(buffer->data, next);
    if (!grown) return false;
    buffer->data = grown;
    buffer->capacity = next;
    return true;
}

static bool byte_buf_append(struct byte_buf *buffer, const void *data, size_t length) {
    if (!byte_buf_reserve(buffer, length)) return false;
    memcpy(buffer->data + buffer->length, data, length);
    buffer->length += length;
    buffer->data[buffer->length] = '\0';
    return true;
}

static void byte_buf_clear(struct byte_buf *buffer, bool sensitive) {
    if (buffer->data && sensitive) secure_zero(buffer->data, buffer->capacity);
    free(buffer->data);
    memset(buffer, 0, sizeof(*buffer));
}

static bool fixed_append_n(struct fixed_buf *buffer, const char *text, size_t length) {
    if (length > BLE_MAX_STATUS - buffer->length) return false;
    memcpy(buffer->data + buffer->length, text, length);
    buffer->length += length;
    buffer->data[buffer->length] = '\0';
    return true;
}

static bool fixed_append(struct fixed_buf *buffer, const char *text) {
    return fixed_append_n(buffer, text, strlen(text));
}

static bool fixed_append_integer(struct fixed_buf *buffer, long long value) {
    char text[32];
    int count = snprintf(text, sizeof(text), "%lld", value);
    return count >= 0 && (size_t)count < sizeof(text) && fixed_append_n(buffer, text, (size_t)count);
}

static bool fixed_append_json_string(struct fixed_buf *buffer, const char *text) {
    const unsigned char *cursor = (const unsigned char *)text;
    if (!fixed_append(buffer, "\"")) return false;
    while (*cursor) {
        char encoded[7];
        switch (*cursor) {
            case '"': if (!fixed_append(buffer, "\\\"")) return false; break;
            case '\\': if (!fixed_append(buffer, "\\\\")) return false; break;
            case '\b': if (!fixed_append(buffer, "\\b")) return false; break;
            case '\f': if (!fixed_append(buffer, "\\f")) return false; break;
            case '\n': if (!fixed_append(buffer, "\\n")) return false; break;
            case '\r': if (!fixed_append(buffer, "\\r")) return false; break;
            case '\t': if (!fixed_append(buffer, "\\t")) return false; break;
            default:
                if (*cursor < 0x20U) {
                    int count = snprintf(encoded, sizeof(encoded), "\\u%04x", *cursor);
                    if (count != 6 || !fixed_append_n(buffer, encoded, 6U)) return false;
                } else if (!fixed_append_n(buffer, (const char *)cursor, 1U)) {
                    return false;
                }
        }
        ++cursor;
    }
    return fixed_append(buffer, "\"");
}

static const char *skip_whitespace(const char *cursor) {
    while (*cursor && isspace((unsigned char)*cursor)) ++cursor;
    return cursor;
}

static const char *skip_json_string(const char *cursor) {
    if (*cursor != '"') return NULL;
    ++cursor;
    while (*cursor) {
        if (*cursor == '"') return cursor + 1;
        if (*cursor == '\\') {
            ++cursor;
            if (!*cursor) return NULL;
            if (*cursor == 'u') {
                unsigned i;
                for (i = 1; i <= 4; ++i) {
                    if (!isxdigit((unsigned char)cursor[i])) return NULL;
                }
                cursor += 4;
            }
        } else if ((unsigned char)*cursor < 0x20U) {
            return NULL;
        }
        ++cursor;
    }
    return NULL;
}

static const char *skip_json_value(const char *cursor) {
    char closers[64];
    size_t depth;
    cursor = skip_whitespace(cursor);
    if (*cursor == '"') return skip_json_string(cursor);
    if (*cursor == '{' || *cursor == '[') {
        closers[0] = *cursor == '{' ? '}' : ']';
        depth = 1U;
        ++cursor;
        while (*cursor && depth) {
            if (*cursor == '"') {
                cursor = skip_json_string(cursor);
                if (!cursor) return NULL;
                continue;
            }
            if (*cursor == '{' || *cursor == '[') {
                if (depth == sizeof(closers)) return NULL;
                closers[depth++] = *cursor == '{' ? '}' : ']';
            } else if (*cursor == '}' || *cursor == ']') {
                if (*cursor != closers[depth - 1U]) return NULL;
                --depth;
            }
            ++cursor;
        }
        return depth == 0U ? cursor : NULL;
    }
    while (*cursor && *cursor != ',' && *cursor != '}' && *cursor != ']') ++cursor;
    return cursor;
}

static bool json_is_complete_object(const char *json) {
    const char *cursor = skip_whitespace(json);
    const char *end;
    if (*cursor != '{') return false;
    end = skip_json_value(cursor);
    return end && *skip_whitespace(end) == '\0';
}

static bool append_utf8_codepoint(char *out, size_t out_size, size_t *used, unsigned codepoint) {
    unsigned char encoded[4];
    size_t count;
    if (codepoint == 0U || codepoint > 0x10ffffU ||
        (codepoint >= 0xd800U && codepoint <= 0xdfffU)) return false;
    if (codepoint <= 0x7fU) {
        encoded[0] = (unsigned char)codepoint;
        count = 1U;
    } else if (codepoint <= 0x7ffU) {
        encoded[0] = (unsigned char)(0xc0U | (codepoint >> 6));
        encoded[1] = (unsigned char)(0x80U | (codepoint & 0x3fU));
        count = 2U;
    } else if (codepoint <= 0xffffU) {
        encoded[0] = (unsigned char)(0xe0U | (codepoint >> 12));
        encoded[1] = (unsigned char)(0x80U | ((codepoint >> 6) & 0x3fU));
        encoded[2] = (unsigned char)(0x80U | (codepoint & 0x3fU));
        count = 3U;
    } else {
        encoded[0] = (unsigned char)(0xf0U | (codepoint >> 18));
        encoded[1] = (unsigned char)(0x80U | ((codepoint >> 12) & 0x3fU));
        encoded[2] = (unsigned char)(0x80U | ((codepoint >> 6) & 0x3fU));
        encoded[3] = (unsigned char)(0x80U | (codepoint & 0x3fU));
        count = 4U;
    }
    if (count >= out_size - *used) return false;
    memcpy(out + *used, encoded, count);
    *used += count;
    return true;
}

static bool parse_hex_quad(const char **cursor, unsigned *codepoint) {
    unsigned value = 0;
    unsigned i;
    for (i = 0; i < 4U; ++i) {
        unsigned char digit = (unsigned char)*(*cursor)++;
        if (!isxdigit(digit)) return false;
        value = value * 16U + (unsigned)(isdigit(digit) ? digit - '0' :
            10 + tolower(digit) - 'a');
    }
    *codepoint = value;
    return true;
}

static bool decode_json_string(const char *cursor, char *out, size_t out_size, const char **after) {
    size_t used = 0;
    if (*cursor++ != '"' || out_size == 0) return false;
    while (*cursor && *cursor != '"') {
        unsigned char value = (unsigned char)*cursor++;
        if (value < 0x20U) return false;
        if (value == '\\') {
            unsigned codepoint;
            value = (unsigned char)*cursor++;
            switch (value) {
                case '"': case '\\': case '/': break;
                case 'b': value = '\b'; break;
                case 'f': value = '\f'; break;
                case 'n': value = '\n'; break;
                case 'r': value = '\r'; break;
                case 't': value = '\t'; break;
                case 'u':
                    if (!parse_hex_quad(&cursor, &codepoint)) return false;
                    if (codepoint >= 0xd800U && codepoint <= 0xdbffU) {
                        unsigned low;
                        if (cursor[0] != '\\' || cursor[1] != 'u') return false;
                        cursor += 2;
                        if (!parse_hex_quad(&cursor, &low) || low < 0xdc00U || low > 0xdfffU)
                            return false;
                        codepoint = 0x10000U + ((codepoint - 0xd800U) << 10) + (low - 0xdc00U);
                    }
                    if (!append_utf8_codepoint(out, out_size, &used, codepoint)) return false;
                    continue;
                default: return false;
            }
        }
        if (used + 1U >= out_size || value == 0) return false;
        out[used++] = (char)value;
    }
    if (*cursor != '"') return false;
    out[used] = '\0';
    if (after) *after = cursor + 1;
    return true;
}

static const char *json_find_top_value(const char *json, const char *key) {
    const char *cursor = skip_whitespace(json);
    char decoded_key[96];
    if (*cursor++ != '{') return NULL;
    for (;;) {
        const char *value;
        cursor = skip_whitespace(cursor);
        if (*cursor == '}') return NULL;
        if (!decode_json_string(cursor, decoded_key, sizeof(decoded_key), &cursor)) return NULL;
        cursor = skip_whitespace(cursor);
        if (*cursor++ != ':') return NULL;
        value = skip_whitespace(cursor);
        if (strcmp(decoded_key, key) == 0) return value;
        cursor = skip_json_value(value);
        if (!cursor) return NULL;
        cursor = skip_whitespace(cursor);
        if (*cursor == ',') {
            ++cursor;
            continue;
        }
        if (*cursor == '}') return NULL;
        return NULL;
    }
}

static bool json_get_string(const char *json, const char *key, char *out, size_t out_size) {
    const char *value = json_find_top_value(json, key);
    return value && decode_json_string(value, out, out_size, NULL);
}

static long long json_get_integer(const char *json, const char *key, long long fallback) {
    const char *value = json_find_top_value(json, key);
    char *end;
    long long parsed;
    if (!value) return fallback;
    errno = 0;
    parsed = strtoll(value, &end, 10);
    return errno == 0 && end != value ? parsed : fallback;
}

static bool json_get_boolean(const char *json, const char *key, bool fallback) {
    const char *value = json_find_top_value(json, key);
    if (!value) return fallback;
    if (strncmp(value, "true", 4) == 0) return true;
    if (strncmp(value, "false", 5) == 0) return false;
    return fallback;
}

static void set_status_text(const char *json) {
    size_t length = strlen(json);
    if (length > BLE_MAX_STATUS) {
        json = "{\"state\":\"failed\",\"error\":\"status_too_large\"}";
        length = strlen(json);
    }
    pthread_mutex_lock(&g_state_lock);
    memcpy(g_status, json, length + 1U);
    g_status_length = length;
    pthread_mutex_unlock(&g_state_lock);
}

static void set_failed_status(const char *operation, const char *error) {
    struct fixed_buf output = {{0}, 0};
    if (!fixed_append(&output, "{\"state\":\"failed\",\"op\":" ) ||
        !fixed_append_json_string(&output, operation) ||
        !fixed_append(&output, ",\"error\":" ) ||
        !fixed_append_json_string(&output, error) || !fixed_append(&output, "}")) {
        set_status_text("{\"state\":\"failed\",\"error\":\"internal_error\"}");
        return;
    }
    set_status_text(output.data);
}

static bool safe_error_code(const char *value) {
    const unsigned char *cursor = (const unsigned char *)value;
    if (!*cursor || strlen(value) > 64U) return false;
    while (*cursor) {
        if (!(isalnum(*cursor) || *cursor == '.' || *cursor == '_' || *cursor == '-')) return false;
        ++cursor;
    }
    return true;
}

static bool compact_wifi_status(const char *response, const char *request, struct fixed_buf *output) {
    char state[32] = "failed";
    char ssid[129] = "";
    char ip_address[64] = "";
    char error[65] = "";
    json_get_string(response, "state", state, sizeof(state));
    if (!json_get_string(response, "ssid", ssid, sizeof(ssid)))
        json_get_string(request, "ssid", ssid, sizeof(ssid));
    json_get_string(response, "ipAddress", ip_address, sizeof(ip_address));
    json_get_string(response, "error", error, sizeof(error));
    if (!fixed_append(output, "{\"state\":" ) || !fixed_append_json_string(output, state) ||
        !fixed_append(output, ",\"op\":\"wifi.configure\"")) return false;
    if (ssid[0] && (!fixed_append(output, ",\"ssid\":" ) || !fixed_append_json_string(output, ssid)))
        return false;
    if (ip_address[0] && (!fixed_append(output, ",\"ipAddress\":" ) ||
                          !fixed_append_json_string(output, ip_address))) return false;
    if (error[0] && safe_error_code(error) &&
        (!fixed_append(output, ",\"error\":" ) || !fixed_append_json_string(output, error))) return false;
    return fixed_append(output, "}");
}

static bool compact_scan_response(const char *response, struct fixed_buf *output) {
    const char *array = json_find_top_value(response, "networks");
    long long scanned_at = json_get_integer(response, "scannedAt", realtime_ms());
    bool truncated = json_get_boolean(response, "truncated", false);
    bool first = true;
    if (!array || *array++ != '[' || !fixed_append(output,
            "{\"state\":\"completed\",\"op\":\"wifi.scan\",\"networks\":[")) return false;
    for (;;) {
        const char *end;
        size_t object_length;
        char object[1024];
        char ssid[129];
        char security[32] = "open";
        long long rssi;
        bool secure;
        struct fixed_buf network = {{0}, 0};
        array = skip_whitespace(array);
        if (*array == ']') break;
        if (*array != '{') return false;
        end = skip_json_value(array);
        if (!end || end <= array) return false;
        object_length = (size_t)(end - array);
        if (object_length >= sizeof(object)) return false;
        memcpy(object, array, object_length);
        object[object_length] = '\0';
        if (!json_get_string(object, "ssid", ssid, sizeof(ssid)) || !ssid[0]) return false;
        json_get_string(object, "security", security, sizeof(security));
        rssi = json_get_integer(object, "rssi", -127);
        if (rssi < -127) rssi = -127;
        if (rssi > 0) rssi = 0;
        secure = json_get_boolean(object, "secure", strcasecmp(security, "open") != 0);
        if (!fixed_append(&network, "{\"ssid\":" ) || !fixed_append_json_string(&network, ssid) ||
            !fixed_append(&network, ",\"rssi\":" ) || !fixed_append_integer(&network, rssi) ||
            !fixed_append(&network, ",\"security\":" ) ||
            !fixed_append_json_string(&network, security) ||
            !fixed_append(&network, secure ? ",\"secure\":true}" : ",\"secure\":false}")) return false;

        /* Reserve enough room for the timestamp, truncation marker and object closure. */
        if (output->length + (first ? 0U : 1U) + network.length + 64U > BLE_MAX_STATUS) {
            truncated = true;
            break;
        }
        if (!first && !fixed_append(output, ",")) return false;
        if (!fixed_append_n(output, network.data, network.length)) return false;
        first = false;
        array = skip_whitespace(end);
        if (*array == ',') {
            ++array;
            continue;
        }
        if (*array == ']') break;
        return false;
    }
    if (!fixed_append(output, "],\"scannedAt\":" ) || !fixed_append_integer(output, scanned_at)) return false;
    if (truncated && !fixed_append(output, ",\"truncated\":true")) return false;
    return fixed_append(output, "}");
}

static bool compact_device_status(const char *response, struct fixed_buf *output) {
    const char *wifi = json_find_top_value(response, "wifi");
    char state[32] = "unknown";
    char ssid[129] = "";
    char ip_address[64] = "";
    if (!json_is_complete_object(response) || !wifi || *wifi != '{') return false;
    json_get_string(wifi, "state", state, sizeof(state));
    json_get_string(wifi, "ssid", ssid, sizeof(ssid));
    json_get_string(wifi, "ipAddress", ip_address, sizeof(ip_address));
    if (!fixed_append(output, "{\"state\":\"ready\",\"op\":\"device.status\",\"wifi\":{\"state\":" ) ||
        !fixed_append_json_string(output, state)) return false;
    if (ssid[0] && (!fixed_append(output, ",\"ssid\":" ) ||
                    !fixed_append_json_string(output, ssid))) return false;
    if (ip_address[0] && (!fixed_append(output, ",\"ipAddress\":" ) ||
                          !fixed_append_json_string(output, ip_address))) return false;
    return fixed_append(output, "}}");
}

static bool send_all(int fd, const void *data, size_t length) {
    const unsigned char *cursor = data;
    while (length) {
        ssize_t written = send(fd, cursor, length, 0);
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

static bool wait_socket_ready(int fd, short events, uint64_t deadline) {
    struct pollfd descriptor;
    for (;;) {
        uint64_t now = monotonic_ms();
        uint64_t remaining;
        if (now >= deadline) break;
        remaining = deadline - now;
        int timeout_ms = remaining > 1000ULL ? 1000 : (int)remaining;
        int result;
        descriptor.fd = fd;
        descriptor.events = events;
        descriptor.revents = 0;
        result = poll(&descriptor, 1, timeout_ms > 0 ? timeout_ms : 1);
        if (result > 0) {
            if (descriptor.revents & (POLLERR | POLLNVAL)) return false;
            if (descriptor.revents & events) return true;
            if ((events & POLLIN) && (descriptor.revents & POLLHUP)) return true;
            continue;
        }
        if (result == 0) continue;
        if (errno != EINTR) return false;
    }
    errno = ETIMEDOUT;
    return false;
}

static bool send_all_until(int fd, const void *data, size_t length, uint64_t deadline) {
    const unsigned char *cursor = data;
    while (length && monotonic_ms() < deadline) {
        ssize_t written;
        if (!wait_socket_ready(fd, POLLOUT, deadline)) return false;
        written = send(fd, cursor, length, 0);
        if (written > 0) {
            cursor += written;
            length -= (size_t)written;
            continue;
        }
        if (written < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
        return false;
    }
    if (length) errno = ETIMEDOUT;
    return length == 0;
}

/* Returns 1 when Content-Length is valid, 0 when absent, and -1 when malformed. */
static int http_content_length(const char *response, const char *separator, size_t *length) {
    const char *cursor = strstr(response, "\r\n");
    bool found = false;
    size_t parsed_length = 0;
    if (!cursor || cursor >= separator) return -1;
    cursor += 2;
    while (cursor < separator) {
        const char *line_end = strstr(cursor, "\r\n");
        const char *value;
        char *end;
        unsigned long parsed;
        if (!line_end || line_end > separator) return -1;
        if ((size_t)(line_end - cursor) < sizeof("Content-Length:") - 1U ||
            strncasecmp(cursor, "Content-Length:", sizeof("Content-Length:") - 1U) != 0) {
            cursor = line_end + 2;
            continue;
        }
        value = cursor + sizeof("Content-Length:") - 1U;
        while (value < line_end && (*value == ' ' || *value == '\t')) ++value;
        if (value == line_end || *value == '-') return -1;
        errno = 0;
        parsed = strtoul(value, &end, 10);
        while (end < line_end && (*end == ' ' || *end == '\t')) ++end;
        if (errno || end != line_end || parsed > HTTP_MAX_RESPONSE) return -1;
        if (found && parsed_length != (size_t)parsed) return -1;
        found = true;
        parsed_length = (size_t)parsed;
        cursor = line_end + 2;
    }
    if (found) *length = parsed_length;
    return found ? 1 : 0;
}

static bool http_request(const char *method, const char *path, const char *body,
                         size_t body_length, unsigned timeout_seconds,
                         int *status_code, char **response_body) {
    int fd = -1;
    struct sockaddr_in address;
    struct timeval timeout;
    char header[768];
    int header_length;
    struct byte_buf response = {0};
    char block[4096];
    ssize_t count;
    char *separator;
    char *line_end;
    int parsed_status = 0;
    int flags;
    uint64_t deadline = monotonic_ms() + (uint64_t)timeout_seconds * 1000ULL;
    bool response_closed = false;
    bool content_length_known = false;
    size_t expected_body_length = 0;
    size_t response_body_offset = 0;

    *status_code = 0;
    *response_body = NULL;
    timeout.tv_sec = (time_t)timeout_seconds;
    timeout.tv_usec = 0;
    fd = socket(AF_INET, SOCK_STREAM, 0);
    if (fd < 0) return false;
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0) {
        log_line("ERROR", "HTTP socket timeout setup failed");
        goto fail;
    }
    flags = fcntl(fd, F_GETFL, 0);
    if (flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) != 0) {
        log_line("ERROR", "HTTP nonblocking setup failed");
        goto fail;
    }
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_port = htons((uint16_t)g_http_port);
    address.sin_addr.s_addr = htonl(UINT32_C(0x7f000001));
    if (connect(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        int socket_error = 0;
        socklen_t socket_error_length = sizeof(socket_error);
        if (errno != EINPROGRESS || !wait_socket_ready(fd, POLLOUT, deadline) ||
            getsockopt(fd, SOL_SOCKET, SO_ERROR, &socket_error, &socket_error_length) != 0 ||
            socket_error != 0) goto fail;
    }
    header_length = snprintf(header, sizeof(header),
        "%s %s HTTP/1.0\r\nHost: 127.0.0.1\r\nAccept: application/json\r\n"
        "Content-Type: application/json\r\nX-SynCap-Claim: %s\r\n"
        "Content-Length: %zu\r\nConnection: close\r\n\r\n",
        method, path, FIXED_CLAIM_CODE, body_length);
    if (header_length < 0 || (size_t)header_length >= sizeof(header) ||
        !send_all_until(fd, header, (size_t)header_length, deadline) ||
        (body_length && !send_all_until(fd, body, body_length, deadline))) goto fail;
    while (monotonic_ms() < deadline) {
        int length_state;
        if (!wait_socket_ready(fd, POLLIN, deadline)) goto fail;
        count = recv(fd, block, sizeof(block), 0);
        if (count > 0) {
            if (!byte_buf_append(&response, block, (size_t)count)) goto fail;
        } else if (count == 0) {
            response_closed = true;
            break;
        } else if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) {
            continue;
        } else {
            goto fail;
        }
        separator = response.data ? strstr(response.data, "\r\n\r\n") : NULL;
        if (!separator) continue;
        response_body_offset = (size_t)(separator + 4 - response.data);
        length_state = http_content_length(response.data, separator, &expected_body_length);
        if (length_state < 0) goto fail;
        content_length_known = length_state > 0;
        if (content_length_known && response.length >= response_body_offset + expected_body_length)
            break;
    }
    close(fd);
    fd = -1;
    if (!response.data) goto fail;
    line_end = strstr(response.data, "\r\n");
    separator = strstr(response.data, "\r\n\r\n");
    if (!line_end || !separator || sscanf(response.data, "HTTP/%*u.%*u %d", &parsed_status) != 1)
        goto fail;
    response_body_offset = (size_t)(separator + 4 - response.data);
    if (!content_length_known) {
        int length_state = http_content_length(response.data, separator, &expected_body_length);
        if (length_state < 0) goto fail;
        content_length_known = length_state > 0;
    }
    if (content_length_known) {
        if (response.length < response_body_offset + expected_body_length) goto fail;
    } else {
        if (!response_closed) goto fail;
        expected_body_length = response.length - response_body_offset;
    }
    *response_body = malloc(expected_body_length + 1U);
    if (!*response_body) goto fail;
    memcpy(*response_body, response.data + response_body_offset, expected_body_length);
    (*response_body)[expected_body_length] = '\0';
    *status_code = parsed_status;
    byte_buf_clear(&response, true);
    return true;

fail:
    if (fd >= 0) close(fd);
    byte_buf_clear(&response, true);
    return false;
}

static void *worker_main(void *opaque) {
    struct worker_request *request = opaque;
    const char *operation;
    const char *method;
    const char *path;
    const char *body;
    size_t body_length;
    unsigned timeout_seconds;
    char *response = NULL;
    int http_status = 0;
    struct fixed_buf compact = {{0}, 0};
    bool valid;

    switch (request->kind) {
        case COMMAND_WIFI_SCAN:
            operation = "wifi.scan";
            method = "GET";
            path = "/v1/wifi/scan";
            body = NULL;
            body_length = 0;
            timeout_seconds = 25U;
            break;
        case COMMAND_WIFI_CONFIGURE:
            operation = "wifi.configure";
            method = "POST";
            path = "/v1/wifi/configure";
            body = request->payload;
            body_length = request->payload_length;
            timeout_seconds = 90U;
            break;
        case COMMAND_DEVICE_STATUS:
            operation = "device.status";
            method = "GET";
            path = "/v1/status";
            body = NULL;
            body_length = 0;
            timeout_seconds = 10U;
            break;
        default:
            set_failed_status("unknown", "internal_error");
            goto finished;
    }

    if (!http_request(method, path, body, body_length, timeout_seconds,
                      &http_status, &response)) {
        set_failed_status(operation, "service_unreachable");
    } else if (http_status < 200 || http_status >= 300) {
        char error[65] = "request_failed";
        if (!json_get_string(response, "error", error, sizeof(error)) || !safe_error_code(error))
            snprintf(error, sizeof(error), "http_%d", http_status);
        set_failed_status(operation, error);
    } else {
        if (request->kind == COMMAND_WIFI_SCAN)
            valid = compact_scan_response(response, &compact);
        else if (request->kind == COMMAND_WIFI_CONFIGURE)
            valid = compact_wifi_status(response, request->payload, &compact);
        else
            valid = compact_device_status(response, &compact);
        if (valid) set_status_text(compact.data);
        else set_failed_status(operation, "invalid_response");
    }
finished:
    if (response) {
        secure_zero(response, strlen(response));
        free(response);
    }
    secure_zero(request->payload, request->payload_length);
    free(request->payload);
    free(request);
    pthread_mutex_lock(&g_state_lock);
    g_worker_active = false;
    pthread_mutex_unlock(&g_state_lock);
    return NULL;
}

static enum command_result dispatch_command(const unsigned char *payload, size_t length) {
    char operation[32];
    char claim_code[16];
    char ssid[129];
    struct worker_request *request;
    pthread_t worker;
    enum command_kind kind;

    if (length == 0 || length > BLE_MAX_PAYLOAD || memchr(payload, '\0', length))
        return COMMAND_INVALID_FORMAT;
    request = calloc(1, sizeof(*request));
    if (!request) return COMMAND_INTERNAL_ERROR;
    request->payload = malloc(length + 1U);
    if (!request->payload) {
        free(request);
        return COMMAND_INTERNAL_ERROR;
    }
    memcpy(request->payload, payload, length);
    request->payload[length] = '\0';
    request->payload_length = length;
    if (!json_is_complete_object(request->payload)) {
        secure_zero(request->payload, length);
        free(request->payload);
        free(request);
        return COMMAND_INVALID_FORMAT;
    }
    if (!json_get_string(request->payload, "op", operation, sizeof(operation))) {
        if (json_get_string(request->payload, "ssid", ssid, sizeof(ssid)) && ssid[0])
            strcpy(operation, "wifi.configure");
        else {
            secure_zero(request->payload, length);
            free(request->payload);
            free(request);
            return COMMAND_UNSUPPORTED;
        }
    }
    if (!json_get_string(request->payload, "claimCode", claim_code, sizeof(claim_code)) ||
        strcmp(claim_code, FIXED_CLAIM_CODE) != 0) {
        secure_zero(request->payload, length);
        free(request->payload);
        free(request);
        return COMMAND_UNAUTHORIZED;
    }
    if (strcmp(operation, "wifi.scan") == 0) {
        kind = COMMAND_WIFI_SCAN;
    } else if (strcmp(operation, "wifi.configure") == 0) {
        if (!json_get_string(request->payload, "ssid", ssid, sizeof(ssid)) ||
            strlen(ssid) == 0 || strlen(ssid) > 32U) {
            secure_zero(request->payload, length);
            free(request->payload);
            free(request);
            return COMMAND_INVALID_FORMAT;
        }
        kind = COMMAND_WIFI_CONFIGURE;
    } else if (strcmp(operation, "device.status") == 0) {
        kind = COMMAND_DEVICE_STATUS;
    } else {
        secure_zero(request->payload, length);
        free(request->payload);
        free(request);
        return COMMAND_UNSUPPORTED;
    }
    request->kind = kind;
    pthread_mutex_lock(&g_state_lock);
    if (g_worker_active) {
        pthread_mutex_unlock(&g_state_lock);
        secure_zero(request->payload, length);
        free(request->payload);
        free(request);
        return COMMAND_BUSY;
    }
    g_worker_active = true;
    snprintf(g_status, sizeof(g_status), "{\"state\":\"working\",\"op\":\"%s\"}", operation);
    g_status_length = strlen(g_status);
    pthread_mutex_unlock(&g_state_lock);
    if (pthread_create(&worker, NULL, worker_main, request) != 0) {
        pthread_mutex_lock(&g_state_lock);
        g_worker_active = false;
        pthread_mutex_unlock(&g_state_lock);
        secure_zero(request->payload, length);
        free(request->payload);
        free(request);
        set_failed_status(operation, "worker_start_failed");
        return COMMAND_INTERNAL_ERROR;
    }
    pthread_detach(worker);
    return COMMAND_ACCEPTED;
}

static void clear_reassembly(void) {
    unsigned i;
    for (i = 0; i < BLE_MAX_FRAGMENTS; ++i) {
        if (g_reassembly.parts[i].data) {
            secure_zero(g_reassembly.parts[i].data, g_reassembly.parts[i].length);
            free(g_reassembly.parts[i].data);
        }
    }
    memset(&g_reassembly, 0, sizeof(g_reassembly));
}

static uint16_t read_be16(const unsigned char *data) {
    return (uint16_t)(((uint16_t)data[0] << 8) | data[1]);
}

static uint32_t read_be32(const unsigned char *data) {
    return ((uint32_t)data[0] << 24) | ((uint32_t)data[1] << 16) |
           ((uint32_t)data[2] << 8) | data[3];
}

static void write_be16(unsigned char *data, uint16_t value) {
    data[0] = (unsigned char)(value >> 8);
    data[1] = (unsigned char)value;
}

static void write_be32(unsigned char *data, uint32_t value) {
    data[0] = (unsigned char)(value >> 24);
    data[1] = (unsigned char)(value >> 16);
    data[2] = (unsigned char)(value >> 8);
    data[3] = (unsigned char)value;
}

static size_t encode_test_fragment(unsigned char *output, size_t capacity, uint32_t message_id,
                                   uint16_t index, uint16_t count,
                                   const unsigned char *payload, size_t payload_length) {
    if (payload_length > UINT16_MAX || capacity < BLE_HEADER_SIZE + payload_length) return 0;
    output[0] = 'S';
    output[1] = 'C';
    output[2] = 1;
    output[3] = 1;
    write_be32(output + 4U, message_id);
    write_be16(output + 8U, index);
    write_be16(output + 10U, count);
    write_be16(output + 12U, (uint16_t)payload_length);
    write_be16(output + 14U, 0);
    memcpy(output + BLE_HEADER_SIZE, payload, payload_length);
    return BLE_HEADER_SIZE + payload_length;
}

static enum command_result accept_command_value(const unsigned char *value, size_t length) {
    uint32_t message_id;
    uint16_t index;
    uint16_t count;
    uint16_t payload_length;
    uint16_t reserved;
    uint64_t now;
    unsigned char *copy;
    size_t total = 0;
    unsigned i;
    enum command_result result;

    if (length == 0 || length > 512U) return COMMAND_INVALID_LENGTH;
    if (length < 2U || value[0] != 'S' || value[1] != 'C') return dispatch_command(value, length);
    if (length < BLE_HEADER_SIZE || value[2] != 1U || (value[3] & 1U) == 0U)
        return COMMAND_INVALID_FORMAT;
    message_id = read_be32(value + 4U);
    index = read_be16(value + 8U);
    count = read_be16(value + 10U);
    payload_length = read_be16(value + 12U);
    reserved = read_be16(value + 14U);
    if (count == 0 || count > BLE_MAX_FRAGMENTS || index >= count || reserved != 0 ||
        payload_length != length - BLE_HEADER_SIZE) return COMMAND_INVALID_FORMAT;
    now = monotonic_ms();
    if (g_reassembly.active && now - g_reassembly.started_ms > BLE_FRAGMENT_TTL_MS)
        clear_reassembly();
    if (!g_reassembly.active || g_reassembly.message_id != message_id) {
        if (g_reassembly.active && index != 0) return COMMAND_BUSY;
        clear_reassembly();
        g_reassembly.active = true;
        g_reassembly.message_id = message_id;
        g_reassembly.count = count;
        g_reassembly.started_ms = now;
    }
    if (g_reassembly.count != count) {
        clear_reassembly();
        return COMMAND_INVALID_FORMAT;
    }
    if (g_reassembly.parts[index].data) {
        if (g_reassembly.parts[index].length == payload_length &&
            memcmp(g_reassembly.parts[index].data, value + BLE_HEADER_SIZE, payload_length) == 0)
            return COMMAND_WAITING;
        clear_reassembly();
        return COMMAND_INVALID_FORMAT;
    }
    if (payload_length > BLE_MAX_PAYLOAD - g_reassembly.total_length) {
        clear_reassembly();
        return COMMAND_INVALID_LENGTH;
    }
    copy = malloc(payload_length ? payload_length : 1U);
    if (!copy) return COMMAND_INTERNAL_ERROR;
    if (payload_length) memcpy(copy, value + BLE_HEADER_SIZE, payload_length);
    g_reassembly.parts[index].data = copy;
    g_reassembly.parts[index].length = payload_length;
    g_reassembly.total_length += payload_length;
    ++g_reassembly.received;
    if (g_reassembly.received != count) return COMMAND_WAITING;
    for (i = 0; i < count; ++i) {
        if (!g_reassembly.parts[i].data || total > BLE_MAX_PAYLOAD - g_reassembly.parts[i].length) {
            clear_reassembly();
            return COMMAND_INVALID_LENGTH;
        }
        total += g_reassembly.parts[i].length;
    }
    copy = malloc(total ? total : 1U);
    if (!copy) {
        clear_reassembly();
        return COMMAND_INTERNAL_ERROR;
    }
    total = 0;
    for (i = 0; i < count; ++i) {
        memcpy(copy + total, g_reassembly.parts[i].data, g_reassembly.parts[i].length);
        total += g_reassembly.parts[i].length;
    }
    clear_reassembly();
    result = dispatch_command(copy, total);
    secure_zero(copy, total);
    free(copy);
    return result;
}

static bool rfcomm_receive_exact(int fd, void *data, size_t length, uint64_t deadline) {
    unsigned char *cursor = data;
    while (length && !stop_requested() && !rfcomm_stop_requested() &&
           monotonic_ms() < deadline) {
        ssize_t received = recv(fd, cursor, length, 0);
        if (received > 0) {
            cursor += received;
            length -= (size_t)received;
            continue;
        }
        if (received == 0) return false;
        if (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK) continue;
        return false;
    }
    return length == 0;
}

static bool rfcomm_send_json(int fd, const char *json) {
    unsigned char prefix[4];
    size_t length = strlen(json);
    if (length == 0 || length > BLE_MAX_PAYLOAD) return false;
    write_be32(prefix, (uint32_t)length);
    return send_all(fd, prefix, sizeof(prefix)) && send_all(fd, json, length);
}

static const char *rfcomm_command_error(enum command_result result) {
    switch (result) {
        case COMMAND_INVALID_LENGTH: return "invalid_length";
        case COMMAND_INVALID_FORMAT: return "invalid_format";
        case COMMAND_UNAUTHORIZED: return "unauthorized";
        case COMMAND_UNSUPPORTED: return "unsupported";
        case COMMAND_BUSY: return "busy";
        default: return "internal_error";
    }
}

static void copy_status(char *output, size_t output_size) {
    size_t length;
    pthread_mutex_lock(&g_state_lock);
    length = g_status_length < output_size - 1U ? g_status_length : output_size - 1U;
    memcpy(output, g_status, length);
    output[length] = '\0';
    pthread_mutex_unlock(&g_state_lock);
}

static void rfcomm_serve_client(int fd) {
    unsigned char prefix[4];
    struct timeval timeout = {1, 0};
    uint64_t receive_deadline = monotonic_ms() + 10000ULL;
    uint32_t length;
    unsigned char *payload;
    enum command_result result;
    char response[BLE_MAX_STATUS + 1U];
    if (setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout)) != 0 ||
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &timeout, sizeof(timeout)) != 0) {
        log_line("ERROR", "RFCOMM client timeout setup failed");
        return;
    }
    if (!rfcomm_receive_exact(fd, prefix, sizeof(prefix), receive_deadline)) return;
    length = read_be32(prefix);
    if (length == 0 || length > BLE_MAX_PAYLOAD) {
        (void)rfcomm_send_json(fd,
            "{\"state\":\"failed\",\"error\":\"invalid_length\"}");
        return;
    }
    payload = malloc((size_t)length + 1U);
    if (!payload) {
        (void)rfcomm_send_json(fd,
            "{\"state\":\"failed\",\"error\":\"internal_error\"}");
        return;
    }
    if (!rfcomm_receive_exact(fd, payload, length, receive_deadline)) {
        secure_zero(payload, (size_t)length + 1U);
        free(payload);
        return;
    }
    payload[length] = '\0';
    result = dispatch_command(payload, length);
    secure_zero(payload, (size_t)length + 1U);
    free(payload);
    if (result == COMMAND_ACCEPTED) {
        uint64_t deadline = monotonic_ms() + RFCOMM_RESPONSE_WAIT_MS;
        bool active;
        do {
            struct timespec delay = {0, 100000000L};
            pthread_mutex_lock(&g_state_lock);
            active = g_worker_active;
            pthread_mutex_unlock(&g_state_lock);
            if (!active) break;
            nanosleep(&delay, NULL);
        } while (!stop_requested() && !rfcomm_stop_requested() &&
                 monotonic_ms() < deadline);
        pthread_mutex_lock(&g_state_lock);
        active = g_worker_active;
        pthread_mutex_unlock(&g_state_lock);
        if (active)
            snprintf(response, sizeof(response),
                     "{\"state\":\"failed\",\"error\":\"service_timeout\"}");
        else
            copy_status(response, sizeof(response));
    } else {
        snprintf(response, sizeof(response),
                 "{\"state\":\"failed\",\"error\":\"%s\"}",
                 rfcomm_command_error(result));
    }
    (void)rfcomm_send_json(fd, response);
}

static bool set_rfcomm_security(int fd) {
    struct syncap_bt_security security = {BT_SECURITY_MEDIUM, 0};
    int legacy = RFCOMM_LM_AUTH | RFCOMM_LM_ENCRYPT;
    if (setsockopt(fd, SOL_BLUETOOTH, BT_SECURITY, &security, sizeof(security)) == 0)
        return true;
    if (errno != ENOPROTOOPT) return false;
    return setsockopt(fd, SOL_RFCOMM, RFCOMM_LM, &legacy, sizeof(legacy)) == 0;
}

static void *rfcomm_server_main(void *unused) {
    int listen_fd = g_rfcomm_listen_fd;
    (void)unused;
    while (!stop_requested() && !rfcomm_stop_requested()) {
        int client = accept(listen_fd, NULL, NULL);
        if (client < 0) {
            if (errno == EINTR) continue;
            if (!stop_requested() && !rfcomm_stop_requested())
                log_line("ERROR", "RFCOMM accept failed");
            break;
        }
        (void)fcntl(client, F_SETFD, FD_CLOEXEC);
        if (!set_rfcomm_security(client)) {
            log_line("ERROR", "RFCOMM client security setup failed");
            close(client);
            continue;
        }
        rfcomm_serve_client(client);
        shutdown(client, SHUT_RDWR);
        close(client);
    }
    return NULL;
}

static bool start_rfcomm_server(pthread_t *thread) {
    struct syncap_sockaddr_rc address;
    int fd = socket(AF_BLUETOOTH, SOCK_STREAM, BTPROTO_RFCOMM);
    if (fd < 0) {
        log_line("ERROR", "RFCOMM socket unavailable");
        return false;
    }
    (void)fcntl(fd, F_SETFD, FD_CLOEXEC);
    memset(&address, 0, sizeof(address));
    address.family = AF_BLUETOOTH;
    address.channel = RFCOMM_CHANNEL;
    if (bind(fd, (struct sockaddr *)&address, sizeof(address)) != 0) {
        log_line("ERROR", "RFCOMM channel 7 bind failed");
        close(fd);
        return false;
    }
    if (!set_rfcomm_security(fd)) {
        log_line("ERROR", "RFCOMM medium security setup failed");
        close(fd);
        return false;
    }
    if (listen(fd, 1) != 0) {
        log_line("ERROR", "RFCOMM listen failed");
        close(fd);
        return false;
    }
    atomic_store_explicit(&g_rfcomm_stop, false, memory_order_relaxed);
    g_rfcomm_listen_fd = fd;
    if (pthread_create(thread, NULL, rfcomm_server_main, NULL) != 0) {
        g_rfcomm_listen_fd = -1;
        close(fd);
        log_line("ERROR", "RFCOMM worker start failed");
        return false;
    }
    return true;
}

static void stop_rfcomm_server(pthread_t thread) {
    atomic_store_explicit(&g_rfcomm_stop, true, memory_order_relaxed);
    if (g_rfcomm_listen_fd >= 0) {
        shutdown(g_rfcomm_listen_fd, SHUT_RDWR);
        close(g_rfcomm_listen_fd);
        g_rfcomm_listen_fd = -1;
    }
    pthread_join(thread, NULL);
}

static bool append_property_string(DBusMessageIter *properties, const char *key, const char *value) {
    DBusMessageIter entry;
    DBusMessageIter variant;
    if (!dbus_message_iter_open_container(properties, DBUS_TYPE_DICT_ENTRY, NULL, &entry) ||
        !dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key) ||
        !dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "s", &variant) ||
        !dbus_message_iter_append_basic(&variant, DBUS_TYPE_STRING, &value) ||
        !dbus_message_iter_close_container(&entry, &variant) ||
        !dbus_message_iter_close_container(properties, &entry)) return false;
    return true;
}

static bool append_property_object_path(DBusMessageIter *properties, const char *key, const char *value) {
    DBusMessageIter entry;
    DBusMessageIter variant;
    if (!dbus_message_iter_open_container(properties, DBUS_TYPE_DICT_ENTRY, NULL, &entry) ||
        !dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key) ||
        !dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "o", &variant) ||
        !dbus_message_iter_append_basic(&variant, DBUS_TYPE_OBJECT_PATH, &value) ||
        !dbus_message_iter_close_container(&entry, &variant) ||
        !dbus_message_iter_close_container(properties, &entry)) return false;
    return true;
}

static bool append_property_boolean(DBusMessageIter *properties, const char *key, bool value) {
    DBusMessageIter entry;
    DBusMessageIter variant;
    dbus_bool_t encoded = value ? TRUE : FALSE;
    if (!dbus_message_iter_open_container(properties, DBUS_TYPE_DICT_ENTRY, NULL, &entry) ||
        !dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key) ||
        !dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "b", &variant) ||
        !dbus_message_iter_append_basic(&variant, DBUS_TYPE_BOOLEAN, &encoded) ||
        !dbus_message_iter_close_container(&entry, &variant) ||
        !dbus_message_iter_close_container(properties, &entry)) return false;
    return true;
}

static bool append_property_string_array(DBusMessageIter *properties, const char *key,
                                         const char *const *values) {
    DBusMessageIter entry;
    DBusMessageIter variant;
    DBusMessageIter array;
    unsigned i;
    if (!dbus_message_iter_open_container(properties, DBUS_TYPE_DICT_ENTRY, NULL, &entry) ||
        !dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key) ||
        !dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "as", &variant) ||
        !dbus_message_iter_open_container(&variant, DBUS_TYPE_ARRAY, "s", &array)) return false;
    for (i = 0; values[i]; ++i) {
        const char *value = values[i];
        if (!dbus_message_iter_append_basic(&array, DBUS_TYPE_STRING, &value)) return false;
    }
    return dbus_message_iter_close_container(&variant, &array) &&
           dbus_message_iter_close_container(&entry, &variant) &&
           dbus_message_iter_close_container(properties, &entry);
}

static bool append_properties(DBusMessageIter *properties, enum object_kind kind) {
    static const char *const command_flags[] = {"encrypt-write", NULL};
    static const char *const status_flags[] = {"encrypt-read", NULL};
    static const char *const service_uuids[] = {SYNCAP_SERVICE_UUID, NULL};
    switch (kind) {
        case OBJECT_SERVICE:
            return append_property_string(properties, "UUID", SYNCAP_SERVICE_UUID) &&
                   append_property_boolean(properties, "Primary", true);
        case OBJECT_COMMAND:
            return append_property_string(properties, "UUID", SYNCAP_COMMAND_UUID) &&
                   append_property_object_path(properties, "Service", SERVICE_PATH) &&
                   append_property_string_array(properties, "Flags", command_flags);
        case OBJECT_STATUS:
            return append_property_string(properties, "UUID", SYNCAP_STATUS_UUID) &&
                   append_property_object_path(properties, "Service", SERVICE_PATH) &&
                   append_property_string_array(properties, "Flags", status_flags);
        case OBJECT_ADVERTISEMENT:
            return append_property_string(properties, "Type", "peripheral") &&
                   append_property_string_array(properties, "ServiceUUIDs", service_uuids) &&
                   append_property_string(properties, "LocalName", g_local_name) &&
                   append_property_boolean(properties, "Discoverable", true);
        default:
            return false;
    }
}

static const char *interface_for_kind(enum object_kind kind) {
    switch (kind) {
        case OBJECT_SERVICE: return GATT_SERVICE_IFACE;
        case OBJECT_COMMAND: case OBJECT_STATUS: return GATT_CHARACTERISTIC_IFACE;
        case OBJECT_ADVERTISEMENT: return LE_ADVERTISEMENT_IFACE;
        case OBJECT_AGENT: return AGENT_IFACE;
        case OBJECT_ROOT: return DBUS_OBJECT_MANAGER_IFACE;
    }
    return "";
}

static bool append_managed_object(DBusMessageIter *objects, const char *path,
                                  const char *interface_name, enum object_kind kind) {
    DBusMessageIter object_entry;
    DBusMessageIter interfaces;
    DBusMessageIter interface_entry;
    DBusMessageIter properties;
    if (!dbus_message_iter_open_container(objects, DBUS_TYPE_DICT_ENTRY, NULL, &object_entry) ||
        !dbus_message_iter_append_basic(&object_entry, DBUS_TYPE_OBJECT_PATH, &path) ||
        !dbus_message_iter_open_container(&object_entry, DBUS_TYPE_ARRAY, "{sa{sv}}", &interfaces) ||
        !dbus_message_iter_open_container(&interfaces, DBUS_TYPE_DICT_ENTRY, NULL, &interface_entry) ||
        !dbus_message_iter_append_basic(&interface_entry, DBUS_TYPE_STRING, &interface_name) ||
        !dbus_message_iter_open_container(&interface_entry, DBUS_TYPE_ARRAY, "{sv}", &properties) ||
        !append_properties(&properties, kind) ||
        !dbus_message_iter_close_container(&interface_entry, &properties) ||
        !dbus_message_iter_close_container(&interfaces, &interface_entry) ||
        !dbus_message_iter_close_container(&object_entry, &interfaces) ||
        !dbus_message_iter_close_container(objects, &object_entry)) return false;
    return true;
}

static DBusMessage *managed_objects_reply(DBusMessage *request) {
    DBusMessage *reply = dbus_message_new_method_return(request);
    DBusMessageIter iterator;
    DBusMessageIter objects;
    if (!reply) return NULL;
    dbus_message_iter_init_append(reply, &iterator);
    if (!dbus_message_iter_open_container(&iterator, DBUS_TYPE_ARRAY, "{oa{sa{sv}}}", &objects) ||
        !append_managed_object(&objects, SERVICE_PATH, GATT_SERVICE_IFACE, OBJECT_SERVICE) ||
        !append_managed_object(&objects, COMMAND_PATH, GATT_CHARACTERISTIC_IFACE, OBJECT_COMMAND) ||
        !append_managed_object(&objects, STATUS_PATH, GATT_CHARACTERISTIC_IFACE, OBJECT_STATUS) ||
        !dbus_message_iter_close_container(&iterator, &objects)) {
        dbus_message_unref(reply);
        return NULL;
    }
    return reply;
}

static DBusMessage *properties_get_all_reply(DBusMessage *request, enum object_kind kind) {
    const char *requested_interface;
    const char *expected_interface = interface_for_kind(kind);
    DBusMessage *reply;
    DBusMessageIter iterator;
    DBusMessageIter properties;
    if (!dbus_message_get_args(request, NULL, DBUS_TYPE_STRING, &requested_interface,
                               DBUS_TYPE_INVALID) || strcmp(requested_interface, expected_interface) != 0)
        return dbus_message_new_error(request, "org.freedesktop.DBus.Error.InvalidArgs",
                                      "unsupported interface");
    reply = dbus_message_new_method_return(request);
    if (!reply) return NULL;
    dbus_message_iter_init_append(reply, &iterator);
    if (!dbus_message_iter_open_container(&iterator, DBUS_TYPE_ARRAY, "{sv}", &properties) ||
        !append_properties(&properties, kind) ||
        !dbus_message_iter_close_container(&iterator, &properties)) {
        dbus_message_unref(reply);
        return NULL;
    }
    return reply;
}

static bool read_offset_option(DBusMessage *request, size_t *offset) {
    DBusMessageIter arguments;
    DBusMessageIter options;
    *offset = 0;
    if (!dbus_message_iter_init(request, &arguments) ||
        dbus_message_iter_get_arg_type(&arguments) != DBUS_TYPE_ARRAY)
        return false;
    dbus_message_iter_recurse(&arguments, &options);
    while (dbus_message_iter_get_arg_type(&options) != DBUS_TYPE_INVALID) {
        DBusMessageIter entry;
        DBusMessageIter variant;
        const char *key;
        int value_type;
        if (dbus_message_iter_get_arg_type(&options) != DBUS_TYPE_DICT_ENTRY) return false;
        dbus_message_iter_recurse(&options, &entry);
        if (dbus_message_iter_get_arg_type(&entry) != DBUS_TYPE_STRING) return false;
        dbus_message_iter_get_basic(&entry, &key);
        if (!dbus_message_iter_next(&entry) ||
            dbus_message_iter_get_arg_type(&entry) != DBUS_TYPE_VARIANT) return false;
        dbus_message_iter_recurse(&entry, &variant);
        value_type = dbus_message_iter_get_arg_type(&variant);
        if (strcmp(key, "offset") == 0) {
            if (value_type == DBUS_TYPE_UINT16) {
                dbus_uint16_t value;
                dbus_message_iter_get_basic(&variant, &value);
                *offset = value;
            } else if (value_type == DBUS_TYPE_UINT32) {
                dbus_uint32_t value;
                dbus_message_iter_get_basic(&variant, &value);
                *offset = value;
            } else {
                return false;
            }
        }
        dbus_message_iter_next(&options);
    }
    return true;
}

static DBusMessage *status_read_reply(DBusMessage *request) {
    DBusMessage *reply;
    DBusMessageIter iterator;
    DBusMessageIter bytes;
    unsigned char snapshot[BLE_MAX_STATUS];
    const unsigned char *data = snapshot;
    size_t offset;
    size_t length;
    size_t total_length;
    int encoded_length;
    if (!read_offset_option(request, &offset))
        return dbus_message_new_error(request, "org.freedesktop.DBus.Error.InvalidArgs",
                                      "options dictionary required");
    pthread_mutex_lock(&g_state_lock);
    total_length = g_status_length;
    length = total_length;
    if (offset <= total_length) {
        length = total_length - offset;
        memcpy(snapshot, g_status + offset, length);
    }
    pthread_mutex_unlock(&g_state_lock);
    if (offset > total_length)
        return dbus_message_new_error(request, "org.bluez.Error.InvalidOffset", "invalid offset");
    reply = dbus_message_new_method_return(request);
    if (!reply) return NULL;
    dbus_message_iter_init_append(reply, &iterator);
    if (!dbus_message_iter_open_container(&iterator, DBUS_TYPE_ARRAY, "y", &bytes)) {
        dbus_message_unref(reply);
        return NULL;
    }
    encoded_length = (int)length;
    if (encoded_length && !dbus_message_iter_append_fixed_array(&bytes, DBUS_TYPE_BYTE,
                                                                 &data, encoded_length)) {
        dbus_message_unref(reply);
        return NULL;
    }
    if (!dbus_message_iter_close_container(&iterator, &bytes)) {
        dbus_message_unref(reply);
        return NULL;
    }
    return reply;
}

static const char *command_error_name(enum command_result result) {
    switch (result) {
        case COMMAND_INVALID_LENGTH: return "org.bluez.Error.InvalidValueLength";
        case COMMAND_UNAUTHORIZED: return "org.bluez.Error.NotAuthorized";
        case COMMAND_BUSY: return "org.bluez.Error.InProgress";
        case COMMAND_UNSUPPORTED: return "org.bluez.Error.NotSupported";
        default: return "org.bluez.Error.Failed";
    }
}

static DBusMessage *command_write_reply(DBusMessage *request) {
    DBusMessageIter iterator;
    DBusMessageIter array;
    const unsigned char *value = NULL;
    int length = 0;
    enum command_result result;
    if (!dbus_message_iter_init(request, &iterator) ||
        dbus_message_iter_get_arg_type(&iterator) != DBUS_TYPE_ARRAY ||
        dbus_message_iter_get_element_type(&iterator) != DBUS_TYPE_BYTE) {
        return dbus_message_new_error(request, "org.freedesktop.DBus.Error.InvalidArgs",
                                      "byte array required");
    }
    dbus_message_iter_recurse(&iterator, &array);
    dbus_message_iter_get_fixed_array(&array, &value, &length);
    if (length < 0) return dbus_message_new_error(request, "org.bluez.Error.InvalidValueLength",
                                                   "invalid value length");
    result = accept_command_value(value, (size_t)length);
    if (result == COMMAND_ACCEPTED || result == COMMAND_WAITING)
        return dbus_message_new_method_return(request);
    return dbus_message_new_error(request, command_error_name(result), "command rejected");
}

static DBusMessage *agent_reply(DBusMessage *request) {
    const char *member = dbus_message_get_member(request);
    DBusMessage *reply = dbus_message_new_method_return(request);
    if (!reply) return NULL;
    if (member && strcmp(member, "RequestPinCode") == 0) {
        const char *pin = "000000";
        if (!dbus_message_append_args(reply, DBUS_TYPE_STRING, &pin, DBUS_TYPE_INVALID)) goto fail;
    } else if (member && strcmp(member, "RequestPasskey") == 0) {
        dbus_uint32_t passkey = 0;
        if (!dbus_message_append_args(reply, DBUS_TYPE_UINT32, &passkey, DBUS_TYPE_INVALID)) goto fail;
    } else if (!member || (strcmp(member, "Release") != 0 &&
                           strcmp(member, "DisplayPinCode") != 0 &&
                           strcmp(member, "DisplayPasskey") != 0 &&
                           strcmp(member, "RequestConfirmation") != 0 &&
                           strcmp(member, "RequestAuthorization") != 0 &&
                           strcmp(member, "AuthorizeService") != 0 &&
                           strcmp(member, "Cancel") != 0)) {
        dbus_message_unref(reply);
        return dbus_message_new_error(request, "org.freedesktop.DBus.Error.UnknownMethod",
                                      "unsupported agent method");
    }
    return reply;
fail:
    dbus_message_unref(reply);
    return NULL;
}

static bool send_reply(DBusConnection *connection, DBusMessage *reply) {
    dbus_bool_t sent;
    if (!reply) return false;
    sent = dbus_connection_send(connection, reply, NULL);
    dbus_message_unref(reply);
    return sent;
}

static void stop_after_bluez_loss(void) {
    if (!stop_requested())
        atomic_store_explicit(&g_bluez_lost, true, memory_order_relaxed);
    atomic_store_explicit(&g_stop, true, memory_order_relaxed);
}

static DBusHandlerResult bus_message(DBusConnection *connection, DBusMessage *message,
                                     void *user_data) {
    const char *name;
    const char *old_owner;
    const char *new_owner;
    DBusError error;
    (void)connection;
    (void)user_data;
    if (!dbus_message_is_signal(message, DBUS_INTERFACE_DBUS, "NameOwnerChanged"))
        return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
    dbus_error_init(&error);
    if (dbus_message_get_args(message, &error,
                              DBUS_TYPE_STRING, &name,
                              DBUS_TYPE_STRING, &old_owner,
                              DBUS_TYPE_STRING, &new_owner,
                              DBUS_TYPE_INVALID) &&
        strcmp(name, BLUEZ_SERVICE) == 0 && old_owner[0] && !new_owner[0]) {
        stop_after_bluez_loss();
    }
    if (dbus_error_is_set(&error)) dbus_error_free(&error);
    return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
}

static bool add_bluez_owner_watch(void) {
    static const char match[] = "type='signal',interface='org.freedesktop.DBus',"
                                "member='NameOwnerChanged',arg0='org.bluez'";
    DBusError error;
    if (!dbus_connection_add_filter(g_bus, bus_message, NULL, NULL)) return false;
    dbus_error_init(&error);
    dbus_bus_add_match(g_bus, match, &error);
    if (dbus_error_is_set(&error)) {
        dbus_error_free(&error);
        dbus_connection_remove_filter(g_bus, bus_message, NULL);
        return false;
    }
    return true;
}

static void remove_bluez_owner_watch(void) {
    static const char match[] = "type='signal',interface='org.freedesktop.DBus',"
                                "member='NameOwnerChanged',arg0='org.bluez'";
    DBusError error;
    dbus_error_init(&error);
    dbus_bus_remove_match(g_bus, match, &error);
    if (dbus_error_is_set(&error)) dbus_error_free(&error);
    dbus_connection_remove_filter(g_bus, bus_message, NULL);
}

static DBusHandlerResult object_message(DBusConnection *connection, DBusMessage *message,
                                        void *user_data) {
    struct object_context *context = user_data;
    DBusMessage *reply = NULL;
    if (context->kind == OBJECT_ROOT && dbus_message_is_method_call(message,
            DBUS_OBJECT_MANAGER_IFACE, "GetManagedObjects")) {
        reply = managed_objects_reply(message);
    } else if (dbus_message_is_method_call(message, DBUS_PROPERTIES_IFACE, "GetAll")) {
        if (context->kind == OBJECT_SERVICE || context->kind == OBJECT_COMMAND ||
            context->kind == OBJECT_STATUS || context->kind == OBJECT_ADVERTISEMENT)
            reply = properties_get_all_reply(message, context->kind);
    } else if (context->kind == OBJECT_COMMAND && dbus_message_is_method_call(message,
            GATT_CHARACTERISTIC_IFACE, "WriteValue")) {
        reply = command_write_reply(message);
    } else if (context->kind == OBJECT_STATUS && dbus_message_is_method_call(message,
            GATT_CHARACTERISTIC_IFACE, "ReadValue")) {
        reply = status_read_reply(message);
    } else if (context->kind == OBJECT_ADVERTISEMENT && dbus_message_is_method_call(message,
            LE_ADVERTISEMENT_IFACE, "Release")) {
        stop_after_bluez_loss();
        if (dbus_message_get_no_reply(message)) return DBUS_HANDLER_RESULT_HANDLED;
        reply = dbus_message_new_method_return(message);
    } else if (context->kind == OBJECT_AGENT &&
               strcmp(dbus_message_get_interface(message) ? dbus_message_get_interface(message) : "",
                      AGENT_IFACE) == 0) {
        if (dbus_message_is_method_call(message, AGENT_IFACE, "Release")) {
            stop_after_bluez_loss();
        }
        if (dbus_message_get_no_reply(message)) return DBUS_HANDLER_RESULT_HANDLED;
        reply = agent_reply(message);
    }
    if (!reply)
        reply = dbus_message_new_error(message, "org.freedesktop.DBus.Error.UnknownMethod",
                                       "unsupported method");
    return send_reply(connection, reply) ? DBUS_HANDLER_RESULT_HANDLED : DBUS_HANDLER_RESULT_NEED_MEMORY;
}

static DBusObjectPathVTable g_object_vtable = {
    .unregister_function = NULL,
    .message_function = object_message,
};

static DBusMessage *bluez_call(DBusMessage *request, int timeout_ms, const char *operation) {
    DBusPendingCall *pending = NULL;
    DBusMessage *reply = NULL;
    uint64_t deadline = monotonic_ms() + (uint64_t)timeout_ms + 250U;
    const char *error_name;
    const char *error_message = NULL;
    char text[384];
    if (!dbus_connection_send_with_reply(g_bus, request, &pending, timeout_ms) || !pending) {
        dbus_message_unref(request);
        snprintf(text, sizeof(text), "%s failed: cannot queue D-Bus call", operation);
        log_line("ERROR", text);
        return NULL;
    }
    dbus_message_unref(request);
    while (!dbus_pending_call_get_completed(pending) &&
           dbus_connection_get_is_connected(g_bus) && monotonic_ms() < deadline) {
        uint64_t now = monotonic_ms();
        uint64_t remaining = now < deadline ? deadline - now : 0;
        int wait_ms = remaining > 100ULL ? 100 : (int)remaining;
        if (!remaining) break;
        if (!dbus_connection_read_write_dispatch(g_bus, wait_ms > 0 ? wait_ms : 1)) break;
    }
    if (dbus_pending_call_get_completed(pending)) reply = dbus_pending_call_steal_reply(pending);
    else dbus_pending_call_cancel(pending);
    dbus_pending_call_unref(pending);
    if (!reply) {
        snprintf(text, sizeof(text), "%s failed: no reply", operation);
        log_line("ERROR", text);
        return NULL;
    }
    if (dbus_message_get_type(reply) == DBUS_MESSAGE_TYPE_ERROR) {
        error_name = dbus_message_get_error_name(reply);
        (void)dbus_message_get_args(reply, NULL, DBUS_TYPE_STRING, &error_message, DBUS_TYPE_INVALID);
        snprintf(text, sizeof(text), "%s failed: %s%s%s", operation,
                 error_name ? error_name : "D-Bus error",
                 error_message ? ": " : "", error_message ? error_message : "");
        log_line("ERROR", text);
        dbus_message_unref(reply);
        return NULL;
    }
    return reply;
}

static bool call_path_and_options(const char *interface_name, const char *method,
                                  const char *object_path) {
    DBusMessage *request = dbus_message_new_method_call(BLUEZ_SERVICE, g_adapter_path,
                                                         interface_name, method);
    DBusMessage *reply;
    DBusMessageIter iterator;
    DBusMessageIter options;
    if (!request) return false;
    dbus_message_iter_init_append(request, &iterator);
    if (!dbus_message_iter_append_basic(&iterator, DBUS_TYPE_OBJECT_PATH, &object_path) ||
        !dbus_message_iter_open_container(&iterator, DBUS_TYPE_ARRAY, "{sv}", &options) ||
        !dbus_message_iter_close_container(&iterator, &options)) {
        dbus_message_unref(request);
        return false;
    }
    reply = bluez_call(request,
                       strcmp(interface_name, LE_ADVERTISING_MANAGER_IFACE) == 0 ? 30000 : 10000,
                       method);
    if (!reply) return false;
    dbus_message_unref(reply);
    return true;
}

static bool call_path_only(const char *destination_path, const char *interface_name,
                           const char *method, const char *object_path, bool required) {
    DBusMessage *request = dbus_message_new_method_call(BLUEZ_SERVICE, destination_path,
                                                         interface_name, method);
    DBusMessage *reply;
    if (!request) return false;
    if (!dbus_message_append_args(request, DBUS_TYPE_OBJECT_PATH, &object_path, DBUS_TYPE_INVALID)) {
        dbus_message_unref(request);
        return false;
    }
    reply = bluez_call(request, 10000, method);
    if (!reply) return !required;
    dbus_message_unref(reply);
    return true;
}

static bool register_agent(void) {
    const char *capability = "NoInputNoOutput";
    DBusMessage *request = dbus_message_new_method_call(BLUEZ_SERVICE, "/org/bluez",
                                                         AGENT_MANAGER_IFACE, "RegisterAgent");
    DBusMessage *reply;
    const char *agent_path = AGENT_PATH;
    if (!request) return false;
    if (!dbus_message_append_args(request, DBUS_TYPE_OBJECT_PATH, &agent_path,
                                  DBUS_TYPE_STRING, &capability, DBUS_TYPE_INVALID)) {
        dbus_message_unref(request);
        return false;
    }
    reply = bluez_call(request, 10000, "RegisterAgent");
    if (!reply) return false;
    dbus_message_unref(reply);
    if (call_path_only("/org/bluez", AGENT_MANAGER_IFACE, "RequestDefaultAgent",
                       AGENT_PATH, true)) return true;
    call_path_only("/org/bluez", AGENT_MANAGER_IFACE, "UnregisterAgent", AGENT_PATH, false);
    return false;
}

static bool set_adapter_property(const char *property, int value_type, const void *value) {
    DBusMessage *request = dbus_message_new_method_call(BLUEZ_SERVICE, g_adapter_path,
                                                         DBUS_PROPERTIES_IFACE, "Set");
    DBusMessage *reply;
    DBusMessageIter iterator;
    DBusMessageIter variant;
    const char *interface_name = ADAPTER_IFACE;
    const char *signature = value_type == DBUS_TYPE_BOOLEAN ? "b" :
                            value_type == DBUS_TYPE_UINT32 ? "u" : "s";
    if (!request) return false;
    dbus_message_iter_init_append(request, &iterator);
    if (!dbus_message_iter_append_basic(&iterator, DBUS_TYPE_STRING, &interface_name) ||
        !dbus_message_iter_append_basic(&iterator, DBUS_TYPE_STRING, &property) ||
        !dbus_message_iter_open_container(&iterator, DBUS_TYPE_VARIANT, signature, &variant) ||
        !dbus_message_iter_append_basic(&variant, value_type, value) ||
        !dbus_message_iter_close_container(&iterator, &variant)) {
        dbus_message_unref(request);
        return false;
    }
    reply = bluez_call(request, 10000, property);
    if (!reply) return false;
    dbus_message_unref(reply);
    return true;
}

static bool register_objects(void) {
    return dbus_connection_register_object_path(g_bus, ROOT_PATH, &g_object_vtable, &g_root_context) &&
           dbus_connection_register_object_path(g_bus, SERVICE_PATH, &g_object_vtable, &g_service_context) &&
           dbus_connection_register_object_path(g_bus, COMMAND_PATH, &g_object_vtable, &g_command_context) &&
           dbus_connection_register_object_path(g_bus, STATUS_PATH, &g_object_vtable, &g_status_context) &&
           dbus_connection_register_object_path(g_bus, ADVERTISEMENT_PATH, &g_object_vtable,
                                                 &g_advertisement_context) &&
           dbus_connection_register_object_path(g_bus, AGENT_PATH, &g_object_vtable, &g_agent_context);
}

static void unregister_objects(void) {
    dbus_connection_unregister_object_path(g_bus, AGENT_PATH);
    dbus_connection_unregister_object_path(g_bus, ADVERTISEMENT_PATH);
    dbus_connection_unregister_object_path(g_bus, STATUS_PATH);
    dbus_connection_unregister_object_path(g_bus, COMMAND_PATH);
    dbus_connection_unregister_object_path(g_bus, SERVICE_PATH);
    dbus_connection_unregister_object_path(g_bus, ROOT_PATH);
}

static void signal_handler(int signal_number) {
    (void)signal_number;
    g_signal_stop = 1;
}

static void *dbus_dispatch_main(void *unused) {
    (void)unused;
    while (!dispatch_stop_requested() && dbus_connection_get_is_connected(g_bus)) {
        if (!dbus_connection_read_write_dispatch(g_bus, 250)) break;
    }
    if (!dispatch_stop_requested())
        atomic_store_explicit(&g_stop, true, memory_order_relaxed);
    return NULL;
}

static DBusMessage *make_test_read_request(dbus_uint16_t offset) {
    DBusMessage *request = dbus_message_new_method_call("org.syncap.SelfTest", STATUS_PATH,
                                                        GATT_CHARACTERISTIC_IFACE, "ReadValue");
    DBusMessageIter arguments;
    DBusMessageIter options;
    DBusMessageIter entry;
    DBusMessageIter variant;
    const char *key = "offset";
    if (!request) return NULL;
    dbus_message_set_serial(request, 1);
    dbus_message_iter_init_append(request, &arguments);
    if (!dbus_message_iter_open_container(&arguments, DBUS_TYPE_ARRAY, "{sv}", &options) ||
        !dbus_message_iter_open_container(&options, DBUS_TYPE_DICT_ENTRY, NULL, &entry) ||
        !dbus_message_iter_append_basic(&entry, DBUS_TYPE_STRING, &key) ||
        !dbus_message_iter_open_container(&entry, DBUS_TYPE_VARIANT, "q", &variant) ||
        !dbus_message_iter_append_basic(&variant, DBUS_TYPE_UINT16, &offset) ||
        !dbus_message_iter_close_container(&entry, &variant) ||
        !dbus_message_iter_close_container(&options, &entry) ||
        !dbus_message_iter_close_container(&arguments, &options)) {
        dbus_message_unref(request);
        return NULL;
    }
    return request;
}

static bool self_test_status_offset(void) {
    static const char value[] = "{\"state\":\"ready\",\"detail\":\"offset\"}";
    DBusMessage *request;
    DBusMessage *reply;
    DBusMessageIter arguments;
    DBusMessageIter array;
    const unsigned char *actual = NULL;
    int actual_length = 0;
    bool valid = false;
    set_status_text(value);
    request = make_test_read_request(5);
    if (!request) return false;
    reply = status_read_reply(request);
    dbus_message_unref(request);
    if (!reply) return false;
    if (dbus_message_get_type(reply) == DBUS_MESSAGE_TYPE_METHOD_RETURN &&
        dbus_message_iter_init(reply, &arguments) &&
        dbus_message_iter_get_arg_type(&arguments) == DBUS_TYPE_ARRAY) {
        dbus_message_iter_recurse(&arguments, &array);
        dbus_message_iter_get_fixed_array(&array, &actual, &actual_length);
        valid = actual_length == (int)(strlen(value) - 5U) &&
                memcmp(actual, value + 5U, (size_t)actual_length) == 0;
    }
    dbus_message_unref(reply);
    if (!valid) return false;
    request = make_test_read_request(500);
    if (!request) return false;
    reply = status_read_reply(request);
    dbus_message_unref(request);
    if (!reply) return false;
    valid = dbus_message_get_type(reply) == DBUS_MESSAGE_TYPE_ERROR &&
            strcmp(dbus_message_get_error_name(reply), "org.bluez.Error.InvalidOffset") == 0;
    dbus_message_unref(reply);
    set_status_text("{\"state\":\"ready\"}");
    return valid;
}

static bool self_test_rfcomm_frame(void) {
    static const char message[] = "{\"state\":\"ready\"}";
    int sockets[2];
    unsigned char prefix[4];
    char body[sizeof(message)] = {0};
    bool valid;
    if (socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) != 0) return false;
    valid = rfcomm_send_json(sockets[0], message) &&
            rfcomm_receive_exact(sockets[1], prefix, sizeof(prefix), monotonic_ms() + 1000ULL) &&
            read_be32(prefix) == sizeof(message) - 1U &&
            rfcomm_receive_exact(sockets[1], body, sizeof(message) - 1U,
                                 monotonic_ms() + 1000ULL) &&
            memcmp(body, message, sizeof(message) - 1U) == 0;
    close(sockets[0]);
    close(sockets[1]);
    return valid;
}

struct http_test_server {
    int listen_fd;
    bool slow_body;
    bool ok;
};

static void *http_test_server_main(void *opaque) {
    static const char header[] = "HTTP/1.0 200 OK\r\nContent-Length: 11\r\n\r\n";
    static const char body[] = "{\"ok\":true}";
    struct http_test_server *server = opaque;
    struct timespec hold = {1, 200000000L};
    char request[512];
    int client = accept(server->listen_fd, NULL, NULL);
    if (client < 0 || recv(client, request, sizeof(request), 0) <= 0 ||
        !send_all(client, header, sizeof(header) - 1U)) {
        if (client >= 0) close(client);
        return NULL;
    }
    if (server->slow_body) {
        if (!send_all(client, body, 1U)) {
            close(client);
            return NULL;
        }
        nanosleep(&hold, NULL);
    } else {
        if (!send_all(client, body, sizeof(body) - 1U)) {
            close(client);
            return NULL;
        }
        server->ok = true;
        nanosleep(&hold, NULL); /* The client must not wait for connection close. */
    }
    close(client);
    if (server->slow_body) server->ok = true;
    return NULL;
}

static bool self_test_http_deadline_case(bool slow_body) {
    struct http_test_server server = {-1, slow_body, false};
    struct sockaddr_in address;
    socklen_t address_length = sizeof(address);
    pthread_t thread;
    char *response = NULL;
    int status = 0;
    unsigned saved_port = g_http_port;
    uint64_t started;
    uint64_t elapsed;
    bool result;
    server.listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (server.listen_fd < 0) return false;
    memset(&address, 0, sizeof(address));
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(UINT32_C(0x7f000001));
    if (bind(server.listen_fd, (struct sockaddr *)&address, sizeof(address)) != 0 ||
        listen(server.listen_fd, 1) != 0 ||
        getsockname(server.listen_fd, (struct sockaddr *)&address, &address_length) != 0) {
        close(server.listen_fd);
        return false;
    }
    if (pthread_create(&thread, NULL, http_test_server_main, &server) != 0) {
        close(server.listen_fd);
        return false;
    }
    g_http_port = ntohs(address.sin_port);
    started = monotonic_ms();
    result = http_request("GET", "/test", NULL, 0, 1U, &status, &response);
    elapsed = monotonic_ms() - started;
    g_http_port = saved_port;
    pthread_join(thread, NULL);
    close(server.listen_fd);
    if (!server.ok) {
        free(response);
        return false;
    }
    if (slow_body) {
        free(response);
        return !result && elapsed >= 800ULL && elapsed < 1800ULL;
    }
    result = result && status == 200 && response && strcmp(response, "{\"ok\":true}") == 0 &&
             elapsed < 800ULL;
    free(response);
    return result;
}

static int self_test(void) {
    const char *scan = "{\"state\":\"completed\",\"networks\":["
        "{\"ssid\":\"Phone hotspot\",\"rssi\":-42,\"security\":\"wpa2-psk\",\"secure\":true},"
        "{\"ssid\":\"Lab\",\"rssi\":-65,\"security\":\"open\",\"secure\":false}],"
        "\"scannedAt\":1786700000000}";
    const char *status = "{\"state\":\"connected\",\"ssid\":\"Phone hotspot\","
        "\"ipAddress\":\"192.168.43.20\",\"password\":\"must-not-leak\"}";
    const char *request = "{\"op\":\"wifi.configure\",\"ssid\":\"Phone hotspot\","
        "\"password\":\"must-not-leak\",\"claimCode\":\"123456\"}";
    const char *device_status = "{\"capture\":{\"state\":\"idle\"},"
        "\"wifi\":{\"state\":\"connected\",\"ssid\":\"Phone hotspot\","
        "\"ipAddress\":\"192.168.43.20\",\"bssid\":\"must-not-leak\"}}";
    struct fixed_buf output = {{0}, 0};
    const char *unauthorized = "{\"ignored\":{\"nested\":[1,{\"x\":2}]},"
        "\"op\":\"wifi.scan\",\"claimCode\":\"000000\"}";
    const char *malformed = "{\"op\":\"wifi.scan\",\"claimCode\":\"000000\"}trailing";
    const char *legacy = "{\"ssid\":\"legacy\",\"claimCode\":\"000000\"}";
    unsigned char fragment[256];
    size_t unauthorized_length = strlen(unauthorized);
    size_t split = unauthorized_length / 2U;
    size_t fragment_length;
    char parsed[64];
    if (!json_get_string(request, "op", parsed, sizeof(parsed)) || strcmp(parsed, "wifi.configure") != 0)
        return 1;
    if (!compact_scan_response(scan, &output) || output.length > BLE_MAX_STATUS ||
        !strstr(output.data, "Phone hotspot") || !strstr(output.data, "\"op\":\"wifi.scan\"")) return 2;
    memset(&output, 0, sizeof(output));
    if (!compact_wifi_status(status, request, &output) || output.length > BLE_MAX_STATUS ||
        strstr(output.data, "must-not-leak") || !strstr(output.data, "192.168.43.20")) return 3;
    memset(&output, 0, sizeof(output));
    if (!compact_device_status(device_status, &output) || output.length > BLE_MAX_STATUS ||
        strstr(output.data, "must-not-leak") || !json_is_complete_object(output.data) ||
        !strstr(output.data, "\"op\":\"device.status\"") ||
        !strstr(output.data, "192.168.43.20")) return 4;
    if (!json_get_string("{\"ssid\":\"\\u4e2d\\u6587\\ud83d\\ude80\"}",
                         "ssid", parsed, sizeof(parsed)) || strcmp(parsed, "中文🚀") != 0) return 5;
    if (!json_get_string(unauthorized, "op", parsed, sizeof(parsed)) ||
        strcmp(parsed, "wifi.scan") != 0) return 6;
    fragment_length = encode_test_fragment(fragment, sizeof(fragment), UINT32_C(0x10203040),
                                           0, 2, (const unsigned char *)unauthorized, split);
    if (!fragment_length || accept_command_value(fragment, fragment_length) != COMMAND_WAITING)
        return 7;
    fragment_length = encode_test_fragment(fragment, sizeof(fragment), UINT32_C(0x10203040),
                                           1, 2, (const unsigned char *)unauthorized + split,
                                           unauthorized_length - split);
    if (!fragment_length || accept_command_value(fragment, fragment_length) != COMMAND_UNAUTHORIZED ||
        g_reassembly.active) return 8;
    if (dispatch_command((const unsigned char *)malformed, strlen(malformed)) !=
            COMMAND_INVALID_FORMAT) return 9;
    if (dispatch_command((const unsigned char *)legacy, strlen(legacy)) !=
            COMMAND_UNAUTHORIZED) return 10;
    if (!self_test_status_offset()) return 11;
    if (!self_test_rfcomm_frame()) return 12;
    if (!self_test_http_deadline_case(false)) return 13;
    if (!self_test_http_deadline_case(true)) return 14;
    puts("syncap-tina-ble self-test: ok");
    return 0;
}

static void usage(const char *program) {
    fprintf(stderr, "Usage: %s [--adapter PATH] [--name NAME] [--http-port PORT] [--self-test]\n",
            program);
}

int main(int argc, char **argv) {
    DBusError error;
    dbus_bool_t enabled = TRUE;
    dbus_uint32_t no_timeout = 0;
    bool app_registered = false;
    bool advertisement_registered = false;
    bool agent_registered = false;
    bool dispatcher_started = false;
    bool owner_watch_added = false;
    bool rfcomm_started = false;
    bool service_ready = false;
    pthread_t dispatcher;
    pthread_t rfcomm;
    const char *local_name;
    int i;

    if (!dbus_threads_init_default()) {
        log_line("ERROR", "cannot initialize D-Bus threading");
        return 1;
    }
    for (i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--self-test") == 0) return self_test();
        if (strcmp(argv[i], "--adapter") == 0 && i + 1 < argc) {
            if (!dbus_validate_path(argv[++i], NULL) || strlen(argv[i]) >= sizeof(g_adapter_path)) {
                usage(argv[0]);
                return 2;
            }
            strcpy(g_adapter_path, argv[i]);
        } else if (strcmp(argv[i], "--name") == 0 && i + 1 < argc) {
            if (!argv[i + 1][0] || strlen(argv[i + 1]) >= sizeof(g_local_name)) {
                usage(argv[0]);
                return 2;
            }
            strcpy(g_local_name, argv[++i]);
        } else if (strcmp(argv[i], "--http-port") == 0 && i + 1 < argc) {
            char *end;
            unsigned long parsed = strtoul(argv[++i], &end, 10);
            if (*end || parsed == 0 || parsed > 65535UL) {
                usage(argv[0]);
                return 2;
            }
            g_http_port = (unsigned)parsed;
        } else {
            usage(argv[0]);
            return 2;
        }
    }

    signal(SIGPIPE, SIG_IGN);
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    dbus_error_init(&error);
    g_bus = dbus_bus_get(DBUS_BUS_SYSTEM, &error);
    if (!g_bus) {
        log_line("ERROR", dbus_error_is_set(&error) && error.name ? error.name : "system bus unavailable");
        if (dbus_error_is_set(&error)) dbus_error_free(&error);
        return 1;
    }
    dbus_connection_set_exit_on_disconnect(g_bus, FALSE);
    if (!add_bluez_owner_watch()) {
        log_line("ERROR", "cannot watch BlueZ service lifecycle");
        goto done;
    }
    owner_watch_added = true;
    if (!register_objects()) {
        log_line("ERROR", "cannot export GATT objects");
        goto done;
    }
    local_name = g_local_name;
    if (!set_adapter_property("Powered", DBUS_TYPE_BOOLEAN, &enabled) ||
        !set_adapter_property("Pairable", DBUS_TYPE_BOOLEAN, &enabled) ||
        !set_adapter_property("PairableTimeout", DBUS_TYPE_UINT32, &no_timeout) ||
        !set_adapter_property("Discoverable", DBUS_TYPE_BOOLEAN, &enabled) ||
        !set_adapter_property("DiscoverableTimeout", DBUS_TYPE_UINT32, &no_timeout) ||
        !set_adapter_property("Alias", DBUS_TYPE_STRING, &local_name)) goto done;
    if (!register_agent()) goto done;
    agent_registered = true;
    if (start_rfcomm_server(&rfcomm)) {
        rfcomm_started = true;
        if (!publish_ready_marker()) {
            log_line("ERROR", "cannot publish RFCOMM ready marker");
            goto done;
        }
    } else {
        log_line("WARN", "encrypted RFCOMM fallback unavailable");
    }
    if (!call_path_and_options(GATT_MANAGER_IFACE, "RegisterApplication", ROOT_PATH)) {
        log_line("WARN", "GATT manager unavailable; RFCOMM remains active");
    } else {
        app_registered = true;
    }
    if (app_registered) {
        if (call_path_and_options(LE_ADVERTISING_MANAGER_IFACE, "RegisterAdvertisement",
                                  ADVERTISEMENT_PATH)) {
            advertisement_registered = true;
        } else {
            log_line("WARN", "LE advertisement unavailable; classic discovery remains enabled");
        }
    } else {
        log_line("INFO", "LE advertisement disabled because the GATT service is unavailable");
    }
    if (!rfcomm_started && !(app_registered && advertisement_registered)) goto done;
    if (pthread_create(&dispatcher, NULL, dbus_dispatch_main, NULL) != 0) {
        log_line("ERROR", "cannot start D-Bus dispatcher");
        goto done;
    }
    dispatcher_started = true;
    service_ready = true;
    if (app_registered && advertisement_registered)
        log_line("INFO", rfcomm_started ? "GATT and RFCOMM provisioning services ready"
                                         : "GATT provisioning service ready");
    else
        log_line("INFO", "RFCOMM provisioning service ready on channel 7");
    while (!g_signal_stop && !stop_requested() && dbus_connection_get_is_connected(g_bus)) {
        struct timespec delay = {0, 100000000L};
        nanosleep(&delay, NULL);
    }
    if (g_signal_stop) atomic_store_explicit(&g_stop, true, memory_order_relaxed);

done:
    clear_ready_marker();
    if (rfcomm_started) stop_rfcomm_server(rfcomm);
    if (dispatcher_started) {
        atomic_store_explicit(&g_dispatch_stop, true, memory_order_relaxed);
        pthread_join(dispatcher, NULL);
    }
    if (advertisement_registered && !bluez_was_lost())
        call_path_only(g_adapter_path, LE_ADVERTISING_MANAGER_IFACE,
                       "UnregisterAdvertisement", ADVERTISEMENT_PATH, false);
    if (app_registered && !bluez_was_lost())
        call_path_only(g_adapter_path, GATT_MANAGER_IFACE,
                       "UnregisterApplication", ROOT_PATH, false);
    if (agent_registered && !bluez_was_lost())
        call_path_only("/org/bluez", AGENT_MANAGER_IFACE, "UnregisterAgent", AGENT_PATH, false);
    clear_reassembly();
    unregister_objects();
    if (owner_watch_added) remove_bluez_owner_watch();
    dbus_connection_unref(g_bus);
    g_bus = NULL;
    return service_ready && !bluez_was_lost() ? 0 : 1;
}
