package com.syncap.studio;

import android.bluetooth.BluetoothDevice;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.UUID;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.Test;

public class SynCapDevicePluginTest {
    @Test
    public void acceptsSafeOpaqueDeviceIdentifiersWithoutTimestampFormatLock() {
        assertTrue(SynCapDevicePlugin.validOpaqueIdentifier("vendor-capture.42"));
        assertTrue(SynCapDevicePlugin.validOpaqueIdentifier("session_01"));
        assertFalse(SynCapDevicePlugin.validOpaqueIdentifier(""));
        assertFalse(SynCapDevicePlugin.validOpaqueIdentifier(".."));
        assertFalse(SynCapDevicePlugin.validOpaqueIdentifier("../session"));
        assertFalse(SynCapDevicePlugin.validOpaqueIdentifier("session/child"));
        assertFalse(SynCapDevicePlugin.validOpaqueIdentifier(" session"));
    }

    @Test
    public void validatesDownloadedSessionManifestIdentityWhenDeclared() throws Exception {
        LinkedHashMap<String, String> matching = new LinkedHashMap<>();
        matching.put("id", "vendor-session.42");
        SynCapDevicePlugin.validateDeclaredSessionIdentities(matching, "vendor-session.42");
        SynCapDevicePlugin.validateDeclaredSessionIdentities(new LinkedHashMap<>(), "vendor-session.42");

        LinkedHashMap<String, String> conflicting = new LinkedHashMap<>();
        conflicting.put("id", "vendor-session.42");
        conflicting.put("session_id", "another-session");
        assertManifestIdentityRejected(conflicting, "vendor-session.42");
    }

    @Test
    public void acceptsOnlyTheRequestedSessionManifestEndpoint() throws Exception {
        String expected = "/v1/sessions/vendor-session.42/manifest";
        assertEquals(
            expected,
            SynCapDevicePlugin.normalizeSessionManifestPath(
                expected,
                "192.168.1.13",
                8080,
                "vendor-session.42"
            )
        );
        assertEquals(
            expected,
            SynCapDevicePlugin.normalizeSessionManifestPath(
                "http://192.168.1.13:8080" + expected,
                "192.168.1.13",
                8080,
                "vendor-session.42"
            )
        );
        assertManifestUrlRejected("http://192.168.1.14:8080" + expected);
        assertManifestUrlRejected("http://192.168.1.13:8080/v1/sessions/other/manifest");
    }

    @Test
    public void sanitizesRtspPaths() {
        assertEquals("/cam2", SynCapDevicePlugin.normalizeRtspPath("/cam2"));
        assertEquals("/PRR", SynCapDevicePlugin.normalizeRtspPath("cam2"));
        assertEquals("/PRR", SynCapDevicePlugin.normalizeRtspPath("/cam2\r\nInjected: yes"));
    }

    @Test
    public void allowsSlowDeviceStatusTelemetry() {
        assertTrue(SynCapDevicePlugin.DEVICE_STATUS_READ_TIMEOUT_MS > 4500);
        assertTrue(
            SynCapDevicePlugin.DEVICE_STATUS_READ_TIMEOUT_MS
                > SynCapDevicePlugin.FAST_DEVICE_READ_TIMEOUT_MS
        );
    }

    @Test
    public void acceptsRtspAndRawHevcTcpPreviewUrls() {
        assertTrue(SynCapDevicePlugin.isSupportedPreviewUrl("rtsp://192.168.1.12:554/PRR"));
        assertTrue(SynCapDevicePlugin.isSupportedPreviewUrl("tcp://192.168.1.13:9100"));
        assertFalse(SynCapDevicePlugin.isSupportedPreviewUrl("http://192.168.1.13/video"));
        assertEquals("29.4118", LivePreviewActivity.RAW_HEVC_FRAME_RATE);
    }

