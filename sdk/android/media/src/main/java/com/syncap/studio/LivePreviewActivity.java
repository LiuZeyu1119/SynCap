package com.syncap.studio;

import android.content.Intent;
import android.graphics.Color;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Debug;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.annotation.Nullable;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import org.videolan.libvlc.LibVLC;
import org.videolan.libvlc.Media;
import org.videolan.libvlc.MediaPlayer;
import org.videolan.libvlc.interfaces.IMedia;
import org.videolan.libvlc.util.VLCVideoLayout;

import java.lang.ref.WeakReference;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

public class LivePreviewActivity extends AppCompatActivity {
    public static final String EXTRA_URLS = "syncap.preview.urls";
    public static final String EXTRA_LABELS = "syncap.preview.labels";

    private static final String TAG = "SynCapPreview";
    private static final int COLOR_OBSIDIAN = Color.rgb(5, 5, 5);
    private static final int COLOR_GOLD = Color.rgb(225, 188, 92);
    private static final int COLOR_HEALTHY = Color.rgb(62, 207, 96);
    private static final int COLOR_ERROR = Color.rgb(239, 96, 96);
    private static final long WATCHDOG_INTERVAL_MS = 1_000;
    private static final long STARTUP_STALL_TIMEOUT_MS = 12_000;
    private static final long PLAYING_STALL_TIMEOUT_MS = 6_000;
    private static final long STALE_LABEL_TIMEOUT_MS = 2_000;
    private static final long STABLE_PLAYBACK_RESET_MS = 15_000;
    private static final int START_STAGGER_MS = 150;
    static final String RAW_HEVC_FRAME_RATE = "29.4118";
    private static final AtomicInteger RELEASE_THREAD_SEQUENCE = new AtomicInteger();
    private static final Object ENGINE_GATE = new Object();
    private static final ArrayDeque<WeakReference<LivePreviewActivity>> ENGINE_WAITERS = new ArrayDeque<>();
    private static boolean engineLeased;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final List<StreamSession> sessions = new ArrayList<>();
    private final ExecutorService engineReleaseExecutor = Executors.newSingleThreadExecutor(
        new PlayerReleaseThreadFactory()
    );
    private final Runnable watchdog = this::runWatchdog;
    private TextView streamCountView;
    private LibVLC libVLC;
    private int liveStreamCount;
    private int totalStreamCount;
    private int pendingPlayerReleases;
    private boolean activityStarted;
    private volatile boolean destroyed;
    private boolean engineReleaseScheduled;
    private boolean playerReleaseFailed;
    private volatile boolean engineLeaseHeld;

