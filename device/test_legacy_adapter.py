import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from legacy_adapter import (
    AdapterState,
    camera_state,
    parse_pmc_status,
    parse_ptp_status,
    query_pmc_status,
    stats,
    tail_lines,
)
from syncap_service import (
    Handler,
    ServiceState,
    StorageManager,
    WifiManager,
    apply_manifest_host,
    ensure_claim_code,
    normalize_endpoint_host,
    parse_byte_range,
    safe_capture_name,
    wifi_configuration_operation,
)


class LegacyAdapterTests(unittest.TestCase):
    @staticmethod
    def _manifest() -> dict[str, object]:
        return {
            "device": {"ipAddress": "192.168.1.12"},
            "cameras": [
                {
                    "id": f"cam{index}",
                    "preview": {"transport": "rtsp", "url": f"rtsp://192.168.1.12:{554 + index}/PRR"},
                }
                for index in range(4)
            ],
        }

    def test_manifest_uses_the_interface_that_accepted_the_request(self) -> None:
        manifest = self._manifest()
        telemetry = SimpleNamespace(device_ip="192.168.1.12", manifest=lambda: manifest)
        handler = Handler.__new__(Handler)
        handler.path = "/v1/manifest"
        handler.headers = {"Host": "192.168.1.12:8080"}
        handler.connection = SimpleNamespace(getsockname=lambda: ("192.168.1.13", 8080))
        handler.client_address = ("192.168.1.20", 49152)
        handler.state = SimpleNamespace(telemetry=telemetry)
        handler.send_json = Mock()

        handler.do_GET()

        payload = handler.send_json.call_args.args[0]
        self.assertEqual(payload["device"]["ipAddress"], "192.168.1.13")
        self.assertEqual(
            [camera["preview"]["url"] for camera in payload["cameras"]],
            [f"rtsp://192.168.1.13:{554 + index}/PRR" for index in range(4)],
        )

    def test_manifest_uses_a_valid_proxy_host_for_a_loopback_connection(self) -> None:
        manifest = self._manifest()
        handler = Handler.__new__(Handler)
        handler.headers = {"Host": "headring.local:8080"}
        handler.connection = SimpleNamespace(getsockname=lambda: ("127.0.0.1", 8080))
        handler.client_address = ("127.0.0.1", 49152)
        handler.state = SimpleNamespace(telemetry=SimpleNamespace(device_ip="192.168.1.12"))

        self.assertEqual(handler.manifest_host(), "headring.local")
        apply_manifest_host(manifest, handler.manifest_host())
        self.assertEqual(manifest["device"]["ipAddress"], "headring.local")
        self.assertEqual(manifest["cameras"][0]["preview"]["url"], "rtsp://headring.local:554/PRR")

    def test_invalid_host_header_falls_back_to_the_request_interface(self) -> None:
        handler = Handler.__new__(Handler)
        handler.headers = {"Host": "192.168.1.13:bad"}
        handler.connection = SimpleNamespace(getsockname=lambda: ("10.51.121.11", 8080))
        handler.client_address = ("10.51.121.5", 49152)
        handler.state = SimpleNamespace(telemetry=SimpleNamespace(device_ip="192.168.1.12"))

        self.assertIsNone(normalize_endpoint_host(handler.headers["Host"]))
        self.assertEqual(handler.manifest_host(), "10.51.121.11")

    @patch("syncap_service.routed_local_host", return_value="192.168.43.12")
    def test_manifest_falls_back_to_the_route_selected_interface(self, route: Mock) -> None:
        handler = Handler.__new__(Handler)
        handler.headers = {"Host": "invalid host"}
        handler.connection = SimpleNamespace(getsockname=lambda: ("0.0.0.0", 8080))
        handler.client_address = ("192.168.43.8", 49152)
        handler.state = SimpleNamespace(telemetry=SimpleNamespace(device_ip="192.168.1.12"))

        self.assertEqual(handler.manifest_host(), "192.168.43.12")
        route.assert_called_once_with(handler.client_address)

    def test_stats_uses_latest_sample_and_nearest_rank_p95(self) -> None:
        result = stats([0.041, 0.047, 0.043, 0.052])

        self.assertEqual(result["current"], 0.052)
        self.assertEqual(result["p95"], 0.052)
        self.assertEqual(result["samples"], 4)

    def test_capture_clock_status_is_lightweight_before_status_has_run(self) -> None:
        state = AdapterState("192.168.1.12", Path("/missing/camera.log"))

        with patch("legacy_adapter.query_pmc_status") as query_pmc:
            result = state.capture_clock_status()

        query_pmc.assert_not_called()
        self.assertEqual(result["state"], "unknown")
        self.assertFalse(result["requiredForCapture"])
        self.assertEqual(result["statusReason"], "status_not_sampled")

    def test_full_status_caches_an_isolated_clock_snapshot_for_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = AdapterState(
                "192.168.1.12",
                root / "camera.log",
                ptp_pid_path=root / "missing-ptp4l.pid",
                phc_path=root / "missing-ptp-device",
            )
            usage = SimpleNamespace(total=8 * 1024**3, free=6 * 1024**3)
            with patch("legacy_adapter.port_open", return_value=False), \
                    patch("legacy_adapter.process_named", return_value=False), \
                    patch("legacy_adapter.shutil.disk_usage", return_value=usage):
                status = state.status()

        cached = state.capture_clock_status()
        self.assertEqual(cached, status["clock"])
        cached["state"] = "tampered"
        self.assertEqual(state.capture_clock_status()["state"], status["clock"]["state"])

    def test_fresh_slave_servo_sample_is_locked(self) -> None:
        result = parse_ptp_status(
            [
                "ptp4l[12.1]: selected best master clock 001122.fffe.334455",
                "ptp4l[12.2]: port 1: UNCALIBRATED to SLAVE on MASTER_CLOCK_SELECTED",
                "ptp4l[12.3]: master offset -41 s2 freq +12 path delay 892",
            ],
            age_ms=120,
        )

        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["offsetFromMasterNs"], -41)
        self.assertEqual(result["meanPathDelayNs"], 892)
        self.assertEqual(result["grandmasterIdentity"], "001122.fffe.334455")

    def test_stale_servo_sample_cannot_claim_locked(self) -> None:
        result = parse_ptp_status(
            [
                "ptp4l[12.2]: port 1: UNCALIBRATED to SLAVE on MASTER_CLOCK_SELECTED",
                "ptp4l[12.3]: master offset 17 s2 freq +2 path delay 721",
            ],
            age_ms=8000,
        )

        self.assertEqual(result["state"], "unknown")
        self.assertIsNone(result["offsetFromMasterNs"])
        self.assertIsNone(result["meanPathDelayNs"])

    def test_camera_state_distinguishes_starting_degraded_and_ready(self) -> None:
        self.assertEqual(camera_state(False, [False, False, False, False]), "stopped")
        self.assertEqual(camera_state(True, [False, False, False, False]), "starting")
        self.assertEqual(camera_state(True, [True, True, False, False]), "degraded")
        self.assertEqual(camera_state(True, [True, True, True, True]), "ready")

    def test_listening_state_is_reported_without_fake_offset(self) -> None:
        result = parse_ptp_status(
            [
                "ptp4l[2.0]: selected best master clock 001122.fffe.334455",
                "ptp4l[3.0]: port 1: INITIALIZING to LISTENING on INIT_COMPLETE",
            ],
            age_ms=50,
        )

        self.assertEqual(result["state"], "listening")
        self.assertFalse(result["grandmasterPresent"])
        self.assertEqual(result["statusReason"], "grandmaster_not_detected")
        self.assertFalse(result["requiredForCapture"])
        self.assertIsNone(result["grandmasterIdentity"])
        self.assertIsNone(result["offsetFromMasterNs"])

    def test_pmc_listening_ignores_stale_current_dataset(self) -> None:
        result = parse_pmc_status(
            "portState LISTENING\n",
            "offsetFromMaster -155384.0\nmeanPathDelay 166028.0\n",
            "gmPresent true\ngmIdentity 207bd2.fffe.aca838\n",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "listening")
        self.assertFalse(result["grandmasterPresent"])
        self.assertEqual(result["statusReason"], "grandmaster_not_detected")
        self.assertFalse(result["requiredForCapture"])
        self.assertIsNone(result["offsetFromMasterNs"])
        self.assertIsNone(result["grandmasterIdentity"])

    def test_pmc_slave_reports_current_master_metrics(self) -> None:
        result = parse_pmc_status(
            "portState SLAVE\n",
            "offsetFromMaster -41.0\nmeanPathDelay 892.0\n",
            "gmPresent true\ngmIdentity 001122.fffe.334455\n",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["offsetFromMasterNs"], -41)
        self.assertEqual(result["meanPathDelayNs"], 892)

    def test_pmc_slave_without_current_grandmaster_is_not_locked(self) -> None:
        result = parse_pmc_status(
            "portState SLAVE\n",
            "offsetFromMaster 0.0\nmeanPathDelay 0.0\n",
            "gmPresent false\ngmIdentity e245f8.fffe.acd7c9\n",
        )

        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "locking")
        self.assertFalse(result["grandmasterPresent"])
        self.assertIsNone(result["grandmasterIdentity"])
        self.assertIsNone(result["offsetFromMasterNs"])

    def test_pmc_fault_is_reserved_for_an_actual_faulty_port(self) -> None:
        result = parse_pmc_status("portState FAULTY\n")

        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "faulty")
        self.assertEqual(result["statusReason"], "ptp_port_fault")
        self.assertFalse(result["requiredForCapture"])

    @patch("legacy_adapter.run_pmc")
    def test_query_pmc_slave_fetches_current_data_without_status_api_error(self, run_pmc: Mock) -> None:
        run_pmc.side_effect = [
            "portState SLAVE\n",
            "offsetFromMaster -41.0\nmeanPathDelay 892.0\n",
            "gmPresent true\ngmIdentity 001122.fffe.334455\n",
        ]

        result = query_pmc_status()

        self.assertIsNotNone(result)
        self.assertEqual(result["state"], "locked")
        self.assertEqual(result["offsetFromMasterNs"], -41)
        self.assertEqual(
            [call.args[0] for call in run_pmc.call_args_list],
            ["GET PORT_DATA_SET", "GET CURRENT_DATA_SET", "GET TIME_STATUS_NP"],
        )

    def test_tail_lines_handles_missing_and_truncated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.log"
            self.assertEqual(tail_lines(path), [])
            path.write_text("old\ncam0\ncam1\n", encoding="utf-8")

            self.assertEqual(tail_lines(path, max_bytes=10), ["cam1"])

    def test_http_ranges_support_resume_and_suffix_requests(self) -> None:
        self.assertIsNone(parse_byte_range(None, 100))
        self.assertEqual(parse_byte_range("bytes=20-", 100), (20, 99))
        self.assertEqual(parse_byte_range("bytes=20-39", 100), (20, 39))
        self.assertEqual(parse_byte_range("bytes=-10", 100), (90, 99))
        with self.assertRaises(ValueError):
            parse_byte_range("bytes=100-", 100)

    def test_capture_names_reject_empty_and_control_characters(self) -> None:
        self.assertEqual(safe_capture_name(" walking_test "), "walking_test")
        with self.assertRaises(ValueError):
            safe_capture_name("")
        with self.assertRaises(ValueError):
            safe_capture_name("bad\nname")

    def test_claim_code_is_fixed_internally_and_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "syncap" / "claim-code"
            path.parent.mkdir()
            path.write_text("654321\n", encoding="ascii")
            path.chmod(0o644)

            with patch("syncap_service.os.fchown") as fchown:
                ensure_claim_code(path)

            self.assertEqual(path.read_text(encoding="ascii"), "123456\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            fchown.assert_called_once()

    def test_valid_claim_code_needs_no_write_on_read_only_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claim-code"
            path.write_text("123456\n", encoding="ascii")
            path.chmod(0o600)
            metadata = path.lstat()
            root_metadata = SimpleNamespace(st_mode=metadata.st_mode, st_uid=0)

            with patch.object(Path, "lstat", return_value=root_metadata), \
                    patch.object(Path, "mkdir", side_effect=OSError("read-only filesystem")) as mkdir, \
                    patch("syncap_service.os.fchmod", side_effect=OSError("read-only filesystem")) as chmod, \
                    patch("syncap_service.os.fchown", side_effect=OSError("read-only filesystem")) as chown, \
                    patch("syncap_service.os.replace", side_effect=OSError("read-only filesystem")) as replace:
                ensure_claim_code(path)

            mkdir.assert_not_called()
            chmod.assert_not_called()
            chown.assert_not_called()
            replace.assert_not_called()

    def test_boot_script_valid_claim_code_fast_path_is_read_only(self) -> None:
        script_path = Path(__file__).with_name("S96syncap")
        script = script_path.read_text(encoding="utf-8")
        start = script.index("ensure_claim_code() {")
        end = script.index("\n}\n", start) + 3
        function = script[start:end]

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claim-code"
            path.write_text("123456\n", encoding="ascii")
            path.chmod(0o600)
            harness = "\n".join([
                function,
                "stat() { printf 'regular file:0:600\\n'; }",
                "sha256sum() { printf '%s  %s\\n' \"$CLAIM_CODE_SHA256\" \"$1\"; }",
                "mkdir() { return 91; }",
                "mv() { return 92; }",
                "chown() { return 93; }",
                "chmod() { return 93; }",
                f"CLAIM_CODE_FILE='{path}'",
                "CLAIM_CODE_SHA256=e150a1ec81e8e93e1eae2c3a77e66ec6dbd6a3b460f89c1d08aecf422ee401a0",
                "ensure_claim_code",
            ])

            result = subprocess.run(["/bin/sh", "-c", harness], capture_output=True, text=True, check=False)

            self.assertEqual(result.returncode, 0, result.stderr)

    def test_offline_status_adds_live_wifi_without_credentials(self) -> None:
        state = ServiceState.__new__(ServiceState)
        state.storage = SimpleNamespace(status=lambda: {
            "target": "internal",
            "usb": {"available": False},
        })
        state.capture = SimpleNamespace(
            list_sessions=lambda: [],
            capture_status=lambda: {"state": "idle"},
        )
        state.wifi = SimpleNamespace(status=lambda: {
            "state": "connected",
            "ssid": "Phone-Hotspot",
            "ipAddress": "192.168.43.12",
        })

        result = state.offline_status()

        self.assertEqual(result["wifi"], {
            "state": "connected",
            "ssid": "Phone-Hotspot",
            "ipAddress": "192.168.43.12",
        })

    def test_wifi_configuration_marker_is_removed_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "configuring"

            with self.assertRaises(RuntimeError):
                with wifi_configuration_operation(marker):
                    self.assertTrue(marker.is_file())
                    raise RuntimeError("association failed")

            self.assertFalse(marker.exists())

    def test_wifi_config_is_staged_atomically_without_plaintext_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wpa_supplicant.conf"
            manager = WifiManager(path)
            result = manager.configure("Lab-5G", "correct horse", "wpa2-psk", connect_now=False)

            self.assertEqual(result, {"state": "staged", "ssid": "Lab-5G"})
            self.assertIn('ssid="Lab-5G"', path.read_text(encoding="utf-8"))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_wifi_runtime_status_survives_service_restart(self) -> None:
        result = WifiManager._runtime_status(
            "bssid=00:11:22:33:44:55\nssid=Lab=5G\nwpa_state=COMPLETED\n",
            "4: wlan0 inet 192.168.100.211/24 scope global wlan0\n",
        )

        self.assertEqual(
            result,
            {"state": "connected", "ssid": "Lab=5G", "ipAddress": "192.168.100.211"},
        )

    def test_wifi_scan_results_are_deduplicated_and_sorted_by_rssi(self) -> None:
        output = "\n".join([
            "bssid / frequency / signal level / flags / ssid",
            "00:00:00:00:00:01\t2412\t-71\t[WPA2-PSK-CCMP][ESS]\tHUAWEI_PURA_X",
            "00:00:00:00:00:02\t5180\t-39\t[WPA2-PSK-CCMP][ESS]\tHUAWEI_PURA_X",
            "00:00:00:00:00:03\t2437\t-48\t[ESS]\tOpen-Lab",
            "00:00:00:00:00:04\t5745\t-55\t[WPA2-PSK+SAE-CCMP][ESS]\tPhone-WPA3",
            r"00:00:00:00:00:07\t2462\t-52\t[WPA2-PSK-CCMP][ESS]\t\xe5\x8d\x8e\xe4\xb8\xba\xe7\x83\xad\xe7\x82\xb9".replace("\\t", "\t"),
            "00:00:00:00:00:05\t2412\t-20\t[WPA2-PSK-CCMP][ESS]\t",
            "00:00:00:00:00:06\tbad\tbad\t[ESS]\tInvalid",
        ])

        networks = WifiManager._parse_scan_results(output)

        self.assertEqual([network["ssid"] for network in networks], ["HUAWEI_PURA_X", "Open-Lab", "华为热点", "Phone-WPA3"])
        self.assertEqual(networks[0], {
            "ssid": "HUAWEI_PURA_X",
            "rssi": -39,
            "signal": 100,
            "security": "wpa2-psk",
            "secure": True,
            "frequency": 5180,
        })
        self.assertEqual(networks[1]["security"], "open")
        self.assertFalse(networks[1]["secure"])
        self.assertEqual(networks[2]["security"], "wpa2-psk")
        self.assertEqual(networks[3]["security"], "wpa2-psk")

    def test_wifi_scan_security_keeps_pure_sae_distinct(self) -> None:
        self.assertEqual(WifiManager._scan_security("[WPA2-PSK+SAE-CCMP][ESS]"), "wpa2-psk")
        self.assertEqual(WifiManager._scan_security("[RSN-SAE-CCMP][ESS]"), "wpa3-sae")

    def test_wifi_scan_uses_wlan0_without_changing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wpa_supplicant.conf"
            manager = WifiManager(path)
            manager.SCAN_SETTLE_SECONDS = 0
            manager.SCAN_POLL_ATTEMPTS = 1
            scan_results = "\n".join([
                "bssid / frequency / signal level / flags / ssid",
                *[
                    f"00:00:00:00:00:{index:02x}\t2412\t{-30 - index}\t[WPA2-PSK-CCMP][ESS]\tHotspot-{index:02d}"
                    for index in range(WifiManager.MAX_SCAN_NETWORKS + 1)
                ],
            ])
            commands = [
                SimpleNamespace(returncode=0, stdout="OK\n"),
                SimpleNamespace(returncode=0, stdout=scan_results),
            ]

            with patch("syncap_service.subprocess.run", side_effect=commands) as run, \
                    patch("syncap_service.time.sleep"), patch("syncap_service.time.time", return_value=1234.5):
                result = manager.scan()

            self.assertEqual(result["state"], "completed")
            self.assertEqual(result["scannedAt"], 1_234_500)
            self.assertTrue(result["truncated"])
            self.assertEqual(len(result["networks"]), WifiManager.MAX_SCAN_NETWORKS)
            self.assertEqual(result["networks"][0]["ssid"], "Hotspot-00")
            self.assertEqual(run.call_args_list[0].args[0], ["/usr/sbin/wpa_cli", "-i", "wlan0", "scan"])
            self.assertEqual(run.call_args_list[1].args[0], ["/usr/sbin/wpa_cli", "-i", "wlan0", "scan_results"])
            self.assertFalse(path.exists())

    def test_wifi_scan_http_route_requires_the_internal_claim(self) -> None:
        scan_result = {"state": "completed", "networks": [], "scannedAt": 1_234_500, "truncated": False}
        wifi = SimpleNamespace(scan=Mock(return_value=scan_result))
        state = SimpleNamespace(valid_claim=lambda value: value == "123456", wifi=wifi)

        accepted = Handler.__new__(Handler)
        accepted.path = "/v1/wifi/scan"
        accepted.headers = {"X-SynCap-Claim": "123456"}
        accepted.state = state
        accepted.send_json = Mock()
        accepted.do_GET()

        wifi.scan.assert_called_once_with()
        accepted.send_json.assert_called_once_with(scan_result)

        wifi.scan.reset_mock()
        rejected = Handler.__new__(Handler)
        rejected.path = "/v1/wifi/scan"
        rejected.headers = {"X-SynCap-Claim": "000000"}
        rejected.state = state
        rejected.send_json = Mock()
        rejected.do_GET()

        wifi.scan.assert_not_called()
        rejected.send_json.assert_called_once_with({"error": "claim_required"}, status=403)

    def test_usb_storage_target_is_persisted_and_selected_for_capture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            internal = root / "internal"
            usb_root = root / "usb" / "SynCap"

            class FakeStorageManager(StorageManager):
                def _usb_info(self, mount: bool) -> dict[str, object]:
                    (usb_root / "sessions").mkdir(parents=True, exist_ok=True)
                    return {
                        "available": True,
                        "mounted": True,
                        "label": "SYNCAP",
                        "uuid": "TEST-UUID",
                        "filesystem": "exfat",
                        "mountPoint": str(root / "usb"),
                        "root": str(usb_root),
                        "totalBytes": 4 * 1024**3,
                        "freeBytes": 3 * 1024**3,
                    }

            config = internal / "storage.json"
            manager = FakeStorageManager(internal, config)
            result = manager.configure("usb")
            capture_root, descriptor = manager.capture_root()

            self.assertEqual(result["target"], "usb")
            self.assertEqual(capture_root, usb_root)
            self.assertEqual(descriptor["target"], "usb")
            self.assertEqual(descriptor["volumeUuid"], "TEST-UUID")
            self.assertIn('"target": "usb"', config.read_text(encoding="utf-8"))
            self.assertTrue((usb_root / "device.json").is_file())

    def test_internal_storage_below_minimum_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = StorageManager(root / "internal", root / "storage.json")
            usage = SimpleNamespace(
                total=4 * 1024**3,
                free=StorageManager.MIN_FREE_BYTES - 1,
            )

            with patch("syncap_service.shutil.disk_usage", return_value=usage):
                with self.assertRaisesRegex(RuntimeError, "internal storage has less than 512 MiB free"):
                    manager.configure("internal")
                with self.assertRaisesRegex(RuntimeError, "internal storage has less than 512 MiB free"):
                    manager.capture_root()

    def test_missing_and_low_space_usb_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class MissingUsbStorageManager(StorageManager):
                def _usb_info(self, mount: bool) -> dict[str, object]:
                    return {"available": False, "label": "SYNCAP"}

            missing = MissingUsbStorageManager(root / "missing", root / "missing.json")
            with self.assertRaisesRegex(RuntimeError, "SYNCAP USB drive is not available"):
                missing.configure("usb")
            missing.target = "usb"
            with self.assertRaisesRegex(RuntimeError, "SYNCAP USB drive is not mounted"):
                missing.capture_root()

            class LowSpaceUsbStorageManager(StorageManager):
                def _usb_info(self, mount: bool) -> dict[str, object]:
                    return {
                        "available": True,
                        "mounted": True,
                        "label": "SYNCAP",
                        "root": str(root / "usb" / "SynCap"),
                        "freeBytes": StorageManager.MIN_FREE_BYTES - 1,
                    }

            low_space = LowSpaceUsbStorageManager(root / "low", root / "low.json")
            with self.assertRaisesRegex(RuntimeError, "SYNCAP USB drive has less than 512 MiB free"):
                low_space.configure("usb")
            low_space.target = "usb"
            with self.assertRaisesRegex(RuntimeError, "SYNCAP USB drive has less than 512 MiB free"):
                low_space.capture_root()

    def test_storage_status_adds_capture_policy_and_target_readiness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class MissingUsbStorageManager(StorageManager):
                def _usb_info(self, mount: bool) -> dict[str, object]:
                    return {"available": False, "label": "SYNCAP"}

            manager = MissingUsbStorageManager(root / "internal", root / "storage.json")
            usage = SimpleNamespace(total=4 * 1024**3, free=3 * 1024**3)
            with patch("syncap_service.shutil.disk_usage", return_value=usage):
                status = manager.status()

            self.assertEqual(status["target"], "internal")
            self.assertEqual(status["capturePolicy"], {
                "minimumFreeBytes": StorageManager.MIN_FREE_BYTES,
                "estimatedBytesPerSecond": StorageManager.ESTIMATED_BYTES_PER_SECOND,
            })
            self.assertTrue(status["internal"]["canCapture"])
            self.assertIsNone(status["internal"]["reason"])
            self.assertFalse(status["usb"]["canCapture"])
            self.assertEqual(status["usb"]["reason"], "not_available")

    def test_storage_status_reports_low_space_for_both_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class LowSpaceUsbStorageManager(StorageManager):
                def _usb_info(self, mount: bool) -> dict[str, object]:
                    return {
                        "available": True,
                        "mounted": True,
                        "label": "SYNCAP",
                        "freeBytes": StorageManager.MIN_FREE_BYTES - 1,
                    }

            manager = LowSpaceUsbStorageManager(root / "internal", root / "storage.json")
            usage = SimpleNamespace(
                total=4 * 1024**3,
                free=StorageManager.MIN_FREE_BYTES - 1,
            )
            with patch("syncap_service.shutil.disk_usage", return_value=usage):
                status = manager.status()

            self.assertFalse(status["internal"]["canCapture"])
            self.assertEqual(status["internal"]["reason"], "insufficient_free_space")
            self.assertFalse(status["usb"]["canCapture"])
            self.assertEqual(status["usb"]["reason"], "insufficient_free_space")


if __name__ == "__main__":
    unittest.main()
