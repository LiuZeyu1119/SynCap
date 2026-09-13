package com.syncap.studio;

import android.graphics.Bitmap;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;
import org.opencv.android.Utils;
import org.opencv.calib3d.Calib3d;
import org.opencv.core.Core;
import org.opencv.core.CvType;
import org.opencv.core.Mat;
import org.opencv.core.MatOfDouble;
import org.opencv.core.MatOfPoint2f;
import org.opencv.core.MatOfPoint3f;
import org.opencv.core.Point;
import org.opencv.core.Point3;
import org.opencv.core.Size;
import org.opencv.core.TermCriteria;
import org.opencv.imgproc.Imgproc;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Set;

final class CameraCalibrationEngine {
    static final int IMAGE_WIDTH = 1280;
    static final int IMAGE_HEIGHT = 1088;
    private static final int DETECTION_LONG_EDGE = 640;
    static final int TARGET_SAMPLES = 24;
    static final int MINIMUM_SAMPLES = 18;
    static final int MINIMUM_ZONES = 5;
    private static final double MIN_SHARPNESS = 14.0;
    private static final double MIN_BOARD_AREA_RATIO = 0.006;
    private static final double DUPLICATE_DISTANCE = 0.035;

    private CameraCalibrationEngine() {}

    static Detection detect(Bitmap bitmap, int innerColumns, int innerRows) {
        Mat rgba = new Mat();
        Mat gray = new Mat();
        Mat detectionGray = new Mat();
        Mat laplacian = new Mat();
        MatOfPoint2f corners = new MatOfPoint2f();
        MatOfDouble mean = new MatOfDouble();
        MatOfDouble deviation = new MatOfDouble();
        try {
            int imageWidth = bitmap.getWidth();
            int imageHeight = bitmap.getHeight();
            double detectionScale = DETECTION_LONG_EDGE / (double) Math.max(imageWidth, imageHeight);
            int detectionWidth = Math.max(1, (int) Math.round(imageWidth * detectionScale));
            int detectionHeight = Math.max(1, (int) Math.round(imageHeight * detectionScale));
            Utils.bitmapToMat(bitmap, rgba);
            Imgproc.cvtColor(rgba, gray, Imgproc.COLOR_RGBA2GRAY);
            Imgproc.resize(gray, detectionGray, new Size(detectionWidth, detectionHeight), 0, 0, Imgproc.INTER_AREA);
            Imgproc.Laplacian(detectionGray, laplacian, CvType.CV_64F);
            Core.meanStdDev(laplacian, mean, deviation);
            double[] deviations = deviation.toArray();
            double sharpness = deviations.length == 0 ? 0 : deviations[0] * deviations[0];
            Size patternSize = new Size(innerColumns, innerRows);
            boolean found = Calib3d.findChessboardCorners(
                detectionGray,
                patternSize,
                corners,
                Calib3d.CALIB_CB_ADAPTIVE_THRESH | Calib3d.CALIB_CB_NORMALIZE_IMAGE
            );
            if (found) {
                Imgproc.cornerSubPix(
                    detectionGray,
                    corners,
                    new Size(5, 5),
                    new Size(-1, -1),
                    new TermCriteria(TermCriteria.COUNT + TermCriteria.EPS, 40, 0.001)
                );
            } else {
                int flags = Calib3d.CALIB_CB_EXHAUSTIVE
                    | Calib3d.CALIB_CB_ACCURACY
                    | Calib3d.CALIB_CB_NORMALIZE_IMAGE;
                found = Calib3d.findChessboardCornersSB(detectionGray, patternSize, corners, flags);
            }
            if (!found || corners.rows() != innerColumns * innerRows) {
                return Detection.rejected("未识别到完整棋盘格", sharpness, imageWidth, imageHeight);
            }

            MatOfPoint2f detectedCorners = new MatOfPoint2f(corners);
            Point[] points = detectedCorners.toArray();
            detectedCorners.release();
            if (points.length != innerColumns * innerRows) {
                return Detection.rejected("棋盘格角点数量不匹配", sharpness, imageWidth, imageHeight);
            }
            double scaleX = imageWidth / (double) detectionWidth;
            double scaleY = imageHeight / (double) detectionHeight;
            for (Point point : points) {
                point.x *= scaleX;
                point.y *= scaleY;
            }
            canonicalizePointOrder(points);
            Point topLeft = points[0];
            Point topRight = points[innerColumns - 1];
            Point bottomLeft = points[(innerRows - 1) * innerColumns];
            Point bottomRight = points[points.length - 1];
            double area = polygonArea(topLeft, topRight, bottomRight, bottomLeft);
            double areaRatio = area / (imageWidth * (double) imageHeight);
            double centerX = 0;
            double centerY = 0;
            for (Point point : points) {
                centerX += point.x;
                centerY += point.y;
            }
            centerX /= points.length;
            centerY /= points.length;
            double normalizedX = clamp(centerX / imageWidth, 0, 0.999);
            double normalizedY = clamp(centerY / imageHeight, 0, 0.999);
            int zone = Math.min(2, (int) (normalizedY * 3)) * 3
                + Math.min(2, (int) (normalizedX * 3));
            double angle = Math.atan2(topRight.y - topLeft.y, topRight.x - topLeft.x) / Math.PI;
            double[] signature = new double[] {
                normalizedX,
                normalizedY,
                Math.sqrt(Math.max(0, areaRatio)),
                angle,
                topLeft.x / imageWidth,
                topLeft.y / imageHeight,
                bottomRight.x / imageWidth,
                bottomRight.y / imageHeight
            };
            MatOfPoint2f stableCorners = new MatOfPoint2f(points);
            if (sharpness < MIN_SHARPNESS) {
                stableCorners.release();
                return Detection.rejected("画面较模糊，请保持标定板静止", sharpness, imageWidth, imageHeight);
            }
            if (areaRatio < MIN_BOARD_AREA_RATIO) {
                stableCorners.release();
                return Detection.rejected("棋盘格太远，请靠近一些", sharpness, imageWidth, imageHeight);
            }
            return new Detection(true, null, stableCorners, signature, sharpness, areaRatio, zone, imageWidth, imageHeight);
        } finally {
            rgba.release();
            gray.release();
            detectionGray.release();
            laplacian.release();
            corners.release();
            mean.release();
            deviation.release();
        }
    }

