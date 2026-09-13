#!/usr/bin/env python3
"""Restore and monitor the saved wlan0 station connection without blocking boot."""

from __future__ import annotations

import argparse
import stat
import subprocess
import time
from pathlib import Path


class WifiSupervisor:
    POLL_SECONDS = 5.0
    COMMAND_TIMEOUT_SECONDS = 10
    DHCP_TIMEOUT_SECONDS = 10

    CONFIGURE_MARKER_MAX_AGE_SECONDS = 60

    def __init__(
        self,
        config_path: Path,
        configure_marker: Path = Path("/var/run/syncap-wifi-configuring"),
    ) -> None:
        self.config_path = config_path
        self.configure_marker = configure_marker

    def has_private_config(self) -> bool:
        try:
            metadata = self.config_path.lstat()
        except OSError:
            return False
        return (
            stat.S_ISREG(metadata.st_mode)
            and metadata.st_uid == 0
            and stat.S_IMODE(metadata.st_mode) == 0o600
        )

    def configuration_in_progress(self) -> bool:
        try:
            age = time.time() - self.configure_marker.stat().st_mtime
        except OSError:
            return False
        if age <= self.CONFIGURE_MARKER_MAX_AGE_SECONDS:
            return True
        try:
            self.configure_marker.unlink()
        except OSError:
            pass
        return False

    @staticmethod
    def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess[str] | None:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None

    def _wpa_status(self) -> dict[str, str] | None:
        result = self._run(["/usr/sbin/wpa_cli", "-i", "wlan0", "status"], self.COMMAND_TIMEOUT_SECONDS)
        if result is None or result.returncode != 0:
            return None
        status: dict[str, str] = {}
        for line in result.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                status[key] = value
        return status

    def _ipv4_address(self) -> str | None:
        result = self._run(
            ["/usr/sbin/ip", "-4", "-o", "addr", "show", "dev", "wlan0"],
            self.COMMAND_TIMEOUT_SECONDS,
        )
        if result is None or result.returncode != 0:
            return None
        for column, value in zip(result.stdout.split(), result.stdout.split()[1:]):
            if column == "inet":
                return value.split("/", 1)[0]
        return None

    def _flush_stale_ipv4(self) -> None:
        if self._ipv4_address() is not None:
            self._run(
                ["/usr/sbin/ip", "-4", "addr", "flush", "dev", "wlan0"],
                self.COMMAND_TIMEOUT_SECONDS,
            )

    def step(self) -> str:
        if self.configuration_in_progress():
            return "configuration_in_progress"
        status = self._wpa_status()
        if status is None:
            self._flush_stale_ipv4()
            result = self._run(["/etc/init.d/wifi_init.sh", "sta"], self.COMMAND_TIMEOUT_SECONDS)
            return "starting_wpa" if result is not None and result.returncode == 0 else "wpa_start_failed"

        if status.get("wpa_state") != "COMPLETED":
            self._flush_stale_ipv4()
            return "waiting_for_association"

        if self._ipv4_address() is None:
            result = self._run(
                ["/usr/sbin/udhcpc", "-i", "wlan0", "-q", "-n", "-t", "3", "-T", "2"],
                self.DHCP_TIMEOUT_SECONDS,
            )
            return "requesting_dhcp" if result is not None and result.returncode == 0 else "dhcp_retry"
        return "connected"

    def run_forever(self) -> None:
        if not self.has_private_config():
            print("Wi-Fi restore disabled: saved config is missing or not mode 0600", flush=True)
            return
        previous = None
        while True:
            state = self.step()
            if state != previous:
                print(f"Wi-Fi restore state: {state}", flush=True)
                previous = state
            time.sleep(self.POLL_SECONDS)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/userdata/wpa_supplicant.conf", type=Path)
    args = parser.parse_args()
    WifiSupervisor(args.config).run_forever()


if __name__ == "__main__":
    main()
