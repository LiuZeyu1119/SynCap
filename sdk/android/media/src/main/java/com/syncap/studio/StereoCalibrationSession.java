package com.syncap.studio;

import android.graphics.Bitmap;
import org.json.JSONArray;
import org.json.JSONObject;
import org.opencv.android.OpenCVLoader;
import org.opencv.android.Utils;
import org.opencv.core.CvType;
import org.opencv.core.Mat;
import org.opencv.imgproc.Imgproc;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.List;

/** Stateful fisheye calibration from hash-verified, synchronized full-resolution NV12 pairs. */
public final class StereoCalibrationSession implements AutoCloseable {
    private final int squaresLong;
    private final int squaresWide;
    private final double squareSizeMm;
    private final String[] cameraIds;
    private final List<CameraCalibrationEngine.Sample> samples = new ArrayList<>();
    private int rotationDegrees = -1;
    private boolean closed;

    public StereoCalibrationSession(int squaresLong, int squaresWide, double squareSizeMm, String leftId, String rightId) {
        if (squaresLong < 4 || squaresLong > 20 || squaresWide < 4 || squaresWide > 20
            || !Double.isFinite(squareSizeMm) || squareSizeMm < 1 || squareSizeMm > 200
            || leftId == null || rightId == null || leftId.equals(rightId)
            || !leftId.matches("[A-Za-z0-9_-]{1,64}") || !rightId.matches("[A-Za-z0-9_-]{1,64}")) {
            throw new IllegalArgumentException("Invalid chessboard or camera identifiers");
        }
        this.squaresLong = squaresLong;
        this.squaresWide = squaresWide;
        this.squareSizeMm = squareSizeMm;
        this.cameraIds = new String[] { leftId, rightId };
    }

    public static final class SampleResult {
        public final boolean accepted;
        public final String reason;
        public final int sampleCount;
        public final boolean canSolve;

        private SampleResult(boolean accepted, String reason, int sampleCount, boolean canSolve) {
            this.accepted = accepted;
            this.reason = reason;
            this.sampleCount = sampleCount;
            this.canSolve = canSolve;
        }
    }

    /** Metadata is the device's raw-snapshot response, not a declaration manufactured from a preview. */
    public synchronized SampleResult addRawPair(byte[] leftRaw, byte[] rightRaw, JSONObject metadata) throws Exception {
        ensureOpen();
        if (!"synchronized_raw_nv12".equals(metadata.optString("source"))) {
            throw new IllegalArgumentException("Calibration requires synchronized raw NV12, not preview images");
        }
        long skew = metadata.getLong("syncErrorNs");
        if (skew < -2000000L || skew > 2000000L) throw new IllegalArgumentException("Pair skew exceeds 2 ms");
        int rotation = metadata.getJSONObject("cameraConfiguration").getInt("rotationDegrees");
        if (rotation != 0 && rotation != 90 && rotation != 180 && rotation != 270) {
            throw new IllegalArgumentException("Invalid image rotation");
        }
        if (rotationDegrees >= 0 && rotation != rotationDegrees) {
            throw new IllegalArgumentException("Image rotation changed; use a new calibration session");
        }
        JSONArray cameras = metadata.getJSONArray("cameras");
        JSONObject left = camera(cameras, cameraIds[0]);
        JSONObject right = camera(cameras, cameraIds[1]);
        validateRaw(leftRaw, left);
        validateRaw(rightRaw, right);
        if (left.getInt("width") != right.getInt("width") || left.getInt("height") != right.getInt("height")) {
            throw new IllegalArgumentException("Stereo image dimensions differ");
        }
        if (!samples.isEmpty() && (samples.get(0).imageWidth != left.getInt("width")
            || samples.get(0).imageHeight != left.getInt("height"))) {
            throw new IllegalArgumentException("Image dimensions changed; use a new calibration session");
        }
        if (!OpenCVLoader.initLocal()) throw new IllegalStateException("OpenCV could not be loaded");
        Bitmap leftBitmap = null;
        Bitmap rightBitmap = null;
        CameraCalibrationEngine.Detection a = null;
        CameraCalibrationEngine.Detection b = null;
        boolean transferred = false;
        try {
            leftBitmap = decode(leftRaw, left.getInt("width"), left.getInt("height"));
            rightBitmap = decode(rightRaw, right.getInt("width"), right.getInt("height"));
            a = CameraCalibrationEngine.detect(leftBitmap, squaresLong - 1, squaresWide - 1);
            b = CameraCalibrationEngine.detect(rightBitmap, squaresLong - 1, squaresWide - 1);
            String rejection = !a.accepted ? a.reason : !b.accepted ? b.reason
                : CameraCalibrationEngine.isDuplicate(samples, a, b) ? "Duplicate board pose" : null;
            if (rejection != null) return new SampleResult(false, rejection, samples.size(), canSolve());
            samples.add(CameraCalibrationEngine.createSample(a, b));
            transferred = true;
            rotationDegrees = rotation;
            return new SampleResult(true, null, samples.size(), canSolve());
        } finally {
            if (!transferred) {
                if (a != null) a.release();
                if (b != null) b.release();
            }
            if (leftBitmap != null) leftBitmap.recycle();
            if (rightBitmap != null) rightBitmap.recycle();
        }
    }

