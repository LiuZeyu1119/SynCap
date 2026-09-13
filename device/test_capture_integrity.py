import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from syncap_service import (
    CAPTURE_STARTUP_POLL_SECONDS,
    CAPTURE_STARTUP_TIMEOUT_SECONDS,
    CaptureManager,
    IMU_RECORD_COMMAND,
    IMU_SAMPLE_RATE_HZ,
    capture_duration_ms,
    fragmented_mp4_has_media_fragment,
    inspect_fragmented_mp4,
    validate_capture_videos,
)


def box(box_type: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", len(payload) + 8, box_type) + payload


def fragmented_mp4(sample_durations: list[int], timescale: int = 90_000) -> bytes:
    mdhd = b"\0\0\0\0" + struct.pack(">IIII", 0, 0, timescale, sum(sample_durations)) + b"\0\0\0\0"
    moov = box(b"moov", box(b"trak", box(b"mdia", box(b"mdhd", mdhd))))
    tfhd = b"\0\0\0\0" + struct.pack(">I", 1)
    tfdt = b"\1\0\0\0" + struct.pack(">Q", 0)
    trun = b"\0\0\1\0" + struct.pack(">I", len(sample_durations))
    trun += b"".join(struct.pack(">I", duration) for duration in sample_durations)
    moof = box(b"moof", box(b"traf", box(b"tfhd", tfhd) + box(b"tfdt", tfdt) + box(b"trun", trun)))
    return moov + moof


class CaptureIntegrityTests(unittest.TestCase):
    def test_imu_recorder_persists_every_supported_sample(self) -> None:
        self.assertEqual(IMU_SAMPLE_RATE_HZ, 100)
        self.assertEqual(
            IMU_RECORD_COMMAND,
            [
                "/root/demo/imu_reader_demo",
                "--sample-rate-hz",
                "100",
            ],
        )

    def test_capture_ready_requires_imu_sample_and_all_four_media_fragments(self) -> None:
        payload = fragmented_mp4([1500] * 60)
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            (session / "imu.log").write_text("Starting ICM-42688 demo at 100 Hz\n", encoding="utf-8")
            for index in range(4):
                (session / f"cam{index}.mp4").write_bytes(box(b"ftyp", b"isom"))

            manager = CaptureManager.__new__(CaptureManager)
            manager.processes = [
                SimpleNamespace(name=name, process=SimpleNamespace(poll=lambda: None))
                for name in ["imu0", "cam0", "cam1", "cam2", "cam3"]
            ]
            poll_count = 0

            def publish_startup_data(_: float) -> None:
                nonlocal poll_count
                poll_count += 1
                if poll_count == 1:
                    (session / "imu.log").write_text(
                        "Starting ICM-42688 demo at 100 Hz\nts_ns=10000000 dt_ms=0.000000\n",
                        encoding="utf-8",
                    )
                    for index in range(3):
                        (session / f"cam{index}.mp4").write_bytes(payload)
                elif poll_count == 2:
                    (session / "cam3.mp4").write_bytes(payload)

            with patch("syncap_service.time.monotonic", side_effect=[0.0, 0.1, 0.2]), \
                    patch("syncap_service.time.sleep", side_effect=publish_startup_data) as sleep:
                manager._wait_for_recorders_ready(session)

            self.assertEqual(sleep.call_count, 2)
            sleep.assert_called_with(CAPTURE_STARTUP_POLL_SECONDS)

    def test_capture_startup_timeout_names_every_missing_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            manager = CaptureManager.__new__(CaptureManager)
            manager.processes = [
                SimpleNamespace(name=name, process=SimpleNamespace(poll=lambda: None))
                for name in ["imu0", "cam0", "cam1", "cam2", "cam3"]
            ]

            with patch(
                "syncap_service.time.monotonic",
                side_effect=[0.0, CAPTURE_STARTUP_TIMEOUT_SECONDS],
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    r"timed out.*imu0 ts_ns sample.*cam0 media fragment.*cam3 media fragment",
                ):
                    manager._wait_for_recorders_ready(session)

    def test_start_is_starting_until_first_samples_are_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture_root = Path(directory)
            (capture_root / "sessions").mkdir()
            manager = CaptureManager.__new__(CaptureManager)
            manager.storage = SimpleNamespace(
                capture_root=lambda: (capture_root, {"target": "internal"}),
            )
            capture_clock_status = Mock(return_value={"state": "locked", "requiredForCapture": False})
            manager.telemetry = SimpleNamespace(capture_clock_status=capture_clock_status)
            manager.lock = threading.RLock()
            manager.active = None
            manager.active_session_path = None
            manager.processes = []
            manager._port_open = Mock(return_value=True)
            manager._rtsp_path_ready = Mock(return_value=True)
            manager._spawn_recorders = Mock(return_value=[])
            manager._wait_for_recorders_ready = Mock()
            manifest_states: list[dict[str, object]] = []
            manager._write_active_manifest = lambda: manifest_states.append(dict(manager.active or {}))

            with patch("syncap_service.secrets.token_hex", return_value="abcdef"), \
                    patch("syncap_service.time.monotonic_ns", side_effect=[1_000, 9_000]), \
                    patch(
                        "syncap_service.utc_now",
                        side_effect=["2026-08-26T12:00:00.000Z", "2026-08-26T12:00:04.000Z"],
                    ):
                result = manager.start("first-frame-test")

            self.assertEqual([item["state"] for item in manifest_states], ["starting", "recording"])
            self.assertEqual(manifest_states[0]["startedMonotonicNs"], "1000")
            self.assertEqual(manifest_states[1]["startedMonotonicNs"], "9000")
            self.assertEqual(manifest_states[1]["startedAt"], "2026-08-26T12:00:04.000Z")
            self.assertEqual(manifest_states[0]["imu"]["timestampDomain"], "monotonic_raw")
            self.assertEqual(manifest_states[0]["clockAtStart"]["state"], "locked")
            capture_clock_status.assert_called_once_with()
            self.assertEqual(result["state"], "recording")
            self.assertEqual(result["startedAtDeviceTimeNs"], "9000")

    def test_duration_excludes_recorder_startup_time(self) -> None:
        manifest = {
            "captureRequestedMonotonicNs": "1000000000",
            "startedMonotonicNs": "4000000000",
        }

        self.assertEqual(capture_duration_ms(manifest, 9_000_000_000), 5000)

    def test_media_fragment_probe_rejects_header_only_mp4(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cam0.mp4"
            path.write_bytes(box(b"ftyp", b"isom"))
            self.assertFalse(fragmented_mp4_has_media_fragment(path))
            path.write_bytes(fragmented_mp4([1500] * 2))
            self.assertTrue(fragmented_mp4_has_media_fragment(path))

    def test_four_healthy_sixty_fps_streams_pass(self) -> None:
        payload = fragmented_mp4([1500] * 600)
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            for index in range(4):
                (session / f"cam{index}.mp4").write_bytes(payload)
                (session / f"cam{index}.stderr.log").write_text("", encoding="utf-8")

            result = validate_capture_videos(session, 10_000)

        self.assertTrue(result["valid"], result["errors"])
        self.assertEqual(result["cameras"]["cam0"]["sampleCount"], 600)
        self.assertEqual(result["cameras"]["cam0"]["maxFrameIntervalMs"], 16.667)

    def test_multi_second_startup_gap_is_rejected(self) -> None:
        healthy = fragmented_mp4([1500] * 600)
        frozen = fragmented_mp4([1500] * 42 + [265_500] + [1500] * 557)
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            for index in range(4):
                (session / f"cam{index}.mp4").write_bytes(frozen if index == 0 else healthy)
                (session / f"cam{index}.stderr.log").write_text("", encoding="utf-8")

            result = validate_capture_videos(session, 10_000)

        self.assertFalse(result["valid"])
        self.assertEqual(result["cameras"]["cam0"]["maxFrameIntervalMs"], 2950.0)
        self.assertTrue(any("frame interval 2950.0 ms" in error for error in result["errors"]))

    def test_rtsp_and_h264_errors_are_rejected(self) -> None:
        payload = fragmented_mp4([1500] * 600)
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory)
            for index in range(4):
                (session / f"cam{index}.mp4").write_bytes(payload)
                log = "CSeq 4 expected, 0 received\nInvalid NAL unit 0, skipping.\n" if index == 2 else ""
                (session / f"cam{index}.stderr.log").write_text(log, encoding="utf-8")

            result = validate_capture_videos(session, 10_000)

        self.assertFalse(result["valid"])
        self.assertTrue(any("cam2: recorder reported: CSeq" in error for error in result["errors"]))

    def test_real_fragmented_mp4_metrics_include_nominal_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cam0.mp4"
            path.write_bytes(fragmented_mp4([1500] * 120))

            metrics = inspect_fragmented_mp4(path)

        self.assertEqual(metrics["durationMs"], 2000)
        self.assertEqual(metrics["averageFps"], 60.0)
        self.assertEqual(metrics["nominalFps"], 60.0)


if __name__ == "__main__":
    unittest.main()
