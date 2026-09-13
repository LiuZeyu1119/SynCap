import json
import tempfile
import unittest
from pathlib import Path

from syncap_service import CameraOrientationManager


class CameraOrientationManagerTests(unittest.TestCase):
    def test_missing_configuration_defaults_to_full_rate_landscape(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = CameraOrientationManager(Path(directory) / "camera.json", lambda: None).status()
            self.assertEqual(status["rotationDegrees"], 0)
            self.assertEqual(status["frameRate"], 60)
            self.assertEqual((status["outputWidth"], status["outputHeight"]), (1280, 1088))
            self.assertEqual(status["appliesTo"], ["preview", "calibration", "capture"])

    def test_configuration_is_persisted_and_requests_one_source_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            restarts = []
            result = CameraOrientationManager(path, lambda: restarts.append(True)).configure(270)
            self.assertEqual(restarts, [True])
            self.assertEqual(result["state"], "restarting")
            self.assertEqual(result["frameRate"], 60)
            self.assertEqual((result["outputWidth"], result["outputHeight"]), (1088, 1280))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["rotationDegrees"], 270)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_180_degree_rotation_remains_full_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = CameraOrientationManager(
                Path(directory) / "camera.json", lambda: None
            ).configure(180)
            self.assertEqual(result["frameRate"], 60)
            self.assertEqual((result["outputWidth"], result["outputHeight"]), (1280, 1088))

    def test_same_rotation_does_not_restart_camera(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            path.write_text('{"rotationDegrees":90}\n', encoding="utf-8")
            restarts = []
            result = CameraOrientationManager(path, lambda: restarts.append(True)).configure(90)
            self.assertEqual(restarts, [])
            self.assertEqual(result["state"], "active")

    def test_invalid_rotation_is_rejected_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            manager = CameraOrientationManager(path, lambda: None)
            with self.assertRaises(ValueError):
                manager.configure(45)
            self.assertFalse(path.exists())

    def test_failed_restart_rolls_back_the_persisted_rotation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera.json"
            path.write_text('{"rotationDegrees":90}\n', encoding="utf-8")
            manager = CameraOrientationManager(
                path,
                lambda: (_ for _ in ()).throw(RuntimeError("restart failed")),
            )
            with self.assertRaises(RuntimeError):
                manager.configure(270)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["rotationDegrees"], 90)


if __name__ == "__main__":
    unittest.main()
