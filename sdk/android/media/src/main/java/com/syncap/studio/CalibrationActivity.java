package com.syncap.studio;

import android.content.Intent;
import android.graphics.Bitmap;
import android.graphics.Color;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.util.Log;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.view.WindowManager;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.annotation.Nullable;
import androidx.appcompat.app.AppCompatActivity;
import androidx.core.content.FileProvider;
import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import org.json.JSONArray;
import org.json.JSONObject;
import org.opencv.android.OpenCVLoader;
import org.opencv.android.Utils;
import org.opencv.core.Core;
import org.opencv.core.CvType;
import org.opencv.core.Mat;
import org.opencv.imgproc.Imgproc;
import org.videolan.libvlc.LibVLC;
import org.videolan.libvlc.Media;
import org.videolan.libvlc.MediaPlayer;
import org.videolan.libvlc.util.VLCVideoLayout;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.text.SimpleDateFormat;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Date;
import java.util.List;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.RejectedExecutionException;
import java.util.concurrent.ThreadFactory;
import java.util.concurrent.atomic.AtomicInteger;

public class CalibrationActivity extends AppCompatActivity {
    public static final String EXTRA_URLS = "syncap.calibration.urls";
    public static final String EXTRA_LABELS = "syncap.calibration.labels";
    public static final String EXTRA_CAMERA_IDS = "syncap.calibration.camera_ids";
    public static final String EXTRA_PAIR = "syncap.calibration.pair";
    public static final String EXTRA_SQUARES_LONG = "syncap.calibration.squares_long";
    public static final String EXTRA_SQUARES_WIDE = "syncap.calibration.squares_wide";
    public static final String EXTRA_SQUARE_SIZE_MM = "syncap.calibration.square_size_mm";

    private static final String TAG = "SynCapCalibration";
    private static final int COLOR_OBSIDIAN = Color.rgb(5, 5, 5);
    private static final int COLOR_SURFACE = Color.rgb(17, 17, 15);
    private static final int COLOR_GOLD = Color.rgb(225, 188, 92);
    private static final int COLOR_LINE = Color.rgb(90, 73, 39);
    private static final int COLOR_HEALTHY = Color.rgb(66, 200, 75);
    private static final int COLOR_ERROR = Color.rgb(231, 72, 72);
    private static final AtomicInteger THREAD_SEQUENCE = new AtomicInteger();

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final ExecutorService calibrationExecutor = Executors.newSingleThreadExecutor(
        new NamedThreadFactory("SynCap-Calibration-")
    );
    private final ExecutorService releaseExecutor = Executors.newSingleThreadExecutor(
        new NamedThreadFactory("SynCap-Calibration-Release-")
    );
    private final List<CameraCalibrationEngine.Sample> samples = new ArrayList<>();
    private final VLCVideoLayout[] videoLayouts = new VLCVideoLayout[2];
    private final MediaPlayer[] players = new MediaPlayer[2];
    private final boolean[] playerStarted = new boolean[2];

    private String[] urls;
    private String[] labels;
    private String[] cameraIds;
    private String deviceHost;
    private String pair;
    private int squaresLong;
    private int squaresWide;
    private double squareSizeMm;
    private int calibrationRotationDegrees = -1;
    private String calibrationId;
    private File runDirectory;
    private File imagesDirectory;
    private File resultFile;
    private LibVLC libVLC;
    private TextView progressView;
    private TextView statusView;
    private TextView coverageView;
    private TextView captureButton;
    private TextView solveButton;
    private TextView shareButton;
    private boolean captureInFlight;
    private boolean solveInFlight;
    private boolean destroyed;
    private boolean releaseStarted;

    @Override
    protected void onCreate(@Nullable Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        if (!readAndValidateIntent()) {
            Log.e(TAG, "Invalid calibration inputs");
            finish();
            return;
        }
        if (!OpenCVLoader.initLocal()) {
            Log.e(TAG, "Unable to initialize OpenCV");
            finish();
            return;
        }
        Core.setUseOptimized(false);
        Core.setNumThreads(1);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        getWindow().setStatusBarColor(COLOR_OBSIDIAN);
        getWindow().setNavigationBarColor(COLOR_OBSIDIAN);
        WindowInsetsControllerCompat controller = WindowCompat.getInsetsController(
            getWindow(), getWindow().getDecorView()
        );
        controller.setAppearanceLightStatusBars(false);
        controller.setAppearanceLightNavigationBars(false);

        createRunDirectory();
        setContentView(createContentView());
        startStreams();
    }