    @Override
    protected void onCreate(@Nullable Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(COLOR_OBSIDIAN);
        getWindow().setNavigationBarColor(COLOR_OBSIDIAN);
        WindowInsetsControllerCompat insetsController = WindowCompat.getInsetsController(
            getWindow(), getWindow().getDecorView()
        );
        insetsController.setAppearanceLightStatusBars(false);
        insetsController.setAppearanceLightNavigationBars(false);

        ArrayList<String> urls = getIntent().getStringArrayListExtra(EXTRA_URLS);
        ArrayList<String> labels = getIntent().getStringArrayListExtra(EXTRA_LABELS);
        if (urls == null || urls.isEmpty()) {
            finish();
            return;
        }
        totalStreamCount = urls.size();

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(COLOR_OBSIDIAN);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, windowInsets) -> {
            Insets bars = windowInsets.getInsets(
                WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout()
            );
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return windowInsets;
        });
        root.addView(createToolbar(urls.size()), new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            dp(56)
        ));

        LinearLayout previewGrid = new LinearLayout(this);
        previewGrid.setOrientation(LinearLayout.VERTICAL);
        previewGrid.setPadding(dp(6), dp(3), dp(6), dp(6));
        root.addView(previewGrid, new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            0,
            1f
        ));

        int columns = urls.size() == 1 ? 1 : 2;
        int rowCount = (urls.size() + columns - 1) / columns;
        for (int rowIndex = 0; rowIndex < rowCount; rowIndex++) {
            LinearLayout row = new LinearLayout(this);
            row.setOrientation(LinearLayout.HORIZONTAL);
            previewGrid.addView(row, new LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                0,
                1f
            ));

            for (int columnIndex = 0; columnIndex < columns; columnIndex++) {
                int streamIndex = rowIndex * columns + columnIndex;
                if (streamIndex < urls.size()) {
                    String label = labels != null && streamIndex < labels.size()
                        ? labels.get(streamIndex)
                        : "CAM " + streamIndex;
                    row.addView(createStreamTile(streamIndex, urls.get(streamIndex), label), new LinearLayout.LayoutParams(
                        0,
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        1f
                    ));
                } else {
                    View spacer = new View(this);
                    row.addView(spacer, new LinearLayout.LayoutParams(
                        0,
                        LinearLayout.LayoutParams.MATCH_PARENT,
                        1f
                    ));
                }
            }
        }

        setContentView(root);
        requestEngineLease();
    }

    private void requestEngineLease() {
        boolean acquired;
        synchronized (ENGINE_GATE) {
            acquired = !engineLeased;
            if (acquired) {
                engineLeased = true;
                engineLeaseHeld = true;
            } else {
                ENGINE_WAITERS.addLast(new WeakReference<>(this));
            }
        }
        if (acquired) {
            initializeEngine();
        } else {
            for (StreamSession session : sessions) {
                setSessionState(session, "等待上一预览释放", Color.LTGRAY);
            }
            Log.i(TAG, "engine_wait queued=true");
        }
    }

    private void initializeEngine() {
        if (destroyed || !engineLeaseHeld || libVLC != null) {
            if (destroyed && engineLeaseHeld) releaseEngineLease();
            return;
        }
        try {
            libVLC = new LibVLC(getApplicationContext(), new ArrayList<>(Arrays.asList(
                "--no-audio",
                // Camera labels are native views. Avoid blocking video startup on font discovery.
                "--no-spu",
                "--no-osd",
                "--text-renderer=none",
                "--network-caching=300",
                "--rtsp-tcp",
                "--stats",
                "--no-video-title-show"
            )));
        } catch (RuntimeException error) {
            Log.e(TAG, "engine_create_failed", error);
            for (StreamSession session : sessions) {
                setSessionState(session, "播放器初始化失败", COLOR_ERROR);
            }
            releaseEngineLease();
            return;
        }
        Log.i(TAG, "engine=libvlc version=" + LibVLC.version()
            + " streams=" + totalStreamCount + " transport=tcp cacheMs=300");
        if (activityStarted) {
            for (StreamSession session : sessions) scheduleInitialStart(session);
        }
    }

    private void releaseEngineLease() {
        LivePreviewActivity next = null;
        synchronized (ENGINE_GATE) {
            if (!engineLeaseHeld) return;
            engineLeaseHeld = false;
            while (!ENGINE_WAITERS.isEmpty()) {
                LivePreviewActivity candidate = ENGINE_WAITERS.removeFirst().get();
                if (candidate != null && !candidate.destroyed) {
                    candidate.engineLeaseHeld = true;
                    next = candidate;
                    break;
                }
            }
            if (next == null) engineLeased = false;
        }
        if (next != null) {
            LivePreviewActivity nextOwner = next;
            nextOwner.mainHandler.post(() -> {
                Log.i(TAG, "engine_wait acquired=true");
                nextOwner.initializeEngine();
            });
        }
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        Log.i(TAG, "duplicate_preview_open ignored=true");
    }

    private View createToolbar(int streamCount) {
        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(18), 0, dp(12), 0);

        TextView title = new TextView(this);
        title.setText("SynCap  ·  LIVE");
        title.setTextColor(COLOR_GOLD);
        title.setTextSize(18);
        title.setTypeface(title.getTypeface(), android.graphics.Typeface.BOLD);
        toolbar.addView(title, new LinearLayout.LayoutParams(
            0,
            LinearLayout.LayoutParams.WRAP_CONTENT,
            1f
        ));

        streamCountView = new TextView(this);
        streamCountView.setText("0 / " + streamCount + " 路实时");
        streamCountView.setTextColor(Color.LTGRAY);
        streamCountView.setTextSize(13);
        streamCountView.setPadding(0, 0, dp(20), 0);
        toolbar.addView(streamCountView);

        TextView close = new TextView(this);
        close.setText("关闭");
        close.setTextColor(COLOR_GOLD);
        close.setTextSize(15);
        close.setGravity(Gravity.CENTER);
        close.setBackgroundColor(Color.rgb(24, 22, 16));
        close.setOnClickListener(view -> finish());
        toolbar.addView(close, new LinearLayout.LayoutParams(dp(72), dp(38)));
        return toolbar;
    }

    private View createStreamTile(int streamIndex, String url, String label) {
        FrameLayout tile = new FrameLayout(this);
        GradientDrawable tileBackground = new GradientDrawable();
        tileBackground.setColor(Color.BLACK);
        tileBackground.setStroke(dp(1), Color.rgb(94, 75, 35));
        tile.setBackground(tileBackground);
        LinearLayout.LayoutParams tileParams = new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT,
            LinearLayout.LayoutParams.MATCH_PARENT
        );
        tileParams.setMargins(dp(3), dp(3), dp(3), dp(3));
        tile.setLayoutParams(tileParams);
        tile.setPadding(dp(1), dp(1), dp(1), dp(1));

        VLCVideoLayout videoLayout = new VLCVideoLayout(this);
        tile.addView(videoLayout, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT,
            FrameLayout.LayoutParams.MATCH_PARENT
        ));

        TextView streamLabel = overlayText(label, COLOR_GOLD);
        tile.addView(streamLabel, wrapTop(Gravity.START));

        TextView state = overlayText("连接中", Color.LTGRAY);
        tile.addView(state, wrapTop(Gravity.END));

        StreamSession session = new StreamSession(streamIndex, url, label, videoLayout, state);
        tile.setOnClickListener(view -> retryManually(session));
        sessions.add(session);
        return tile;
    }

    private void buildAndPreparePlayer(StreamSession session) {
        if (!activityStarted || destroyed || libVLC == null || session.player != null || session.releasing) return;

        session.generation++;
        int generation = session.generation;
        session.lastDisplayedPictures = -1;
        session.lastDecodedVideo = -1;
        session.lastProgressRealtimeMs = SystemClock.elapsedRealtime();
        session.liveSinceRealtimeMs = 0;
        session.hasRenderedFrame = false;
        session.retryExhausted = false;
        setSessionLive(session, false);
        setSessionState(session, session.reconnectAttempt == 0 ? "连接中" : "正在重连", Color.LTGRAY);

        MediaPlayer player = new MediaPlayer(libVLC);
        session.player = player;
        player.setEventListener(event -> mainHandler.post(() -> {
            if (!isCurrentPlayer(session, player, generation)) return;
            handlePlayerEvent(session, event);
        }));
        player.attachViews(session.videoLayout, null, false, false);
        player.setVideoScale(MediaPlayer.ScaleType.SURFACE_BEST_FIT);

        Media media = new Media(libVLC, Uri.parse(session.url));
        media.setHWDecoderEnabled(true, false);
        media.addOption(":no-audio");
        media.addOption(":network-caching=300");
        if (session.url.startsWith("tcp://")) {
            // Raw camera TCP arrives in bursts without sender presentation timestamps.
            // Dropping every late frame can leave a healthy decoder permanently blank.
            media.addOption(":network-caching=600");
            media.addOption(":no-drop-late-frames");
            media.addOption(":demux=hevc");
            media.addOption(":hevc-fps=" + RAW_HEVC_FRAME_RATE);
        } else {
            media.addOption(":rtsp-tcp");
        }
        player.setMedia(media);
        media.release();
        player.play();
        Log.i(TAG, session.label + " prepare engine=libvlc transport="
            + (session.url.startsWith("tcp://") ? "tcp-hevc" : "rtsp-tcp")
            + " url=" + session.url);
    }

    private void handlePlayerEvent(StreamSession session, MediaPlayer.Event event) {
        if (event.type == MediaPlayer.Event.Opening) {
            setSessionState(session, "连接中", Color.LTGRAY);
        } else if (event.type == MediaPlayer.Event.Buffering) {
            if (!session.live) {
                setSessionState(session, "缓冲中 · " + Math.round(event.getBuffering()) + "%", Color.LTGRAY);
            }
        } else if (event.type == MediaPlayer.Event.Playing) {
            setSessionState(session, "等待画面", Color.LTGRAY);
            Log.i(TAG, session.label + " state=PLAYING");
        } else if (event.type == MediaPlayer.Event.EndReached) {
            scheduleReconnect(session, "stream_ended");
        } else if (event.type == MediaPlayer.Event.EncounteredError) {
            scheduleReconnect(session, "player_error");
        }
    }

    private void runWatchdog() {
        if (!activityStarted || destroyed) return;
        if (libVLC == null) {
            Log.i(TAG, "summary live=0/" + totalStreamCount + " engine=waiting");
            mainHandler.postDelayed(watchdog, WATCHDOG_INTERVAL_MS);
            return;
        }
        for (StreamSession session : sessions) {
            MediaPlayer player = session.player;
            if (player == null) {
                if (!session.releasing && session.retryRunnable == null && !session.retryExhausted) {
                    scheduleReconnect(session, "missing_player");
                }
                continue;
            }
            requestHealthSample(session, player, session.generation);
        }
        Runtime runtime = Runtime.getRuntime();
        long usedHeapMb = (runtime.totalMemory() - runtime.freeMemory()) / (1024 * 1024);
        long nativeHeapMb = Debug.getNativeHeapAllocatedSize() / (1024 * 1024);
        Log.i(TAG, "summary live=" + liveStreamCount + "/" + totalStreamCount
            + " javaHeapMb=" + usedHeapMb + " nativeHeapMb=" + nativeHeapMb);
        mainHandler.postDelayed(watchdog, WATCHDOG_INTERVAL_MS);
    }

    private void requestHealthSample(StreamSession session, MediaPlayer player, int generation) {
        if (session.statsInFlight) return;
        session.statsInFlight = true;
        long queuedMs = SystemClock.elapsedRealtime();
        executePlayerTask(session, () -> {
            long startedMs = SystemClock.elapsedRealtime();
            StreamStats stats = readStats(player);
            long completedMs = SystemClock.elapsedRealtime();
            long queueDurationMs = startedMs - queuedMs;
            long statsDurationMs = completedMs - startedMs;
            mainHandler.post(() -> {
                session.statsInFlight = false;
                if (!isCurrentPlayer(session, player, generation)) return;
                handleHealthSample(session, stats, completedMs, queueDurationMs, statsDurationMs);
            });
        });
    }

    private void handleHealthSample(
        StreamSession session,
        @Nullable StreamStats stats,
        long completedMs,
        long queueDurationMs,
        long statsDurationMs
    ) {
        if (queueDurationMs >= 250 || statsDurationMs >= 250) {
            Log.w(TAG, session.label + " health_stats_slow queueMs=" + queueDurationMs
                + " durationMs=" + statsDurationMs);
        }
        int displayedPictures = stats == null ? -1 : stats.displayedPictures;
        int decodedVideo = stats == null ? -1 : stats.decodedVideo;
        boolean frameAdvanced = displayedPictures >= 0
            && (session.lastDisplayedPictures < 0 || displayedPictures > session.lastDisplayedPictures);
        if (frameAdvanced && displayedPictures > 0) {
            session.hasRenderedFrame = true;
            markFrameProgress(session, completedMs);
        }

        long staleMs = completedMs - session.lastProgressRealtimeMs;
        if (session.live && staleMs >= STALE_LABEL_TIMEOUT_MS) {
            setSessionLive(session, false);
            setSessionState(session, "画面停滞", COLOR_ERROR);
        }
        long stallTimeoutMs = session.hasRenderedFrame
            ? PLAYING_STALL_TIMEOUT_MS
            : STARTUP_STALL_TIMEOUT_MS;
        Log.i(TAG, session.label
            + " health engine=libvlc"
            + " displayedPictures=" + displayedPictures
            + " decodedVideo=" + decodedVideo
            + " lostPictures=" + (stats == null ? -1 : stats.lostPictures)
            + " demuxBytes=" + (stats == null ? -1 : stats.demuxReadBytes)
            + " statsQueueMs=" + queueDurationMs
            + " statsMs=" + statsDurationMs
            + " staleMs=" + staleMs
            + " retry=" + session.reconnectAttempt);

        session.lastDisplayedPictures = displayedPictures;
        session.lastDecodedVideo = decodedVideo;
        if (staleMs >= stallTimeoutMs) {
            scheduleReconnect(session, "frame_stalled:" + staleMs + "ms");
        }
    }

    @Nullable
    private StreamStats readStats(MediaPlayer player) {
        IMedia media = null;
        try {
            media = player.getMedia();
            if (media == null) return null;
            IMedia.Stats stats = media.getStats();
            if (stats == null) return null;
            return new StreamStats(
                stats.displayedPictures,
                stats.decodedVideo,
                stats.lostPictures,
                stats.demuxReadBytes
            );
        } catch (RuntimeException error) {
            Log.w(TAG, "Unable to read LibVLC statistics", error);
            return null;
        } finally {
            if (media != null) media.release();
        }
    }

    private void markFrameProgress(StreamSession session, long now) {
        session.lastProgressRealtimeMs = now;
        if (!session.live) session.liveSinceRealtimeMs = now;
        setSessionLive(session, true);
        setSessionState(session, "● 实时", COLOR_HEALTHY);
        if (PreviewReconnectPolicy.resetRetryBudgetAfterStablePlayback(session.url)
            && session.reconnectAttempt > 0
            && session.liveSinceRealtimeMs > 0
            && now - session.liveSinceRealtimeMs >= STABLE_PLAYBACK_RESET_MS) {
            session.reconnectAttempt = 0;
        }
    }

    private void scheduleReconnect(StreamSession session, String reason) {
        if (!activityStarted || destroyed || session.retryRunnable != null
            || session.retryExhausted || session.releasing) return;
        setSessionLive(session, false);
        if (session.reconnectAttempt >= PreviewReconnectPolicy.automaticRetryLimit(session.url)) {
            session.retryExhausted = true;
            setSessionState(session, "连接失败 · 点击重试", COLOR_ERROR);
            Log.e(TAG, session.label + " reconnect_exhausted reason=" + reason);
            releasePlayerAsync(session, null);
            return;
        }

        session.reconnectAttempt++;
        long retryDelayMs = PreviewReconnectPolicy.automaticRetryDelayMs(
            session.url,
            session.reconnectAttempt,
            session.index
        );
        setSessionState(session, "正在恢复", COLOR_ERROR);
        Log.w(TAG, session.label + " reconnect reason=" + reason
            + " delayMs=" + retryDelayMs + " attempt=" + session.reconnectAttempt);
        releasePlayerAsync(session, () -> {
            if (!activityStarted || destroyed) return;
            setSessionState(session, "重连中 · " + Math.max(1, retryDelayMs / 1_000) + "s", COLOR_ERROR);
            Runnable retry = () -> {
                session.retryRunnable = null;
                buildAndPreparePlayer(session);
            };
            session.retryRunnable = retry;
            mainHandler.postDelayed(retry, retryDelayMs);
        });
    }

    private void retryManually(StreamSession session) {
        if (!activityStarted || destroyed || session.releasing || !session.retryExhausted) return;
        if (session.retryRunnable != null) {
            mainHandler.removeCallbacks(session.retryRunnable);
            session.retryRunnable = null;
        }
        session.reconnectAttempt = 0;
        session.retryExhausted = false;
        setSessionState(session, "正在恢复", Color.LTGRAY);
        releasePlayerAsync(session, () -> {
            if (activityStarted && !destroyed) scheduleInitialStart(session);
        });
    }

    private void scheduleInitialStart(StreamSession session) {
        if (!activityStarted || destroyed || libVLC == null || session.player != null
            || session.retryRunnable != null || session.releasing) return;
        long delayMs = (long) session.index * START_STAGGER_MS;
        Runnable start = () -> {
            session.retryRunnable = null;
            buildAndPreparePlayer(session);
        };
        session.retryRunnable = start;
        mainHandler.postDelayed(start, delayMs);
    }

    private void releasePlayerAsync(StreamSession session, @Nullable Runnable afterRelease) {
        MediaPlayer player = session.player;
        session.player = null;
        session.generation++;
        if (player == null) {
            if (!session.releasing && afterRelease != null) afterRelease.run();
            maybeShutdownPlayerExecutor(session);
            return;
        }
        session.releasing = true;
        pendingPlayerReleases++;
        try {
            player.setEventListener(null);
        } catch (RuntimeException error) {
            Log.w(TAG, session.label + " clear_listener_failed", error);
        }
        long releaseStartedMs = SystemClock.elapsedRealtime();
        Log.i(TAG, session.label + " teardown_start pending=" + pendingPlayerReleases);
        executePlayerTask(session, () -> {
            try {
                player.stop();
            } catch (RuntimeException error) {
                Log.w(TAG, session.label + " stop_failed", error);
            }
            mainHandler.post(() -> {
                long detachStartedMs = SystemClock.elapsedRealtime();
                Log.i(TAG, session.label + " detach_start");
                try {
                    player.detachViews();
                } catch (RuntimeException error) {
                    Log.w(TAG, session.label + " detach_failed", error);
                }
                Log.i(TAG, session.label + " detach_done durationMs="
                    + (SystemClock.elapsedRealtime() - detachStartedMs));
                executePlayerTask(session, () -> {
                    boolean released = true;
                    try {
                        player.release();
                    } catch (RuntimeException error) {
                        released = false;
                        Log.w(TAG, session.label + " release_failed", error);
                    }
                    boolean releaseSucceeded = released;
                    Log.i(TAG, session.label + " teardown_done durationMs="
                        + (SystemClock.elapsedRealtime() - releaseStartedMs));
                    mainHandler.post(() -> completePlayerRelease(
                        session,
                        afterRelease,
                        releaseSucceeded
                    ));
                });
            });
        });
    }

    private void completePlayerRelease(
        StreamSession session,
        @Nullable Runnable afterRelease,
        boolean releaseSucceeded
    ) {
        pendingPlayerReleases = Math.max(0, pendingPlayerReleases - 1);
        session.releasing = false;
        if (!releaseSucceeded) playerReleaseFailed = true;
        if (releaseSucceeded) {
            if (afterRelease != null) afterRelease.run();
        } else if (!destroyed) {
            session.retryExhausted = true;
            setSessionState(session, "播放器恢复失败 · 请关闭预览", COLOR_ERROR);
        }
        maybeShutdownPlayerExecutor(session);
        maybeReleaseEngine();
    }

    private void executePlayerTask(StreamSession session, Runnable task) {
        try {
            session.playerExecutor.execute(task);
        } catch (RejectedExecutionException error) {
            Log.e(TAG, session.label + " player_executor_rejected; using isolated daemon thread", error);
            new PlayerReleaseThreadFactory().newThread(task).start();
        }
    }

    private void executeEngineReleaseTask(Runnable task) {
        try {
            engineReleaseExecutor.execute(task);
        } catch (RejectedExecutionException error) {
            Log.e(TAG, "engine_executor_rejected; using isolated daemon thread", error);
            new PlayerReleaseThreadFactory().newThread(task).start();
        }
    }

    private void maybeShutdownPlayerExecutor(StreamSession session) {
        if (destroyed && session.player == null && !session.releasing) {
            session.playerExecutor.shutdown();
        }
    }

    private void maybeReleaseEngine() {
        if (!destroyed || engineReleaseScheduled || pendingPlayerReleases > 0) return;
        engineReleaseScheduled = true;
        if (playerReleaseFailed) {
            Log.e(TAG, "engine_release_skipped because a player did not release cleanly");
            engineReleaseExecutor.shutdown();
            return;
        }
        LibVLC engine = libVLC;
        libVLC = null;
        if (engine == null) {
            engineReleaseExecutor.shutdown();
            releaseEngineLease();
            return;
        }
        executeEngineReleaseTask(() -> {
            long startedMs = SystemClock.elapsedRealtime();
            boolean released = true;
            try {
                engine.release();
            } catch (RuntimeException error) {
                released = false;
                Log.w(TAG, "engine_release_failed", error);
            } finally {
                Log.i(TAG, "engine_release_done durationMs="
                    + (SystemClock.elapsedRealtime() - startedMs));
                engineReleaseExecutor.shutdown();
                if (released) {
                    releaseEngineLease();
                } else {
                    Log.e(TAG, "engine_gate retained after release failure");
                }
            }
        });
    }

    private boolean isCurrentPlayer(StreamSession session, MediaPlayer player, int generation) {
        return session.player == player && session.generation == generation && !destroyed;
    }

    private void setSessionLive(StreamSession session, boolean live) {
        if (session.live == live) return;
        session.live = live;
        liveStreamCount += live ? 1 : -1;
        liveStreamCount = Math.max(0, Math.min(totalStreamCount, liveStreamCount));
        updateStreamCount();
    }

    private void setSessionState(StreamSession session, String value, int color) {
        session.stateView.setText(value);
        session.stateView.setTextColor(color);
    }

    private void updateStreamCount() {
        if (streamCountView == null) return;
        streamCountView.setText(liveStreamCount + " / " + totalStreamCount + " 路实时");
        streamCountView.setTextColor(liveStreamCount > 0 ? COLOR_HEALTHY : Color.LTGRAY);
    }

    private TextView overlayText(String value, int color) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextColor(color);
        text.setTextSize(13);
        text.setTypeface(text.getTypeface(), android.graphics.Typeface.BOLD);
        text.setPadding(dp(10), dp(7), dp(10), dp(7));
        text.setBackgroundColor(Color.argb(190, 0, 0, 0));
        return text;
    }

    private FrameLayout.LayoutParams wrapTop(int horizontalGravity) {
        FrameLayout.LayoutParams params = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT,
            FrameLayout.LayoutParams.WRAP_CONTENT
        );
        params.gravity = Gravity.TOP | horizontalGravity;
        params.setMargins(dp(8), dp(8), dp(8), 0);
        return params;
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onStart() {
        super.onStart();
        activityStarted = true;
        for (StreamSession session : sessions) scheduleInitialStart(session);
        mainHandler.removeCallbacks(watchdog);
        mainHandler.postDelayed(watchdog, WATCHDOG_INTERVAL_MS);
    }

    @Override
    protected void onStop() {
        activityStarted = false;
        mainHandler.removeCallbacks(watchdog);
        for (StreamSession session : sessions) {
            if (session.retryRunnable != null) {
                mainHandler.removeCallbacks(session.retryRunnable);
                session.retryRunnable = null;
            }
            setSessionLive(session, false);
            releasePlayerAsync(session, () -> {
                if (activityStarted && !destroyed) scheduleInitialStart(session);
            });
            session.reconnectAttempt = 0;
            session.retryExhausted = false;
            setSessionState(session, "已暂停", Color.LTGRAY);
        }
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        destroyed = true;
        mainHandler.removeCallbacks(watchdog);
        for (StreamSession session : sessions) {
            if (session.retryRunnable != null) {
                mainHandler.removeCallbacks(session.retryRunnable);
                session.retryRunnable = null;
            }
        }
        for (StreamSession session : sessions) releasePlayerAsync(session, null);
        sessions.clear();
        maybeReleaseEngine();
        super.onDestroy();
    }

    private static final class StreamSession {
        final int index;
        final String url;
        final String label;
        final VLCVideoLayout videoLayout;
        final TextView stateView;
        final ExecutorService playerExecutor = Executors.newSingleThreadExecutor(
            new PlayerReleaseThreadFactory()
        );
        MediaPlayer player;
        Runnable retryRunnable;
        int generation;
        int reconnectAttempt;
        int lastDisplayedPictures = -1;
        int lastDecodedVideo = -1;
        long lastProgressRealtimeMs;
        long liveSinceRealtimeMs;
        boolean hasRenderedFrame;
        boolean retryExhausted;
        boolean releasing;
        boolean statsInFlight;
        boolean live;

        StreamSession(int index, String url, String label, VLCVideoLayout videoLayout, TextView stateView) {
            this.index = index;
            this.url = url;
            this.label = label;
            this.videoLayout = videoLayout;
            this.stateView = stateView;
        }
    }

    private static final class StreamStats {
        final int displayedPictures;
        final int decodedVideo;
        final int lostPictures;
        final int demuxReadBytes;

        StreamStats(int displayedPictures, int decodedVideo, int lostPictures, int demuxReadBytes) {
            this.displayedPictures = displayedPictures;
            this.decodedVideo = decodedVideo;
            this.lostPictures = lostPictures;
            this.demuxReadBytes = demuxReadBytes;
        }
    }

    private static final class PlayerReleaseThreadFactory implements ThreadFactory {
        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(
                runnable,
                "SynCap-VLC-Release-" + RELEASE_THREAD_SEQUENCE.incrementAndGet()
            );
            thread.setDaemon(true);
            return thread;
        }
    }
}