    static boolean isDuplicate(List<Sample> samples, Detection left, Detection right) {
        double[] candidate = averageSignature(left.signature, right.signature);
        for (Sample sample : samples) {
            if (signatureDistance(candidate, sample.signature) < DUPLICATE_DISTANCE) return true;
        }
        return false;
    }

    static Sample createSample(Detection left, Detection right) {
        if (left.imageWidth != right.imageWidth || left.imageHeight != right.imageHeight) {
            throw new IllegalArgumentException("双路原始帧尺寸不一致");
        }
        return new Sample(
            left.corners,
            right.corners,
            averageSignature(left.signature, right.signature),
            (left.areaRatio + right.areaRatio) / 2.0,
            left.zone,
            right.zone,
            left.imageWidth,
            left.imageHeight
        );
    }

    static Coverage coverage(List<Sample> samples) {
        Set<Integer> zones = new HashSet<>();
        double minArea = Double.POSITIVE_INFINITY;
        double maxArea = 0;
        for (Sample sample : samples) {
            zones.add(sample.leftZone);
            zones.add(sample.rightZone);
            minArea = Math.min(minArea, sample.areaRatio);
            maxArea = Math.max(maxArea, sample.areaRatio);
        }
        if (samples.isEmpty()) minArea = 0;
        double scaleRatio = minArea > 0 ? maxArea / minArea : 0;
        return new Coverage(zones.size(), scaleRatio);
    }

    static boolean canSolve(List<Sample> samples) {
        Coverage coverage = coverage(samples);
        return samples.size() >= MINIMUM_SAMPLES
            && coverage.zones >= MINIMUM_ZONES
            && coverage.scaleRatio >= 1.35;
    }

