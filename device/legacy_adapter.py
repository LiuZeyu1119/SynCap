#!/usr/bin/env python3
"""Read-only SynCap development adapter for the legacy RoboBaton-4P demo."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


CAMERA_LINE = re.compile(
    r"cam(?P<camera>\d+) fps=(?P<fps>[\d.]+).*?"
    r"group_skew_ns=(?P<skew>\d+).*?queue=(?P<queue>\d+/\d+).*?"
    r"full_waits=(?P<waits>\d+).*?pipeline_delay_ms=(?P<delay>\d+)"
)
DIAGNOSTIC_LINE = re.compile(
    r"\[diag\] rtsp_pts_skew_ms=(?P<rtsp>[\d.]+) "
    r"camera_latest_skew_ms=(?P<camera>[\d.]+)"
)
PTP_STATE_LINE = re.compile(r"port \d+: .* to (?P<state>[A-Z_]+)")
PTP_SERVO_LINE = re.compile(
    r"master offset\s+(?P<offset>-?\d+).*?path delay\s+(?P<delay>-?\d+)"
)
PTP_GRANDMASTER_LINE = re.compile(r"selected best master clock\s+(?P<identity>[0-9a-fA-F.:-]+)")


def ptp_status_reason(state: str) -> str:
    return {
        "locked": "synchronized",
        "locking": "synchronizing",
        "listening": "grandmaster_not_detected",
        "faulty": "ptp_port_fault",
    }.get(state, "ptp_state_unavailable")


def tail_lines(path: Path, max_bytes: int = 512 * 1024) -> list[str]:
    try:
        with path.open("rb") as source:
            source.seek(0, os.SEEK_END)
            size = source.tell()
            source.seek(max(0, size - max_bytes))
            data = source.read()
    except OSError:
        return []
    if size > max_bytes:
        data = data.split(b"\n", 1)[-1]
    return data.decode("utf-8", errors="replace").splitlines()


def stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"current": None, "mean": None, "p95": None, "min": None, "max": None, "samples": 0}
    ordered = sorted(values)
    p95_index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * 0.95) - 1))
    return {
        "current": round(values[-1], 6),
        "mean": round(sum(values) / len(values), 6),
        "p95": round(ordered[p95_index], 6),
        "min": round(ordered[0], 6),
        "max": round(ordered[-1], 6),
        "samples": len(values),
    }


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def temperature_celsius() -> float | None:
    for path in sorted(Path("/sys/class/thermal").glob("thermal_zone*/temp")):
        try:
            value = float(path.read_text().strip())
        except (OSError, ValueError):
            continue
        if value > 1000:
            value /= 1000
        if -20 <= value <= 150:
            return round(value, 1)
    return None


def uptime_seconds() -> float:
    try:
        return round(float(Path("/proc/uptime").read_text().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        return round(time.monotonic(), 1)


def process_from_pid_file(pid_path: Path, name: str) -> bool:
    try:
        pid = int(pid_path.read_text().strip())
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return False
    return name in command


def process_named(name: str) -> bool:
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for command_path in proc.glob("[0-9]*/cmdline"):
        try:
            command = command_path.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace")
        except OSError:
            continue
        if name in command:
            return True
    return False


def camera_state(process_running: bool, online: list[bool]) -> str:
    if online and all(online):
        return "ready"
    if any(online):
        return "degraded"
    if process_running:
        return "starting"
    return "stopped"


def parse_ptp_status(lines: list[str], age_ms: float | None, fresh_after_ms: int = 5000) -> dict[str, object]:
    port_state: str | None = None
    offset_ns: int | None = None
    path_delay_ns: int | None = None
    grandmaster_identity: str | None = None

    for line in lines:
        state_match = PTP_STATE_LINE.search(line)
        if state_match:
            port_state = state_match.group("state")

        servo_match = PTP_SERVO_LINE.search(line)
        if servo_match:
            offset_ns = int(servo_match.group("offset"))
            path_delay_ns = int(servo_match.group("delay"))

        grandmaster_match = PTP_GRANDMASTER_LINE.search(line)
        if grandmaster_match:
            grandmaster_identity = grandmaster_match.group("identity")

    fresh = age_ms is not None and age_ms <= fresh_after_ms
    if port_state == "SLAVE" and offset_ns is not None and fresh:
        state = "locked"
    elif port_state == "UNCALIBRATED" and fresh:
        state = "locking"
    elif port_state in {"LISTENING", "PASSIVE", "INITIALIZING"} and fresh:
        state = "listening"
    elif port_state == "FAULTY" and fresh:
        state = "faulty"
    else:
        state = "unknown"

    return {
        "state": state,
        "grandmasterPresent": state == "locked",
        "statusReason": ptp_status_reason(state),
        "requiredForCapture": False,
        "grandmasterIdentity": grandmaster_identity if state == "locked" else None,
        "offsetFromMasterNs": offset_ns if state == "locked" else None,
        "meanPathDelayNs": path_delay_ns if state == "locked" else None,
        "lastUpdateAgeMs": round(age_ms, 1) if age_ms is not None else None,
    }


def parse_pmc_status(port_output: str, current_output: str = "", time_output: str = "") -> dict[str, object] | None:
    port_match = re.search(r"\bportState\s+(?P<state>[A-Z_]+)", port_output)
    if not port_match:
        return None

    port_state = port_match.group("state")
    present_match = re.search(r"\bgmPresent\s+(true|false)", time_output)
    grandmaster_present = present_match is not None and present_match.group(1) == "true"
    state = {
        "SLAVE": "locked" if grandmaster_present else "locking",
        "UNCALIBRATED": "locking",
        "LISTENING": "listening",
        "PASSIVE": "listening",
        "INITIALIZING": "listening",
        "FAULTY": "faulty",
    }.get(port_state, "unknown")

    offset_match = re.search(r"\boffsetFromMaster\s+(-?[\d.]+)", current_output)
    path_match = re.search(r"\bmeanPathDelay\s+(-?[\d.]+)", current_output)
    identity_match = re.search(r"\bgmIdentity\s+([0-9a-fA-F.:-]+)", time_output)
    has_current_master = state == "locked" and grandmaster_present

    return {
        "state": state,
        "grandmasterPresent": has_current_master,
        "statusReason": ptp_status_reason(state),
        "requiredForCapture": False,
        "grandmasterIdentity": identity_match.group(1) if has_current_master and identity_match else None,
        "offsetFromMasterNs": round(float(offset_match.group(1))) if has_current_master and offset_match else None,
        "meanPathDelayNs": round(float(path_match.group(1))) if has_current_master and path_match else None,
        "lastUpdateAgeMs": 0.0 if has_current_master else None,
    }


def run_pmc(command: str, config_path: Path = Path("/etc/linuxptp.cfg")) -> str:
    executable = shutil.which("pmc")
    if executable is None or not config_path.exists():
        return ""
    try:
        result = subprocess.run(
            [executable, "-u", "-b", "0", "-f", str(config_path), command],
            capture_output=True,
            check=False,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout


def query_pmc_status() -> dict[str, object] | None:
    port_output = run_pmc("GET PORT_DATA_SET")
    if "portState" not in port_output:
        return None
    if not re.search(r"\bportState\s+SLAVE\b", port_output):
        return parse_pmc_status(port_output)
    return parse_pmc_status(
        port_output,
        run_pmc("GET CURRENT_DATA_SET"),
        run_pmc("GET TIME_STATUS_NP"),
    )


class AdapterState:
    def __init__(
        self,
        device_ip: str,
        log_path: Path,
        ptp_log_path: Path | None = None,
        ptp_pid_path: Path = Path("/var/run/ptp4l.pid"),
        phc_path: Path = Path("/dev/ptp0"),
    ) -> None:
        self.device_ip = device_ip
        self.log_path = log_path
        self.ptp_log_path = ptp_log_path
        self.ptp_pid_path = ptp_pid_path
        self.phc_path = phc_path
        self.ports = [554, 555, 556, 557]
        self._capture_clock_lock = threading.Lock()
        self._capture_clock: dict[str, object] | None = None

    def capture_clock_status(self) -> dict[str, object]:
        """Return the last full-status clock sample without rerunning PTP probes."""
        with self._capture_clock_lock:
            if self._capture_clock is not None:
                return dict(self._capture_clock)
        return {
            "state": "unknown",
            "grandmasterPresent": False,
            "statusReason": "status_not_sampled",
            "requiredForCapture": False,
        }

    def manifest(self) -> dict[str, object]:
        directions = ["front", "right", "rear", "left"]
        mount_positions = [
            "wearer_right_outer",
            "wearer_left_inner",
            "wearer_right_inner",
            "wearer_left_outer",
        ]
        return {
            "protocolVersion": "1.0",
            "adapter": {"kind": "legacy-readonly", "security": "development"},
            "device": {
                "id": "dev_rb4p_a4df62",
                "displayName": "Head Ring 001",
                "model": "RoboBaton-4P",
                "ipAddress": self.device_ip,
            },
            "capabilities": ["camera.preview", "sync.metrics", "sensor.imu", "clock.sync", "exposure.phase"],
            "cameras": [
                {
                    "id": f"cam{index}",
                    "label": f"CAM {index}",
                    "direction": directions[index],
                    "mountPosition": mount_positions[index],
                    "preview": {"transport": "rtsp", "url": f"rtsp://{self.device_ip}:{port}/PRR"},
                }
                for index, port in enumerate(self.ports)
            ],
        }

    def status(self) -> dict[str, object]:
        camera_samples: dict[int, list[dict[str, float | int | str]]] = {index: [] for index in range(4)}
        acquisition_skew_ms: list[float] = []
        preview_pts_skew_ms: list[float] = []
        preview_camera_skew_ms: list[float] = []

        for line in tail_lines(self.log_path):
            camera_match = CAMERA_LINE.search(line)
            if camera_match:
                camera = int(camera_match.group("camera"))
                sample = {
                    "fps": float(camera_match.group("fps")),
                    "groupSkewMs": int(camera_match.group("skew")) / 1_000_000,
                    "queue": camera_match.group("queue"),
                    "fullWaits": int(camera_match.group("waits")),
                    "pipelineDelayMs": int(camera_match.group("delay")),
                }
                if camera in camera_samples:
                    camera_samples[camera].append(sample)
                    acquisition_skew_ms.append(float(sample["groupSkewMs"]))

            diagnostic_match = DIAGNOSTIC_LINE.search(line)
            if diagnostic_match:
                preview_pts_skew_ms.append(float(diagnostic_match.group("rtsp")))
                preview_camera_skew_ms.append(float(diagnostic_match.group("camera")))

        disk = shutil.disk_usage("/")
        cameras = []
        for index, port in enumerate(self.ports):
            samples = camera_samples[index]
            latest = samples[-1] if samples else {}
            cameras.append({"id": f"cam{index}", "port": port, "online": port_open(port), **latest})

        camera_process_running = process_named("/root/demo/bin/cam_demo")
        online_cameras = [bool(camera["online"]) for camera in cameras]

        ptp4l_running = process_from_pid_file(self.ptp_pid_path, "ptp4l")
        ptp_age_ms: float | None = None
        ptp_lines: list[str] = []
        if self.ptp_log_path is not None:
            ptp_lines = tail_lines(self.ptp_log_path, max_bytes=128 * 1024)
            try:
                ptp_age_ms = max(0.0, (time.time() - self.ptp_log_path.stat().st_mtime) * 1000)
            except OSError:
                pass
        ptp_status = query_pmc_status() if ptp4l_running else None
        if ptp_status is None:
            ptp_status = parse_ptp_status(ptp_lines, ptp_age_ms)

        observed_fps = [float(camera["fps"]) for camera in cameras if camera.get("fps")]
        average_fps = sum(observed_fps) / len(observed_fps) if observed_fps else None
        target_period_ns = str(round(1_000_000_000 / average_fps)) if average_fps else None

        payload = {
            "v": "1.0",
            "deviceTimeNs": str(time.monotonic_ns()),
            "adapter": "legacy-readonly",
            "cameraDemoRunning": all(online_cameras),
            "cameraProcessRunning": camera_process_running,
            "cameraState": camera_state(camera_process_running, online_cameras),
            "cameras": cameras,
            "sync": {
                "acquisitionSkewMs": stats(acquisition_skew_ms),
                "previewPtsSkewMs": stats(preview_pts_skew_ms),
                "previewCameraSkewMs": stats(preview_camera_skew_ms),
            },
            "clock": {
                "source": "ptp" if ptp4l_running else "free_running",
                "domain": 0,
                "interface": "eth0",
                "hardwareTimestamping": self.phc_path.exists(),
                "phcDevice": str(self.phc_path) if self.phc_path.exists() else None,
                "ptp4lRunning": ptp4l_running,
                "phc2sysRunning": process_named("phc2sys"),
                **ptp_status,
                "systemClockDisciplined": False,
                "sensorClockMapped": False,
            },
            "exposureSync": {
                "phaseLockedToGrandmaster": False,
                "targetPeriodNs": target_period_ns,
                "phaseErrorNs": None,
                "timestampTraceable": False,
                "timestampDomain": "monotonic_raw",
                "timestampSource": "gpio417_trigger",
                "measurementPoint": "trigger_anchor",
                "physicalExposureDelayCalibrated": False,
                "physicalExposureDelayNs": None,
            },
            "cameraImuSync": {
                "physicalTimeOffsetCalibrated": False,
                "timeOffsetNs": None,
                "clockDomainMapped": False,
            },
            "system": {
                "uptimeSeconds": uptime_seconds(),
                "temperatureC": temperature_celsius(),
                "storage": {
                    "totalGiB": round(disk.total / 1024**3, 2),
                    "availableGiB": round(disk.free / 1024**3, 2),
                },
            },
        }
        with self._capture_clock_lock:
            self._capture_clock = dict(payload["clock"])
        return payload


class Handler(BaseHTTPRequestHandler):
    state: AdapterState

    def do_GET(self) -> None:
        if self.path == "/health":
            self.send_json({"ok": True})
        elif self.path == "/v1/manifest":
            self.send_json(self.state.manifest())
        elif self.path == "/v1/status":
            self.send_json(self.state.status())
        else:
            self.send_json({"error": "not_found"}, status=404)

    def send_json(self, payload: dict[str, object], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--device-ip", default="192.168.1.12")
    parser.add_argument("--log", default="/tmp/cam_demo-live.log", type=Path)
    parser.add_argument("--ptp-log", type=Path)
    args = parser.parse_args()

    Handler.state = AdapterState(args.device_ip, args.log, args.ptp_log)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SynCap legacy adapter listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