    private boolean readAndValidateIntent() {
        ArrayList<String> urlList = stringListExtra(EXTRA_URLS);
        ArrayList<String> labelList = stringListExtra(EXTRA_LABELS);
        ArrayList<String> idList = stringListExtra(EXTRA_CAMERA_IDS);
        pair = getIntent().getStringExtra(EXTRA_PAIR);
        squaresLong = getIntent().getIntExtra(EXTRA_SQUARES_LONG, 0);
        squaresWide = getIntent().getIntExtra(EXTRA_SQUARES_WIDE, 0);
        Object squareSizeValue = getIntent().getExtras() == null
            ? null
            : getIntent().getExtras().get(EXTRA_SQUARE_SIZE_MM);
        squareSizeMm = squareSizeValue instanceof Number
            ? ((Number) squareSizeValue).doubleValue()
            : 0;
        if (urlList == null || urlList.size() != 2 || idList == null || idList.size() != 2) {
            Log.e(TAG, "Expected two URLs and two camera ids");
            return false;
        }
        if (!("pair14".equals(pair) || "pair23".equals(pair))) {
            Log.e(TAG, "Invalid pair " + pair);
            return false;
        }
        if (squaresLong < 4 || squaresLong > 20 || squaresWide < 4 || squaresWide > 20) {
            Log.e(TAG, "Invalid board counts " + squaresLong + "x" + squaresWide);
            return false;
        }
        if (!Double.isFinite(squareSizeMm) || squareSizeMm < 1 || squareSizeMm > 200) {
            Log.e(TAG, "Invalid square size " + squareSizeMm);
            return false;
        }
        for (String url : urlList) {
            if (url == null || !url.startsWith("rtsp://")) {
                Log.e(TAG, "Invalid RTSP URL");
                return false;
            }
        }
        urls = urlList.toArray(new String[0]);
        deviceHost = Uri.parse(urls[0]).getHost();
        if (deviceHost == null || deviceHost.trim().isEmpty()) {
            Log.e(TAG, "Calibration device host is missing");
            return false;
        }
        cameraIds = idList.toArray(new String[0]);
        labels = new String[] {
            labelList != null && labelList.size() > 0 ? labelList.get(0) : cameraIds[0],
            labelList != null && labelList.size() > 1 ? labelList.get(1) : cameraIds[1]
        };
        return true;
    }

    @Nullable
    private ArrayList<String> stringListExtra(String key) {
        ArrayList<String> values = getIntent().getStringArrayListExtra(key);
        if (values != null) return values;
        String[] array = getIntent().getStringArrayExtra(key);
        return array == null ? null : new ArrayList<>(Arrays.asList(array));
    }

    private void createRunDirectory() {
        String stamp = new SimpleDateFormat("yyyyMMdd_HHmmss", Locale.US).format(new Date());
        calibrationId = "cal_" + pair + "_" + stamp;
        File base = getExternalFilesDir("calibrations");
        if (base == null) base = new File(getFilesDir(), "calibrations");
        runDirectory = new File(base, calibrationId);
        imagesDirectory = new File(runDirectory, "images");
        if (!imagesDirectory.mkdirs() && !imagesDirectory.isDirectory()) {
            Log.e(TAG, "Unable to create calibration directory " + imagesDirectory);
        }
    }