    static CalibrationResult calibrate(
        List<Sample> samples,
        int innerColumns,
        int innerRows,
        double squareSizeMillimeters
    ) {
        if (!canSolve(samples)) throw new IllegalStateException("有效照片的视角覆盖还不够");
        int imageWidth = samples.get(0).imageWidth;
        int imageHeight = samples.get(0).imageHeight;
        List<Mat> objectPoints = new ArrayList<>();
        List<Mat> leftPoints = new ArrayList<>();
        List<Mat> rightPoints = new ArrayList<>();
        MatOfPoint3f board = createBoard(innerColumns, innerRows, squareSizeMillimeters / 1000.0);
        for (Sample sample : samples) {
            if (sample.imageWidth != imageWidth || sample.imageHeight != imageHeight) {
                throw new IllegalStateException("采样期间图像方向发生变化，请重新采集标定照片");
            }
            objectPoints.add(board.clone());
            leftPoints.add(canonicalizedCorners(sample.leftCorners));
            rightPoints.add(canonicalizedCorners(sample.rightCorners));
        }
        board.release();

        Mat leftK = Mat.eye(3, 3, CvType.CV_64F);
        Mat leftD = Mat.zeros(4, 1, CvType.CV_64F);
        Mat rightK = Mat.eye(3, 3, CvType.CV_64F);
        Mat rightD = Mat.zeros(4, 1, CvType.CV_64F);
        Mat rotation = new Mat();
        Mat translation = new Mat();
        List<Mat> leftRvecs = new ArrayList<>();
        List<Mat> leftTvecs = new ArrayList<>();
        List<Mat> rightRvecs = new ArrayList<>();
        List<Mat> rightTvecs = new ArrayList<>();
        TermCriteria criteria = new TermCriteria(TermCriteria.COUNT + TermCriteria.EPS, 100, 1e-6);
        int monoFlags = Calib3d.fisheye_CALIB_RECOMPUTE_EXTRINSIC
            | Calib3d.fisheye_CALIB_FIX_SKEW;
        try {
            double leftRms = Calib3d.fisheye_calibrate(
                objectPoints, leftPoints, new Size(imageWidth, imageHeight),
                leftK, leftD, leftRvecs, leftTvecs, monoFlags, criteria
            );
            double rightRms = Calib3d.fisheye_calibrate(
                objectPoints, rightPoints, new Size(imageWidth, imageHeight),
                rightK, rightD, rightRvecs, rightTvecs, monoFlags, criteria
            );
            double stereoRms = Calib3d.fisheye_stereoCalibrate(
                objectPoints, leftPoints, rightPoints,
                leftK, leftD, rightK, rightD,
                new Size(imageWidth, imageHeight), rotation, translation,
                Calib3d.fisheye_CALIB_FIX_INTRINSIC, criteria
            );
            double baselineMeters = Core.norm(translation);
            if (!Double.isFinite(stereoRms) || !Double.isFinite(baselineMeters) || baselineMeters <= 0) {
                throw new IllegalStateException("标定计算未收敛，请补拍更多角度");
            }
            return new CalibrationResult(
                leftK.clone(), leftD.clone(), rightK.clone(), rightD.clone(),
                rotation.clone(), translation.clone(), leftRms, rightRms, stereoRms,
                baselineMeters, coverage(samples), imageWidth, imageHeight
            );
        } finally {
            releaseAll(objectPoints);
            releaseAll(leftPoints);
            releaseAll(rightPoints);
            releaseAll(leftRvecs);
            releaseAll(leftTvecs);
            releaseAll(rightRvecs);
            releaseAll(rightTvecs);
            leftK.release();
            leftD.release();
            rightK.release();
            rightD.release();
            rotation.release();
            translation.release();
        }
    }

    static JSONObject toJson(
        CalibrationResult result,
        String calibrationId,
        String pair,
        String[] cameraIds,
        int squaresLong,
        int squaresWide,
        double squareSizeMillimeters,
        int sampleCount,
        int rotationDegrees,
        long createdAtMilliseconds
    ) throws JSONException {
        JSONObject root = new JSONObject();
        root.put("schema", "syncap.calibration/1.0");
        root.put("calibration_id", calibrationId);
        root.put("created_at_unix_ms", createdAtMilliseconds);
        root.put("source", "user_chessboard_android");
        root.put("pair", pair);
        root.put("image_size", new JSONArray().put(result.imageWidth).put(result.imageHeight));
        root.put("rotation_degrees_clockwise", rotationDegrees);
        root.put("board", new JSONObject()
            .put("squares_long", squaresLong)
            .put("squares_wide", squaresWide)
            .put("inner_corners", new JSONArray().put(squaresLong - 1).put(squaresWide - 1))
            .put("square_size_mm", squareSizeMillimeters));

        JSONObject intrinsics = new JSONObject();
        intrinsics.put(cameraIds[0], cameraJson(result.leftK, result.leftD, result.leftRms, result.imageWidth, result.imageHeight));
        intrinsics.put(cameraIds[1], cameraJson(result.rightK, result.rightD, result.rightRms, result.imageWidth, result.imageHeight));
        root.put("camera_intrinsics", intrinsics);

        JSONObject transform = new JSONObject();
        transform.put("from", cameraIds[0] + "_optical");
        transform.put("to", cameraIds[1] + "_optical");
        transform.put("rotation_matrix", matrixToJson(result.rotation));
        transform.put("translation_m", vectorToJson(result.translation));
        transform.put("quaternion_xyzw", quaternionToJson(result.rotation));
        root.put("pair_transform", transform);
        root.put("quality", new JSONObject()
            .put("model", "opencv_fisheye")
            .put("valid_pair_samples", sampleCount)
            .put("coverage_zones", result.coverage.zones)
            .put("scale_ratio", round(result.coverage.scaleRatio, 4))
            .put("left_rms_px", round(result.leftRms, 6))
            .put("right_rms_px", round(result.rightRms, 6))
            .put("stereo_rms_px", round(result.stereoRms, 6))
            .put("baseline_mm", round(result.baselineMeters * 1000.0, 3)));
        return root;
    }

