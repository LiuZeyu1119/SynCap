#!/usr/bin/env python3
"""Keep the vendor four-camera demo alive and bound its diagnostics log."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


class RollingLog:
    def __init__(self, path: Path, max_bytes: int) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.output = None

    def _open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.output = self.path.open("ab", buffering=0)

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self.output is None:
            self._open()
        assert self.output is not None
        if self.output.tell() + len(data) > self.max_bytes:
            self.output.close()
            rotated = self.path.with_name(self.path.name + ".1")
            rotated.unlink(missing_ok=True)
            if self.path.exists():
                os.replace(self.path, rotated)
            self._open()
        assert self.output is not None
        self.output.write(data[-self.max_bytes :])

    def close(self) -> None:
        if self.output is not None:
            self.output.close()
            self.output = None


class CameraSupervisor:
    def __init__(
        self,
        command: list[str],
        log: RollingLog,
        restart_delay: float,
        settings_path: Path | None = None,
        default_fps: int = 60,
        rotation_shim_path: Path | None = Path("/opt/syncap/libcamera_venc_rotation.so"),
    ) -> None:
        self.command = command
        self.log = log
        self.restart_delay = restart_delay
        self.settings_path = settings_path
        self.default_fps = default_fps
        self.rotation_shim_path = rotation_shim_path
        self.child: subprocess.Popen[bytes] | None = None
        self.stopping = False

    def _marker(self, message: str) -> None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.log.write(f"[syncap-supervisor] {timestamp} {message}\n".encode("utf-8"))

    def request_stop(self, _signum: int, _frame: object) -> None:
        self.stopping = True
        if self.child is not None and self.child.poll() is None:
            try:
                os.killpg(self.child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def request_reload(self, _signum: int, _frame: object) -> None:
        if self.stopping or self.child is None or self.child.poll() is not None:
            return
        self._marker("camera configuration changed; restarting camera demo")
        try:
            os.killpg(self.child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _rotation(self) -> int:
        rotation = 0
        if self.settings_path is not None:
            try:
                settings = json.loads(self.settings_path.read_text(encoding="utf-8"))
                value = settings.get("rotationDegrees")
                if type(value) is int and value in {0, 90, 180, 270}:
                    rotation = value
            except (OSError, ValueError, AttributeError):
                pass
        return rotation

    def _uses_encoder_rotation(self) -> bool:
        return self.rotation_shim_path is not None and self.rotation_shim_path.is_file()

    def _start_command(self) -> list[str]:
        requested_rotation = self._rotation()
        if self._uses_encoder_rotation():
            return [*self.command, "--fps", str(self.default_fps), "--rotate", "0"]
        fallback_fps = 30 if requested_rotation == 180 else self.default_fps
        return [
            *self.command, "--fps", str(fallback_fps), "--rotate", str(requested_rotation),
        ]

    def _start_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        if self._uses_encoder_rotation():
            assert self.rotation_shim_path is not None
            preload = str(self.rotation_shim_path)
            if environment.get("LD_PRELOAD"):
                preload = f"{preload}:{environment['LD_PRELOAD']}"
            environment["LD_PRELOAD"] = preload
            environment["SYNCAP_VENC_ROTATION"] = str(self._rotation())
        return environment

    def run(self) -> int:
        while not self.stopping:
            command = self._start_command()
            self._marker(
                f"starting camera demo rotation={self._rotation()} fps={command[-3]} "
                f"stage={'encoder' if self._uses_encoder_rotation() else 'camera'}"
            )
            try:
                self.child = subprocess.Popen(
                    command,
                    cwd=str(Path(self.command[0]).parent),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env=self._start_environment(),
                )
            except OSError as error:
                self._marker(f"start failed: {error}")
                self._wait_before_restart()
                continue

            assert self.child.stdout is not None
            for line in iter(self.child.stdout.readline, b""):
                self.log.write(line)
            return_code = self.child.wait()
            self.child = None
            if not self.stopping:
                self._marker(f"camera demo exited with status {return_code}; restarting")
                self._wait_before_restart()

        self._marker("camera supervisor stopped")
        self.log.close()
        return 0

    def _wait_before_restart(self) -> None:
        deadline = time.monotonic() + self.restart_delay
        while not self.stopping and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demo", default="/root/demo/cam_demo", type=Path)
    parser.add_argument("--log", default="/tmp/cam_demo-live.log", type=Path)
    parser.add_argument("--max-log-bytes", default=4 * 1024 * 1024, type=int)
    parser.add_argument("--restart-delay", default=2.0, type=float)
    parser.add_argument("--diag-interval-ms", default=1000, type=int)
    parser.add_argument("--fps", default=60, choices=(25, 30, 40, 50, 60), type=int)
    parser.add_argument("--settings", default="/userdata/syncap/camera.json", type=Path)
    args = parser.parse_args()

    log = RollingLog(args.log, max(64 * 1024, args.max_log_bytes))
    supervisor = CameraSupervisor(
        [
            str(args.demo), "--diagnostics", "--diag-interval-ms", str(args.diag_interval_ms),
        ],
        log,
        max(0.2, args.restart_delay),
        args.settings,
        args.fps,
    )
    signal.signal(signal.SIGTERM, supervisor.request_stop)
    signal.signal(signal.SIGINT, supervisor.request_stop)
    signal.signal(signal.SIGHUP, supervisor.request_reload)
    raise SystemExit(supervisor.run())


if __name__ == "__main__":
    main()
