import tempfile
import unittest
from pathlib import Path

from camera_supervisor import CameraSupervisor, RollingLog


class CameraSupervisorTests(unittest.TestCase):
    def test_log_rotates_without_exceeding_the_configured_bound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.log"
            log = RollingLog(path, max_bytes=32)

            log.write(b"first-diagnostic-line\n")
            log.write(b"second-diagnostic-line\n")
            log.close()

            self.assertEqual(path.read_bytes(), b"second-diagnostic-line\n")
            self.assertEqual(path.with_name("camera.log.1").read_bytes(), b"first-diagnostic-line\n")
            self.assertLessEqual(path.stat().st_size, 32)

    def test_camera_settings_apply_rotation_to_the_encoder_at_full_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "camera.json"
            settings.write_text('{"rotationDegrees":90}\n', encoding="utf-8")
            shim = root / "libcamera_venc_rotation.so"
            shim.touch()
            supervisor = CameraSupervisor(
                ["/root/demo/cam_demo", "--diagnostics"],
                RollingLog(root / "camera.log", 1024),
                1.0,
                settings,
                60,
                shim,
            )
            self.assertEqual(
                supervisor._start_command(),
                ["/root/demo/cam_demo", "--diagnostics", "--fps", "60", "--rotate", "0"],
            )
            environment = supervisor._start_environment()
            self.assertEqual(environment["SYNCAP_VENC_ROTATION"], "90")
            self.assertEqual(environment["LD_PRELOAD"], str(shim))

    def test_180_degree_rotation_keeps_60_fps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "camera.json"
            settings.write_text('{"rotationDegrees":180}\n', encoding="utf-8")
            shim = root / "libcamera_venc_rotation.so"
            shim.touch()
            supervisor = CameraSupervisor(
                ["/root/demo/cam_demo"], RollingLog(root / "camera.log", 1024), 1.0,
                settings, 60, shim
            )
            self.assertEqual(supervisor._start_command()[-4:], ["--fps", "60", "--rotate", "0"])
            self.assertEqual(supervisor._start_environment()["SYNCAP_VENC_ROTATION"], "180")


if __name__ == "__main__":
    unittest.main()