    private static JSONObject cameraJson(
        Mat cameraMatrix, Mat distortion, double rms, int imageWidth, int imageHeight
    ) throws JSONException {
        return new JSONObject()
            .put("model", "fisheye_equidistant")
            .put("resolution", new JSONArray().put(imageWidth).put(imageHeight))
            .put("camera_matrix", matrixToJson(cameraMatrix))
            .put("distortion", vectorToJson(distortion))
            .put("rms_px", round(rms, 6));
    }

    private static JSONArray matrixToJson(Mat matrix) throws JSONException {
        JSONArray rows = new JSONArray();
        for (int row = 0; row < matrix.rows(); row++) {
            JSONArray values = new JSONArray();
            for (int column = 0; column < matrix.cols(); column++) {
                double[] value = matrix.get(row, column);
                values.put(value == null || value.length == 0 ? JSONObject.NULL : round(value[0], 12));
            }
            rows.put(values);
        }
        return rows;
    }

    private static JSONArray vectorToJson(Mat vector) throws JSONException {
        JSONArray values = new JSONArray();
        int count = (int) vector.total() * vector.channels();
        double[] data = new double[count];
        vector.get(0, 0, data);
        for (double value : data) values.put(round(value, 12));
        return values;
    }

    private static JSONArray quaternionToJson(Mat rotation) throws JSONException {
        double m00 = rotation.get(0, 0)[0];
        double m01 = rotation.get(0, 1)[0];
        double m02 = rotation.get(0, 2)[0];
        double m10 = rotation.get(1, 0)[0];
        double m11 = rotation.get(1, 1)[0];
        double m12 = rotation.get(1, 2)[0];
        double m20 = rotation.get(2, 0)[0];
        double m21 = rotation.get(2, 1)[0];
        double m22 = rotation.get(2, 2)[0];
        double x;
        double y;
        double z;
        double w;
        double trace = m00 + m11 + m22;
        if (trace > 0) {
            double s = Math.sqrt(trace + 1.0) * 2;
            w = 0.25 * s;
            x = (m21 - m12) / s;
            y = (m02 - m20) / s;
            z = (m10 - m01) / s;
        } else if (m00 > m11 && m00 > m22) {
            double s = Math.sqrt(1.0 + m00 - m11 - m22) * 2;
            w = (m21 - m12) / s;
            x = 0.25 * s;
            y = (m01 + m10) / s;
            z = (m02 + m20) / s;
        } else if (m11 > m22) {
            double s = Math.sqrt(1.0 + m11 - m00 - m22) * 2;
            w = (m02 - m20) / s;
            x = (m01 + m10) / s;
            y = 0.25 * s;
            z = (m12 + m21) / s;
        } else {
            double s = Math.sqrt(1.0 + m22 - m00 - m11) * 2;
            w = (m10 - m01) / s;
            x = (m02 + m20) / s;
            y = (m12 + m21) / s;
            z = 0.25 * s;
        }
        return new JSONArray().put(round(x, 12)).put(round(y, 12)).put(round(z, 12)).put(round(w, 12));
    }

    private static MatOfPoint3f createBoard(int innerColumns, int innerRows, double squareSizeMeters) {
        Point3[] points = new Point3[innerColumns * innerRows];
        int index = 0;
        for (int row = 0; row < innerRows; row++) {
            for (int column = 0; column < innerColumns; column++) {
                points[index++] = new Point3(column * squareSizeMeters, row * squareSizeMeters, 0);
            }
        }
        return new MatOfPoint3f(points);
    }

    static void canonicalizePointOrder(Point[] points) {
        if (points == null || points.length < 2) return;
        Point first = points[0];
        Point last = points[points.length - 1];
        if (last.x + last.y >= first.x + first.y) return;
        for (int start = 0, end = points.length - 1; start < end; start++, end--) {
            Point value = points[start];
            points[start] = points[end];
            points[end] = value;
        }
    }

    private static MatOfPoint2f canonicalizedCorners(Mat corners) {
        MatOfPoint2f value = new MatOfPoint2f(corners);
        Point[] points = value.toArray();
        value.release();
        canonicalizePointOrder(points);
        return new MatOfPoint2f(points);
    }

