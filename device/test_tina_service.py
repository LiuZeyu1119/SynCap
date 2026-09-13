import hashlib
import json
import os
from pathlib import Path
import signal
import shlex
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
MCAP_MAGIC = bytes.fromhex("894d434150300d0a")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_until(predicate, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.03)
    raise AssertionError("condition did not become true before timeout")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    status = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return bool(status) and not status.startswith("Z")


class RunningService:
    def __init__(self, binary: Path, *, storage: Path, ring: Path, dry_run: bool = True,
                 qgapp: Path | None = None, wifi_command: Path | None = None,
                 iw_command: Path | None = None):
        self.binary = binary
        self.storage = storage
        self.ring = ring
        self.dry_run = dry_run
        self.qgapp = qgapp
        self.wifi_command = wifi_command
        self.iw_command = iw_command
        self.port = free_port()
        self.process: subprocess.Popen[bytes] | None = None
        self.log = tempfile.TemporaryFile()
        self.pidfile = ring.parent / "qgapp.pid"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "RunningService":
        command = [
            str(self.binary),
            "--port", str(self.port),
            "--ring", str(self.ring),
            "--storage", str(self.storage),
            "--allow-unmounted-storage",
            "--min-free-bytes", "1",
            "--qgapp-pidfile", str(self.pidfile),
            "--qgapp-log", str(self.ring.parent / "qgapp.log"),
        ]
        if self.dry_run:
            command.append("--dry-run")
        else:
            command.append("--allow-missing-preview-listeners")
        if self.qgapp:
            command.extend(["--qgapp", str(self.qgapp)])
        if self.wifi_command:
            command.extend(["--wifi-command", str(self.wifi_command)])
        if self.iw_command:
            command.extend(["--iw-command", str(self.iw_command)])
        self.process = subprocess.Popen(
            command,
            cwd=self.ring.parent,
            stdout=self.log,
            stderr=self.log,
        )

        def healthy() -> bool:
            try:
                return self.request("GET", "/health")[0] == 200
            except (OSError, URLError):
                return False

        wait_until(healthy)
        return self

    def close(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.pidfile.exists():
            try:
                qgapp_pid = int(self.pidfile.read_text().strip())
                os.kill(qgapp_pid, signal.SIGKILL)
            except (OSError, ValueError, ProcessLookupError):
                pass
        self.log.close()

    def request(self, method: str, path: str, payload=None, headers=None):
        body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        request = Request(self.base_url + path, data=body, method=method,
                          headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urlopen(request, timeout=12) as response:
                return response.status, response.headers, response.read()
        except HTTPError as error:
            try:
                return error.code, error.headers, error.read()
            finally:
                error.close()

    def diagnostics(self) -> str:
        assert self.process is not None
        qgapp_log = self.ring.parent / "qgapp.log"
        qgapp_output = ""
        if qgapp_log.exists():
            with qgapp_log.open("rb") as stream:
                qgapp_output = "\nqgapp fixture log:\n" + stream.read(65536).decode(errors="replace")
        return (f"service pid={self.process.pid} port={self.port} exit={self.process.poll()}\n" +
                os.pread(self.log.fileno(), 65536, 0).decode(errors="replace") + qgapp_output)


class TinaServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build_dir = tempfile.TemporaryDirectory(prefix="syncap-tina-build-")
        cls.binary = Path(cls.build_dir.name) / "syncap-tina-service"
        compiler = os.environ.get("CC", "cc")
        subprocess.run([
            compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-pedantic",
            str(ROOT / "tina_service.c"), "-o", str(cls.binary),
        ], check=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.build_dir.cleanup()

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="syncap-tina-test-")
        self.root = Path(self.temp.name)
        self.ring = self.root / "ring"
        self.storage = self.root / "sd"
        self.ring.mkdir()
        self.storage.mkdir()
        self.services: list[RunningService] = []

    def tearDown(self) -> None:
        for service in reversed(self.services):
            service.close()
        self.temp.cleanup()

    def service(self, **options) -> RunningService:
        service = RunningService(self.binary, storage=self.storage, ring=self.ring, **options).start()
        self.services.append(service)
        return service

    def live_idle_service(self, label: str) -> RunningService:
        fake_qgapp = self.root / f"fake-qgapp-{label}.sh"
        started = self.root / f"fake-qgapp-{label}.started"
        fake_qgapp.write_text(
            f"#!/bin/sh\n: > '{started}'\nwhile :; do sleep 1; done\n"
        )
        fake_qgapp.chmod(0o755)
        service = self.service(dry_run=False, qgapp=fake_qgapp)
        wait_until(started.exists)
        return service

    @staticmethod
    def crash_service(service: RunningService) -> None:
        assert service.process is not None
        service.process.kill()
        service.process.wait(timeout=3)

    def write_segment(
        self,
        name: str,
        *,
        clean: bool = True,
        fsync_marks: int = 62,
        frame_backstep: int = 0,
        ring: Path | None = None,
        status_payload: dict | None = None,
    ) -> tuple[Path, Path]:
        target_ring = ring or self.ring
        mcap = target_ring / f"{name}.mcap"
        status = target_ring / f"{name}.mcap.status.json"
        mcap.write_bytes(b"mcap-fixture-payload" + MCAP_MAGIC)
        payload = status_payload or self.clean_segment_status(
            fsync_marks=fsync_marks,
            frame_backstep=frame_backstep,
        )
        payload["state"] = "clean" if clean else "failed"
        status.write_text(json.dumps(payload))
        return mcap, status

    @staticmethod
    def clean_segment_status(*, fsync_marks: int = 62, frame_backstep: int = 0) -> dict:
        return {
            "state": "clean",
            "recording": {
                "serial": "c06unknown",
                "recording_id": "1970-01-15-20-38-33-c06unknown",
                "recorded_at": "1970-01-15T20:38:33Z",
            },
            "drops": {
                "left": 0,
                "right": 0,
                "imu_gaps": 0,
                "venc_resets": 0,
                "q_drops": 0,
            },
            "audio": {
                "enabled": True,
                "frames": fsync_marks // 2 + 2,
                "timeouts": 0,
                "backsteps": 0,
                "gaps": 0,
                "ts_offset_us": -10300,
            },
            "sync": {
                "hw_trigger": True,
                "fsync_marks": fsync_marks,
                "imu_fc_first": 3,
                "imu_fc_last": fsync_marks + 3,
                "restamped": max(0, fsync_marks * 2 - 2),
                "restamp_by_fc": 0,
                "restamp_by_vft": max(0, fsync_marks * 2 - 2),
                "vft_ambig": 0,
                "vft_fc_disagree": 0,
                "vmeta_miss": 9,
                "restamp_fallback": 2,
                "frame_backstep": frame_backstep,
                "frame_ksnap": 0,
                "imu_ksnap": 0,
                "imu_frame_d_med_us": -3415,
                "fc_drops": [0, 0],
                "sst_marks": fsync_marks,
                "sst_lag_flips": 0,
                "sst_offgrid": 0,
                "imu_held": 208,
                "gyro_drift_ppm": -4247,
                "frame_ts_offset_us": 0,
            },
        }

    @staticmethod
    def progressive_status_shell(payload: dict, index: str = "i") -> str:
        encoded = json.dumps(payload, separators=(",", ":"))
        frames = payload["audio"]["frames"]
        fsync_marks = payload["sync"]["fsync_marks"]
        imu_last = payload["sync"]["imu_fc_last"]
        return (
            f"status=$(printf '%s\\n' {shlex.quote(encoded)} | sed "
            f"-e \"s/\\\"frames\\\":{frames}/\\\"frames\\\":$(({frames} + ({index} * 32)))/\" "
            f"-e \"s/\\\"fsync_marks\\\":{fsync_marks}/\\\"fsync_marks\\\":$(({fsync_marks} + ({index} * 60)))/\" "
            f"-e \"s/\\\"imu_fc_last\\\":{imu_last}/\\\"imu_fc_last\\\":$(({imu_last} + ({index} * 60)))/\")"
        )

    @classmethod
    def cumulative_segment_status(cls, step: int, *, imu_gaps: int = 9) -> dict:
        fsync_marks = 2402 + step * 60
        payload = cls.clean_segment_status(fsync_marks=fsync_marks, frame_backstep=6)
        payload["drops"].update(
            left=2,
            right=3,
            imu_gaps=imu_gaps,
            venc_resets=4,
            q_drops=5,
        )
        payload["audio"].update(
            frames=1276 + step * 32,
            timeouts=7,
            backsteps=8,
            gaps=9,
        )
        payload["sync"].update(
            imu_fc_last=2405 + step * 60,
            fc_drops=[5, 5],
        )
        return payload

    @staticmethod
    def decoded(body: bytes):
        return json.loads(body.decode())

    def assert_sync_skew_is_unmeasured(self, runtime: dict) -> None:
        empty_metric = {
            "current": None, "mean": None, "p95": None,
            "min": None, "max": None, "samples": 0,
        }
        for name in ("acquisitionSkewMs", "previewPtsSkewMs", "previewCameraSkewMs"):
            self.assertEqual(runtime["sync"][name], empty_metric)
        for camera in runtime["cameras"]:
            self.assertIsNone(camera["groupSkewMs"])

    def test_manifest_storage_and_explicit_no_sd_error(self) -> None:
        missing_storage = self.root / "not-mounted"
        service = RunningService(
            self.binary, storage=missing_storage, ring=self.ring, dry_run=True,
        ).start()
        self.services.append(service)

        status, _, body = service.request("GET", "/v1/manifest")
        manifest = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual([camera["id"] for camera in manifest["cameras"]], ["left", "right"])
        self.assertEqual(manifest["cameras"][0]["preview"]["transport"], "tcp-hevc")
        self.assertAlmostEqual(manifest["cameras"][0]["preview"]["frameRate"], 29.4118)
        self.assertIn("storage.sd", manifest["capabilities"])
        self.assertNotIn("calibration", manifest)

        status, _, body = service.request("GET", "/v1/storage")
        storage = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertFalse(storage["canCapture"])
        self.assertEqual(storage["usb"]["mediaType"], "sd")
        self.assertEqual(set(missing_storage.parent.iterdir()), {self.ring, self.storage})

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "no sd"})
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "storage.insufficient")

        status, _, body = service.request("POST", "/v1/storage/configure", {"target": "internal"})
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "storage.unsupported_target")

    def test_wifi_scan_status_validation_and_argument_safety(self) -> None:
        fake_wifi = self.root / "fake-wifi.sh"
        fake_iw = self.root / "fake-iw.sh"
        wifi_log = self.root / "wifi-arguments.txt"
        wifi_log.write_text("")
        current_ssid = self.root / "current-ssid.txt"
        safe_ssid = 'Headset "Gold"\\Lab'
        current_ssid.write_text(safe_ssid)
        fake_wifi.write_text(f"""#!/bin/sh
printf '[%s]\\n' "$@" >> '{wifi_log}'
if [ "$1" = "-c" ]; then
    printf '%s' "$2" > '{current_ssid}'
fi
exit 0
""")
        fake_iw.write_text(f"""#!/bin/sh
if [ "$1" = "dev" ] && [ "$2" = "wlan0" ] && [ "$3" = "scan" ]; then
    printf '%s\\n' \\
        'BSS 00:00:00:00:00:01(on wlan0)' \\
        '    signal: -30.00 dBm' \\
        '    SSID: {safe_ssid}' \\
        'BSS 00:00:00:00:00:02(on wlan0)' \\
        '    signal: -45.00 dBm' \\
        '    SSID: {safe_ssid}' \\
        '    RSN:' \\
        '        Authentication suites: PSK' \\
        'BSS 00:00:00:00:00:03(on wlan0)' \\
        '    signal: -55.00 dBm' \\
        '    SSID: Guest'
elif [ "$1" = "dev" ] && [ "$2" = "wlan0" ] && [ "$3" = "link" ]; then
    ssid=$(sed -n '1p' '{current_ssid}')
    printf 'Connected to 00:00:00:00:00:02\\nSSID: %s\\n' "$ssid"
else
    exit 2
fi
""")
        fake_wifi.chmod(0o755)
        fake_iw.chmod(0o755)
        service = RunningService(
            self.binary,
            storage=self.storage,
            ring=self.ring,
            dry_run=True,
            wifi_command=fake_wifi,
            iw_command=fake_iw,
        ).start()
        self.services.append(service)

        status, _, body = service.request("GET", "/v1/wifi/scan")
        scan = self.decoded(body)
        self.assertEqual(status, 200, f"{body!r}\n{service.diagnostics()}")
        self.assertIn(b'\\"Gold\\"\\\\Lab', body)
        selected = next(network for network in scan["networks"] if network["ssid"] == safe_ssid)
        self.assertEqual(selected["security"], "wpa2-psk")
        self.assertTrue(selected["secure"])
        self.assertEqual(
            len([network for network in scan["networks"] if network["ssid"] == safe_ssid]),
            1,
        )

        status, _, body = service.request("GET", "/v1/wifi/status")
        wifi_status = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(wifi_status["state"], "obtaining_ip")
        self.assertEqual(wifi_status["ssid"], safe_ssid)
        self.assertEqual(wifi_log.read_text(), "", "scan/status must not reconfigure the radio")

        commands_before_rejected_requests = wifi_log.read_bytes()
        status, _, body = service.request("POST", "/v1/wifi/configure", {
            "claimCode": "123456",
            "ssid": safe_ssid,
            "security": "wpa2-psk",
            "password": "short",
        })
        self.assertEqual(status, 400)
        self.assertEqual(self.decoded(body)["error"], "wifi.invalid_password")

        status, _, body = service.request("POST", "/v1/wifi/configure", {
            "claimCode": "123456",
            "ssid": safe_ssid,
            "security": "wpa3-sae",
            "password": "long-enough-password",
        })
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "wifi.unsupported_security")
        self.assertEqual(wifi_log.read_bytes(), commands_before_rejected_requests)

        injection_ssid = "$(touch wifi-pwn)"
        injection_sentinel = self.root / "wifi-pwn"
        configure_done = threading.Event()

        def configure_with_metacharacters() -> None:
            try:
                service.request("POST", "/v1/wifi/configure", {
                    "claimCode": "123456",
                    "ssid": injection_ssid,
                    "security": "wpa2-psk",
                    "password": "valid-password",
                })
            except Exception:
                pass
            finally:
                configure_done.set()

        thread = threading.Thread(target=configure_with_metacharacters, daemon=True)
        thread.start()
        wait_until(lambda: f"[{injection_ssid}]" in wifi_log.read_text())
        time.sleep(0.2)
        self.assertFalse(injection_sentinel.exists())
        arguments = wifi_log.read_text().splitlines()
        self.assertEqual(arguments[:3], ["[-S]", "[countrycode]", "[cCN]"])
        connect_index = len(arguments) - 3
        self.assertEqual(arguments[connect_index:], [
            "[-c]", f"[{injection_ssid}]", "[valid-password]",
        ])

        assert service.process is not None
        service.process.kill()
        service.process.wait(timeout=3)
        thread.join(timeout=3)
        self.assertTrue(configure_done.is_set())
        self.assertFalse(injection_sentinel.exists())

    def wifi_scan_service(self, attempts: list[tuple[int, str]],
                          cache: tuple[int, str] = (0, "")) -> tuple[RunningService, Path]:
        prefix = self.root / f"wifi-scan-{len(self.services)}"
        fake_iw = prefix.with_suffix(".sh")
        command_log = prefix.with_suffix(".log")
        counter = prefix.with_suffix(".count")

        def response(result: tuple[int, str]) -> str:
            code, output = result
            return f"printf '%s' {shlex.quote(output)}; exit {code}"

        cases = "\n".join(
            f"    {index}) {response(result)} ;;" for index, result in enumerate(attempts)
        )
        fake_iw.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> {shlex.quote(str(command_log))}