    @Test
    public void neverActivelyProbesSingleConsumerRawTcpStreams() {
        SynCapDevicePlugin.RtspProbeResult result = SynCapDevicePlugin.probeTcp(
            "127.0.0.1", 9100
        );
        assertFalse(result.online);
        assertEquals("service_telemetry_required", result.reason);
        assertEquals(0, result.latencyMs);
    }

    @Test
    public void rawTcpPreviewNeverReconnectsAutomatically() {
        String rawTcpUrl = "tcp://192.168.1.13:9100";
        assertEquals(0, PreviewReconnectPolicy.automaticRetryLimit(rawTcpUrl));
        assertFalse(PreviewReconnectPolicy.resetRetryBudgetAfterStablePlayback(rawTcpUrl));

        String rtspUrl = "rtsp://192.168.1.12:554/PRR";
        assertEquals(3, PreviewReconnectPolicy.automaticRetryLimit(rtspUrl));
        assertEquals(1000, PreviewReconnectPolicy.automaticRetryDelayMs(rtspUrl, 1, 0));
        assertEquals(2000, PreviewReconnectPolicy.automaticRetryDelayMs(rtspUrl, 2, 0));
        assertEquals(4000, PreviewReconnectPolicy.automaticRetryDelayMs(rtspUrl, 3, 0));
        assertEquals(1000, PreviewReconnectPolicy.automaticRetryDelayMs(
            rtspUrl, Integer.MIN_VALUE, -1
        ));
        assertEquals(4000, PreviewReconnectPolicy.automaticRetryDelayMs(
            rtspUrl, Integer.MAX_VALUE, 0
        ));
        assertTrue(PreviewReconnectPolicy.resetRetryBudgetAfterStablePlayback(rtspUrl));
    }

    @Test
    public void rfcommUsesFourByteBigEndianLengthAndUtf8Payload() throws Exception {
        assertEquals(
            UUID.fromString("00001101-0000-1000-8000-00805f9b34fb"),
            SynCapDevicePlugin.SPP_SERVICE_UUID
        );
        byte[] payload = "{\"op\":\"wifi.scan\",\"label\":\"头环\"}".getBytes(StandardCharsets.UTF_8);
        ByteArrayOutputStream encoded = new ByteArrayOutputStream();

        SynCapDevicePlugin.writeRfcommFrame(encoded, payload);

        byte[] frame = encoded.toByteArray();
        assertEquals(payload.length + 4, frame.length);
        assertEquals(0, frame[0]);
        assertEquals(0, frame[1]);
        assertEquals(0, frame[2]);
        assertEquals(payload.length, frame[3] & 0xff);
        assertArrayEquals(payload, SynCapDevicePlugin.readRfcommFrame(new ByteArrayInputStream(frame)));
    }

    @Test
    public void rfcommRejectsInvalidLengthsAndTruncatedFrames() throws Exception {
        assertRfcommReadRejected(new byte[] { 0, 0, 0, 0 });
        assertRfcommReadRejected(new byte[] { 0, 0, 16, 1 });
        assertRfcommReadRejected(new byte[] { 0, 0, 0, 4, '{', '}' });
        assertRfcommReadRejected(new byte[] { 0, 0 });

        assertRfcommWriteRejected(new byte[0]);
        assertRfcommWriteRejected(new byte[SynCapDevicePlugin.BLUETOOTH_COMMAND_MAX_BYTES + 1]);
    }

    @Test
    public void rfcommRejectsMalformedUtf8BeforeJsonParsing() throws Exception {
        try {
            SynCapDevicePlugin.validateRfcommJson(new byte[] { '{', (byte) 0xc3, 0x28, '}' });
        } catch (IOException expected) {
            assertTrue(expected.getMessage().contains("UTF-8"));
            return;
        }
        throw new AssertionError("Expected malformed UTF-8 to be rejected");
    }

    @Test
    public void rfcommFallbackRequiresPairingTimeAndNoGattWrite() {
        assertTrue(SynCapDevicePlugin.shouldUseRfcommFallback(true, false, 999, 1000));
        assertFalse(SynCapDevicePlugin.shouldUseRfcommFallback(false, false, 999, 1000));
        assertFalse(SynCapDevicePlugin.shouldUseRfcommFallback(true, true, 999, 1000));
        assertFalse(SynCapDevicePlugin.shouldUseRfcommFallback(true, false, 1000, 1000));
    }

