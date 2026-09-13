"""Host-only S96 health regressions. No Bluetooth devices or services are used."""

import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest


SCRIPT = Path(__file__).with_name("S96syncap-tina-ble")
DEFINITIONS, DISPATCH = SCRIPT.read_text().split('\ncase "${1:-}" in', 1)
DISPATCH = '\ncase "${1:-}" in' + DISPATCH
HEALTHY = """hci0: Type: Primary Bus: UART
    BD Address: B4:E0:77:21:A4:BD
    UP RUNNING PSCAN ISCAN
    Name: 'SynCap-Tina'
    Class: 0x000000
"""


class TinaBleHealthTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="syncap-ble-health-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.command = self.root / "hciconfig"
        self.trace = self.root / "trace"
        self.command.write_text(
            "#!/bin/sh\n"
            "printf 'probe\\n' >>\"$TEST_TRACE\"\n"
            "printf '%s' \"$TEST_HCI_OUTPUT\"\n"
            "exit \"$TEST_HCI_STATUS\"\n"
        )
        self.command.chmod(0o700)
        self.env = {
            **os.environ,
            "SYNCAP_TINA_BLE_HCICONFIG": str(self.command),
            "SYNCAP_TINA_BLE_LOG": str(self.root / "service.log"),
            "SYNCAP_TINA_BLE_READY_FILE": str(self.root / "ready"),
            "SYNCAP_TINA_BLE_PID": str(self.root / "supervisor.pid"),
            "SYNCAP_TINA_BLE_CHILD_PID": str(self.root / "helper.pid"),
            "TEST_TRACE": str(self.trace),
            "TEST_HCI_OUTPUT": HEALTHY,
            "TEST_HCI_STATUS": "0",
        }

    def run_shell(self, suffix, *, overrides="", argument=None):
        # macOS lacks /proc; replace only the process-presence primitive. The
        # production timeout/termination loops and tool runner are exercised.
        source = DEFINITIONS + """
sdp_process_running() { kill -0 "$1" 2>/dev/null; }
usleep() { sleep 0.1; }
""" + overrides + "\n" + suffix
        command = ["/bin/sh", "-s", "--"]
        if argument is not None:
            command.append(argument)
        return subprocess.run(command, input=source, text=True, env=self.env,
                              capture_output=True, timeout=12)

    def test_healthy_controller_requires_local_name_response(self):
        result = self.run_shell("bluetooth_controller_healthy")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text(), "probe\n")
        self.assertEqual(list(self.root.glob("*.hciconfig.*")), [])

    def test_up_running_with_local_name_timeout_is_not_healthy(self):
        self.env["TEST_HCI_OUTPUT"] = (
            "hci0: Type: Primary Bus: UART\n    UP RUNNING\n"
            "Can't read local name on hci0: Operation timed out (110)\n"
        )
        result = self.run_shell("bluetooth_controller_healthy")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Operation timed out (110)", result.stderr)

    def test_error_exit_rejects_even_healthy_looking_output(self):
        self.env["TEST_HCI_STATUS"] = "1"
        result = self.run_shell("bluetooth_controller_healthy")
        self.assertEqual(result.returncode, 1)

    def test_missing_up_or_missing_output_is_not_healthy(self):
        for output in ["    Name: 'SynCap-Tina'\n", ""]:
            with self.subTest(output=output):
                self.env["TEST_HCI_OUTPUT"] = output
                result = self.run_shell("bluetooth_controller_healthy")
                self.assertEqual(result.returncode, 1)

    def test_missing_hciconfig_is_not_healthy(self):
        self.command.unlink()
        result = self.run_shell("bluetooth_controller_healthy")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.trace.exists())

    def test_stuck_probe_has_bounded_termination_and_no_survivor(self):
        pid_file = self.root / "probe.pid"
        self.command.write_text(
            f"#!{sys.executable}\n"
            "import os, signal, time\n"
            f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "while True: time.sleep(0.01)\n"
        )
        started = time.monotonic()
        try:
            result = self.run_shell("bluetooth_controller_healthy")
            self.assertEqual(result.returncode, 1)
            self.assertIn("status 124", result.stderr)
            self.assertLess(time.monotonic() - started, 11)
            pid = int(pid_file.read_text())
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            if pid_file.exists():
                try:
                    os.kill(int(pid_file.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_failed_probe_stops_supervisor_without_retry_or_helper(self):
        self.env["TEST_HCI_OUTPUT"] = "    UP RUNNING\n"
        overrides = """
wait_for_system_bus() { return 0; }
prepare_bluetooth() { printf 'prepare\\n' >>"$TEST_TRACE"; }
ensure_spp_record() { printf 'unexpected-sdp\\n' >>"$TEST_TRACE"; }
remove_spp_record() { printf 'remove-sdp\\n' >>"$TEST_TRACE"; }
"""
        result = self.run_shell("supervise_service", overrides=overrides)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.trace.read_text(), "prepare\nprobe\nremove-sdp\n")
        self.assertIn("no automatic controller reset", result.stdout)
        self.assertFalse((self.root / "helper.pid").exists())

    def test_status_does_not_probe_and_describes_only_listener(self):
        overrides = """
running() { return 0; }
recorded_process_running() { RECORDED_PID=123; return 0; }
helper_rfcomm_ready() { return 0; }
"""
        result = self.run_shell(DISPATCH, overrides=overrides, argument="status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("helper/RFCOMM listener only", result.stdout)
        self.assertFalse(self.trace.exists())

    def test_explicit_health_checks_controller_not_just_listener(self):
        result = self.run_shell(DISPATCH,
                                overrides="bluetooth_prerequisites_ready() { return 0; }",
                                argument="health")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.trace.read_text(), "probe\n")
        self.assertIn("not an end-to-end provisioning test", result.stdout)

    def test_explicit_health_without_prerequisites_skips_hci(self):
        result = self.run_shell(DISPATCH,
                                overrides="bluetooth_prerequisites_ready() { return 1; }",
                                argument="health")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.trace.exists())


if __name__ == "__main__":
    unittest.main()