if [ "$*" = "dev wlan0 scan dump" ]; then
    {response(cache)}
elif [ "$*" = "dev wlan0 scan" ]; then
    count=0
    [ ! -f {shlex.quote(str(counter))} ] || read count < {shlex.quote(str(counter))}
    printf '%s\\n' "$((count + 1))" > {shlex.quote(str(counter))}
    case "$count" in
{cases}
    *) {response(attempts[-1])} ;;
    esac
fi
exit 2
""")
        fake_iw.chmod(0o755)
        return self.service(iw_command=fake_iw), command_log

    @staticmethod
    def wifi_bss(ssid: str, age: str | None = "1000 ms ago") -> str:
        age_line = "" if age is None else f"    last seen: {age}\n"
        return ("BSS 00:00:00:00:00:01(on wlan0)\n"
                "    signal: -40.00 dBm\n" + age_line + f"    SSID: {ssid}\n"
                "    RSN:\n        Authentication suites: PSK\n")

    def test_wifi_scan_retries_busy_then_returns_active_result(self) -> None:
        busy = (240, "command failed: Resource busy (-16)\n")
        service, command_log = self.wifi_scan_service([
            busy, busy, (0, self.wifi_bss("Phone hotspot")),
        ])
        started = time.monotonic()
        status, _, body = service.request("GET", "/v1/wifi/scan")
        trace = command_log.read_text().splitlines() if command_log.exists() else []
        details = f"{body!r}\ncommands={trace!r}\n{service.diagnostics()}"
        self.assertEqual(status, 200, details)
        self.assertEqual([item["ssid"] for item in self.decoded(body)["networks"]], ["Phone hotspot"], details)
        self.assertNotIn("cached", self.decoded(body), details)
        self.assertEqual(trace, ["dev wlan0 scan"] * 3, details)
        self.assertLess(time.monotonic() - started, 5, details)

    def test_wifi_scan_success_with_no_bss_returns_empty_networks(self) -> None:
        service, command_log = self.wifi_scan_service([(0, "")],
                                                      (0, self.wifi_bss("Cached")))
        status, _, body = service.request("GET", "/v1/wifi/scan")
        trace = command_log.read_text().splitlines() if command_log.exists() else []
        details = f"{body!r}\ncommands={trace!r}\n{service.diagnostics()}"
        self.assertEqual(status, 200, details)
        self.assertEqual(self.decoded(body)["networks"], [], details)
        self.assertNotIn("cached", self.decoded(body), details)
        self.assertEqual(trace, ["dev wlan0 scan"], details)

    def test_wifi_scan_busy_uses_only_recent_nonempty_cache(self) -> None:
        busy = (240, "command failed: Resource busy (-16)\n")
        cache = (self.wifi_bss("Phone hotspot", "0 ms ago") +
                 self.wifi_bss("Router", "15000 ms ago") +
                 self.wifi_bss("Old network", "15001 ms ago") +
                 self.wifi_bss("Unknown age", None) +
                 self.wifi_bss("Invalid age", "1 seconds ago"))
        service, command_log = self.wifi_scan_service([busy], (0, cache))
        started = time.monotonic()
        status, _, body = service.request("GET", "/v1/wifi/scan")
        scan = self.decoded(body)
        trace = command_log.read_text().splitlines() if command_log.exists() else []
        details = f"{body!r}\ncommands={trace!r}\n{service.diagnostics()}"
        self.assertEqual(status, 200, details)
        self.assertEqual([item["ssid"] for item in scan["networks"]], ["Phone hotspot", "Router"], details)
        self.assertTrue(scan["cached"], details)
        self.assertEqual(scan["cacheMaxAgeMs"], 15000, details)
        self.assertGreaterEqual(time.time() * 1000 - scan["scannedAt"], 15000, details)
        self.assertEqual(trace, ["dev wlan0 scan"] * 3 + ["dev wlan0 scan dump"], details)
        self.assertLess(time.monotonic() - started, 5, details)

    def test_wifi_scan_busy_rejects_empty_stale_unknown_and_failed_cache(self) -> None:
        busy = (240, "command failed: Resource busy (-16)\n")
        for cache in [(0, ""), (0, self.wifi_bss("Old network", "15001 ms ago")),
                      (0, self.wifi_bss("Unknown age", None)),
                      (0, self.wifi_bss("Invalid age", "-1 ms ago")),
                      (1, self.wifi_bss("Failed dump"))]:
            with self.subTest(cache=cache):
                service, command_log = self.wifi_scan_service([busy], cache)
                status, _, body = service.request("GET", "/v1/wifi/scan")
                trace = command_log.read_text().splitlines() if command_log.exists() else []
                details = f"{body!r}\ncommands={trace!r}\n{service.diagnostics()}"
                self.assertEqual(status, 500, details)
                self.assertEqual(self.decoded(body)["error"], "wifi.scan_failed", details)
                self.assertEqual(trace, ["dev wlan0 scan"] * 3 + ["dev wlan0 scan dump"], details)

    def test_wifi_scan_real_failure_is_not_retried_or_hidden_by_cache(self) -> None:
        for failure in [(237, "command failed: No such device (-19)\n"),
                        (1, "Resource busy unrelated failure\n"),
                        (240, "unrecognized command failure\n")]:
            with self.subTest(failure=failure):
                service, command_log = self.wifi_scan_service([failure],
                                                              (0, self.wifi_bss("Cached")))
                status, _, body = service.request("GET", "/v1/wifi/scan")
                trace = command_log.read_text().splitlines() if command_log.exists() else []
                details = f"{body!r}\ncommands={trace!r}\n{service.diagnostics()}"
                self.assertEqual(status, 500, details)
                self.assertEqual(self.decoded(body)["error"], "wifi.scan_failed", details)
                self.assertEqual(trace, ["dev wlan0 scan"], details)

    def test_wifi_scan_hung_command_is_bounded_without_cache_fallback(self) -> None:
        fake_iw = self.root / "hung-iw.sh"
        command_log = self.root / "hung-iw.log"
        fake_iw.write_text(f"""#!/bin/sh
