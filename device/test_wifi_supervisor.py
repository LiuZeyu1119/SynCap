import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wifi_supervisor import WifiSupervisor


class WifiSupervisorTests(unittest.TestCase):
    def test_saved_config_must_be_regular_and_mode_0600(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "wpa_supplicant.conf"
            path.write_text("network={}\n", encoding="utf-8")
            supervisor = WifiSupervisor(path)

            path.chmod(0o644)
            self.assertFalse(supervisor.has_private_config())
            path.chmod(0o600)
            metadata = path.lstat()
            with patch.object(Path, "lstat", return_value=SimpleNamespace(
                st_mode=metadata.st_mode,
                st_uid=0,
            )):
                self.assertTrue(supervisor.has_private_config())
            self.assertFalse(supervisor.has_private_config())

    def test_active_configuration_marker_suppresses_all_network_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "configuring"
            marker.write_text("123\n", encoding="ascii")
            supervisor = WifiSupervisor(Path("/unused"), marker)

            with patch.object(supervisor, "_run") as run:
                state = supervisor.step()

            self.assertEqual(state, "configuration_in_progress")
            run.assert_not_called()

    def test_missing_wpa_flushes_stale_ip_then_starts_station_mode(self) -> None:
        supervisor = WifiSupervisor(Path("/unused"))
        responses = [
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout="4: wlan0 inet 192.168.43.12/24\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]

        with patch.object(supervisor, "_run", side_effect=responses) as run:
            state = supervisor.step()

        self.assertEqual(state, "starting_wpa")
        self.assertEqual(run.call_args_list[2].args[0], [
            "/usr/sbin/ip", "-4", "addr", "flush", "dev", "wlan0",
        ])
        self.assertEqual(run.call_args_list[3].args[0], ["/etc/init.d/wifi_init.sh", "sta"])

    def test_unassociated_wpa_flushes_stale_ip_without_requesting_dhcp(self) -> None:
        supervisor = WifiSupervisor(Path("/unused"))
        responses = [
            SimpleNamespace(returncode=0, stdout="wpa_state=SCANNING\nssid=Phone-Hotspot\n"),
            SimpleNamespace(returncode=0, stdout="4: wlan0 inet 192.168.43.12/24\n"),
            SimpleNamespace(returncode=0, stdout=""),
        ]

        with patch.object(supervisor, "_run", side_effect=responses) as run:
            state = supervisor.step()

        self.assertEqual(state, "waiting_for_association")
        self.assertEqual(len(run.call_args_list), 3)
        self.assertNotIn("udhcpc", " ".join(call.args[0][0] for call in run.call_args_list))

    def test_completed_association_without_ip_runs_bounded_dhcp(self) -> None:
        supervisor = WifiSupervisor(Path("/unused"))
        responses = [
            SimpleNamespace(returncode=0, stdout="wpa_state=COMPLETED\nssid=Phone-Hotspot\n"),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=1, stdout=""),
        ]

        with patch.object(supervisor, "_run", side_effect=responses) as run:
            state = supervisor.step()

        self.assertEqual(state, "dhcp_retry")
        self.assertEqual(run.call_args_list[2].args[0], [
            "/usr/sbin/udhcpc", "-i", "wlan0", "-q", "-n", "-t", "3", "-T", "2",
        ])
        self.assertEqual(run.call_args_list[2].args[1], supervisor.DHCP_TIMEOUT_SECONDS)

    def test_connected_station_does_not_restart_or_request_dhcp(self) -> None:
        supervisor = WifiSupervisor(Path("/unused"))
        responses = [
            SimpleNamespace(returncode=0, stdout="wpa_state=COMPLETED\nssid=Phone-Hotspot\n"),
            SimpleNamespace(returncode=0, stdout="4: wlan0 inet 192.168.43.12/24\n"),
        ]

        with patch.object(supervisor, "_run", side_effect=responses) as run:
            state = supervisor.step()

        self.assertEqual(state, "connected")
        self.assertEqual(len(run.call_args_list), 2)


if __name__ == "__main__":
    unittest.main()