    public synchronized int sampleCount() { ensureOpen(); return samples.size(); }
    public synchronized boolean canSolve() { ensureOpen(); return CameraCalibrationEngine.canSolve(samples); }

    public synchronized JSONObject solve(String calibrationId, String pairLabel) throws Exception {
        ensureOpen();
        if (calibrationId == null || !calibrationId.matches("[A-Za-z0-9_-]{1,96}")
            || pairLabel == null || !pairLabel.matches("[A-Za-z0-9_-]{1,96}")) {
            throw new IllegalArgumentException("Invalid calibration identifier");
        }
        CameraCalibrationEngine.CalibrationResult result = CameraCalibrationEngine.calibrate(
            samples, squaresLong - 1, squaresWide - 1, squareSizeMm
        );
        try {
            return CameraCalibrationEngine.toJson(result, calibrationId, pairLabel, cameraIds,
                squaresLong, squaresWide, squareSizeMm, samples.size(), rotationDegrees, System.currentTimeMillis());
        } finally { result.release(); }
    }

    @Override public synchronized void close() {
        if (closed) return;
        closed = true;
        for (CameraCalibrationEngine.Sample sample : samples) sample.release();
        samples.clear();
    }

    private void ensureOpen() {
        if (closed) throw new IllegalStateException("Calibration session is closed");
    }

    private static JSONObject camera(JSONArray values, String id) throws Exception {
        JSONObject found = null;
        for (int index = 0; index < values.length(); index++) {
            JSONObject item = values.getJSONObject(index);
            if (id.equals(item.optString("id"))) {
                if (found != null) throw new IllegalArgumentException("Duplicate camera metadata");
                found = item;
            }
        }
        if (found == null) throw new IllegalArgumentException("Missing camera metadata");
        return found;
    }

    private static void validateRaw(byte[] raw, JSONObject camera) throws Exception {
        int width = camera.getInt("width");
        int height = camera.getInt("height");
        long pixels = (long) width * height;
        long expected = pixels * 3 / 2;
        if (!"nv12".equals(camera.optString("format")) || width < 2 || height < 2
            || width % 2 != 0 || height % 2 != 0 || pixels > 16777216
            || raw == null || raw.length != expected || camera.getLong("sizeBytes") != expected) {
            throw new IllegalArgumentException("Invalid full-resolution NV12 frame");
        }
        byte[] digest = MessageDigest.getInstance("SHA-256").digest(raw);
        StringBuilder hash = new StringBuilder();
        for (byte value : digest) hash.append(String.format(java.util.Locale.ROOT, "%02x", value & 255));
        if (!hash.toString().equals(camera.getString("sha256"))) {
            throw new IllegalArgumentException("Raw frame SHA-256 mismatch");
        }
    }

    private static Bitmap decode(byte[] raw, int width, int height) {
        Mat nv12 = new Mat(height * 3 / 2, width, CvType.CV_8UC1);
        Mat rgba = new Mat();
        Bitmap bitmap = null;
        try {
            nv12.put(0, 0, raw);
            Imgproc.cvtColor(nv12, rgba, Imgproc.COLOR_YUV2RGBA_NV12);
            bitmap = Bitmap.createBitmap(width, height, Bitmap.Config.ARGB_8888);
            Utils.matToBitmap(rgba, bitmap);
            return bitmap;
        } catch (RuntimeException error) {
            if (bitmap != null) bitmap.recycle();
            throw error;
        } finally { nv12.release(); rgba.release(); }
    }
}