printf '%s\\n' "$*" >> {shlex.quote(str(command_log))}
exec sleep 30
""")
        fake_iw.chmod(0o755)
        service = self.service(iw_command=fake_iw)
        started = time.monotonic()
        status, _, body = service.request("GET", "/v1/wifi/scan")
        trace = command_log.read_text().splitlines() if command_log.exists() else []
        details = f"{body!r}\ncommands={trace!r}\n{service.diagnostics()}"
        self.assertEqual(status, 500, details)
        self.assertEqual(self.decoded(body)["error"], "wifi.scan_failed", details)
        self.assertEqual(trace, ["dev wlan0 scan"], details)
        self.assertLess(time.monotonic() - started, 8, details)

    def test_sync_health_uses_stable_counter_deltas_not_lifetime_startup_totals(self) -> None:
        service = self.live_idle_service("sync")
        status, _, body = service.request("GET", "/v1/status")
        runtime = self.decoded(body)
        self.assertEqual(status, 200)
        self.assert_sync_skew_is_unmeasured(runtime)
        self.assertIsNone(runtime["sync"]["hardwareTrigger"])
        self.assertIsNone(runtime["sync"]["observedTriggerCount"])
        self.assertEqual(runtime["sync"]["counterHealth"], "unverified")

        first_mcap, first_status = self.write_segment(
            "1970-sync-0001", fsync_marks=73, frame_backstep=6,
        )
        wait_until(lambda: not first_mcap.exists() and not first_status.exists())
        status, _, body = service.request("GET", "/v1/status")
        self.assertEqual(status, 200)
        runtime = self.decoded(body)
        self.assert_sync_skew_is_unmeasured(runtime)
        self.assertEqual(runtime["sync"]["state"], "waiting_for_clean_segment")
        self.assertTrue(runtime["sync"]["hardwareTrigger"])
        self.assertEqual(runtime["sync"]["counterHealth"], "unverified")
        self.assertEqual(runtime["sync"]["observedTriggerCount"], 73)

        second_mcap, second_status = self.write_segment(
            "1970-sync-0002", fsync_marks=133, frame_backstep=6,
        )
        wait_until(lambda: not second_mcap.exists() and not second_status.exists())
        status, _, body = service.request("GET", "/v1/status")
        runtime = self.decoded(body)
        self.assertEqual(status, 200)
        self.assert_sync_skew_is_unmeasured(runtime)
        self.assertEqual(runtime["sync"]["state"], "hardware_trigger_active")
        self.assertTrue(runtime["sync"]["hardwareTrigger"])
        self.assertEqual(runtime["sync"]["counterHealth"], "stable")
        self.assertEqual(runtime["sync"]["observedTriggerCount"], 133)

    def test_capture_stop_session_export_range_and_idle_cleanup(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "双目 测试"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(capture["state"], "recording")
        active_marker = self.decoded((self.ring / ".syncap-active.json").read_bytes())
        self.assertEqual(active_marker["captureId"], capture["captureId"])
        self.assertEqual(active_marker["sessionId"], capture["sessionId"])

        first_mcap, first_status = self.write_segment("1970-fixture-0001")
        wait_until(lambda: not first_mcap.exists() and not first_status.exists())

        stop_result: dict[str, object] = {}

        def stop_capture() -> None:
            code, headers, response = service.request(
                "POST", f"/v1/captures/{capture['captureId']}/stop", {},
            )
            stop_result.update(code=code, headers=headers, body=response)

        thread = threading.Thread(target=stop_capture)
        thread.start()
        time.sleep(0.15)
        self.write_segment("1970-fixture-0002", fsync_marks=124)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(stop_result["code"], 200)
        completed = self.decoded(stop_result["body"])
        self.assertEqual(completed["state"], "completed")
        self.assertFalse((self.ring / ".syncap-active.json").exists())
        self.assertGreater((self.ring / ".syncap-quarantine.reserve").stat().st_size, 0)
        session_id = completed["sessionId"]
        status, _, body = service.request("GET", f"/v1/sessions/{session_id}/manifest")
        manifest = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(manifest["state"], "complete")
        self.assertNotIn("failureReason", manifest)
        self.assertEqual(manifest["durationMs"], completed["session"]["durationMs"])
        self.assertGreaterEqual(manifest["durationMs"], 100)

        status, _, body = service.request("POST", f"/v1/sessions/{session_id}/prepare-export", {})
        export = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertTrue(export["rangeSupported"])
        self.assertEqual(len(export["files"]), 4)
        mcap_entry = next(item for item in export["files"] if item["name"] == "1970-fixture-0002.mcap")
        expected = hashlib.sha256(b"mcap-fixture-payload" + MCAP_MAGIC).hexdigest()
        self.assertEqual(mcap_entry["sha256"], expected)

        status, headers, body = service.request("GET", mcap_entry["url"], headers={"Range": "bytes=2-8"})
        self.assertEqual(status, 206)
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(headers["Content-Range"], "bytes 2-8/28")
        self.assertEqual(body, (b"mcap-fixture-payload" + MCAP_MAGIC)[2:9])

        status, _, body = service.request("GET", "/v1/status")
        runtime = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(runtime["clock"]["state"], "unsupported")
        self.assertEqual(runtime["clock"]["statusReason"], "not_required")
        self.assertFalse(runtime["clock"]["requiredForCapture"])
        self.assert_sync_skew_is_unmeasured(runtime)
        self.assertEqual(runtime["sync"]["state"], "hardware_trigger_active")
        self.assertEqual(runtime["sync"]["counterHealth"], "stable")
        self.assertEqual(runtime["sync"]["observedTriggerCount"], 124)

        idle_mcap, idle_status = self.write_segment("1970-fixture-idle")
        wait_until(lambda: not idle_mcap.exists() and not idle_status.exists())

    def test_real_mode_discards_open_preroll_before_started_boundary(self) -> None:
        fake_qgapp = self.root / "fake-qgapp.sh"
        first_clean_written = self.root / "first-clean-written"
        second_clean_written = self.root / "second-clean-written"
        progressive_status = self.progressive_status_shell(self.clean_segment_status())
        fake_qgapp.write_text(f"""#!/bin/sh
ring=$1
i=0
while :; do
    name=$(printf '1970-fake-%04d' "$i")
    printf 'open-segment' > "$ring/$name.mcap"
    sleep 0.25
    printf '\\211MCAP0\\r\\n' >> "$ring/$name.mcap"
    {progressive_status}
    printf '%s\\n' "$status" > "$ring/$name.mcap.status.json"
    if [ "$i" -eq 0 ]; then : > '{first_clean_written}'; fi
    if [ "$i" -eq 1 ]; then : > '{second_clean_written}'; fi
    i=$((i + 1))
    sleep 0.03
done
""")
        fake_qgapp.chmod(0o755)
        service = self.service(dry_run=False, qgapp=fake_qgapp)

        def current_open_segment():
            for path in self.ring.glob("*.mcap"):
                if not Path(str(path) + ".status.json").exists():
                    return path.name
            return None

        wait_until(first_clean_written.exists)
        first_mcap = self.ring / "1970-fake-0000.mcap"
        first_status = self.ring / "1970-fake-0000.mcap.status.json"
        wait_until(lambda: not first_mcap.exists() and not first_status.exists())
        try:
            wait_until(second_clean_written.exists)
        except AssertionError as error:
            self.fail(f"{error}\n{service.diagnostics()}")
        second_mcap = self.ring / "1970-fake-0001.mcap"
        second_status = self.ring / "1970-fake-0001.mcap.status.json"
        wait_until(lambda: not second_mcap.exists() and not second_status.exists())
        wait_until(lambda: current_open_segment() is not None)
        preroll_name = current_open_segment()
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "boundary"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertFalse((self.ring / preroll_name).exists())

        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        completed = self.decoded(body)
        self.assertEqual(status, 200)
        session_id = completed["sessionId"]
        status, _, body = service.request("POST", f"/v1/sessions/{session_id}/prepare-export", {})
        exported_names = {item["name"] for item in self.decoded(body)["files"]}
        self.assertNotIn(preroll_name, exported_names)
        self.assertGreaterEqual(len([name for name in exported_names if name.endswith(".mcap")]), 1)

    def test_idle_imu_warmup_does_not_kill_qgapp_and_active_error_still_fails(self) -> None:
        fake_qgapp = self.root / "fake-qgapp-warmup.sh"
        enable_clean = self.root / "enable-clean"
        emit_active_bad = self.root / "emit-active-bad"
        warmup_written = self.root / "warmup-written"
        strict_written = self.root / "strict-written"
        strict_second_written = self.root / "strict-second-written"
        warmup_status = self.clean_segment_status()
        warmup_status["drops"]["imu_gaps"] = 5
        warmup_json = json.dumps(warmup_status, separators=(",", ":"))
        progressive_clean = self.progressive_status_shell(self.clean_segment_status())
        fake_qgapp.write_text(f"""#!/bin/sh
ring=$1
printf 'warmup' > "$ring/1970-warmup-0000.mcap"
printf '\\211MCAP0\\r\\n' >> "$ring/1970-warmup-0000.mcap"
printf '%s\\n' '{warmup_json}' > "$ring/1970-warmup-0000.mcap.status.json"
: > '{warmup_written}'
while [ ! -f '{enable_clean}' ]; do sleep 0.02; done
i=0
while [ ! -f '{emit_active_bad}' ]; do
    name=$(printf '1970-strict-%04d' "$i")
    printf 'strict' > "$ring/$name.mcap"
    printf '\\211MCAP0\\r\\n' >> "$ring/$name.mcap"
    {progressive_clean}
    printf '%s\\n' "$status" > "$ring/$name.mcap.status.json"
    if [ "$i" -eq 0 ]; then : > '{strict_written}'; fi
    if [ "$i" -eq 1 ]; then : > '{strict_second_written}'; fi
    i=$((i + 1))
    sleep 0.08
