package com.syncap.studio;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

public class SynCapDevicePluginHttpErrorTest {
    @Test
    public void deviceJsonMessageAndCodeArePreserved() {
        SynCapDevicePlugin.DeviceHttpException error = SynCapDevicePlugin.DeviceHttpException.fromParsedResponse(
            409,
            "conflict",
            "SYNCAP USB drive is not mounted",
            "{\"error\":\"conflict\",\"message\":\"SYNCAP USB drive is not mounted\"}"
        );

        assertEquals(409, error.statusCode);
        assertEquals("conflict", error.serverCode);
        assertEquals("SYNCAP USB drive is not mounted", error.serverMessage);
        assertEquals("SYNCAP USB drive is not mounted", error.messageFor("Unable to start capture"));
        assertEquals("SYNCAP_CONFLICT", error.rejectionCode());
    }

    @Test
    public void malformedResponseUsesStableHttpFallback() {
        SynCapDevicePlugin.DeviceHttpException error = SynCapDevicePlugin.DeviceHttpException.fromResponse(
            503,
            "service unavailable"
        );

        assertNull(error.serverCode);
        assertNull(error.serverMessage);
        assertEquals("Unable to start capture", error.messageFor("Unable to start capture"));
        assertEquals("SYNCAP_HTTP_503", error.rejectionCode());
    }

    @Test
    public void storageTargetsAreStrict() {
        assertTrue(SynCapDevicePlugin.validStorageTarget("internal"));
        assertTrue(SynCapDevicePlugin.validStorageTarget("usb"));
        assertFalse(SynCapDevicePlugin.validStorageTarget(null));
        assertFalse(SynCapDevicePlugin.validStorageTarget("USB"));
        assertFalse(SynCapDevicePlugin.validStorageTarget(""));
    }

    @Test
    public void wifiScanCommandUsesDeviceOperationAndFixedClaimCode() {
        assertEquals(
            "{\"op\":\"wifi.scan\",\"claimCode\":\"123456\"}",
            SynCapDevicePlugin.buildWifiScanPayload()
        );
    }

    @Test
    public void wifiScanBleTimeoutAndDiscoveryFallbackAreBounded() {
        assertTrue(SynCapDevicePlugin.BLE_WIFI_SCAN_TIMEOUT_MS < 60000);
        assertTrue(SynCapDevicePlugin.BLE_WIFI_SCAN_TIMEOUT_MS > 30000);
        assertTrue(SynCapDevicePlugin.BLE_DEVICE_STATUS_TIMEOUT_MS < 30000);
        assertTrue(SynCapDevicePlugin.BLE_DEVICE_STATUS_TIMEOUT_MS > 15000);
        assertTrue(SynCapDevicePlugin.BLE_MTU_CALLBACK_TIMEOUT_MS > 800);
        assertTrue(
            SynCapDevicePlugin.BLE_SERVICE_FALLBACK_INTERVAL_MS
                * SynCapDevicePlugin.BLE_SERVICE_FALLBACK_ATTEMPTS
                <= 5000
        );
    }
}