    @Test
    public void pairedClassicDevicesUseRfcommWithoutGatt() {
        assertTrue(SynCapDevicePlugin.shouldUseDirectRfcomm(
            true, BluetoothDevice.DEVICE_TYPE_CLASSIC, false
        ));
        assertFalse(SynCapDevicePlugin.shouldUseDirectRfcomm(
            false, BluetoothDevice.DEVICE_TYPE_CLASSIC, false
        ));
        assertFalse(SynCapDevicePlugin.shouldUseDirectRfcomm(
            true, BluetoothDevice.DEVICE_TYPE_DUAL, false
        ));
        assertFalse(SynCapDevicePlugin.shouldUseDirectRfcomm(
            true, BluetoothDevice.DEVICE_TYPE_LE, false
        ));
        assertFalse(SynCapDevicePlugin.shouldUseDirectRfcomm(
            true, BluetoothDevice.DEVICE_TYPE_UNKNOWN, false
        ));
    }

    @Test
    public void pairedDualDevicesAdvertisingSppDoNotProbeGatt() {
        assertTrue(SynCapDevicePlugin.shouldUseDirectRfcomm(
            true, BluetoothDevice.DEVICE_TYPE_DUAL, true
        ));
        assertFalse(SynCapDevicePlugin.shouldUseDirectRfcomm(
            false, BluetoothDevice.DEVICE_TYPE_DUAL, true
        ));
        assertFalse(SynCapDevicePlugin.shouldUseDirectRfcomm(
            true, BluetoothDevice.DEVICE_TYPE_LE, true
        ));
        assertFalse(SynCapDevicePlugin.shouldUseDirectRfcomm(
            true, BluetoothDevice.DEVICE_TYPE_UNKNOWN, true
        ));
    }

    @Test
    public void backgroundRecoveryRequiresCurrentBondAndPermission() {
        assertTrue(SynCapDevicePlugin.shouldSkipBackgroundRecovery(true, true, BluetoothDevice.BOND_NONE));
        assertTrue(SynCapDevicePlugin.shouldSkipBackgroundRecovery(true, true, BluetoothDevice.BOND_BONDING));
        assertTrue(SynCapDevicePlugin.shouldSkipBackgroundRecovery(true, false, BluetoothDevice.BOND_BONDED));
        assertFalse(SynCapDevicePlugin.shouldSkipBackgroundRecovery(true, true, BluetoothDevice.BOND_BONDED));
        // Each connection/retry checks the current state, not the cached device selection.
        assertTrue(SynCapDevicePlugin.shouldSkipBackgroundRecovery(true, true, BluetoothDevice.BOND_NONE));
        assertFalse(SynCapDevicePlugin.shouldSkipBackgroundRecovery(true, true, BluetoothDevice.BOND_BONDED));
    }

    @Test
    public void explicitProvisioningIsNotBlockedByBackgroundRecoveryGate() {
        assertFalse(SynCapDevicePlugin.shouldSkipBackgroundRecovery(false, false, BluetoothDevice.BOND_NONE));
        assertFalse(SynCapDevicePlugin.shouldSkipBackgroundRecovery(false, true, BluetoothDevice.BOND_BONDING));
        assertFalse(SynCapDevicePlugin.shouldSkipBackgroundRecovery(false, true, BluetoothDevice.BOND_BONDED));
    }

    @Test
    public void backgroundAuthenticationFailuresNeverInitiatePairing() {
        for (int retries = 0; retries < 4; retries++) {
            assertFalse(SynCapDevicePlugin.shouldRetryGattAuthentication(true, 5, retries));
            assertFalse(SynCapDevicePlugin.shouldRetryGattAuthentication(true, 15, retries));
        }
        assertTrue(SynCapDevicePlugin.shouldRetryGattAuthentication(false, 5, 0));
        assertTrue(SynCapDevicePlugin.shouldRetryGattAuthentication(false, 15, 2));
        assertFalse(SynCapDevicePlugin.shouldRetryGattAuthentication(false, 5, 3));
        assertFalse(SynCapDevicePlugin.shouldRetryGattAuthentication(false, 133, 0));
    }