done
printf 'active-bad' > "$ring/1970-active-bad.mcap"
printf '\\211MCAP0\\r\\n' >> "$ring/1970-active-bad.mcap"
printf '%s\\n' '{warmup_json}' > "$ring/1970-active-bad.mcap.status.json"
while :; do sleep 1; done
""")
        fake_qgapp.chmod(0o755)
        service = self.service(dry_run=False, qgapp=fake_qgapp)
        qgapp_pid = int(service.pidfile.read_text().strip())

        wait_until(warmup_written.exists)
        warmup_mcap = self.ring / "1970-warmup-0000.mcap"
        warmup_sidecar = self.ring / "1970-warmup-0000.mcap.status.json"
        wait_until(lambda: not warmup_mcap.exists() and not warmup_sidecar.exists())
        self.assertTrue(pid_alive(qgapp_pid))
        self.assertFalse((self.ring / ".syncap-quarantine").exists())

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "not ready"})
        self.assertEqual(status, 409)
        unavailable = self.decoded(body)
        self.assertEqual(unavailable["error"], "camera.unavailable")
        self.assertIn("warming up", unavailable["message"])
        self.assertTrue(pid_alive(qgapp_pid))

        enable_clean.touch()
        wait_until(strict_written.exists)
        first_strict_mcap = self.ring / "1970-strict-0000.mcap"
        first_strict_sidecar = self.ring / "1970-strict-0000.mcap.status.json"
        wait_until(lambda: not first_strict_mcap.exists() and not first_strict_sidecar.exists())
        wait_until(strict_second_written.exists)
        second_strict_mcap = self.ring / "1970-strict-0001.mcap"
        second_strict_sidecar = self.ring / "1970-strict-0001.mcap.status.json"
        wait_until(lambda: not second_strict_mcap.exists() and not second_strict_sidecar.exists())

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "strict"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        emit_active_bad.touch()

        def capture_failed() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            current = self.decoded(response)
            return code == 200 and current.get("state") == "failed"

        wait_until(capture_failed)
        status, _, body = service.request("GET", "/v1/captures/current")
        current = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(current["captureId"], capture["captureId"])
        self.assertIn("failed integrity checks", current["failureReason"])
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())
        wait_until(lambda: not pid_alive(qgapp_pid))

    def test_cumulative_error_baseline_accepts_stable_lifetime_totals_and_rejects_growth(self) -> None:
        service = self.live_idle_service("cumulative")
        qgapp_pid = int(service.pidfile.read_text().strip())

        first = self.write_segment(
            "1970-cumulative-0039",
            status_payload=self.cumulative_segment_status(0),
        )
        wait_until(lambda: not first[0].exists() and not first[1].exists())
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "too early"})
        self.assertEqual(status, 409)
        self.assertIn("warming up", self.decoded(body)["message"])

        second = self.write_segment(
            "1970-cumulative-0040",
            status_payload=self.cumulative_segment_status(1),
        )
        wait_until(lambda: not second[0].exists() and not second[1].exists())

        start_result: list[tuple] = []
        start_thread = threading.Thread(
            target=lambda: start_result.append(
                service.request("POST", "/v1/captures/start", {"name": "cumulative"})
            ),
            daemon=True,
        )
        start_thread.start()
        time.sleep(0.15)
        boundary = self.write_segment(
            "1970-cumulative-0041",
            status_payload=self.cumulative_segment_status(2),
        )
        start_thread.join(timeout=5)
        self.assertFalse(start_thread.is_alive())
        self.assertEqual(start_result[0][0], 200)
        capture = self.decoded(start_result[0][2])
        self.assertFalse(boundary[0].exists())
        self.assertFalse(boundary[1].exists())

        accepted = self.write_segment(
            "1970-cumulative-0042",
            status_payload=self.cumulative_segment_status(3),
        )
        wait_until(lambda: not accepted[0].exists() and not accepted[1].exists())

        grown = self.cumulative_segment_status(4, imu_gaps=10)
        grown["drops"].update(left=3, right=4, venc_resets=5, q_drops=6)
        grown["audio"].update(timeouts=8, backsteps=9, gaps=10)
        grown["sync"].update(frame_backstep=7, fc_drops=[6, 6])
        rejected = self.write_segment(
            "1970-cumulative-0043",
            status_payload=grown,
        )

        def capture_failed() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            return code == 200 and self.decoded(response).get("state") == "failed"

        wait_until(capture_failed)
        self.assertTrue(rejected[0].exists())
        self.assertTrue(rejected[1].exists())
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())
        wait_until(lambda: not pid_alive(qgapp_pid))
        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body)["error"], "capture.interrupted")

    def test_capture_alignment_rejects_counter_change_instead_of_absorbing_it(self) -> None:
        service = self.live_idle_service("alignment-drift")
        qgapp_pid = int(service.pidfile.read_text().strip())
        for step in (0, 1):
            segment = self.write_segment(
                f"1970-alignment-{step:04d}",
                status_payload=self.cumulative_segment_status(step),
            )
            wait_until(lambda pair=segment: not pair[0].exists() and not pair[1].exists())

        start_result: list[tuple] = []
        start_thread = threading.Thread(
            target=lambda: start_result.append(
                service.request("POST", "/v1/captures/start", {"name": "must reject"})
            ),
            daemon=True,
        )
        start_thread.start()
        time.sleep(0.15)
        drift = self.write_segment(
            "1970-alignment-0002",
            status_payload=self.cumulative_segment_status(2, imu_gaps=10),
        )
        start_thread.join(timeout=5)
        self.assertFalse(start_thread.is_alive())
        self.assertEqual(start_result[0][0], 409)
        self.assertIn("changed while aligning", self.decoded(start_result[0][2])["message"])
        wait_until(lambda: not drift[0].exists() and not drift[1].exists())
        self.assertFalse((self.ring / ".syncap-quarantine").exists())
        self.assertTrue(pid_alive(qgapp_pid))

        stable_again = self.write_segment(
            "1970-alignment-0003",
            status_payload=self.cumulative_segment_status(3, imu_gaps=10),
        )
        wait_until(lambda: not stable_again[0].exists() and not stable_again[1].exists())

        retry_result: list[tuple] = []
        retry_thread = threading.Thread(
            target=lambda: retry_result.append(
                service.request("POST", "/v1/captures/start", {"name": "retry"})
            ),
            daemon=True,
        )
        retry_thread.start()
        time.sleep(0.15)
        retry_boundary = self.write_segment(
            "1970-alignment-0004",
            status_payload=self.cumulative_segment_status(4, imu_gaps=10),
        )
        retry_thread.join(timeout=5)
        self.assertFalse(retry_thread.is_alive())
        self.assertEqual(retry_result[0][0], 200)
        self.assertFalse(retry_boundary[0].exists())
        self.assertFalse(retry_boundary[1].exists())

    def test_active_capture_rejects_stalled_or_reversed_progress_counters(self) -> None:
        for label, second_marks in (("stalled", 124), ("reversed", 62)):
            with self.subTest(case=label):
                case_root = self.root / f"progress-{label}"
                case_ring = case_root / "ring"
                case_storage = case_root / "sd"
                case_ring.mkdir(parents=True)
                case_storage.mkdir()
                service = RunningService(
                    self.binary,
                    storage=case_storage,
                    ring=case_ring,
                    dry_run=True,
                ).start()
                self.services.append(service)
                status, _, body = service.request(
                    "POST", "/v1/captures/start", {"name": label},
                )
                self.assertEqual(status, 200)
                capture = self.decoded(body)
                first = self.write_segment(
                    f"1970-{label}-0001",
                    fsync_marks=124,
                    ring=case_ring,
                )
                wait_until(lambda pair=first: not pair[0].exists() and not pair[1].exists())
                rejected = self.write_segment(
                    f"1970-{label}-0002",
                    fsync_marks=second_marks,
                    ring=case_ring,
                )

                def capture_failed() -> bool:
                    code, _, response = service.request("GET", "/v1/captures/current")
                    return code == 200 and self.decoded(response).get("state") == "failed"

                wait_until(capture_failed)
                self.assertTrue(rejected[0].exists())
                self.assertTrue(rejected[1].exists())
                status, _, body = service.request(
                    "POST", f"/v1/captures/{capture['captureId']}/stop", {},
                )
                self.assertEqual(status, 500)
                self.assertEqual(self.decoded(body)["error"], "capture.interrupted")

    def test_backlogged_segments_are_processed_in_numeric_sequence_order(self) -> None:
        service = self.live_idle_service("ordered-backlog")
        assert service.process is not None
        os.kill(service.process.pid, signal.SIGSTOP)
        try:
            higher = self.write_segment(
                "1970-order-10000",
                status_payload=self.cumulative_segment_status(1),
            )
            lower = self.write_segment(
                "1970-order-9999",
                status_payload=self.cumulative_segment_status(0),
            )
        finally:
            os.kill(service.process.pid, signal.SIGCONT)
        wait_until(
            lambda: all(not path.exists() for path in (*higher, *lower)),
        )

        start_result: list[tuple] = []
        start_thread = threading.Thread(
            target=lambda: start_result.append(
                service.request("POST", "/v1/captures/start", {"name": "ordered"})
            ),
            daemon=True,
        )
        start_thread.start()
        time.sleep(0.15)
        boundary = self.write_segment(
            "1970-order-10001",
            status_payload=self.cumulative_segment_status(2),
        )
        start_thread.join(timeout=5)
        self.assertFalse(start_thread.is_alive())
        self.assertEqual(start_result[0][0], 200)
        self.assertFalse(boundary[0].exists())
        self.assertFalse(boundary[1].exists())

    def test_service_restart_adopts_live_qgapp_before_classifying_cumulative_residue(self) -> None:
        service = self.live_idle_service("adopt-residue")
        qgapp_pid = int(service.pidfile.read_text().strip())
        assert service.process is not None
        service.process.terminate()
        service.process.wait(timeout=3)
        self.assertTrue(pid_alive(qgapp_pid))

        residue = self.write_segment(
            "1970-adopt-0039",
            status_payload=self.cumulative_segment_status(0),
        )
        adopted = RunningService(
            self.binary,
            storage=self.storage,
            ring=self.ring,
            dry_run=False,
            qgapp=service.qgapp,
        ).start()
        self.services.append(adopted)
        wait_until(lambda: not residue[0].exists() and not residue[1].exists())
        self.assertFalse((self.ring / ".syncap-quarantine").exists())
        self.assertTrue(pid_alive(qgapp_pid))
        self.assertEqual(int(adopted.pidfile.read_text().strip()), qgapp_pid)

    def test_service_hot_restart_rebinds_same_port_while_qgapp_stays_alive(self) -> None:
        service = self.live_idle_service("same-port-restart")
        qgapp_pid = int(service.pidfile.read_text().strip())
        original_port = service.port
        assert service.process is not None
        service.process.terminate()
        service.process.wait(timeout=3)
        self.assertTrue(pid_alive(qgapp_pid))

        restarted = RunningService(
            self.binary,
            storage=self.storage,
            ring=self.ring,
            dry_run=False,
            qgapp=service.qgapp,
        )
        restarted.port = original_port
        restarted.start()
        self.services.append(restarted)
        status, _, _ = restarted.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(int(restarted.pidfile.read_text().strip()), qgapp_pid)
        self.assertTrue(pid_alive(qgapp_pid))

    def test_qgapp_launch_arguments_and_duplicate_service_guard(self) -> None:
        fake_qgapp = self.root / "fake-qgapp-contract.sh"
        arguments = self.root / "qgapp-arguments.txt"
        stream_mode = self.root / "qgapp-stream-mode.txt"
        starts = self.root / "qgapp-starts.txt"
        fake_qgapp.write_text(f"""#!/bin/sh
