package com.syncap.studio;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import androidx.test.ext.junit.runners.AndroidJUnit4;

import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;
import org.opencv.android.OpenCVLoader;
import org.opencv.calib3d.Calib3d;
import org.opencv.core.Core;
import org.opencv.core.CvType;
import org.opencv.core.Mat;
import org.opencv.core.MatOfPoint2f;
import org.opencv.core.MatOfPoint3f;
import org.opencv.core.Point;
import org.opencv.core.Point3;

import java.util.ArrayList;
import java.util.List;

@RunWith(AndroidJUnit4.class)
public final class CameraCalibrationEngineInstrumentedTest {
    private static final int INNER_COLUMNS = 9;
    private static final int INNER_ROWS = 6;
    private static final double SQUARE_METERS = 0.03;
    private static final double BASELINE_METERS = 0.12;

    @Test
    public void recoversKnownStereoBaselineAndExportsSchema() throws Exception {
        assertTrue(OpenCVLoader.initLocal());
        Core.setUseOptimized(false);
        Core.setNumThreads(1);

        Mat cameraMatrix = cameraMatrix(690, 688, 640, 544);
        Mat distortion = Mat.zeros(4, 1, CvType.CV_64F);
        MatOfPoint3f board = boardPoints();
        List<CameraCalibrationEngine.Sample> samples = new ArrayList<>();
        try {
            for (int index = 0; index < 24; index++) {
                double x = ((index % 4) - 1.5) * 0.045;
                double y = (((index / 4) % 3) - 1.0) * 0.045;
                double z = 0.72 + (index % 6) * 0.075;
                Mat rvec = vector(
                    (((index % 3) - 1) * 0.055),
                    (((index % 5) - 2) * 0.045),
                    (((index % 4) - 1.5) * 0.035)
                );
                Mat leftTranslation = vector(x, y, z);
                Mat rightTranslation = vector(x - BASELINE_METERS, y, z);
                MatOfPoint2f left = new MatOfPoint2f();
                MatOfPoint2f right = new MatOfPoint2f();
                try {
                    Calib3d.fisheye_projectPoints(
                        board, left, rvec, leftTranslation, cameraMatrix, distortion
                    );
                    Calib3d.fisheye_projectPoints(
                        board, right, rvec, rightTranslation, cameraMatrix, distortion
                    );
                    if (index == 5 || index == 11 || index == 18 || index == 27) {
                        Point[] reversed = right.toArray();
                        for (int start = 0, end = reversed.length - 1; start < end; start++, end--) {
                            Point value = reversed[start];
                            reversed[start] = reversed[end];
                            reversed[end] = value;
                        }
                        right.fromArray(reversed);
                    }
                    double areaRatio = 0.02 * (1.0 + (index % 6) * 0.12);
                    samples.add(new CameraCalibrationEngine.Sample(
                        left.clone(),
                        right.clone(),
                        new double[] { index / 24.0 },
                        areaRatio,
                        index % 9,
                        (index + 1) % 9,
                        CameraCalibrationEngine.IMAGE_WIDTH,
                        CameraCalibrationEngine.IMAGE_HEIGHT
                    ));
                } finally {
                    rvec.release();
                    leftTranslation.release();
                    rightTranslation.release();
                    left.release();
                    right.release();
                }
            }

            assertTrue(CameraCalibrationEngine.canSolve(samples));
            CameraCalibrationEngine.CalibrationResult result = CameraCalibrationEngine.calibrate(
                samples, INNER_COLUMNS, INNER_ROWS, SQUARE_METERS * 1000.0
            );
            try {
                assertTrue(Double.isFinite(result.stereoRms));
                assertEquals(BASELINE_METERS, result.baselineMeters, 0.012);
                JSONObject json = CameraCalibrationEngine.toJson(
                    result,
                    "cal_test",
                    "pair14",
                    new String[] { "cam3", "cam0" },
                    INNER_COLUMNS + 1,
                    INNER_ROWS + 1,
                    SQUARE_METERS * 1000.0,
                    samples.size(),
                    180,
                    1_700_000_000_000L
                );
                assertEquals("syncap.calibration/1.0", json.getString("schema"));
                assertEquals("pair14", json.getString("pair"));
                assertEquals(24, json.getJSONObject("quality").getInt("valid_pair_samples"));
                assertEquals(
                    BASELINE_METERS * 1000.0,
                    json.getJSONObject("quality").getDouble("baseline_mm"),
                    12.0
                );
                assertTrue(json.getJSONObject("camera_intrinsics").has("cam3"));
                assertTrue(json.getJSONObject("camera_intrinsics").has("cam0"));
            } finally {
                result.release();
            }
        } finally {
            for (CameraCalibrationEngine.Sample sample : samples) sample.release();
            cameraMatrix.release();
            distortion.release();
            board.release();
        }
    }

    private static Mat cameraMatrix(double fx, double fy, double cx, double cy) {
        Mat matrix = Mat.eye(3, 3, CvType.CV_64F);
        matrix.put(0, 0, fx);
        matrix.put(1, 1, fy);
        matrix.put(0, 2, cx);
        matrix.put(1, 2, cy);
        return matrix;
    }

    private static Mat vector(double x, double y, double z) {
        Mat vector = new Mat(3, 1, CvType.CV_64F);
        vector.put(0, 0, x, y, z);
        return vector;
    }

    private static MatOfPoint3f boardPoints() {
        Point3[] points = new Point3[INNER_COLUMNS * INNER_ROWS];
        int index = 0;
        for (int row = 0; row < INNER_ROWS; row++) {
            for (int column = 0; column < INNER_COLUMNS; column++) {
                points[index++] = new Point3(
                    column * SQUARE_METERS,
                    row * SQUARE_METERS,
                    0
                );
            }
        }
        return new MatOfPoint3f(points);
    }
}