    @Test
    public void rfcommAcceptsOneJsonObjectAndRejectsTrailingData() throws Exception {
        assertTrue(SynCapDevicePlugin.hasSingleJsonObjectEnvelope("{\"state\":\"ready\"}"));
        assertTrue(SynCapDevicePlugin.hasSingleJsonObjectEnvelope(" \n{\"text\":\"} \\\" ok\"}\r"));
        assertFalse(SynCapDevicePlugin.hasSingleJsonObjectEnvelope("[\"not-an-object\"]"));
        assertFalse(SynCapDevicePlugin.hasSingleJsonObjectEnvelope("{\"state\":\"ready\"}junk"));
        try {
            SynCapDevicePlugin.validateRfcommJson("{\"state\":\"ready\"}junk".getBytes(StandardCharsets.UTF_8));
        } catch (IOException expected) {
            return;
        }
        throw new AssertionError("Expected trailing RFCOMM response data to be rejected");
    }

    @Test
    public void probesTheRequestedHisiPath() throws Exception {
        AtomicReference<String> requestLine = new AtomicReference<>();
        AtomicReference<Throwable> serverError = new AtomicReference<>();
        try (ServerSocket server = new ServerSocket(0)) {
            Thread responder = new Thread(() -> {
                try (Socket socket = server.accept();
                     BufferedReader reader = new BufferedReader(new InputStreamReader(
                         socket.getInputStream(), StandardCharsets.US_ASCII))) {
                    requestLine.set(reader.readLine());
                    socket.getOutputStream().write(
                        "RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n"
                            .getBytes(StandardCharsets.US_ASCII)
                    );
                } catch (Throwable error) {
                    serverError.set(error);
                }
            }, "test-rtsp-server");
            responder.start();

            SynCapDevicePlugin.RtspProbeResult result = SynCapDevicePlugin.probeRtsp(
                "127.0.0.1", server.getLocalPort(), "/cam3"
            );
            responder.join(2000);

            if (serverError.get() != null) throw new AssertionError(serverError.get());
            assertTrue(result.online);
            assertEquals(
                "DESCRIBE rtsp://127.0.0.1:" + server.getLocalPort() + "/cam3 RTSP/1.0",
                requestLine.get()
            );
        }
    }

    private static void assertManifestIdentityRejected(
        LinkedHashMap<String, String> identities,
        String sessionId
    ) throws Exception {
        try {
            SynCapDevicePlugin.validateDeclaredSessionIdentities(identities, sessionId);
        } catch (IOException expected) {
            return;
        }
        throw new AssertionError("Expected session manifest validation to fail");
    }

    private static void assertManifestUrlRejected(String value) throws Exception {
        try {
            SynCapDevicePlugin.normalizeSessionManifestPath(
                value,
                "192.168.1.13",
                8080,
                "vendor-session.42"
            );
        } catch (IOException expected) {
            return;
        }
        throw new AssertionError("Expected session manifest URL validation to fail");
    }

    private static void assertRfcommReadRejected(byte[] frame) throws Exception {
        try {
            SynCapDevicePlugin.readRfcommFrame(new ByteArrayInputStream(frame));
        } catch (IOException expected) {
            return;
        }
        throw new AssertionError("Expected invalid RFCOMM frame to be rejected");
    }

    private static void assertRfcommWriteRejected(byte[] payload) throws Exception {
        try {
            SynCapDevicePlugin.writeRfcommFrame(new ByteArrayOutputStream(), payload);
        } catch (IOException expected) {
            return;
        }
        throw new AssertionError("Expected invalid RFCOMM payload to be rejected");
    }
}
