#define _GNU_SOURCE

#include <dlfcn.h>
#include <stdlib.h>

typedef int (*init_rtsp_rotate_fn)(int, int, int, long long, int, char *, int);

static int configured_rotation(int fallback) {
    const char *value = getenv("SYNCAP_VENC_ROTATION");
    if (value == NULL) {
        return fallback;
    }
    char *end = NULL;
    long rotation = strtol(value, &end, 10);
    if (end == value || *end != '\0' ||
        (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270)) {
        return fallback;
    }
    return (int)rotation;
}

static init_rtsp_rotate_fn next_function(const char *name) {
    return (init_rtsp_rotate_fn)dlsym(RTLD_NEXT, name);
}

#define DEFINE_ROTATED_INIT(name)                                                   \
    int name(int width, int height, int fps, long long bps, int port, char *url,    \
             int rotation) {                                                        \
        init_rtsp_rotate_fn next = next_function(#name);                            \
        if (next == NULL) {                                                         \
            return -1;                                                             \
        }                                                                           \
        return next(width, height, fps, bps, port, url, configured_rotation(rotation)); \
    }

DEFINE_ROTATED_INIT(init_rtsp_ch1_rotate)
DEFINE_ROTATED_INIT(init_rtsp_ch2_rotate)
DEFINE_ROTATED_INIT(init_rtsp_ch3_rotate)
DEFINE_ROTATED_INIT(init_rtsp_ch4_rotate)

