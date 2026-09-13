import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


def install_dbus_stub() -> None:
    if "dbus" in sys.modules:
        return

    dbus = types.ModuleType("dbus")
    exceptions = types.ModuleType("dbus.exceptions")
    service = types.ModuleType("dbus.service")
    mainloop = types.ModuleType("dbus.mainloop")
    glib = types.ModuleType("dbus.mainloop.glib")

    class DBusException(Exception):
        pass

    class Object:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

    class Array(list):
        def __init__(self, values: object = (), signature: str | None = None) -> None:
            super().__init__(values)
            self.signature = signature

    def method(*_args: object, **_kwargs: object):
        return lambda function: function

    exceptions.DBusException = DBusException
    service.Object = Object
    service.method = method
    dbus.exceptions = exceptions
    dbus.service = service
    dbus.mainloop = mainloop
    dbus.SystemBus = object
    dbus.ObjectPath = str
    dbus.UInt32 = int
    dbus.Byte = int
    dbus.Array = Array
    dbus.String = str
    dbus.Boolean = bool
    dbus.Interface = lambda value, _interface: value
    mainloop.glib = glib
    glib.DBusGMainLoop = lambda **_kwargs: None
    sys.modules.update({
        "dbus": dbus,
        "dbus.exceptions": exceptions,
        "dbus.service": service,
        "dbus.mainloop": mainloop,
        "dbus.mainloop.glib": glib,
    })


install_dbus_stub()

from ble_provisioning import BLE_STATUS_MAX_BYTES, ProvisioningStatus


class BleWifiScanTests(unittest.TestCase):
    def test_wifi_scan_compaction_keeps_strongest_networks_under_gatt_limit(self) -> None:
        networks = [
            {
                "ssid": f"Phone-Hotspot-{index:02d}-long-name",
                "rssi": -30 - index,
                "security": "wpa2-psk",
                "secure": True,
                "frequency": 2412,
                "password": "must-not-leak",
            }
            for index in range(12)
        ]

        compact = ProvisioningStatus._compact("wifi.scan", {
            "state": "completed",
            "networks": [networks[4], networks[0], networks[2], networks[0], *networks[5:]],
            "scannedAt": 1_786_700_000_000,
        })
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        self.assertLessEqual(len(encoded), BLE_STATUS_MAX_BYTES)
        self.assertEqual(compact["state"], "completed")
        self.assertEqual(compact["op"], "wifi.scan")
        self.assertEqual(compact["scannedAt"], 1_786_700_000_000)
        self.assertTrue(compact["truncated"])
        self.assertEqual(compact["networks"][0]["ssid"], "Phone-Hotspot-00-long-name")
        self.assertEqual(
            [network["rssi"] for network in compact["networks"]],
            sorted((network["rssi"] for network in compact["networks"]), reverse=True),
        )
        self.assertTrue(all(set(network) == {"ssid", "rssi", "security", "secure"} for network in compact["networks"]))
        self.assertNotIn(b"password", encoded)
        self.assertNotIn(b"frequency", encoded)

    def test_wifi_scan_dispatch_uses_claimed_read_only_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "claim-code"
            claim_path.write_text("123456\n", encoding="ascii")
            status = ProvisioningStatus(claim_path)
            result = {
                "state": "completed",
                "networks": [{"ssid": "HUAWEI_PURA_X", "rssi": -42, "security": "wpa2-psk", "secure": True}],
                "scannedAt": 1_786_700_000_000,
            }

            with patch.object(ProvisioningStatus, "_request", return_value=result) as request, \
                    patch("ble_provisioning.threading.Thread") as thread:
                status.dispatch({"op": "wifi.scan", "claimCode": "123456"})
                thread.call_args.kwargs["target"]()

            request.assert_called_once_with("/v1/wifi/scan", "GET", None, "123456", 15)
            payload = json.loads(status.encoded().decode("utf-8"))
            self.assertEqual(payload, {
                "state": "completed",
                "op": "wifi.scan",
                "networks": [{"ssid": "HUAWEI_PURA_X", "rssi": -42, "security": "wpa2-psk", "secure": True}],
                "scannedAt": 1_786_700_000_000,
            })

    def test_wifi_scan_rejects_an_invalid_internal_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claim_path = Path(directory) / "claim-code"
            claim_path.write_text("123456\n", encoding="ascii")
            status = ProvisioningStatus(claim_path)

            with patch("ble_provisioning.threading.Thread") as thread:
                status.dispatch({"op": "wifi.scan", "claimCode": "000000"})

            thread.assert_not_called()
            self.assertEqual(json.loads(status.encoded().decode("utf-8")), {
                "state": "failed",
                "error": "claim_required",
            })

    def test_device_status_compaction_whitelists_wifi_identity_and_address(self) -> None:
        compact = ProvisioningStatus._compact("device.status", {
            "state": "ready",
            "storage": {"target": "internal"},
            "capture": {"state": "idle"},
            "sessionCount": 0,
            "latestSession": None,
            "wifi": {
                "state": "connected",
                "ssid": "Phone-Hotspot",
                "ipAddress": "192.168.43.12",
                "password": "must-not-leak",
                "bssid": "00:11:22:33:44:55",
            },
        })
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        self.assertEqual(compact["wifi"], {
            "state": "connected",
            "ssid": "Phone-Hotspot",
            "ipAddress": "192.168.43.12",
        })
        self.assertNotIn(b"password", encoded)
        self.assertNotIn(b"bssid", encoded)

    def test_device_status_without_wifi_keeps_legacy_shape(self) -> None:
        compact = ProvisioningStatus._compact("device.status", {
            "state": "ready",
            "storage": {},
            "capture": {"state": "idle"},
        })

        self.assertNotIn("wifi", compact)

    def test_device_status_with_worst_case_session_and_capture_fits_gatt_limit(self) -> None:
        compact = ProvisioningStatus._compact("device.status", {
            "state": "ready",
            "storage": {
                "target": "usb",
                "usbAvailable": True,
                "usbMounted": True,
                "usbLabel": "S" * 64,
                "usbUuid": "U" * 64,
                "usbTotalBytes": 99_999_999_999,
                "usbFreeBytes": 88_888_888_888,
            },
            "capture": {
                "state": "recording",
                "captureId": "cap_20260814_123456_abcdef",
                "sessionId": "ses_20260814_123456_abcdef",
                "name": "采" * 32,
                "startedAt": "2026-08-14T12:34:56.789Z",
            },
            "sessionCount": 999_999,
            "latestSession": {
                "id": "ses_20260814_123456_abcdef",
                "name": "集" * 32,
                "createdAt": "2026-08-14T12:34:56.789Z",
                "durationMs": 999_999_999,
                "sizeBytes": 99_999_999_999,
                "status": "complete",
                "fileCount": 999,
            },
            "wifi": {
                "state": "connected",
                "ssid": "热" * 10,
                "ipAddress": "192.168.100.211",
            },
        })
        encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

        self.assertLessEqual(len(encoded), BLE_STATUS_MAX_BYTES)
        self.assertEqual(compact["wifi"]["state"], "connected")
        self.assertEqual(compact["wifi"]["ipAddress"], "192.168.100.211")


if __name__ == "__main__":
    unittest.main()
