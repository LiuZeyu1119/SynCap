package com.syncap.studio;

final class PreviewReconnectPolicy {
    private static final int RTSP_MAX_AUTOMATIC_RETRIES = 3;
    private static final long RTSP_MAX_RETRY_DELAY_MS = 4_000;
    private static final int RAW_TCP_MAX_AUTOMATIC_RETRIES = 0;
    private static final int STREAM_START_STAGGER_MS = 150;

    private PreviewReconnectPolicy() {}

    static boolean isRawHevcUrl(String url) {
        return url != null && url.startsWith("tcp://");
    }

    static int automaticRetryLimit(String url) {
        return isRawHevcUrl(url)
            ? RAW_TCP_MAX_AUTOMATIC_RETRIES
            : RTSP_MAX_AUTOMATIC_RETRIES;
    }

    static long automaticRetryDelayMs(String url, int reconnectAttempt, int streamIndex) {
        int boundedAttempt = Math.max(
            1,
            Math.min(reconnectAttempt, RTSP_MAX_AUTOMATIC_RETRIES)
        );
        long baseDelayMs = isRawHevcUrl(url)
            ? 0
            : Math.min(
                1_000L << (boundedAttempt - 1),
                RTSP_MAX_RETRY_DELAY_MS
            );
        return baseDelayMs + (long) Math.max(0, streamIndex) * STREAM_START_STAGGER_MS;
    }

    static boolean resetRetryBudgetAfterStablePlayback(String url) {
        return !isRawHevcUrl(url);
    }
}