printf 'start\\n' >> '{starts}'
printf '%s\\n' "$@" > '{arguments}'
printf '%s\\n' "$C06_STREAM" > '{stream_mode}'
while :; do sleep 1; done
""")
        fake_qgapp.chmod(0o755)
        service = self.service(dry_run=False, qgapp=fake_qgapp)
        wait_until(lambda: arguments.exists() and stream_mode.exists())
        self.assertEqual(arguments.read_text().splitlines(), [
            str(self.ring), "1600", "1200", "30", "4000", "2",
        ])
        self.assertEqual(stream_mode.read_text().strip(), "1")

        duplicate = subprocess.run([
            str(self.binary),
            "--port", str(service.port),
            "--ring", str(self.ring),
            "--storage", str(self.storage),
            "--allow-unmounted-storage",
            "--min-free-bytes", "1",
            "--qgapp-pidfile", str(service.pidfile),
            "--qgapp-log", str(self.root / "duplicate-qgapp.log"),
            "--allow-missing-preview-listeners",
            "--qgapp", str(fake_qgapp),
        ], check=False, capture_output=True, timeout=3)
        self.assertEqual(duplicate.returncode, 1)
        self.assertIn(b"cannot listen", duplicate.stderr)
        self.assertEqual(starts.read_text().splitlines(), ["start"])
        self.assertTrue(pid_alive(int(service.pidfile.read_text().strip())))

    def test_qgapp_death_marks_capture_failed_without_splicing_restart(self) -> None:
        fake_qgapp = self.root / "fake-qgapp-death.sh"
        first_clean_written = self.root / "death-first-clean-written"
        second_clean_written = self.root / "death-second-clean-written"
        progressive_status = self.progressive_status_shell(self.clean_segment_status())
        fake_qgapp.write_text(f"""#!/bin/sh
ring=$1
i=0
while :; do
    name=$(printf '1970-death-%04d' "$i")
    printf 'open-segment' > "$ring/$name.mcap"
    sleep 0.20
    printf '\\211MCAP0\\r\\n' >> "$ring/$name.mcap"
    {progressive_status}
    printf '%s\\n' "$status" > "$ring/$name.mcap.status.json"
    if [ "$i" -eq 0 ]; then : > '{first_clean_written}'; fi
    if [ "$i" -eq 1 ]; then : > '{second_clean_written}'; fi
    i=$((i + 1))
    sleep 0.03
done
""")
        fake_qgapp.chmod(0o755)
        service = self.service(dry_run=False, qgapp=fake_qgapp)
        wait_until(first_clean_written.exists)
        first_mcap = self.ring / "1970-death-0000.mcap"
        first_status = self.ring / "1970-death-0000.mcap.status.json"
        wait_until(lambda: not first_mcap.exists() and not first_status.exists())
        wait_until(second_clean_written.exists)
        second_mcap = self.ring / "1970-death-0001.mcap"
        second_status = self.ring / "1970-death-0001.mcap.status.json"
        wait_until(lambda: not second_mcap.exists() and not second_status.exists())
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "death"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        session_dir = self.storage / "SynCap" / capture["sessionId"]
        wait_until(lambda: any(session_dir.glob("*.mcap")))

        qgapp_pid = int(service.pidfile.read_text().strip())
        os.kill(qgapp_pid, signal.SIGKILL)

        def capture_failed() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            return code == 200 and self.decoded(response).get("state") == "failed"

        wait_until(capture_failed, timeout=5)
        retained = list(session_dir.glob("*.mcap"))
        self.assertTrue(retained)
        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body)["error"], "capture.interrupted")
        self.assertTrue(all(path.exists() for path in retained))

        status, _, body = service.request("GET", "/v1/sessions")
        session = next(item for item in self.decoded(body)["sessions"] if item["id"] == capture["sessionId"])
        self.assertEqual(status, 200)
        self.assertEqual(session["status"], "failed")

    def test_idle_qgapp_death_requires_reboot_without_restart_loop(self) -> None:
        fake_qgapp = self.root / "fake-qgapp-idle-death.sh"
        starts = self.root / "starts.txt"
        fake_qgapp.write_text(f"""#!/bin/sh
printf 'start\\n' >> '{starts}'
sleep 0.25
""")
        fake_qgapp.chmod(0o755)
        service = self.service(dry_run=False, qgapp=fake_qgapp)

        def recovery_required() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            capture = self.decoded(response)
            return code == 200 and capture.get("recoveryRequired") is True

        wait_until(recovery_required, timeout=5)
        time.sleep(2.5)
        self.assertEqual(starts.read_text().splitlines(), ["start"])

        status, _, body = service.request("GET", "/health")
        health = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertFalse(health["qgappRunning"])
        self.assertTrue(health["ringQuarantined"])

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "blocked"})
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "capture.recovery_required")

    def test_failed_capture_keeps_current_and_later_clean_ring_segments(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "copy failure"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)

        session_dir = self.storage / "SynCap" / capture["sessionId"]
        displaced_session = self.storage / "SynCap" / f"{capture['sessionId']}.blocked"
        session_dir.rename(displaced_session)
        session_dir.write_text("not a directory")
        try:
            first = self.write_segment("1970-copy-failed-0001")

            def capture_failed() -> bool:
                code, _, response = service.request("GET", "/v1/captures/current")
                return code == 200 and self.decoded(response).get("state") == "failed"

            wait_until(capture_failed)
            second = self.write_segment("1970-copy-failed-0002")
            time.sleep(0.5)
            self.assertTrue(all(path.exists() for path in (*first, *second)))

            status, _, body = service.request(
                "POST", f"/v1/captures/{capture['captureId']}/stop", {},
            )
            self.assertEqual(status, 500)
            self.assertEqual(self.decoded(body)["error"], "capture.interrupted")
            time.sleep(0.5)
            self.assertTrue(all(path.exists() for path in (*first, *second)))
            self.assertTrue((self.ring / ".syncap-quarantine").is_file())

            status, _, body = service.request("GET", "/v1/captures/current")
            held = self.decoded(body)
            self.assertEqual(status, 200)
            self.assertEqual(held["state"], "failed")
            self.assertTrue(held["recoveryRequired"])

            status, _, body = service.request("POST", "/v1/captures/start", {"name": "must block"})
            self.assertEqual(status, 409)
            self.assertEqual(self.decoded(body)["error"], "capture.recovery_required")
        finally:
            session_dir.unlink(missing_ok=True)
            displaced_session.rename(session_dir)

    def test_quarantine_kills_producer_and_supervisor_does_not_restart_it(self) -> None:
        fake_qgapp = self.root / "fake-qgapp-quarantine.sh"
        first_clean_written = self.root / "quarantine-first-clean-written"
        second_clean_written = self.root / "quarantine-second-clean-written"
        progressive_status = self.progressive_status_shell(self.clean_segment_status())
        fake_qgapp.write_text(f"""#!/bin/sh
ring=$1
i=0
while :; do
    name=$(printf '1970-quarantine-%04d' "$i")
    printf 'open-segment' > "$ring/$name.mcap"
    sleep 0.15
    printf '\\211MCAP0\\r\\n' >> "$ring/$name.mcap"
    {progressive_status}
    printf '%s\\n' "$status" > "$ring/$name.mcap.status.json"
    if [ "$i" -eq 0 ]; then : > '{first_clean_written}'; fi
    if [ "$i" -eq 1 ]; then : > '{second_clean_written}'; fi
    i=$((i + 1))
    sleep 0.03
