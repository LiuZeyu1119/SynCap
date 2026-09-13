import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import calibration_snapshot


class CalibrationSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.ready = self.root / "ready"
        self.request = self.root / "request"
        self.ready.write_text("synchronized_raw_nv12\n", encoding="ascii")
        self.patches = [
            patch.object(calibration_snapshot, "CALIBRATION_SNAPSHOT_ROOT", self.root),
            patch.object(calibration_snapshot, "CALIBRATION_SNAPSHOT_READY", self.ready),
            patch.object(calibration_snapshot, "CALIBRATION_SNAPSHOT_REQUEST", self.request),
            patch.object(calibration_snapshot, "CALIBRATION_FRAME_TIMEOUT_SECONDS", 0.5),
            patch.object(
                calibration_snapshot,
                "_regular_root_file",
                side_effect=lambda path: path.is_file(),
            ),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.temporary.cleanup()

    def _respond(self, source: str = "synchronized_raw_nv12", sync_error_ns: int = 1000) -> None:
        deadline = time.monotonic() + 0.4
        while time.monotonic() < deadline and not self.request.is_file():
            time.sleep(0.005)
        request = self.request.read_text(encoding="ascii").strip().split()
        snapshot_id, first, second = request
        cameras = []
        for index, camera_id in enumerate((first, second)):
            raw = bytes([32 + index]) * 12
            filename = f"{snapshot_id}_{camera_id}.nv12"
            (self.root / filename).write_bytes(raw)
            cameras.append({
                "id": camera_id,
                "width": 4,
                "height": 2,
                "format": "nv12",
                "sizeBytes": len(raw),
                "file": filename,
                "sequence": 42,
                "frameId": 7,
                "timestampNs": 123456,
            })
        (self.root / f"{snapshot_id}.json").write_text(json.dumps({
            "schema": "syncap.raw-frame/1.0",
            "snapshotId": snapshot_id,
            "source": source,
            "syncErrorNs": sync_error_ns,
            "cameras": cameras,
        }), encoding="utf-8")

    def test_returns_only_verified_raw_nv12_frames(self) -> None:
        worker = threading.Thread(target=self._respond)
        worker.start()
        response = calibration_snapshot.capture_synchronized_calibration_pair(("cam3", "cam0"))
        worker.join()

        self.assertEqual(response["schema"], "syncap.calibration-snapshot/1.0")
        self.assertEqual(response["source"], "synchronized_raw_nv12")
        self.assertEqual([camera["id"] for camera in response["cameras"]], ["cam3", "cam0"])
        for camera in response["cameras"]:
            expected = hashlib.sha256(bytes([32 if camera["id"] == "cam3" else 33]) * 12).hexdigest()
            self.assertEqual(camera["sha256"], expected)
            self.assertTrue(camera["path"].endswith(f"/{camera['id']}.nv12"))
            self.assertNotIn("file", camera)

    def test_rejects_non_raw_source_without_fallback(self) -> None:
        worker = threading.Thread(target=lambda: self._respond(source="synchronized_main_rtsp_idr"))
        worker.start()
        with self.assertRaisesRegex(RuntimeError, "invalid raw-frame contract"):
            calibration_snapshot.capture_synchronized_calibration_pair(("cam1", "cam2"))
        worker.join()

    def test_rejects_unsupported_pair_before_writing_request(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported calibration camera pair"):
            calibration_snapshot.capture_synchronized_calibration_pair(("cam0", "cam1"))
        self.assertFalse(self.request.exists())

    def test_requires_camera_raw_snapshot_service(self) -> None:
        self.ready.unlink()
        with self.assertRaisesRegex(RuntimeError, "not ready"):
            calibration_snapshot.capture_synchronized_calibration_pair(("cam3", "cam0"))

    def test_rejects_frame_group_over_two_milliseconds(self) -> None:
        worker = threading.Thread(target=lambda: self._respond(sync_error_ns=2_000_001))
        worker.start()
        with self.assertRaisesRegex(RuntimeError, "exceeds 2 ms"):
            calibration_snapshot.capture_synchronized_calibration_pair(("cam3", "cam0"))
        worker.join()


if __name__ == "__main__":
    unittest.main()
