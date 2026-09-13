package com.syncap.studio;

import static org.junit.Assert.*;
import androidx.test.ext.junit.runners.AndroidJUnit4;
import androidx.test.platform.app.InstrumentationRegistry;
import java.security.MessageDigest;
import java.util.Arrays;
import java.util.Collections;
import org.json.JSONArray;
import org.json.JSONObject;
import org.junit.Test;
import org.junit.runner.RunWith;

@RunWith(AndroidJUnit4.class)
public final class MediaContractTest {
    @Test public void validatesPreviewAndRejectsTcpCalibration() {
        android.content.Context context = InstrumentationRegistry.getInstrumentation().getTargetContext();
        assertNotNull(SynCapMedia.previewIntent(context,
            Arrays.asList("tcp://192.168.1.2:9100", "tcp://192.168.1.2:9101"), Arrays.asList("Left", "Right")));
        assertThrows(IllegalArgumentException.class, () -> SynCapMedia.previewIntent(context,
            Collections.singletonList("file:///private/video"), Collections.singletonList("Camera")));
        assertThrows(IllegalArgumentException.class, () -> SynCapMedia.previewIntent(context,
            Collections.singletonList("rtsp://secret@device:554/preview"), Collections.singletonList("Camera")));
        assertThrows(IllegalArgumentException.class, () -> SynCapMedia.calibrationIntent(context,
            Arrays.asList("tcp://192.168.1.2:9100", "tcp://192.168.1.2:9101"), Arrays.asList("Left", "Right"),
            Arrays.asList("left", "right"), "pair14", 10, 7, 30));
    }

    @Test public void rejectsPreviewSkewAndTamperedRawFrames() throws Exception {
        byte[] raw = new byte[64 * 48 * 3 / 2];
        try (StereoCalibrationSession session = new StereoCalibrationSession(10, 7, 30, "left", "right")) {
            JSONObject preview = metadata(raw).put("source", "preview");
            assertThrows(IllegalArgumentException.class, () -> session.addRawPair(raw, raw, preview));
            JSONObject skewed = metadata(raw).put("syncErrorNs", "2000001");
            assertThrows(IllegalArgumentException.class, () -> session.addRawPair(raw, raw, skewed));
            JSONObject valid = metadata(raw);
            raw[0] = 42;
            assertThrows(IllegalArgumentException.class, () -> session.addRawPair(raw, raw, valid));
            assertEquals(0, session.sampleCount());
        }
    }

    @Test public void blankRawImageIsNotAnAcceptedChessboard() throws Exception {
        byte[] raw = new byte[64 * 48 * 3 / 2];
        Arrays.fill(raw, (byte) 128);
        try (StereoCalibrationSession session = new StereoCalibrationSession(10, 7, 30, "left", "right")) {
            StereoCalibrationSession.SampleResult result = session.addRawPair(raw, raw, metadata(raw));
            assertFalse(result.accepted);
            assertEquals(0, result.sampleCount);
            assertFalse(session.canSolve());
            assertThrows(IllegalStateException.class, () -> session.solve("test", "stereo"));
        }
    }

    @Test public void validatesBoardAndSessionLifetime() {
        assertThrows(IllegalArgumentException.class, () -> new StereoCalibrationSession(2, 7, 30, "left", "right"));
        assertThrows(IllegalArgumentException.class, () -> new StereoCalibrationSession(10, 7, Double.NaN, "left", "right"));
        StereoCalibrationSession session = new StereoCalibrationSession(10, 7, 30, "left", "right");
        session.close();
        session.close();
        assertThrows(IllegalStateException.class, session::sampleCount);
    }

    @Test public void acceptsFullResolutionRawBoardAndRejectsMixedRotation() throws Exception {
        int width = 640;
        int height = 480;
        byte[] raw = new byte[width * height * 3 / 2];
        Arrays.fill(raw, (byte) 128);
        for (int y = 100; y < 380; y++) for (int x = 120; x < 520; x++) {
            raw[y * width + x] = (byte) (((x - 120) / 40 + (y - 100) / 40) % 2 == 0 ? 16 : 235);
        }
        try (StereoCalibrationSession session = new StereoCalibrationSession(10, 7, 30, "left", "right")) {
            StereoCalibrationSession.SampleResult accepted = session.addRawPair(raw, raw, metadata(raw, width, height));
            assertTrue(accepted.reason, accepted.accepted);
            assertEquals(1, session.sampleCount());
            JSONObject rotated = metadata(raw, width, height);
            rotated.getJSONObject("cameraConfiguration").put("rotationDegrees", 90);
            assertThrows(IllegalArgumentException.class, () -> session.addRawPair(raw, raw, rotated));
            StereoCalibrationSession.SampleResult duplicate = session.addRawPair(raw, raw, metadata(raw, width, height));
            assertFalse(duplicate.accepted);
            assertEquals(1, session.sampleCount());
        }
    }

    private static JSONObject metadata(byte[] raw) throws Exception {
        return metadata(raw, 64, 48);
    }

    private static JSONObject metadata(byte[] raw, int width, int height) throws Exception {
        StringBuilder hash = new StringBuilder();
        for (byte item : MessageDigest.getInstance("SHA-256").digest(raw)) {
            hash.append(String.format(java.util.Locale.ROOT, "%02x", item & 255));
        }
        JSONArray cameras = new JSONArray();
        for (String id : Arrays.asList("left", "right")) cameras.put(new JSONObject()
            .put("id", id).put("format", "nv12").put("width", width).put("height", height)
            .put("sizeBytes", raw.length).put("sha256", hash.toString()));
        return new JSONObject().put("source", "synchronized_raw_nv12").put("syncErrorNs", "1000")
            .put("cameraConfiguration", new JSONObject().put("rotationDegrees", 0)).put("cameras", cameras);
    }
}
