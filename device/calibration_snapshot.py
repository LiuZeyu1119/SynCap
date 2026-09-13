#!/usr/bin/env python3
"""Bridge synchronized raw NV12 frame groups from cam_demo to the device API."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
import time
from pathlib import Path


CALIBRATION_SNAPSHOT_ROOT = Path("/tmp/syncap-calibration")
CALIBRATION_SNAPSHOT_READY = CALIBRATION_SNAPSHOT_ROOT / "ready"
CALIBRATION_SNAPSHOT_REQUEST = CALIBRATION_SNAPSHOT_ROOT / "request"
CALIBRATION_CAMERA_PAIRS = {("cam3", "cam0"), ("cam1", "cam2")}
CALIBRATION_SNAPSHOT_LOCK = threading.Lock()
CALIBRATION_FRAME_TIMEOUT_SECONDS = 5.0
CALIBRATION_SNAPSHOTS_TO_KEEP = 8


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_root_file(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(metadata.st_mode) and metadata.st_uid == 0


def _clean_old_snapshots() -> None:
    metadata_files = sorted(
        CALIBRATION_SNAPSHOT_ROOT.glob("snap_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for metadata in metadata_files[CALIBRATION_SNAPSHOTS_TO_KEEP:]:
        snapshot_id = metadata.stem
        metadata.unlink(missing_ok=True)
        for raw_frame in CALIBRATION_SNAPSHOT_ROOT.glob(f"{snapshot_id}_cam[0-3].nv12"):
            raw_frame.unlink(missing_ok=True)


def capture_synchronized_calibration_pair(camera_ids: tuple[str, str]) -> dict[str, object]:
    if camera_ids not in CALIBRATION_CAMERA_PAIRS:
        raise ValueError("unsupported calibration camera pair")
    with CALIBRATION_SNAPSHOT_LOCK:
        if not _regular_root_file(CALIBRATION_SNAPSHOT_READY):
            raise RuntimeError("raw camera snapshot service is not ready")
        snapshot_id = f"snap_{int(time.time() * 1000)}_{secrets.token_hex(3)}"
        result_path = CALIBRATION_SNAPSHOT_ROOT / f"{snapshot_id}.json"
        request_temporary = CALIBRATION_SNAPSHOT_ROOT / f".request-{os.getpid()}-{secrets.token_hex(3)}"
        request_temporary.write_text(
            f"{snapshot_id} {camera_ids[0]} {camera_ids[1]}\n",
            encoding="ascii",
        )
        os.chmod(request_temporary, 0o600)
        os.replace(request_temporary, CALIBRATION_SNAPSHOT_REQUEST)
        try:
            deadline = time.monotonic() + CALIBRATION_FRAME_TIMEOUT_SECONDS
            while time.monotonic() < deadline and not result_path.is_file():
                time.sleep(0.02)
            if not result_path.is_file():
                raise RuntimeError("timed out waiting for a synchronized raw camera frame")
            metadata = json.loads(result_path.read_text(encoding="utf-8"))
            if metadata.get("error"):
                raise RuntimeError(f"raw camera snapshot failed: {metadata['error']}")
            if (
                metadata.get("schema") != "syncap.raw-frame/1.0"
                or metadata.get("snapshotId") != snapshot_id
                or metadata.get("source") != "synchronized_raw_nv12"
            ):
                raise RuntimeError("camera snapshot returned an invalid raw-frame contract")
            if abs(int(metadata.get("syncErrorNs", 2_000_001))) > 2_000_000:
                raise RuntimeError("camera snapshot synchronization error exceeds 2 ms")

            cameras = metadata.get("cameras")
            if not isinstance(cameras, list) or len(cameras) != 2:
                raise RuntimeError("camera snapshot did not return two raw frames")
            by_id = {camera.get("id"): camera for camera in cameras if isinstance(camera, dict)}
            output_cameras = []
            for camera_id in camera_ids:
                camera = by_id.get(camera_id)
                if camera is None or camera.get("format") != "nv12":
                    raise RuntimeError(f"camera snapshot is missing raw {camera_id}")
                width = int(camera.get("width", 0))
                height = int(camera.get("height", 0))
                filename = camera.get("file")
                expected_size = width * height * 3 // 2
                if (
                    width <= 0
                    or height <= 0
                    or width % 2 != 0
                    or height % 2 != 0
                    or int(camera.get("sizeBytes", -1)) != expected_size
                    or filename != f"{snapshot_id}_{camera_id}.nv12"
                ):
                    raise RuntimeError(f"camera snapshot returned invalid {camera_id} dimensions")
                raw_path = CALIBRATION_SNAPSHOT_ROOT / filename
                if not _regular_root_file(raw_path) or raw_path.stat().st_size != expected_size:
                    raise RuntimeError(f"camera snapshot returned an incomplete raw {camera_id} frame")
                output_camera = dict(camera)
                output_camera.pop("file", None)
                output_camera["sha256"] = _sha256(raw_path)
                output_camera["path"] = (
                    f"/v1/calibration/snapshots/{snapshot_id}/{camera_id}.nv12"
                )
                output_cameras.append(output_camera)
            metadata["schema"] = "syncap.calibration-snapshot/1.0"
            metadata["cameras"] = output_cameras
            _clean_old_snapshots()
            return metadata
        finally:
            request_temporary.unlink(missing_ok=True)
            try:
                if CALIBRATION_SNAPSHOT_REQUEST.read_text(encoding="ascii").startswith(snapshot_id + " "):
                    CALIBRATION_SNAPSHOT_REQUEST.unlink(missing_ok=True)
            except OSError:
                pass