    private View createContentView() {
        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setBackgroundColor(COLOR_OBSIDIAN);
        ViewCompat.setOnApplyWindowInsetsListener(root, (view, insets) -> {
            Insets bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout()
            );
            view.setPadding(bars.left, bars.top, bars.right, bars.bottom);
            return insets;
        });
        root.addView(createToolbar(), new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, dp(54)
        ));
        root.addView(createInstructionRow(), new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, dp(58)
        ));
        root.addView(createPreviewRow(), new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, 0, 1f
        ));
        root.addView(createControlRow(), new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, dp(72)
        ));
        return root;
    }

    private View createToolbar() {
        LinearLayout toolbar = new LinearLayout(this);
        toolbar.setGravity(Gravity.CENTER_VERTICAL);
        toolbar.setPadding(dp(16), 0, dp(12), 0);

        TextView title = new TextView(this);
        title.setText("SynCap  ·  双目棋盘格标定");
        title.setTextColor(COLOR_GOLD);
        title.setTextSize(18);
        title.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        toolbar.addView(title, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        TextView board = new TextView(this);
        board.setText(squaresLong + " × " + squaresWide + " 格  ·  " + format(squareSizeMm) + " mm");
        board.setTextColor(Color.LTGRAY);
        board.setTextSize(12);
        board.setPadding(0, 0, dp(18), 0);
        toolbar.addView(board);

        TextView close = actionButton("关闭", false);
        close.setOnClickListener(view -> finish());
        toolbar.addView(close, new LinearLayout.LayoutParams(dp(72), dp(38)));
        return toolbar;
    }

    private View createInstructionRow() {
        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(16), dp(6), dp(16), dp(6));
        row.setBackgroundColor(Color.rgb(11, 11, 10));

        statusView = new TextView(this);
        statusView.setText("正在连接双路画面 · 标定板移动后请停稳再拍");
        statusView.setTextColor(Color.LTGRAY);
        statusView.setTextSize(12);
        row.addView(statusView, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        coverageView = new TextView(this);
        coverageView.setText("0/9 区域");
        coverageView.setTextColor(Color.LTGRAY);
        coverageView.setTextSize(12);
        coverageView.setPadding(0, 0, dp(22), 0);
        row.addView(coverageView);

        progressView = new TextView(this);
        progressView.setText("0 / " + CameraCalibrationEngine.TARGET_SAMPLES + " 组有效");
        progressView.setTextColor(COLOR_GOLD);
        progressView.setTextSize(14);
        progressView.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        row.addView(progressView);
        return row;
    }

    private View createPreviewRow() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setPadding(dp(7), dp(5), dp(7), dp(5));
        for (int index = 0; index < 2; index++) {
            FrameLayout tile = new FrameLayout(this);
            GradientDrawable background = new GradientDrawable();
            background.setColor(Color.BLACK);
            background.setStroke(dp(1), COLOR_LINE);
            tile.setBackground(background);
            LinearLayout.LayoutParams tileParams = new LinearLayout.LayoutParams(
                0, LinearLayout.LayoutParams.MATCH_PARENT, 1f
            );
            tileParams.setMargins(dp(3), 0, dp(3), 0);
            row.addView(tile, tileParams);

            VLCVideoLayout layout = new VLCVideoLayout(this);
            videoLayouts[index] = layout;
            tile.addView(layout, new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT
            ));

            TextView label = overlay(labels[index], COLOR_GOLD);
            FrameLayout.LayoutParams labelParams = new FrameLayout.LayoutParams(
                FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT
            );
            labelParams.gravity = Gravity.TOP | Gravity.START;
            labelParams.setMargins(dp(8), dp(8), 0, 0);
            tile.addView(label, labelParams);
        }
        return row;
    }

    private View createControlRow() {
        LinearLayout row = new LinearLayout(this);
        row.setGravity(Gravity.CENTER_VERTICAL);
        row.setPadding(dp(16), dp(9), dp(16), dp(9));
        row.setBackgroundColor(Color.rgb(11, 11, 10));

        TextView hint = new TextView(this);
        hint.setText("拍摄建议：中央 → 四角 → 远近 → 左右倾斜\n两路必须同时看到完整棋盘格");
        hint.setTextColor(Color.LTGRAY);
        hint.setTextSize(10);
        row.addView(hint, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f));

        shareButton = actionButton("分享结果", false);
        shareButton.setVisibility(View.GONE);
        shareButton.setOnClickListener(view -> shareResult());
        row.addView(shareButton, controlButtonParams());

        solveButton = actionButton("计算标定", false);
        solveButton.setEnabled(false);
        solveButton.setAlpha(0.45f);
        solveButton.setOnClickListener(view -> solveCalibration());
        row.addView(solveButton, controlButtonParams());

        captureButton = actionButton("拍摄一组", true);
        captureButton.setEnabled(false);
        captureButton.setAlpha(0.45f);
        captureButton.setOnClickListener(view -> capturePair());
        row.addView(captureButton, controlButtonParams());
        return row;
    }

    private LinearLayout.LayoutParams controlButtonParams() {
        LinearLayout.LayoutParams params = new LinearLayout.LayoutParams(dp(112), dp(48));
        params.setMargins(dp(8), 0, 0, 0);
        return params;
    }

    private void startStreams() {
        libVLC = new LibVLC(getApplicationContext(), new ArrayList<>(Arrays.asList(
            "--no-audio", "--network-caching=300", "--rtsp-tcp", "--no-video-title-show"
        )));
        for (int index = 0; index < 2; index++) {
            final int streamIndex = index;
            MediaPlayer player = new MediaPlayer(libVLC);
            players[index] = player;
            player.setEventListener(event -> mainHandler.post(() -> {
                if (destroyed || players[streamIndex] != player) return;
                if (event.type == MediaPlayer.Event.Playing) {
                    playerStarted[streamIndex] = true;
                    updateStreamReadiness();
                } else if (event.type == MediaPlayer.Event.EncounteredError) {
                    playerStarted[streamIndex] = false;
                    setStatus(labels[streamIndex] + " 连接失败，请检查设备网络", COLOR_ERROR);
                    updateStreamReadiness();
                }
            }));
            player.attachViews(videoLayouts[index], null, false, true);
            player.setVideoScale(MediaPlayer.ScaleType.SURFACE_BEST_FIT);
            Media media = new Media(libVLC, Uri.parse(urls[index]));
            media.setHWDecoderEnabled(true, false);
            media.addOption(":no-audio");
            media.addOption(":network-caching=300");
            media.addOption(":rtsp-tcp");
            player.setMedia(media);
            media.release();
            player.play();
        }
    }

    private void updateStreamReadiness() {
        boolean ready = playerStarted[0] && playerStarted[1] && !captureInFlight && !solveInFlight;
        captureButton.setEnabled(ready);
        captureButton.setAlpha(ready ? 1f : 0.45f);
        if (playerStarted[0] && playerStarted[1]) {
            setStatus("双路已就绪 · 移动标定板后停稳，再拍摄一组", COLOR_HEALTHY);
        }
    }

    private void capturePair() {
        if (captureInFlight || solveInFlight || !playerStarted[0] || !playerStarted[1]) return;
        captureInFlight = true;
        captureButton.setEnabled(false);
        captureButton.setAlpha(0.45f);
        captureButton.setText("正在抓取");
        setStatus("正在从头环提取同一硬件同步组的全分辨率原始帧…", COLOR_GOLD);
        calibrationExecutor.execute(() -> {
            try {
                DeviceSnapshot snapshot = captureDeviceSnapshot();
                processCapturedPair(snapshot);
            } catch (Exception error) {
                Log.e(TAG, "device_snapshot_failed", error);
                mainHandler.post(() -> {
                    if (destroyed) return;
                    captureInFlight = false;
                    captureButton.setText("拍摄一组");
                    updateStreamReadiness();
                    setStatus("设备同步原图抓取失败：" + safeMessage(error), COLOR_ERROR);
                });
            }
        });
    }

    private void processCapturedPair(DeviceSnapshot snapshot) {
        Bitmap leftBitmap = snapshot.left;
        Bitmap rightBitmap = snapshot.right;
        CameraCalibrationEngine.Detection left = null;
        CameraCalibrationEngine.Detection right = null;
        String rejection = null;
        boolean accepted = false;
        try {
            JSONObject cameraConfiguration = snapshot.metadata.optJSONObject("cameraConfiguration");
            int snapshotRotation = cameraConfiguration == null
                ? 0 : cameraConfiguration.optInt("rotationDegrees", 0);
            if (calibrationRotationDegrees >= 0 && calibrationRotationDegrees != snapshotRotation) {
                rejection = "图像方向已在标定过程中改变，请关闭后重新开始标定";
            } else {
                calibrationRotationDegrees = snapshotRotation;
                left = CameraCalibrationEngine.detect(leftBitmap, squaresLong - 1, squaresWide - 1);
                right = CameraCalibrationEngine.detect(rightBitmap, squaresLong - 1, squaresWide - 1);
            }
            if (rejection != null) {
                // Keep the existing samples unchanged; mixed orientations must never enter one solve.
            } else if (!left.accepted) rejection = labels[0] + "：" + left.reason;
            else if (!right.accepted) rejection = labels[1] + "：" + right.reason;
            else if (CameraCalibrationEngine.isDuplicate(samples, left, right)) rejection = "这个姿态和已有照片太接近，请移动或倾斜标定板";
            else {
                int sampleNumber = samples.size() + 1;
                savePng(leftBitmap, new File(imagesDirectory, String.format(Locale.US, "%02d_%s.png", sampleNumber, cameraIds[0])));
                savePng(rightBitmap, new File(imagesDirectory, String.format(Locale.US, "%02d_%s.png", sampleNumber, cameraIds[1])));
                copyFile(snapshot.leftRaw, new File(imagesDirectory, String.format(Locale.US, "%02d_%s.nv12", sampleNumber, cameraIds[0])));
                copyFile(snapshot.rightRaw, new File(imagesDirectory, String.format(Locale.US, "%02d_%s.nv12", sampleNumber, cameraIds[1])));
                writeUtf8(
                    new File(imagesDirectory, String.format(Locale.US, "%02d_sync.json", sampleNumber)),
                    snapshot.metadata.toString()
                );
                samples.add(CameraCalibrationEngine.createSample(left, right));
                left = null;
                right = null;
                accepted = true;
            }
        } catch (RuntimeException | IOException error) {
            Log.e(TAG, "capture_pair_failed", error);
            rejection = "处理照片失败，请重试";
        } finally {
            if (left != null) left.release();
            if (right != null) right.release();
            leftBitmap.recycle();
            rightBitmap.recycle();
            deleteTemporary(snapshot.leftRaw);
            deleteTemporary(snapshot.rightRaw);
        }

        boolean acceptedResult = accepted;
        String rejectionResult = rejection;
        CameraCalibrationEngine.Coverage coverage = CameraCalibrationEngine.coverage(samples);
        boolean enough = CameraCalibrationEngine.canSolve(samples);
        int count = samples.size();
        mainHandler.post(() -> {
            if (destroyed) return;
            captureInFlight = false;
            captureButton.setText("拍摄一组");
            updateStreamReadiness();
            progressView.setText(count + " / " + CameraCalibrationEngine.TARGET_SAMPLES + " 组有效");
            progressView.setTextColor(enough ? COLOR_HEALTHY : COLOR_GOLD);
            coverageView.setText(coverage.summary());
            solveButton.setEnabled(enough);
            solveButton.setAlpha(enough ? 1f : 0.45f);
            if (acceptedResult) {
                String next = count >= CameraCalibrationEngine.TARGET_SAMPLES && enough
                    ? "样本已充足，可以计算标定"
                    : nextCaptureHint(coverage, count);
                setStatus("已接受第 " + count + " 组 · " + next, COLOR_HEALTHY);
            } else {
                setStatus(rejectionResult == null ? "照片未通过检查，请重试" : rejectionResult, COLOR_ERROR);
            }
        });
    }

    private String nextCaptureHint(CameraCalibrationEngine.Coverage coverage, int count) {
        if (coverage.zones < CameraCalibrationEngine.MINIMUM_ZONES) return "下一张请移动到画面另一角";
        if (coverage.scaleRatio < 1.35) return "下一张请明显靠近或远离相机";
        if (count < CameraCalibrationEngine.MINIMUM_SAMPLES) return "继续改变倾斜方向";
        return "已可计算，建议拍满 24 组获得更稳结果";
    }

    private void solveCalibration() {
        if (solveInFlight || captureInFlight || !CameraCalibrationEngine.canSolve(samples)) return;
        solveInFlight = true;
        captureButton.setEnabled(false);
        solveButton.setEnabled(false);
        captureButton.setAlpha(0.45f);
        solveButton.setAlpha(0.45f);
        solveButton.setText("正在计算");
        setStatus("正在计算两台相机的内参、畸变和相对位置…", COLOR_GOLD);
        calibrationExecutor.execute(() -> {
            CameraCalibrationEngine.CalibrationResult result = null;
            try {
                result = CameraCalibrationEngine.calibrate(samples, squaresLong - 1, squaresWide - 1, squareSizeMm);
                JSONObject json = CameraCalibrationEngine.toJson(
                    result, calibrationId, pair, cameraIds,
                    squaresLong, squaresWide, squareSizeMm, samples.size(),
                    Math.max(0, calibrationRotationDegrees), System.currentTimeMillis()
                );
                resultFile = new File(runDirectory, "calibration.json");
                writeUtf8(resultFile, json.toString(2));
                double stereoRms = result.stereoRms;
                double baselineMm = result.baselineMeters * 1000.0;
                mainHandler.post(() -> showCalibrationResult(stereoRms, baselineMm));
            } catch (Exception error) {
                Log.e(TAG, "calibration_failed", error);
                mainHandler.post(() -> {
                    if (destroyed) return;
                    solveInFlight = false;
                    solveButton.setText("重新计算");
                    solveButton.setEnabled(true);
                    solveButton.setAlpha(1f);
                    updateStreamReadiness();
                    setStatus("标定未收敛，请补拍更多距离和倾角后重试", COLOR_ERROR);
                });
            } finally {
                if (result != null) result.release();
            }
        });
    }

    private void showCalibrationResult(double stereoRms, double baselineMm) {
        if (destroyed) return;
        solveInFlight = false;
        solveButton.setText("标定完成");
        solveButton.setEnabled(false);
        solveButton.setAlpha(0.45f);
        captureButton.setEnabled(false);
        captureButton.setAlpha(0.45f);
        shareButton.setVisibility(View.VISIBLE);
        setStatus(
            String.format(Locale.US, "标定完成 · RMS %.3f px · 基线 %.1f mm · 已保存 calibration.json", stereoRms, baselineMm),
            COLOR_HEALTHY
        );
        coverageView.setText("结果已保存");
        progressView.setText(samples.size() + " 组参与计算");
    }

    private void shareResult() {
        if (resultFile == null || !resultFile.isFile()) return;
        Uri uri = FileProvider.getUriForFile(this, getPackageName() + ".syncap-media.files", resultFile);
        Intent share = new Intent(Intent.ACTION_SEND);
        share.setType("application/json");
        share.putExtra(Intent.EXTRA_STREAM, uri);
        share.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
        startActivity(Intent.createChooser(share, "导出标定参数"));
    }

    private DeviceSnapshot captureDeviceSnapshot() throws Exception {
        HttpURLConnection connection = (HttpURLConnection) new URL(
            "http://" + deviceHost + ":8080/v1/calibration/snapshots"
        ).openConnection();
        connection.setConnectTimeout(5000);
        connection.setReadTimeout(15000);
        connection.setRequestMethod("POST");
        connection.setRequestProperty("Content-Type", "application/json");
        connection.setRequestProperty("X-SynCap-Claim", "123456");
        connection.setDoOutput(true);
        JSONObject request = new JSONObject().put("cameraIds", new JSONArray(cameraIds));
        try (OutputStream output = connection.getOutputStream()) {
            output.write(request.toString().getBytes(StandardCharsets.UTF_8));
        }
        int status = connection.getResponseCode();
        InputStream responseStream = status >= 200 && status < 300
            ? connection.getInputStream() : connection.getErrorStream();
        String response = readUtf8(responseStream);
        connection.disconnect();
        if (status < 200 || status >= 300) throw new IOException("HTTP " + status + ": " + response);
        JSONObject metadata = new JSONObject(response);
        if (!"synchronized_raw_nv12".equals(metadata.optString("source"))) {
            throw new IOException("设备未返回同步 NV12 原始帧");
        }
        if (Math.abs(metadata.optLong("syncErrorNs", Long.MAX_VALUE)) > 2_000_000L) {
            throw new IOException("双路同步误差超过 2 ms");
        }
        JSONArray cameras = metadata.getJSONArray("cameras");
        DecodedRawFrame[] frames = new DecodedRawFrame[2];
        try {
            for (int index = 0; index < cameraIds.length; index++) {
                JSONObject camera = null;
                for (int candidate = 0; candidate < cameras.length(); candidate++) {
                    JSONObject value = cameras.getJSONObject(candidate);
                    if (cameraIds[index].equals(value.optString("id"))) camera = value;
                }
                if (camera == null) throw new IOException("设备缺少 " + cameraIds[index] + " 同步帧");
                frames[index] = downloadAndDecode(camera);
            }
            return new DeviceSnapshot(frames[0], frames[1], metadata);
        } catch (Exception error) {
            for (DecodedRawFrame frame : frames) {
                if (frame == null) continue;
                if (!frame.bitmap.isRecycled()) frame.bitmap.recycle();
                deleteTemporary(frame.rawFile);
            }
            throw error;
        }
    }

    private DecodedRawFrame downloadAndDecode(JSONObject camera) throws Exception {
        String cameraId = camera.getString("id");
        int width = camera.getInt("width");
        int height = camera.getInt("height");
        long expectedSize = (long) width * height * 3L / 2L;
        boolean supportedDimensions = (width == CameraCalibrationEngine.IMAGE_WIDTH
            && height == CameraCalibrationEngine.IMAGE_HEIGHT)
            || (width == CameraCalibrationEngine.IMAGE_HEIGHT
                && height == CameraCalibrationEngine.IMAGE_WIDTH);
        if (!"nv12".equals(camera.optString("format"))
            || !supportedDimensions
            || camera.optLong("sizeBytes", -1L) != expectedSize) {
            throw new IOException(cameraId + " 返回的不是受支持的全分辨率 NV12 原始帧");
        }
        File temporary = File.createTempFile("syncap-calibration-", ".nv12", getCacheDir());
        boolean keepTemporary = false;
        try {
            HttpURLConnection connection = (HttpURLConnection) new URL(
                "http://" + deviceHost + ":8080" + camera.getString("path")
            ).openConnection();
            connection.setConnectTimeout(5000);
            connection.setReadTimeout(15000);
            connection.setRequestProperty("X-SynCap-Claim", "123456");
            int status = connection.getResponseCode();
            if (status < 200 || status >= 300) {
                String error = readUtf8(connection.getErrorStream());
                throw new IOException("HTTP " + status + ": " + error);
            }
            try (InputStream input = connection.getInputStream();
                 FileOutputStream output = new FileOutputStream(temporary)) {
                byte[] buffer = new byte[64 * 1024];
                int count;
                while ((count = input.read(buffer)) >= 0) output.write(buffer, 0, count);
                output.getFD().sync();
            } finally {
                connection.disconnect();
            }
            if (temporary.length() != expectedSize) {
                throw new IOException(cameraId + " 原始帧长度不完整");
            }
            if (!camera.getString("sha256").equals(sha256(temporary))) {
                throw new IOException(cameraId + " 原始帧校验失败");
            }
            byte[] raw = new byte[(int) expectedSize];
            try (InputStream input = new java.io.FileInputStream(temporary)) {
                int offset = 0;
                while (offset < raw.length) {
                    int count = input.read(raw, offset, raw.length - offset);
                    if (count < 0) break;
                    offset += count;
                }
                if (offset != raw.length || input.read() != -1) {
                    throw new IOException(cameraId + " 原始帧读取不完整");
                }
            }
            Mat nv12 = new Mat(height * 3 / 2, width, CvType.CV_8UC1);
            Mat rgba = new Mat();
            Bitmap bitmap = null;
            try {
                if (nv12.put(0, 0, raw) != raw.length) {
                    throw new IOException(cameraId + " 原始帧载入失败");
                }
                Imgproc.cvtColor(nv12, rgba, Imgproc.COLOR_YUV2RGBA_NV12);
                bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
                Utils.matToBitmap(rgba, bitmap);
                keepTemporary = true;
                return new DecodedRawFrame(bitmap, temporary);
            } catch (Exception error) {
                if (bitmap != null && !bitmap.isRecycled()) bitmap.recycle();
                throw error;
            } finally {
                nv12.release();
                rgba.release();
            }
        } finally {
            if (!keepTemporary) deleteTemporary(temporary);
        }
    }

    private static String readUtf8(InputStream input) throws IOException {
        if (input == null) return "";
        byte[] buffer = new byte[16 * 1024];
        int used = 0;
        int count;
        while ((count = input.read(buffer, used, buffer.length - used)) > 0) {
            used += count;
            if (used == buffer.length) break;
        }
        input.close();
        return new String(buffer, 0, used, StandardCharsets.UTF_8);
    }

    private static String sha256(File file) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (InputStream input = new java.io.FileInputStream(file)) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) digest.update(buffer, 0, count);
        }
        StringBuilder value = new StringBuilder();
        for (byte part : digest.digest()) value.append(String.format(Locale.US, "%02x", part & 0xff));
        return value.toString();
    }

    private static String safeMessage(Exception error) {
        String value = error.getMessage();
        return value == null || value.trim().isEmpty() ? error.getClass().getSimpleName() : value;
    }

    private void savePng(Bitmap bitmap, File destination) throws IOException {
        try (FileOutputStream output = new FileOutputStream(destination)) {
            if (!bitmap.compress(Bitmap.CompressFormat.PNG, 100, output)) {
                throw new IOException("Bitmap compression failed");
            }
            output.getFD().sync();
        }
    }

    private static void copyFile(File source, File destination) throws IOException {
        try (InputStream input = new java.io.FileInputStream(source);
             FileOutputStream output = new FileOutputStream(destination)) {
            byte[] buffer = new byte[64 * 1024];
            int count;
            while ((count = input.read(buffer)) >= 0) output.write(buffer, 0, count);
            output.getFD().sync();
        }
    }

    private static void deleteTemporary(File file) {
        if (file != null && file.exists() && !file.delete()) file.deleteOnExit();
    }

    private static final class DecodedRawFrame {
        final Bitmap bitmap;
        final File rawFile;

        DecodedRawFrame(Bitmap bitmap, File rawFile) {
            this.bitmap = bitmap;
            this.rawFile = rawFile;
        }
    }

    private static final class DeviceSnapshot {
        final Bitmap left;
        final Bitmap right;
        final File leftRaw;
        final File rightRaw;
        final JSONObject metadata;

        DeviceSnapshot(DecodedRawFrame left, DecodedRawFrame right, JSONObject metadata) {
            this.left = left.bitmap;
            this.right = right.bitmap;
            this.leftRaw = left.rawFile;
            this.rightRaw = right.rawFile;
            this.metadata = metadata;
        }
    }

    private void writeUtf8(File destination, String value) throws IOException {
        try (FileOutputStream output = new FileOutputStream(destination)) {
            output.write(value.getBytes(StandardCharsets.UTF_8));
            output.getFD().sync();
        }
    }

    private TextView actionButton(String text, boolean primary) {
        TextView button = new TextView(this);
        button.setText(text);
        button.setTextSize(13);
        button.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        button.setGravity(Gravity.CENTER);
        button.setTextColor(primary ? Color.rgb(9, 8, 5) : COLOR_GOLD);
        GradientDrawable background = new GradientDrawable();
        background.setColor(primary ? COLOR_GOLD : COLOR_SURFACE);
        background.setStroke(dp(1), primary ? Color.rgb(237, 203, 121) : COLOR_LINE);
        background.setCornerRadius(dp(7));
        button.setBackground(background);
        return button;
    }

    private TextView overlay(String value, int color) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextColor(color);
        text.setTextSize(12);
        text.setTypeface(Typeface.DEFAULT, Typeface.BOLD);
        text.setPadding(dp(9), dp(6), dp(9), dp(6));
        text.setBackgroundColor(Color.argb(190, 0, 0, 0));
        return text;
    }

    private void setStatus(String value, int color) {
        if (statusView == null) return;
        statusView.setText(value);
        statusView.setTextColor(color);
    }

    private String format(double value) {
        if (Math.rint(value) == value) return String.format(Locale.US, "%.0f", value);
        return String.format(Locale.US, "%.1f", value);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    @Override
    protected void onStop() {
        if (!isChangingConfigurations()) {
            beginRelease();
            if (!isFinishing()) finish();
        }
        super.onStop();
    }

    @Override
    protected void onDestroy() {
        destroyed = true;
        beginRelease();
        try {
            calibrationExecutor.execute(() -> {
                for (CameraCalibrationEngine.Sample sample : samples) sample.release();
                samples.clear();
            });
        } catch (RejectedExecutionException ignored) {
            // The executor was already closed after a previous destroy callback.
        }
        calibrationExecutor.shutdown();
        super.onDestroy();
    }

    private void beginRelease() {
        if (releaseStarted) return;
        releaseStarted = true;
        MediaPlayer[] releasingPlayers = players.clone();
        Arrays.fill(players, null);
        LibVLC releasingEngine = libVLC;
        libVLC = null;
        releaseExecutor.execute(() -> {
            for (MediaPlayer player : releasingPlayers) {
                if (player == null) continue;
                try { player.setEventListener(null); } catch (RuntimeException error) { Log.w(TAG, "clear_listener_failed", error); }
                try { player.stop(); } catch (RuntimeException error) { Log.w(TAG, "stop_failed", error); }
            }
            mainHandler.post(() -> {
                for (MediaPlayer player : releasingPlayers) {
                    if (player == null) continue;
                    try { player.detachViews(); } catch (RuntimeException error) { Log.w(TAG, "detach_failed", error); }
                }
                releaseExecutor.execute(() -> {
                    for (MediaPlayer player : releasingPlayers) {
                        if (player == null) continue;
                        try { player.release(); } catch (RuntimeException error) { Log.w(TAG, "player_release_failed", error); }
                    }
                    if (releasingEngine != null) {
                        try { releasingEngine.release(); } catch (RuntimeException error) { Log.w(TAG, "engine_release_failed", error); }
                    }
                    releaseExecutor.shutdown();
                });
            });
        });
    }

    private static final class NamedThreadFactory implements ThreadFactory {
        private final String prefix;

        NamedThreadFactory(String prefix) {
            this.prefix = prefix;
        }

        @Override
        public Thread newThread(Runnable runnable) {
            Thread thread = new Thread(runnable, prefix + THREAD_SEQUENCE.incrementAndGet());
            thread.setDaemon(true);
            return thread;
        }
    }
}