    private static double polygonArea(Point... points) {
        double sum = 0;
        for (int index = 0; index < points.length; index++) {
            Point current = points[index];
            Point next = points[(index + 1) % points.length];
            sum += current.x * next.y - next.x * current.y;
        }
        return Math.abs(sum) * 0.5;
    }

    private static double[] averageSignature(double[] left, double[] right) {
        double[] result = new double[Math.min(left.length, right.length)];
        for (int index = 0; index < result.length; index++) result[index] = (left[index] + right[index]) / 2.0;
        return result;
    }

    private static double signatureDistance(double[] left, double[] right) {
        double sum = 0;
        int count = Math.min(left.length, right.length);
        for (int index = 0; index < count; index++) {
            double delta = left[index] - right[index];
            sum += delta * delta;
        }
        return Math.sqrt(sum / Math.max(1, count));
    }

    private static double clamp(double value, double minimum, double maximum) {
        return Math.max(minimum, Math.min(maximum, value));
    }

    private static double round(double value, int digits) {
        double factor = Math.pow(10, digits);
        return Math.round(value * factor) / factor;
    }

    private static void releaseAll(List<Mat> values) {
        for (Mat value : values) value.release();
    }

    static final class Detection {
        final boolean accepted;
        final String reason;
        final MatOfPoint2f corners;
        final double[] signature;
        final double sharpness;
        final double areaRatio;
        final int zone;
        final int imageWidth;
        final int imageHeight;

        Detection(
            boolean accepted,
            String reason,
            MatOfPoint2f corners,
            double[] signature,
            double sharpness,
            double areaRatio,
            int zone,
            int imageWidth,
            int imageHeight
        ) {
            this.accepted = accepted;
            this.reason = reason;
            this.corners = corners;
            this.signature = signature;
            this.sharpness = sharpness;
            this.areaRatio = areaRatio;
            this.zone = zone;
            this.imageWidth = imageWidth;
            this.imageHeight = imageHeight;
        }

        static Detection rejected(String reason, double sharpness, int imageWidth, int imageHeight) {
            return new Detection(false, reason, null, new double[0], sharpness, 0, -1, imageWidth, imageHeight);
        }

        void release() {
            if (corners != null) corners.release();
        }
    }

    static final class Sample {
        final Mat leftCorners;
        final Mat rightCorners;
        final double[] signature;
        final double areaRatio;
        final int leftZone;
        final int rightZone;
        final int imageWidth;
        final int imageHeight;

        Sample(
            Mat leftCorners, Mat rightCorners, double[] signature, double areaRatio,
            int leftZone, int rightZone, int imageWidth, int imageHeight
        ) {
            this.leftCorners = leftCorners;
            this.rightCorners = rightCorners;
            this.signature = signature;
            this.areaRatio = areaRatio;
            this.leftZone = leftZone;
            this.rightZone = rightZone;
            this.imageWidth = imageWidth;
            this.imageHeight = imageHeight;
        }

        void release() {
            leftCorners.release();
            rightCorners.release();
        }
    }

    static final class Coverage {
        final int zones;
        final double scaleRatio;

        Coverage(int zones, double scaleRatio) {
            this.zones = zones;
            this.scaleRatio = scaleRatio;
        }

        String summary() {
            return String.format(Locale.US, "%d/9 区域 · 远近 %.1fx", zones, scaleRatio);
        }
    }

    static final class CalibrationResult {
        final Mat leftK;
        final Mat leftD;
        final Mat rightK;
        final Mat rightD;
        final Mat rotation;
        final Mat translation;
        final double leftRms;
        final double rightRms;
        final double stereoRms;
        final double baselineMeters;
        final Coverage coverage;
        final int imageWidth;
        final int imageHeight;

        CalibrationResult(
            Mat leftK,
            Mat leftD,
            Mat rightK,
            Mat rightD,
            Mat rotation,
            Mat translation,
            double leftRms,
            double rightRms,
            double stereoRms,
            double baselineMeters,
            Coverage coverage,
            int imageWidth,
            int imageHeight
        ) {
            this.leftK = leftK;
            this.leftD = leftD;
            this.rightK = rightK;
            this.rightD = rightD;
            this.rotation = rotation;
            this.translation = translation;
            this.leftRms = leftRms;
            this.rightRms = rightRms;
            this.stereoRms = stereoRms;
            this.baselineMeters = baselineMeters;
            this.coverage = coverage;
            this.imageWidth = imageWidth;
            this.imageHeight = imageHeight;
        }

        void release() {
            leftK.release();
            leftD.release();
            rightK.release();
            rightD.release();
            rotation.release();
            translation.release();
        }
    }
}