done
""")
        fake_qgapp.chmod(0o755)
        service = self.service(dry_run=False, qgapp=fake_qgapp)
        wait_until(first_clean_written.exists)
        first_mcap = self.ring / "1970-quarantine-0000.mcap"
        first_status = self.ring / "1970-quarantine-0000.mcap.status.json"
        wait_until(lambda: not first_mcap.exists() and not first_status.exists())
        wait_until(second_clean_written.exists)
        second_mcap = self.ring / "1970-quarantine-0001.mcap"
        second_status = self.ring / "1970-quarantine-0001.mcap.status.json"
        wait_until(lambda: not second_mcap.exists() and not second_status.exists())
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "bounded failure"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        qgapp_pid = int(service.pidfile.read_text().strip())

        for _ in range(3):
            status, _, _ = service.request("GET", "/v1/status")
            self.assertEqual(status, 200)
            self.assertTrue(pid_alive(qgapp_pid), "read-only status must not kill qgapp")

        session_dir = self.storage / "SynCap" / capture["sessionId"]
        displaced_session = self.storage / "SynCap" / f"{capture['sessionId']}.blocked"
        session_dir.rename(displaced_session)
        session_dir.write_text("not a directory")
        try:
            def capture_failed() -> bool:
                code, _, response = service.request("GET", "/v1/captures/current")
                return code == 200 and self.decoded(response).get("state") == "failed"

            wait_until(capture_failed)
            wait_until(lambda: not pid_alive(qgapp_pid))
        finally:
            session_dir.unlink(missing_ok=True)
            displaced_session.rename(session_dir)

        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body)["error"], "capture.interrupted")

        def ring_snapshot() -> dict[str, int]:
            return {
                path.name: path.stat().st_size
                for path in self.ring.iterdir()
                if path.name != ".syncap-quarantine"
            }

        snapshot = ring_snapshot()
        self.assertTrue(snapshot)
        time.sleep(2.6)  # Longer than the two-second supervisor interval.
        self.assertEqual(ring_snapshot(), snapshot)
        self.assertFalse(pid_alive(qgapp_pid))
        self.assertEqual(int(service.pidfile.read_text().strip()), qgapp_pid)

        status, _, body = service.request("GET", "/health")
        health = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertTrue(health["ringQuarantined"])
        self.assertTrue(health["producerIntentionallyStopped"])
        service.pidfile.unlink(missing_ok=True)

    def assert_terminal_segment_fails(self, *, clean: bool, valid_footer: bool) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "bad terminal"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)

        good_mcap, good_status = self.write_segment("1970-terminal-good")
        wait_until(lambda: not good_mcap.exists() and not good_status.exists())
        bad_mcap = self.ring / "1970-terminal-bad.mcap"
        bad_status = self.ring / "1970-terminal-bad.mcap.status.json"
        bad_mcap.write_bytes(b"bad-terminal" + (MCAP_MAGIC if valid_footer else b"invalid-footer"))
        payload = self.clean_segment_status()
        payload["state"] = "clean" if clean else "failed"
        bad_status.write_text(json.dumps(payload))

        def capture_failed() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            return code == 200 and self.decoded(response).get("state") == "failed"

        wait_until(capture_failed)
        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body)["error"], "capture.interrupted")
        time.sleep(0.5)
        self.assertTrue(bad_mcap.exists())
        self.assertTrue(bad_status.exists())

        status, _, body = service.request("GET", f"/v1/sessions/{capture['sessionId']}/manifest")
        self.assertEqual(status, 200)
        self.assertEqual(self.decoded(body)["state"], "failed")

    def assert_clean_integrity_status_is_rejected(self, label: str, mutate_status) -> None:
        case_root = self.root / label
        case_ring = case_root / "ring"
        case_storage = case_root / "sd"
        case_ring.mkdir(parents=True)
        case_storage.mkdir()
        service = RunningService(
            self.binary,
            storage=case_storage,
            ring=case_ring,
            dry_run=True,
        ).start()
        self.services.append(service)

        status, _, body = service.request("POST", "/v1/captures/start", {"name": label})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        good_mcap, good_status = self.write_segment(
            f"1970-{label}-good",
            ring=case_ring,
        )
        wait_until(lambda: not good_mcap.exists() and not good_status.exists())

        invalid_payload = self.clean_segment_status(fsync_marks=124)
        mutate_status(invalid_payload)
        bad_mcap, bad_status = self.write_segment(
            f"1970-{label}-invalid",
            ring=case_ring,
            status_payload=invalid_payload,
        )

        def capture_failed() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            return code == 200 and self.decoded(response).get("state") == "failed"

        wait_until(capture_failed)
        self.assertTrue(bad_mcap.exists())
        self.assertTrue(bad_status.exists())
        self.assertTrue((case_ring / ".syncap-quarantine").is_file())
        status, _, body = service.request("GET", "/v1/status")
        runtime = self.decoded(body)
        self.assertEqual(status, 200)
        self.assert_sync_skew_is_unmeasured(runtime)
        self.assertEqual(runtime["sync"]["state"], "producer_stopped")
        self.assertEqual(runtime["sync"]["counterHealth"], "unverified")
        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body)["error"], "capture.interrupted")
        manifest = self.decoded(
            (case_storage / "SynCap" / capture["sessionId"] / "session.json").read_bytes()
        )
        self.assertEqual(manifest["state"], "failed")

    def test_clean_status_missing_required_sensor_evidence_never_completes(self) -> None:
        def remove_left_evidence(status: dict) -> None:
            status["drops"].pop("left")
            status["recording"]["left"] = 0

        def remove_right_evidence(status: dict) -> None:
            status["drops"].pop("right")
            status["recording"]["right"] = 0

        def remove_imu_evidence(status: dict) -> None:
            status["drops"].pop("imu_gaps")
            status["sync"].pop("imu_fc_first")
            status["sync"].pop("imu_fc_last")
            status["recording"].update(imu_gaps=0, imu_fc_first=3, imu_fc_last=65)

        def remove_audio_evidence(status: dict) -> None:
            status.pop("audio")
            status["recording"].update(
                enabled=True,
                frames=100,
                timeouts=0,
                backsteps=0,
                gaps=0,
            )

        cases = [
            ("missing-left", remove_left_evidence),
            ("missing-right", remove_right_evidence),
            ("missing-imu", remove_imu_evidence),
            ("missing-audio", remove_audio_evidence),
            ("audio-disabled", lambda status: status["audio"].update(enabled=False)),
            ("audio-zero-frames", lambda status: status["audio"].update(frames=0)),
        ]
        for label, mutate_status in cases:
            with self.subTest(case=label):
                self.assert_clean_integrity_status_is_rejected(label, mutate_status)

    def test_clean_status_with_data_loss_counters_never_completes(self) -> None:
        cases = [
            ("left-drop", lambda status: status["drops"].update(left=1)),
            ("right-drop", lambda status: status["drops"].update(right=1)),
            ("imu-gap", lambda status: status["drops"].update(imu_gaps=1)),
            ("venc-reset", lambda status: status["drops"].update(venc_resets=1)),
            ("queue-drop", lambda status: status["drops"].update(q_drops=1)),
            ("audio-timeout", lambda status: status["audio"].update(timeouts=1)),
            ("audio-backstep", lambda status: status["audio"].update(backsteps=1)),
            ("audio-gap", lambda status: status["audio"].update(gaps=1)),
            ("frame-backstep", lambda status: status["sync"].update(frame_backstep=1)),
            ("left-frame-counter-drop", lambda status: status["sync"].update(fc_drops=[1, 0])),
            ("right-frame-counter-drop", lambda status: status["sync"].update(fc_drops=[0, 1])),
        ]
        for label, mutate_status in cases:
            with self.subTest(case=label):
                self.assert_clean_integrity_status_is_rejected(label, mutate_status)

    def test_non_clean_terminal_segment_never_completes_capture(self) -> None:
        self.assert_terminal_segment_fails(clean=False, valid_footer=True)

    def test_clean_status_with_invalid_mcap_footer_never_completes_capture(self) -> None:
        self.assert_terminal_segment_fails(clean=True, valid_footer=False)

    def test_capture_elapsed_time_uses_device_monotonic_clock(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "clock domains"})
        started = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(started["elapsedMs"], 0)
        time.sleep(0.12)
        current = self.decoded(service.request("GET", "/v1/captures/current")[2])
        self.assertEqual(current["startedAtDeviceTimeNs"], started["startedAtDeviceTimeNs"])
        self.assertGreaterEqual(current["elapsedMs"], 100)
        self.assertLess(current["elapsedMs"], 5000)
        self.assertEqual(current["sizeBytes"], 0)
        self.assertEqual(current["fileCount"], 0)
        status = self.decoded(service.request("GET", "/v1/status")[2])
        elapsed = (int(status["deviceTimeNs"]) - int(started["startedAtDeviceTimeNs"])) // 1_000_000
        self.assertLess(abs(status["capture"]["elapsedMs"] - elapsed), 100)

    def test_first_failure_duration_and_reason_survive_delayed_stop_and_restart(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "frozen failure"})
        self.assertEqual(status, 200)
        capture = self.decoded(body)
        first = self.write_segment("1970-frozen-good")
        wait_until(lambda: not first[0].exists() and not first[1].exists())
        self.write_segment("1970-frozen-failed", clean=False, fsync_marks=124)

        def failed() -> bool:
            return self.decoded(service.request("GET", "/v1/captures/current")[2])["state"] == "failed"

        wait_until(failed)
        current = self.decoded(service.request("GET", "/v1/captures/current")[2])
        frozen_ms = current["elapsedMs"]
        reason = current["failureReason"]
        self.assertIn("1970-frozen-failed.mcap", reason)
        manifest_url = f"/v1/sessions/{capture['sessionId']}/manifest"
        manifest_path = self.storage / "SynCap" / capture["sessionId"] / "session.json"
        before_stop = self.decoded(service.request("GET", manifest_url)[2])
        self.assertEqual(before_stop["durationMs"], frozen_ms)
        self.assertEqual(before_stop["failureReason"], reason)

        self.write_segment("1970-later-failed", clean=False, fsync_marks=186)
        time.sleep(0.35)
        later = self.decoded(service.request("GET", "/v1/captures/current")[2])
        self.assertEqual(later["elapsedMs"], frozen_ms)
        self.assertEqual(later["failureReason"], reason)
        stop_url = f"/v1/captures/{capture['captureId']}/stop"
        status, _, body = service.request("POST", stop_url, {})
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body), {"error": "capture.interrupted", "message": reason})
        saved = manifest_path.read_bytes()
        saved_manifest = self.decoded(saved)
        self.assertEqual(saved_manifest["durationMs"], frozen_ms)
        self.assertEqual(saved_manifest["failureReason"], reason)

        time.sleep(0.15)
        status, _, body = service.request("POST", stop_url, {})
        self.assertEqual(status, 404)
        self.assertEqual(self.decoded(body)["error"], "capture.not_found")
        self.assertEqual(manifest_path.read_bytes(), saved)

        self.crash_service(service)
        restarted = self.service(dry_run=True)
        status, _, body = restarted.request("GET", manifest_url)
        self.assertEqual(status, 200)
        self.assertEqual(self.decoded(body)["durationMs"], frozen_ms)
        self.assertEqual(self.decoded(body)["failureReason"], reason)
        status, _, body = restarted.request("POST", stop_url, {})
        self.assertEqual(status, 404)
        self.assertEqual(self.decoded(body)["error"], "capture.not_found")
        self.assertEqual(manifest_path.read_bytes(), saved)

    def test_failure_during_stop_keeps_first_failed_duration(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "stop failure"})
        self.assertEqual(status, 200)
        capture = self.decoded(body)
        first = self.write_segment("1970-stop-good")
        wait_until(lambda: not first[0].exists() and not first[1].exists())
        boundary = self.ring / "1970-stop-failed.mcap"
        boundary.write_bytes(b"boundary" + MCAP_MAGIC)
        manifest_path = self.storage / "SynCap" / capture["sessionId"] / "session.json"
        result = {}

        def stop_capture() -> None:
            try:
                result["response"] = service.request(
                    "POST", f"/v1/captures/{capture['captureId']}/stop", {},
                )
            except Exception as error:
                result["error"] = repr(error)

        thread = threading.Thread(target=stop_capture, daemon=True)
        thread.start()
        time.sleep(0.15)
        sidecar = self.clean_segment_status(fsync_marks=124)
        sidecar["state"] = "failed"
        Path(str(boundary) + ".status.json").write_text(json.dumps(sidecar))
        wait_until(lambda: self.decoded(manifest_path.read_bytes())["state"] == "failed")
        first_failure = self.decoded(manifest_path.read_bytes())
        self.assertIn("1970-stop-failed.mcap", first_failure["failureReason"])
        # A known terminal failure now returns promptly instead of waiting
        # out the camera-closing deadline. The first failure remains frozen.
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive(), result)
        self.assertNotIn("error", result)
        status, _, body = result["response"]
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body)["error"], "capture.interrupted")
        final_failure = self.decoded(manifest_path.read_bytes())
        self.assertEqual(final_failure["durationMs"], first_failure["durationMs"])
        self.assertEqual(final_failure["failureReason"], first_failure["failureReason"])

    def test_stop_timeout_persists_failure_reason(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "no final segment"})
        self.assertEqual(status, 200)
        capture = self.decoded(body)
        first = self.write_segment("1970-timeout-good")
        wait_until(lambda: not first[0].exists() and not first[1].exists())
        status, _, body = service.request("POST", f"/v1/captures/{capture['captureId']}/stop", {})
        self.assertEqual(status, 500)
        failure = self.decoded(body)
        self.assertEqual(failure["error"], "capture.finalization_failed")
        manifest = self.decoded(service.request("GET", f"/v1/sessions/{capture['sessionId']}/manifest")[2])
        self.assertEqual(manifest["state"], "failed")
        self.assertEqual(manifest["failureReason"], failure["message"])

    def test_empty_and_partial_sidecars_are_pending_not_crashes_or_failures(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "partial sidecar"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)

        mcap = self.ring / "1970-partial.mcap"
        sidecar = self.ring / "1970-partial.mcap.status.json"
        mcap.write_bytes(b"partial-fixture" + MCAP_MAGIC)
        sidecar.write_bytes(b"")
        time.sleep(0.5)  # Cross multiple 200 ms service polls.
        self.assertIsNone(service.process.poll())
        status, _, body = service.request("GET", "/v1/captures/current")
        self.assertEqual(status, 200)
        self.assertEqual(self.decoded(body)["state"], "recording")
        self.assertTrue(mcap.exists())
        self.assertTrue(sidecar.exists())

        sidecar.write_text('{"state":"clean","sync":{}')
        time.sleep(0.5)
        self.assertEqual(service.request("GET", "/v1/captures/current")[0], 200)
        self.assertTrue(mcap.exists())
        self.assertTrue(sidecar.exists())

        sidecar.write_text(json.dumps(self.clean_segment_status()))
        wait_until(lambda: not mcap.exists() and not sidecar.exists())
        self.assertIsNone(service.process.poll())

        stop_result: dict[str, object] = {}

        def stop_capture() -> None:
            code, headers, response = service.request(
                "POST", f"/v1/captures/{capture['captureId']}/stop", {},
            )
            stop_result.update(code=code, headers=headers, body=response)

        thread = threading.Thread(target=stop_capture)
        thread.start()
        time.sleep(0.15)
        self.write_segment("1970-partial-stop", fsync_marks=124)
        thread.join(timeout=10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(stop_result["code"], 200)

    def test_many_empty_sidecars_remain_safe_and_are_not_deleted(self) -> None:
        service = self.service(dry_run=True)
        fixtures: list[tuple[Path, Path]] = []
        for index in range(1000):
            mcap = self.ring / f"1970-empty-{index:04d}.mcap"
            sidecar = self.ring / f"1970-empty-{index:04d}.mcap.status.json"
            mcap.write_bytes(b"stress" + MCAP_MAGIC)
            sidecar.write_bytes(b"")
            fixtures.append((mcap, sidecar))

        time.sleep(0.8)
        self.assertIsNone(service.process.poll())
        self.assertEqual(service.request("GET", "/health")[0], 200)
        self.assertTrue(all(mcap.exists() and sidecar.exists() for mcap, sidecar in fixtures))

    def test_preexisting_incomplete_sidecar_blocks_capture_without_deleting_data(self) -> None:
        service = self.service(dry_run=True)
        mcap = self.ring / "1970-preexisting.mcap"
        sidecar = self.ring / "1970-preexisting.mcap.status.json"
        mcap.write_bytes(b"preexisting" + MCAP_MAGIC)
        sidecar.write_bytes(b"")

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "must block"})
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "capture.recovery_required")
        self.assertTrue(mcap.exists())
        self.assertTrue(sidecar.exists())
        self.assertTrue((self.ring / ".syncap-quarantine").exists())

    def test_stale_incomplete_sidecar_fails_active_capture_and_preserves_ring(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "stale sidecar"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)

        mcap = self.ring / "1970-stale.mcap"
        sidecar = self.ring / "1970-stale.mcap.status.json"
        mcap.write_bytes(b"stale" + MCAP_MAGIC)
        sidecar.write_bytes(b"")

        def capture_failed() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            return code == 200 and self.decoded(response).get("state") == "failed"

        wait_until(capture_failed, timeout=7)
        self.assertTrue(mcap.exists())
        self.assertTrue(sidecar.exists())
        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        self.assertEqual(status, 500)
        self.assertEqual(self.decoded(body)["error"], "capture.interrupted")

    def test_idle_failed_residue_is_quarantined_without_deleting_data(self) -> None:
        service = self.service(dry_run=True)
        reserve = self.ring / ".syncap-quarantine.reserve"
        self.assertGreater(reserve.stat().st_size, 0)
        old_mcap, old_status = self.write_segment("1970-old-bad", clean=False)

        def recovery_required() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            current = self.decoded(response)
            return code == 200 and current.get("recoveryRequired") is True

        wait_until(recovery_required)
        self.assertTrue(old_mcap.exists())
        self.assertTrue(old_status.exists())
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())
        self.assertFalse(reserve.exists())

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "must block"})
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "capture.recovery_required")

    def test_idle_integrity_failed_residue_is_not_mistaken_for_live_warmup(self) -> None:
        service = self.service(dry_run=True)
        status_payload = self.clean_segment_status()
        status_payload["drops"]["imu_gaps"] = 5
        old_mcap, old_status = self.write_segment(
            "1970-old-integrity-failed",
            status_payload=status_payload,
        )

        def recovery_required() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            current = self.decoded(response)
            return code == 200 and current.get("recoveryRequired") is True

        wait_until(recovery_required)
        self.assertTrue(old_mcap.exists())
        self.assertTrue(old_status.exists())
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())

    def test_idle_clean_residue_with_bad_footer_is_quarantined_without_deleting_data(self) -> None:
        service = self.service(dry_run=True)
        old_mcap = self.ring / "1970-old-bad-footer.mcap"
        old_status = self.ring / "1970-old-bad-footer.mcap.status.json"
        old_mcap.write_bytes(b"truncated-without-mcap-footer")
        old_status.write_text(json.dumps(self.clean_segment_status()))

        def recovery_required() -> bool:
            code, _, response = service.request("GET", "/v1/captures/current")
            current = self.decoded(response)
            return code == 200 and current.get("recoveryRequired") is True

        wait_until(recovery_required, timeout=5)
        self.assertTrue(old_mcap.exists())
        self.assertTrue(old_status.exists())
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "must block"})
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "capture.recovery_required")

    def test_idle_pending_and_open_segments_time_out_into_quarantine(self) -> None:
        for label, with_pending_status in (("pending", True), ("open", False)):
            with self.subTest(case=label):
                case_root = self.root / f"idle-{label}"
                case_ring = case_root / "ring"
                case_storage = case_root / "sd"
                case_ring.mkdir(parents=True)
                case_storage.mkdir()
                service = RunningService(
                    self.binary,
                    storage=case_storage,
                    ring=case_ring,
                    dry_run=True,
                ).start()
                self.services.append(service)

                mcap = case_ring / f"1970-idle-{label}.mcap"
                sidecar = Path(str(mcap) + ".status.json")
                mcap.write_bytes(b"unfinished-producer-output")
                if with_pending_status:
                    sidecar.write_bytes(b"")

                def recovery_required() -> bool:
                    code, _, response = service.request("GET", "/v1/captures/current")
                    current = self.decoded(response)
                    return code == 200 and current.get("recoveryRequired") is True

                wait_until(recovery_required, timeout=7)
                self.assertTrue(mcap.exists())
                self.assertEqual(sidecar.exists(), with_pending_status)
                self.assertTrue((case_ring / ".syncap-quarantine").is_file())
                self.assertFalse((case_ring / ".syncap-quarantine.reserve").exists())

    def test_zero_length_quarantine_reserve_is_rebuilt_on_startup(self) -> None:
        reserve = self.ring / ".syncap-quarantine.reserve"
        reserve.write_bytes(b"")

        self.service(dry_run=True)

        self.assertTrue(reserve.is_file())
        self.assertEqual(reserve.stat().st_size, 4096)

    def test_restart_with_missing_active_manifest_retains_ring_for_recovery(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "missing manifest"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        session_dir = self.storage / "SynCap" / capture["sessionId"]
        active_marker = self.decoded((self.ring / ".syncap-active.json").read_bytes())
        self.assertEqual(active_marker["captureId"], capture["captureId"])
        self.assertEqual(active_marker["sessionId"], capture["sessionId"])

        self.crash_service(service)
        (session_dir / "session.json").unlink()
        ring_mcap, ring_status = self.write_segment("1970-missing-manifest-ring")

        restarted = self.service(dry_run=True)
        status, _, body = restarted.request("GET", "/v1/captures/current")
        current = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(current["state"], "failed")
        self.assertTrue(current["recoveryRequired"])
        self.assertTrue(ring_mcap.exists())
        self.assertTrue(ring_status.exists())
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())

    def test_restart_with_active_storage_unavailable_retains_ring_for_recovery(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "detached storage"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)
        active_marker = self.decoded((self.ring / ".syncap-active.json").read_bytes())
        self.assertEqual(active_marker["captureId"], capture["captureId"])
        self.assertEqual(active_marker["sessionId"], capture["sessionId"])

        self.crash_service(service)
        ring_mcap, ring_status = self.write_segment("1970-detached-storage-ring")
        detached_storage = self.root / "sd-detached"
        self.storage.rename(detached_storage)

        restarted = self.service(dry_run=True)
        status, _, body = restarted.request("GET", "/v1/captures/current")
        current = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(current["state"], "failed")
        self.assertTrue(current["recoveryRequired"])
        self.assertTrue(ring_mcap.exists())
        self.assertTrue(ring_status.exists())
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())

    def test_final_session_metadata_commit_failure_does_not_return_to_idle(self) -> None:
        service = self.service(dry_run=True)
        status, _, body = service.request("POST", "/v1/captures/start", {"name": "metadata failure"})
        capture = self.decoded(body)
        self.assertEqual(status, 200)

        captured_mcap, captured_status = self.write_segment("1970-metadata-0001")
        wait_until(lambda: not captured_mcap.exists() and not captured_status.exists())
        session_dir = self.storage / "SynCap" / capture["sessionId"]
        assert service.process is not None
        commit_blocker = session_dir / f"session.json.tmp.{service.process.pid}"
        commit_blocker.mkdir()

        # Reuse an already-captured filename as the open stop boundary. This
        # reaches the final metadata commit without needing another file copy.
        open_boundary = self.ring / "1970-metadata-0001.mcap"
        open_boundary.write_bytes(b"open-stop-boundary")
        status, _, body = service.request(
            "POST", f"/v1/captures/{capture['captureId']}/stop", {},
        )
        self.assertEqual(status, 500)
        failure = self.decoded(body)
        self.assertEqual(failure["error"], "capture.finalization_failed")
        self.assertEqual(failure["message"], "cannot commit session metadata")
        self.assertTrue(commit_blocker.is_dir())

        status, _, body = service.request("GET", "/v1/captures/current")
        current = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertNotEqual(current["state"], "idle")
        self.assertEqual(current["state"], "failed")
        self.assertTrue(current["recoveryRequired"])
        self.assertTrue(open_boundary.exists())
        self.assertTrue((self.ring / ".syncap-active.json").is_file())
        self.assertTrue((self.ring / ".syncap-quarantine").is_file())
        self.assertEqual(self.decoded((session_dir / "session.json").read_bytes())["state"], "recording")

    def test_idle_ring_housekeeping_continues_during_slow_session_download(self) -> None:
        session_id = "ses_slow_download"
        session_dir = self.storage / "SynCap" / session_id
        session_dir.mkdir(parents=True)
        (session_dir / "session.json").write_text(json.dumps({
            "schema": "syncap.session/1.0",
            "id": session_id,
            "name": "slow download",
            "state": "complete",
            "createdAt": "2026-09-04T00:00:00Z",
            "durationMs": 1,
            "sizeBytes": 128 * 1024 * 1024,
            "fileCount": 1,
            "files": [],
        }, separators=(",", ":")))
        download = session_dir / "large.mcap"
        with download.open("wb") as stream:
            stream.truncate(128 * 1024 * 1024)

        service = self.service(dry_run=True)
        client = socket.socket()
        client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        client.settimeout(3)
        try:
            client.connect(("127.0.0.1", service.port))
            client.sendall(
                f"GET /v1/sessions/{session_id}/files/large.mcap HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{service.port}\r\nConnection: close\r\n\r\n".encode()
            )
            received = b""
            while b"\r\n\r\n" not in received:
                received += client.recv(4096)
            self.assertIn(b"HTTP/1.1 200 OK", received)
            self.assertIn(b"Content-Length: 134217728", received)

            idle_mcap, idle_status = self.write_segment("1970-during-download")
            wait_until(
                lambda: not idle_mcap.exists() and not idle_status.exists(),
                timeout=4,
            )
        finally:
            client.close()

    def test_slow_request_headers_do_not_block_idle_ring_housekeeping(self) -> None:
        service = self.service(dry_run=True)
        client = socket.socket()
        client.settimeout(2)
        try:
            client.connect(("127.0.0.1", service.port))
            client.sendall(
                f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{service.port}\r\n"
                "X-Incomplete: waiting".encode()
            )
            # The service select loop is 200 ms. Allow it to accept this
            # connection and enter request parsing before publishing the pair.
            time.sleep(0.35)
            idle_mcap, idle_status = self.write_segment("1970-during-slow-header")
            wait_until(
                lambda: not idle_mcap.exists() and not idle_status.exists(),
                timeout=4,
            )
        finally:
            client.close()

    def test_startup_marks_stale_recording_failed_and_preserves_data(self) -> None:
        session_dir = self.storage / "SynCap" / "ses_stale_fixture"
        session_dir.mkdir(parents=True)
        retained = session_dir / "retained.mcap"
        retained.write_bytes(b"retained" + MCAP_MAGIC)
        (session_dir / "session.json").write_text(json.dumps({
            "schema": "syncap.session/1.0",
            "id": "ses_stale_fixture",
            "captureId": "cap_stale_fixture",
            "name": "stale",
            "state": "recording",
            "createdAt": "2026-09-04T00:00:00Z",
            "durationMs": 0,
            "sizeBytes": 0,
            "fileCount": 0,
            "files": [],
        }, separators=(",", ":")))
        ring_mcap, ring_status = self.write_segment("1970-interrupted-ring")

        service = self.service(dry_run=True)
        status, _, body = service.request("GET", "/v1/sessions/ses_stale_fixture/manifest")
        recovered = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(recovered["state"], "failed")
        self.assertEqual(recovered["failureReason"], "capture.interrupted_by_service_restart")
        self.assertTrue(retained.exists())
        self.assertTrue(ring_mcap.exists())
        self.assertTrue(ring_status.exists())

        status, _, body = service.request("GET", "/v1/captures/current")
        recovery = self.decoded(body)
        self.assertEqual(status, 200)
        self.assertEqual(recovery["state"], "failed")
        self.assertTrue(recovery["recoveryRequired"])
        self.assertIn("ses_stale_fixture", recovery["failureReason"])

        status, _, body = service.request("POST", "/v1/captures/start", {"name": "must not splice"})
        self.assertEqual(status, 409)
        self.assertEqual(self.decoded(body)["error"], "capture.recovery_required")


COMMAND_CAPTURE_HARNESS = r'''
#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

#define WIFI_COMMAND_OUTPUT_LIMIT (512U * 1024U)
struct string_buf { char *data; size_t len; size_t cap; };
static volatile sig_atomic_t g_running = 1;
static uint64_t now_ns = 1000000000ULL;
static int mode;
static int auxiliary_calls;
static int kill_calls;

static bool sb_append_n(struct string_buf *output, const char *data, size_t size) {
    (void)output; (void)data; (void)size;
    return false; /* The test pipe has no writer and must produce only EOF. */
}
static uint64_t monotonic_ns(void) { return now_ns; }
static void process_closed_segments(void) {
    ++auxiliary_calls;
    now_ns += 2000000000ULL;
}
static void supervise_qgapp(void) {}
static pid_t mock_fork(void) { return 42; }
static pid_t mock_waitpid(pid_t pid, int *status, int options) {
    if (mode == 1 && options == WNOHANG) return 0;
    *status = mode == 1 ? SIGKILL : mode == 2 ? (7 << 8) : 0;
    return pid;
}
static int mock_kill(pid_t pid, int number) {
    (void)pid; (void)number;
    ++kill_calls;
    return 0;
}
#define fork mock_fork
#define waitpid mock_waitpid
#define kill mock_kill
__COMMAND_SOURCE__

int main(int argc, char **argv) {
    struct string_buf output = {0};
    char *const command[] = {"unused-fixture-command", NULL};
    int exit_code = -1;
    bool ok;
    if (argc != 2) return 2;
    mode = atoi(argv[1]);
    ok = run_command_capture(command, 1, &output, &exit_code);
    printf("{\"ok\":%s,\"exit\":%d,\"auxiliaryCalls\":%d,\"killCalls\":%d}\n",
           ok ? "true" : "false", exit_code, auxiliary_calls, kill_calls);
    return 0;
}
'''


class TinaCommandCaptureTest(unittest.TestCase):
    """Deterministic command deadline tests without scheduling real children."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory(prefix="syncap-command-deadline-")
        cls.addClassCleanup(cls.build.cleanup)
        source = (ROOT / "tina_service.c").read_text()
        start = source.index("static bool run_command_capture(")
        end = source.index("\nstruct wifi_network", start)
        command_source = source[start:end]
        completed_guard = "        if (child_done && pipe_done) break;\n"
        if command_source.count(completed_guard) != 1:
            raise AssertionError("Update the completed-command negative control")
        cls.current = cls.compile_harness(command_source, "current")
        cls.previous = cls.compile_harness(command_source.replace(completed_guard, ""), "previous")

    @classmethod
    def compile_harness(cls, command_source: str, name: str) -> Path:
        binary = Path(cls.build.name) / name
        subprocess.run([
            os.environ.get("CC", "cc"), "-std=c11", "-O2", "-Wall", "-Wextra",
            "-Werror", "-pedantic", "-x", "c", "-", "-o", str(binary),
        ], input=COMMAND_CAPTURE_HARNESS.replace("__COMMAND_SOURCE__", command_source),
           text=True, check=True)
        return binary

    def run_case(self, mode: int, *, previous: bool = False) -> dict:
        completed = subprocess.run([str(self.previous if previous else self.current), str(mode)],
                                   capture_output=True, text=True, check=True, timeout=5)
        return json.loads(completed.stdout)

    def test_completed_command_is_not_timed_out_by_later_housekeeping(self) -> None:
        self.assertEqual(self.run_case(0), {
            "ok": True, "exit": 0, "auxiliaryCalls": 0, "killCalls": 0,
        })

    def test_previous_order_deterministically_misclassifies_completed_command(self) -> None:
        self.assertEqual(self.run_case(0, previous=True), {
            "ok": False, "exit": 0, "auxiliaryCalls": 1, "killCalls": 0,
        })

    def test_pending_command_still_times_out_and_is_terminated(self) -> None:
        self.assertEqual(self.run_case(1), {
            "ok": False, "exit": 128, "auxiliaryCalls": 1, "killCalls": 1,
        })

    def test_completed_nonzero_exit_is_still_failure(self) -> None:
        self.assertEqual(self.run_case(2), {
            "ok": False, "exit": 7, "auxiliaryCalls": 0, "killCalls": 0,
        })


if __name__ == "__main__":
    unittest.main()
