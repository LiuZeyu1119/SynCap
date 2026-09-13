#!/usr/bin/env python3
"""SynCap device service for capture, sessions, export, Wi-Fi, and telemetry."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import struct
import subprocess
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO
from urllib.parse import unquote, urlparse

from legacy_adapter import AdapterState
from calibration_snapshot import (
    CALIBRATION_SNAPSHOT_READY,
    CALIBRATION_SNAPSHOT_ROOT,
    capture_synchronized_calibration_pair,
)


SESSION_ID = re.compile(r"^ses_[0-9]{8}_[0-9]{6}_[0-9a-f]{6}$")
CAPTURE_STOP_PATH = re.compile(r"^/v1/captures/(?P<capture_id>cap_[0-9]{8}_[0-9]{6}_[0-9a-f]{6})/stop$")
SESSION_MANIFEST_PATH = re.compile(r"^/v1/sessions/(?P<session_id>ses_[^/]+)/manifest$")
SESSION_EXPORT_PATH = re.compile(r"^/v1/sessions/(?P<session_id>ses_[^/]+)/prepare-export$")
SESSION_FILE_PATH = re.compile(r"^/v1/sessions/(?P<session_id>ses_[^/]+)/files/(?P<name>[^/]+)$")
CALIBRATION_SNAPSHOT_FILE_PATH = re.compile(
    r"^/v1/calibration/snapshots/(?P<snapshot_id>snap_[0-9]+_[0-9a-f]{6})/"
    r"(?P<camera_id>cam[0-3])\.nv12$"
)
CAMERA_CONFIGURATION_PATH = Path("/userdata/syncap/camera.json")
CAMERA_SUPERVISOR_PID_PATH = Path("/var/run/syncap-camera.pid")
FIXED_CLAIM_CODE = "123456"
FIXED_CLAIM_CODE_BYTES = (FIXED_CLAIM_CODE + "\n").encode("ascii")
WIFI_CONFIGURE_MARKER = Path("/var/run/syncap-wifi-configuring")
CAPTURE_RELAY_PORT = 8554
CAPTURE_MAX_FRAME_INTERVAL_MS = 100.0
CAPTURE_STARTUP_TIMEOUT_SECONDS = 12.0
CAPTURE_STARTUP_POLL_SECONDS = 0.05
IMU_SAMPLE_RATE_HZ = 100
IMU_RECORD_COMMAND = [
    "/root/demo/imu_reader_demo",
    "--sample-rate-hz",
    str(IMU_SAMPLE_RATE_HZ),
]
CAPTURE_ERROR_PATTERN = re.compile(
    r"CSeq .* expected|NAL unit type|sps_id .* out of range|Invalid NAL unit|"
    r"Non-monotonous DTS|partial file|corrupt decoded frame",
    re.IGNORECASE,
)


def normalize_endpoint_host(value: object) -> str | None:
    """Return a safe host without a port, or None for malformed input."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > 255 or any(character.isspace() for character in value):
        return None
    if any(character in value for character in "/\\@?#,"):
        return None
    try:
        parsed = urlparse(f"//{value}")
        host = parsed.hostname
        _ = parsed.port
    except ValueError:
        return None
    if host is None or parsed.username is not None or parsed.password is not None:
        return None
    host = host.rstrip(".")
    if not host or "%" in host:
        return None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    if len(host) > 253:
        return None
    labels = host.split(".")
    if any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in labels
    ):
        return None
    return host.lower()


def usable_endpoint_host(value: str | None) -> bool:
    if value is None:
        return False
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return value != "localhost"
    return not address.is_unspecified and not address.is_loopback


def format_url_host(host: str) -> str:
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        return host


def apply_manifest_host(manifest: dict[str, object], host: str) -> None:
    """Keep the advertised device endpoint and every preview URL consistent."""
    device = manifest.get("device")
    if isinstance(device, dict):
        device["ipAddress"] = host
    for camera in manifest.get("cameras", []):
        if not isinstance(camera, dict):
            continue
        preview = camera.get("preview")
        if not isinstance(preview, dict) or not isinstance(preview.get("url"), str):
            continue
        parsed = urlparse(preview["url"])
        try:
            port = parsed.port
        except ValueError:
            continue
        authority = format_url_host(host)
        if port is not None:
            authority = f"{authority}:{port}"
        preview["url"] = parsed._replace(netloc=authority).geturl()


def routed_local_host(peer_address: object) -> str | None:
    """Resolve the interface address the kernel would use to reach this client."""
    if not isinstance(peer_address, tuple) or not peer_address:
        return None
    peer = normalize_endpoint_host(peer_address[0])
    if peer is None:
        return None
    try:
        address = ipaddress.ip_address(peer)
        family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        target = (peer, 9, 0, 0) if family == socket.AF_INET6 else (peer, 9)
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect(target)
            return normalize_endpoint_host(probe.getsockname()[0])
    except OSError:
        return None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def ensure_claim_code(path: Path) -> None:
    """Install the development compatibility claim without exposing it in clients."""
    try:
        metadata = path.lstat()
        current = path.read_bytes()
    except OSError:
        metadata = None
        current = b""
    if (
        metadata is not None
        and stat.S_ISREG(metadata.st_mode)
        and metadata.st_uid == 0
        and stat.S_IMODE(metadata.st_mode) == 0o600
        and current == FIXED_CLAIM_CODE_BYTES
    ):
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(4)}")
    try:
        with temporary.open("xb") as output:
            output.write(FIXED_CLAIM_CODE_BYTES)
            output.flush()
            os.fchmod(output.fileno(), 0o600)
            os.fchown(output.fileno(), 0, 0)
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


@contextmanager
def wifi_configuration_operation(path: Path = WIFI_CONFIGURE_MARKER):
    created = False
    try:
        path.write_text(f"{os.getpid()}\n", encoding="ascii")
        created = True
    except OSError:
        pass
    try:
        yield
    finally:
        if created:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def capture_duration_ms(manifest: dict[str, object], stop_requested_ns: int) -> int:
    """Measure user-visible recording time from the verified recorder-ready anchor."""
    started_ns = int(str(manifest["startedMonotonicNs"]))
    return max(0, round((stop_requested_ns - started_ns) / 1_000_000))


def safe_capture_name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("name must be a string")
    value = value.strip()
    if not value or len(value.encode("utf-8")) > 96:
        raise ValueError("name must contain 1 to 96 UTF-8 bytes")
    if any(character in value for character in "\r\n\0"):
        raise ValueError("name contains an invalid character")
    return value


def parse_byte_range(value: str | None, size: int) -> tuple[int, int] | None:
    if value is None:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", value.strip())
    if match is None or size <= 0:
        raise ValueError("invalid range")
    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise ValueError("invalid range")
    if not start_text:
        length = int(end_text)
        if length <= 0:
            raise ValueError("invalid range")
        return max(0, size - length), size - 1
    start = int(start_text)
    end = int(end_text) if end_text else size - 1
    if start >= size or end < start:
        raise ValueError("range not satisfiable")
    return start, min(end, size - 1)


def _mp4_boxes(data: bytes):
    position = 0
    while position + 8 <= len(data):
        size, box_type = struct.unpack_from(">I4s", data, position)
        header_size = 8
        if size == 1:
            if position + 16 > len(data):
                raise ValueError("truncated extended MP4 box")
            size = struct.unpack_from(">Q", data, position + 8)[0]
            header_size = 16
        elif size == 0:
            size = len(data) - position
        if size < header_size or position + size > len(data):
            raise ValueError("invalid MP4 box size")
        yield box_type, data[position + header_size:position + size]
        position += size
    if position != len(data) and any(data[position:]):
        raise ValueError("trailing bytes after MP4 boxes")


def _mp4_timescale(moov: bytes) -> int:
    containers = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"mvex"}

    def visit(payload: bytes) -> int | None:
        for box_type, box in _mp4_boxes(payload):
            if box_type == b"mdhd":
                if len(box) < 16:
                    raise ValueError("truncated mdhd box")
                version = box[0]
                offset = 20 if version == 1 else 12
                if len(box) < offset + 4:
                    raise ValueError("truncated mdhd timescale")
                value = struct.unpack_from(">I", box, offset)[0]
                if value <= 0:
                    raise ValueError("invalid MP4 timescale")
                return value
            if box_type in containers:
                result = visit(box)
                if result is not None:
                    return result
        return None

    timescale = visit(moov)
    if timescale is None:
        raise ValueError("MP4 video timescale is missing")
    return timescale


def _fragment_samples(moof: bytes) -> list[tuple[int, list[int]]]:
    fragments: list[tuple[int, list[int]]] = []
    for box_type, traf in _mp4_boxes(moof):
        if box_type != b"traf":
            continue
        default_duration: int | None = None
        base_decode_time: int | None = None
        runs: list[bytes] = []
        for child_type, child in _mp4_boxes(traf):
            if child_type == b"tfhd":
                if len(child) < 8:
                    raise ValueError("truncated tfhd box")
                flags = int.from_bytes(child[1:4], "big")
                offset = 8
                if flags & 0x000001:
                    offset += 8
                if flags & 0x000002:
                    offset += 4
                if flags & 0x000008:
                    if len(child) < offset + 4:
                        raise ValueError("truncated default sample duration")
                    default_duration = struct.unpack_from(">I", child, offset)[0]
            elif child_type == b"tfdt":
                if len(child) < 8:
                    raise ValueError("truncated tfdt box")
                if child[0] == 1:
                    if len(child) < 12:
                        raise ValueError("truncated 64-bit tfdt box")
                    base_decode_time = struct.unpack_from(">Q", child, 4)[0]
                else:
                    base_decode_time = struct.unpack_from(">I", child, 4)[0]
            elif child_type == b"trun":
                runs.append(child)
        if base_decode_time is None or not runs:
            continue
        cursor = base_decode_time
        for run in runs:
            if len(run) < 8:
                raise ValueError("truncated trun box")
            flags = int.from_bytes(run[1:4], "big")
            sample_count = struct.unpack_from(">I", run, 4)[0]
            offset = 8
            if flags & 0x000001:
                offset += 4
            if flags & 0x000004:
                offset += 4
            durations: list[int] = []
            for _ in range(sample_count):
                if flags & 0x000100:
                    if len(run) < offset + 4:
                        raise ValueError("truncated sample duration")
                    duration = struct.unpack_from(">I", run, offset)[0]
                    offset += 4
                elif default_duration is not None:
                    duration = default_duration
                else:
                    raise ValueError("sample duration is missing")
                if flags & 0x000200:
                    offset += 4
                if flags & 0x000400:
                    offset += 4
                if flags & 0x000800:
                    offset += 4
                if duration <= 0 or len(run) < offset:
                    raise ValueError("invalid trun sample entry")
                durations.append(duration)
            fragments.append((cursor, durations))
            cursor += sum(durations)
    return fragments


def fragmented_mp4_has_media_fragment(path: Path) -> bool:
    """Return true only after FFmpeg has committed a timed media fragment."""
    try:
        file_size = path.stat().st_size
        with path.open("rb") as source:
            position = 0
            while position + 8 <= file_size:
                source.seek(position)
                header = source.read(8)
                if len(header) != 8:
                    return False
                size, box_type = struct.unpack(">I4s", header)
                header_size = 8
                if size == 1:
                    extended = source.read(8)
                    if len(extended) != 8:
                        return False
                    size = struct.unpack(">Q", extended)[0]
                    header_size = 16
                elif size == 0:
                    size = file_size - position
                if size < header_size or position + size > file_size:
                    return False
                if box_type == b"moof":
                    payload = source.read(size - header_size)
                    if len(payload) != size - header_size:
                        return False
                    return any(durations for _, durations in _fragment_samples(payload))
                position += size
    except (OSError, ValueError, struct.error):
        return False
    return False


def inspect_fragmented_mp4(path: Path) -> dict[str, object]:
    file_size = path.stat().st_size
    timescale: int | None = None
    fragments: list[tuple[int, list[int]]] = []
    with path.open("rb") as source:
        position = 0
        while position + 8 <= file_size:
            source.seek(position)
            header = source.read(8)
            if len(header) != 8:
                raise ValueError("truncated MP4 header")
            size, box_type = struct.unpack(">I4s", header)
            header_size = 8
            if size == 1:
                extended = source.read(8)
                if len(extended) != 8:
                    raise ValueError("truncated extended MP4 header")
                size = struct.unpack(">Q", extended)[0]
                header_size = 16
            elif size == 0:
                size = file_size - position
            if size < header_size or position + size > file_size:
                raise ValueError("invalid top-level MP4 box")
            if box_type in {b"moov", b"moof"}:
                payload = source.read(size - header_size)
                if len(payload) != size - header_size:
                    raise ValueError("truncated MP4 payload")
                if box_type == b"moov":
                    timescale = _mp4_timescale(payload)
                else:
                    fragments.extend(_fragment_samples(payload))
            position += size

    if timescale is None or not fragments:
        raise ValueError("fragmented MP4 timing metadata is missing")
    fragments.sort(key=lambda item: item[0])
    interval_counts: dict[int, int] = {}
    sample_count = 0
    first_decode_time = fragments[0][0]
    previous_end = first_decode_time
    maximum_interval = 0
    for base_decode_time, durations in fragments:
        if base_decode_time < previous_end:
            raise ValueError("MP4 decode time regressed")
        if base_decode_time > previous_end:
            maximum_interval = max(maximum_interval, base_decode_time - previous_end)
        for duration in durations:
            interval_counts[duration] = interval_counts.get(duration, 0) + 1
            maximum_interval = max(maximum_interval, duration)
        sample_count += len(durations)
        previous_end = base_decode_time + sum(durations)
    if sample_count < 2 or previous_end <= first_decode_time:
        raise ValueError("MP4 contains too few timed samples")

    midpoint = (sample_count - 1) // 2
    cumulative = 0
    nominal_interval = 0
    for duration, count in sorted(interval_counts.items()):
        cumulative += count
        if cumulative > midpoint:
            nominal_interval = duration
            break
    duration_seconds = (previous_end - first_decode_time) / timescale
    return {
        "sampleCount": sample_count,
        "durationMs": round(duration_seconds * 1000),
        "averageFps": round(sample_count / duration_seconds, 3),
        "nominalFps": round(timescale / nominal_interval, 3),
        "maxFrameIntervalMs": round(maximum_interval * 1000 / timescale, 3),
    }


def validate_capture_videos(session_path: Path, expected_duration_ms: int) -> dict[str, object]:
    cameras: dict[str, object] = {}
    errors: list[str] = []
    durations: list[int] = []
    counts: list[int] = []
    for index in range(4):
        camera_id = f"cam{index}"
        video_path = session_path / f"{camera_id}.mp4"
        log_path = session_path / f"{camera_id}.stderr.log"
        camera_errors: list[str] = []
        try:
            metrics = inspect_fragmented_mp4(video_path)
            durations.append(int(metrics["durationMs"]))
            counts.append(int(metrics["sampleCount"]))
            if float(metrics["maxFrameIntervalMs"]) > CAPTURE_MAX_FRAME_INTERVAL_MS:
                camera_errors.append(
                    f"frame interval {metrics['maxFrameIntervalMs']} ms exceeds {CAPTURE_MAX_FRAME_INTERVAL_MS:.0f} ms"
                )
            nominal_fps = float(metrics["nominalFps"])
            average_fps = float(metrics["averageFps"])
            if nominal_fps <= 0 or average_fps < nominal_fps * 0.95:
                camera_errors.append(
                    f"average frame rate {average_fps:.3f} is below 95% of {nominal_fps:.3f}"
                )
            if abs(int(metrics["durationMs"]) - expected_duration_ms) > 2500:
                camera_errors.append(
                    f"video duration {metrics['durationMs']} ms differs from capture duration {expected_duration_ms} ms"
                )
        except (OSError, ValueError, struct.error) as error:
            metrics = {"inspectionError": str(error)}
            camera_errors.append(str(error))
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            log_text = ""
        protocol_error = CAPTURE_ERROR_PATTERN.search(log_text)
        if protocol_error is not None:
            camera_errors.append(f"recorder reported: {protocol_error.group(0)}")
        if camera_errors:
            errors.extend(f"{camera_id}: {message}" for message in camera_errors)
        cameras[camera_id] = {**metrics, "valid": not camera_errors, "errors": camera_errors}

    if durations and max(durations) - min(durations) > 250:
        errors.append(f"camera duration spread is {max(durations) - min(durations)} ms")
    if counts and max(counts) - min(counts) > max(12, round(max(counts) * 0.01)):
        errors.append(f"camera sample-count spread is {max(counts) - min(counts)} frames")
    return {
        "valid": not errors,
        "policy": {
            "maxFrameIntervalMs": CAPTURE_MAX_FRAME_INTERVAL_MS,
            "minimumAverageFrameRateRatio": 0.95,
            "maximumDurationSpreadMs": 250,
        },
        "cameras": cameras,
        "errors": errors,
    }


class CaptureProcess:
    def __init__(self, name: str, process: subprocess.Popen[bytes], files: list[BinaryIO]) -> None:
        self.name = name
        self.process = process
        self.files = files

    def close_files(self) -> None:
        for file in self.files:
            try:
                file.close()
            except OSError:
                pass


class StorageManager:
    USB_LABEL = "SYNCAP"
    USB_MOUNT = Path("/media/syncap-usb")
    MIN_FREE_BYTES = 512 * 1024 * 1024
    ESTIMATED_BYTES_PER_SECOND = 2_250_000

    def __init__(self, internal_dir: Path, config_path: Path) -> None:
        self.internal_dir = internal_dir
        self.config_path = config_path
        self.lock = threading.RLock()
        self.target = "internal"
        self.internal_dir.mkdir(parents=True, exist_ok=True)
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            if config.get("target") in {"internal", "usb"}:
                self.target = str(config["target"])
        except (OSError, json.JSONDecodeError):
            pass

    @staticmethod
    def _mounts() -> dict[str, Path]:
        mounts: dict[str, Path] = {}
        try:
            lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
        except OSError:
            return mounts
        for line in lines:
            fields = line.split()
            if len(fields) >= 2:
                mounts[fields[0]] = Path(fields[1].replace("\\040", " "))
        return mounts

    @staticmethod
    def _blkid_value(device: str, field: str) -> str | None:
        result = subprocess.run(
            ["/usr/sbin/blkid", "-s", field, "-o", "value", device],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        value = result.stdout.strip()
        return value or None

    def _usb_device(self) -> str | None:
        result = subprocess.run(
            ["/usr/sbin/blkid", "-L", self.USB_LABEL],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        device = result.stdout.strip()
        match = re.fullmatch(r"/dev/(sd[a-z])[0-9]+", device)
        if match is None:
            return None
        try:
            removable = Path(f"/sys/class/block/{match.group(1)}/removable").read_text(encoding="ascii").strip()
        except OSError:
            return None
        if removable != "1" or self._blkid_value(device, "TYPE") != "exfat":
            return None
        return device

    def _usb_info(self, mount: bool) -> dict[str, object]:
        device = self._usb_device()
        if device is None:
            return {"available": False, "label": self.USB_LABEL}
        mount_path = self._mounts().get(device)
        if mount_path is None and mount:
            self.USB_MOUNT.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["mount", "-t", "exfat", "-o", "noatime,umask=0022", device, str(self.USB_MOUNT)],
                timeout=8,
                check=True,
            )
            mount_path = self.USB_MOUNT
        if mount_path is None:
            return {
                "available": True,
                "mounted": False,
                "label": self.USB_LABEL,
                "uuid": self._blkid_value(device, "UUID"),
            }
        root = mount_path / "SynCap"
        (root / "sessions").mkdir(parents=True, exist_ok=True)
        usage = shutil.disk_usage(mount_path)
        return {
            "available": True,
            "mounted": True,
            "label": self.USB_LABEL,
            "uuid": self._blkid_value(device, "UUID"),
            "filesystem": "exfat",
            "mountPoint": str(mount_path),
            "root": str(root),
            "totalBytes": usage.total,
            "freeBytes": usage.free,
        }

    def _persist_target(self) -> None:
        atomic_json(self.config_path, {"schema": "syncap.storage/1.0", "target": self.target})

    @classmethod
    def _target_status(
        cls,
        payload: dict[str, object],
        *,
        require_available: bool = False,
        require_mounted: bool = False,
    ) -> dict[str, object]:
        result = dict(payload)
        reason: str | None = None
        if require_available and not result.get("available"):
            reason = "not_available"
        elif require_mounted and not result.get("mounted"):
            reason = "not_mounted"
        elif int(result.get("freeBytes", 0)) < cls.MIN_FREE_BYTES:
            reason = "insufficient_free_space"
        result["canCapture"] = reason is None
        result["reason"] = reason
        return result

    @classmethod
    def _require_free_space(cls, free_bytes: object, description: str) -> None:
        if int(free_bytes) < cls.MIN_FREE_BYTES:
            raise RuntimeError(f"{description} has less than 512 MiB free")

    def configure(self, target: object) -> dict[str, object]:
        if target not in {"internal", "usb"}:
            raise ValueError("storage target must be internal or usb")
        with self.lock:
            if target == "usb":
                usb = self._usb_info(mount=True)
                if not usb.get("mounted"):
                    raise RuntimeError("SYNCAP USB drive is not available")
                self._require_free_space(usb.get("freeBytes", 0), "SYNCAP USB drive")
                root = Path(str(usb["root"]))
                atomic_json(root / "device.json", {
                    "schema": "syncap.offline-volume/1.0",
                    "label": self.USB_LABEL,
                    "createdAt": utc_now(),
                    "sessionsPath": "SynCap/sessions",
                })
            else:
                usage = shutil.disk_usage(self.internal_dir)
                self._require_free_space(usage.free, "internal storage")
            self.target = str(target)
            self._persist_target()
            return self.status()

    def capture_root(self) -> tuple[Path, dict[str, object]]:
        with self.lock:
            if self.target == "internal":
                usage = shutil.disk_usage(self.internal_dir)
                self._require_free_space(usage.free, "internal storage")
                return self.internal_dir, {
                    "target": "internal",
                    "freeBytesAtStart": usage.free,
                }
            usb = self._usb_info(mount=True)
            if not usb.get("mounted"):
                raise RuntimeError("SYNCAP USB drive is not mounted")
            self._require_free_space(usb.get("freeBytes", 0), "SYNCAP USB drive")
            return Path(str(usb["root"])), {
                "target": "usb",
                "volumeLabel": usb.get("label"),
                "volumeUuid": usb.get("uuid"),
                "filesystem": usb.get("filesystem"),
                "freeBytesAtStart": usb.get("freeBytes"),
            }

    def session_roots(self) -> list[Path]:
        roots = [self.internal_dir / "sessions"]
        with self.lock:
            usb = self._usb_info(mount=self.target == "usb")
        if usb.get("mounted"):
            roots.append(Path(str(usb["root"])) / "sessions")
        return roots

    def status(self) -> dict[str, object]:
        with self.lock:
            internal_usage = shutil.disk_usage(self.internal_dir)
            internal = self._target_status({
                "totalBytes": internal_usage.total,
                "freeBytes": internal_usage.free,
            })
            usb = self._target_status(
                self._usb_info(mount=self.target == "usb"),
                require_available=True,
                require_mounted=True,
            )
            usb.pop("root", None)
            return {
                "target": self.target,
                "capturePolicy": {
                    "minimumFreeBytes": self.MIN_FREE_BYTES,
                    "estimatedBytesPerSecond": self.ESTIMATED_BYTES_PER_SECOND,
                },
                "internal": internal,
                "usb": usb,
            }

    def eject(self) -> dict[str, object]:
        with self.lock:
            usb = self._usb_info(mount=False)
            mount_point = usb.get("mountPoint")
            os.sync()
            if mount_point:
                subprocess.run(["umount", str(mount_point)], timeout=10, check=True)
            self.target = "internal"
            self._persist_target()
            return {"state": "ejected", "target": "internal", "label": self.USB_LABEL}


class CaptureManager:
    def __init__(self, storage: StorageManager, device_ip: str, telemetry: AdapterState) -> None:
        self.storage = storage
        self.sessions_dir = storage.internal_dir / "sessions"
        self.device_ip = device_ip
        self.telemetry = telemetry
        self.lock = threading.RLock()
        self.active: dict[str, object] | None = None
        self.active_session_path: Path | None = None
        self.processes: list[CaptureProcess] = []
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self._recover_interrupted_sessions()

    def _recover_interrupted_sessions(self) -> None:
        for sessions_dir in self.storage.session_roots():
            for manifest_path in sessions_dir.glob("ses_*/session.json"):
                self._recover_manifest(manifest_path)

    @staticmethod
    def _recover_manifest(manifest_path: Path) -> None:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if manifest.get("state") in {"starting", "recording", "finalizing"}:
            manifest["state"] = "failed"
            manifest["failureReason"] = "service_restarted_during_capture"
            manifest["completedAt"] = utc_now()
            atomic_json(manifest_path, manifest)

    def _session_path(self, session_id: str) -> Path:
        if SESSION_ID.fullmatch(session_id) is None:
            raise KeyError("unknown session")
        for sessions_dir in self.storage.session_roots():
            path = sessions_dir / session_id
            if path.is_dir():
                return path
        raise KeyError("unknown session")

    def _write_active_manifest(self) -> None:
        if self.active is None:
            return
        if self.active_session_path is None:
            raise RuntimeError("active session path is unavailable")
        atomic_json(self.active_session_path / "session.json", self.active)

    def start(self, name: object) -> dict[str, object]:
        capture_name = safe_capture_name(name)
        with self.lock:
            if self.active is not None:
                raise RuntimeError("a capture is already recording")
            unavailable = [port for port in (554, 555, 556, 557) if not self._port_open(port)]
            if unavailable:
                raise RuntimeError(f"camera streams unavailable: {unavailable}")
            deadline = time.monotonic() + 5.0
            relay_unavailable = list(range(4))
            while relay_unavailable and time.monotonic() < deadline:
                relay_unavailable = [
                    index for index in range(4)
                    if not self._rtsp_path_ready(f"cam{index}")
                ]
                if relay_unavailable:
                    time.sleep(0.1)
            if relay_unavailable:
                raise RuntimeError(f"capture relay unavailable: {relay_unavailable}")

            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            suffix = secrets.token_hex(3)
            session_id = f"ses_{stamp}_{suffix}"
            capture_id = f"cap_{stamp}_{suffix}"
            capture_root, storage_descriptor = self.storage.capture_root()
            session_path = capture_root / "sessions" / session_id
            session_path.mkdir(mode=0o750)
            self.active_session_path = session_path
            requested_monotonic_ns = time.monotonic_ns()
            requested_at = utc_now()
            self.active = {
                "schema": "syncap.session/1.0",
                "id": session_id,
                "captureId": capture_id,
                "name": capture_name,
                "state": "starting",
                "createdAt": requested_at,
                "captureRequestedAt": requested_at,
                "captureRequestedMonotonicNs": str(requested_monotonic_ns),
                "startedAt": requested_at,
                "startedMonotonicNs": str(requested_monotonic_ns),
                "durationMs": None,
                "sources": ["cam0", "cam1", "cam2", "cam3", "imu0"],
                "video": {
                    "container": "fragmented-mp4",
                    "codec": "h264",
                    "copyOnly": True,
                    "source": "persistent_rtsp_relay",
                },
                "imu": {
                    "format": "syncap-imu-text/1.0",
                    "sampleRateHz": IMU_SAMPLE_RATE_HZ,
                    "timestampDomain": "monotonic_raw",
                },
                "clockAtStart": self.telemetry.capture_clock_status(),
                "storage": storage_descriptor,
                "files": [],
            }
            try:
                self._write_active_manifest()
                self.processes = self._spawn_recorders(session_path)
                self._wait_for_recorders_ready(session_path)
                ready_monotonic_ns = time.monotonic_ns()
                self.active["state"] = "recording"
                self.active["startedAt"] = utc_now()
                self.active["startedMonotonicNs"] = str(ready_monotonic_ns)
                self.active["recordingReadyMonotonicNs"] = str(ready_monotonic_ns)
                self._write_active_manifest()
            except Exception as error:
                self._stop_processes()
                if self.active is not None:
                    self.active["state"] = "failed"
                    self.active["failureReason"] = str(error)
                    self.active["completedAt"] = utc_now()
                    try:
                        self._write_active_manifest()
                    except OSError:
                        pass
                self.active = None
                self.active_session_path = None
                raise

            return {
                "captureId": capture_id,
                "sessionId": session_id,
                "state": "recording",
                "startedAtDeviceTimeNs": str(ready_monotonic_ns),
                "storage": storage_descriptor,
            }

    def _spawn_recorders(self, session_path: Path) -> list[CaptureProcess]:
        processes: list[CaptureProcess] = []

        imu_path = session_path / "imu.log"
        imu_output = imu_path.open("wb")
        imu_stderr = (session_path / "imu.stderr.log").open("wb")
        imu_process = subprocess.Popen(
            IMU_RECORD_COMMAND,
            cwd="/root/demo",
            stdout=imu_output,
            stderr=imu_stderr,
            start_new_session=True,
        )
        processes.append(CaptureProcess("imu0", imu_process, [imu_output, imu_stderr]))

        for index in range(4):
            stderr = (session_path / f"cam{index}.stderr.log").open("wb")
            output = session_path / f"cam{index}.mp4"
            command = [
                "/usr/bin/ffmpeg",
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-rtsp_transport",
                "tcp",
                "-i",
                f"rtsp://127.0.0.1:{CAPTURE_RELAY_PORT}/cam{index}",
                "-map",
                "0:v:0",
                "-c",
                "copy",
                "-movflags",
                "frag_keyframe+empty_moov+default_base_moof",
                "-f",
                "mp4",
                str(output),
            ]
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=stderr,
                start_new_session=True,
            )
            processes.append(CaptureProcess(f"cam{index}", process, [stderr]))
        return processes

    def _wait_for_recorders_ready(self, session_path: Path) -> None:
        deadline = time.monotonic() + CAPTURE_STARTUP_TIMEOUT_SECONDS
        while True:
            failed = [item.name for item in self.processes if item.process.poll() is not None]
            if failed:
                names = ", ".join(failed)
                raise RuntimeError(f"capture startup failed: recorder exited before first sample: {names}")

            imu_ready = self._imu_sample_ready(session_path / "imu.log")
            pending_cameras = [
                f"cam{index}"
                for index in range(4)
                if not fragmented_mp4_has_media_fragment(session_path / f"cam{index}.mp4")
            ]
            if imu_ready and not pending_cameras:
                return

            if time.monotonic() >= deadline:
                pending = ([] if imu_ready else ["imu0 ts_ns sample"]) + [
                    f"{camera_id} media fragment" for camera_id in pending_cameras
                ]
                raise RuntimeError(
                    "capture startup timed out after "
                    f"{CAPTURE_STARTUP_TIMEOUT_SECONDS:g} seconds waiting for: {', '.join(pending)}"
                )
            time.sleep(CAPTURE_STARTUP_POLL_SECONDS)

    @staticmethod
    def _imu_sample_ready(path: Path) -> bool:
        try:
            with path.open("rb") as source:
                return b"ts_ns=" in source.read(256 * 1024)
        except OSError:
            return False

    def stop(self, capture_id: str) -> dict[str, object]:
        with self.lock:
            if self.active is None or self.active.get("captureId") != capture_id:
                raise KeyError("capture is not active")
            stop_requested_ns = time.monotonic_ns()
            self.active["state"] = "finalizing"
            try:
                self._write_active_manifest()
            except OSError:
                pass
            self._stop_processes()

            try:
                self.active["durationMs"] = capture_duration_ms(self.active, stop_requested_ns)
                self.active["completedAt"] = utc_now()
                if self.active_session_path is None:
                    raise RuntimeError("active session path is unavailable")
                session_path = self.active_session_path
                validation = validate_capture_videos(session_path, int(self.active["durationMs"]))
                self.active["videoValidation"] = validation
                diagnostics = {
                    "schema": "syncap.capture-diagnostics/1.0",
                    "capturedAt": utc_now(),
                    "status": self.telemetry.status(),
                    "videoValidation": validation,
                }
                atomic_json(session_path / "diagnostics.json", diagnostics)

                files = []
                for path in sorted(session_path.iterdir()):
                    if path.name == "session.json" or not path.is_file() or path.stat().st_size == 0:
                        continue
                    if not (path.name.endswith(".mp4") or path.name in {"imu.log", "diagnostics.json"}):
                        continue
                    files.append({
                        "name": path.name,
                        "sizeBytes": path.stat().st_size,
                        "sha256": sha256_file(path),
                    })
                self.active["files"] = files
                self.active["sizeBytes"] = sum(int(item["sizeBytes"]) for item in files)
                if not validation["valid"]:
                    summary = "; ".join(str(error) for error in validation["errors"][:4])
                    raise RuntimeError(f"video integrity check failed: {summary}")
                self.active["state"] = "complete"
                self._write_active_manifest()
                os.sync()
                return {
                    "captureId": capture_id,
                    "sessionId": self.active["id"],
                    "state": "completed",
                    "session": self._session_summary(self.active),
                }
            except Exception as error:
                self.active["state"] = "failed"
                self.active["failureReason"] = f"capture_finalization_failed: {error}"
                self.active["completedAt"] = utc_now()
                try:
                    self._write_active_manifest()
                except OSError:
                    pass
                raise RuntimeError(str(self.active["failureReason"])) from error
            finally:
                self.active = None
                self.active_session_path = None

    def _stop_processes(self) -> None:
        for item in self.processes:
            if item.process.poll() is None:
                try:
                    os.killpg(item.process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 8
        for item in self.processes:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                item.process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(item.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    item.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(item.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            item.close_files()
        self.processes = []

    @staticmethod
    def _port_open(port: int) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.3):
                return True
        except OSError:
            return False

    @staticmethod
    def _rtsp_path_ready(path: str) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", CAPTURE_RELAY_PORT), timeout=0.5) as client:
                client.settimeout(0.5)
                request = (
                    f"DESCRIBE rtsp://127.0.0.1:{CAPTURE_RELAY_PORT}/{path} RTSP/1.0\r\n"
                    "CSeq: 1\r\n"
                    "Accept: application/sdp\r\n\r\n"
                )
                client.sendall(request.encode("ascii"))
                response = b""
                while b"\r\n\r\n" not in response and len(response) < 8192:
                    block = client.recv(2048)
                    if not block:
                        break
                    response += block
                return response.startswith(b"RTSP/1.0 200 ")
        except OSError:
            return False

    @staticmethod
    def _session_summary(manifest: dict[str, object]) -> dict[str, object]:
        return {
            "id": manifest.get("id"),
            "name": manifest.get("name"),
            "createdAt": manifest.get("createdAt"),
            "durationMs": manifest.get("durationMs"),
            "sizeBytes": manifest.get("sizeBytes", 0),
            "status": manifest.get("state"),
            "fileCount": len(manifest.get("files", [])),
            "storage": manifest.get("storage"),
        }

    def list_sessions(self) -> list[dict[str, object]]:
        sessions = []
        seen: set[str] = set()
        for sessions_dir in self.storage.session_roots():
            for manifest_path in sessions_dir.glob("ses_*/session.json"):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                session_id = str(manifest.get("id", ""))
                if session_id in seen:
                    continue
                seen.add(session_id)
                sessions.append(self._session_summary(manifest))
        sessions.sort(key=lambda item: str(item.get("createdAt", "")), reverse=True)
        return sessions

    def manifest(self, session_id: str) -> dict[str, object]:
        path = self._session_path(session_id) / "session.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def prepare_export(self, session_id: str) -> dict[str, object]:
        manifest = self.manifest(session_id)
        if manifest.get("state") != "complete":
            raise RuntimeError("session is not complete")
        files = []
        for item in manifest.get("files", []):
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            files.append({
                **item,
                "url": f"/v1/sessions/{session_id}/files/{name}",
            })
        return {
            "sessionId": session_id,
            "rangeSupported": True,
            "totalBytes": sum(int(item.get("sizeBytes", 0)) for item in files),
            "manifestUrl": f"/v1/sessions/{session_id}/manifest",
            "files": files,
        }

    def export_file(self, session_id: str, name: str) -> Path:
        session_path = self._session_path(session_id).resolve()
        decoded = unquote(name)
        if Path(decoded).name != decoded:
            raise KeyError("unknown file")
        manifest = self.manifest(session_id)
        allowed = {str(item.get("name")) for item in manifest.get("files", []) if isinstance(item, dict)}
        if decoded not in allowed:
            raise KeyError("unknown file")
        path = (session_path / decoded).resolve()
        if path.parent != session_path or not path.is_file():
            raise KeyError("unknown file")
        return path

    def capture_status(self) -> dict[str, object]:
        with self.lock:
            if self.active is None:
                return {"state": "idle"}
            return {
                "state": self.active.get("state"),
                "captureId": self.active.get("captureId"),
                "sessionId": self.active.get("id"),
                "name": self.active.get("name"),
                "startedAt": self.active.get("startedAt"),
                "storage": self.active.get("storage"),
            }


class WifiManager:
    MAX_SCAN_NETWORKS = 16
    SCAN_SETTLE_SECONDS = 2.0
    SCAN_POLL_ATTEMPTS = 6
    SCAN_POLL_SECONDS = 0.5

    def __init__(self, config_path: Path = Path("/userdata/wpa_supplicant.conf")) -> None:
        self.config_path = config_path
        self.lock = threading.Lock()
        self.last_status: dict[str, object] = {"state": "idle"}

    @staticmethod
    def _quoted(value: str) -> str:
        if any(character in value for character in "\r\n\0"):
            raise ValueError("Wi-Fi value contains an invalid character")
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def configure(self, ssid: object, password: object, security: object, connect_now: bool) -> dict[str, object]:
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32:
            raise ValueError("ssid must contain 1 to 32 UTF-8 bytes")
        if not isinstance(password, str):
            raise ValueError("password must be a string")
        security_name = str(security or "wpa2-psk")
        if security_name != "open" and not 8 <= len(password) <= 63:
            raise ValueError("password must contain 8 to 63 characters")

        with self.lock, wifi_configuration_operation():
            lines = ["ctrl_interface=/var/run/wpa_supplicant", "update_config=1", "country=CN", "network={"]
            lines.append(f'    ssid="{self._quoted(ssid)}"')
            lines.append("    scan_ssid=1")
            if security_name == "open":
                lines.append("    key_mgmt=NONE")
            else:
                lines.append(f'    psk="{self._quoted(password)}"')
                lines.append("    key_mgmt=WPA-PSK")
            lines.append("}")
            temporary = self.config_path.with_suffix(".tmp")
            temporary.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.config_path)
            self.last_status = {"state": "staged", "ssid": ssid}
            if not connect_now:
                return dict(self.last_status)

            self.last_status = {"state": "connecting", "ssid": ssid}
            subprocess.run(["/etc/init.d/wifi_init.sh", "sta"], timeout=15, check=False)
            connected = False
            for _ in range(20):
                status = subprocess.run(
                    ["/usr/sbin/wpa_cli", "-i", "wlan0", "status"],
                    capture_output=True,
                    text=True,
                    timeout=2,
                    check=False,
                ).stdout
                if "wpa_state=COMPLETED" in status:
                    connected = True
                    break
                time.sleep(1)
            if not connected:
                self.last_status = {"state": "failed", "ssid": ssid, "error": "wifi_association_timeout"}
                return dict(self.last_status)

            subprocess.run(["udhcpc", "-i", "wlan0", "-q", "-n", "-t", "5"], timeout=20, check=False)
            address_output = subprocess.run(
                ["/usr/sbin/ip", "-4", "-o", "addr", "show", "dev", "wlan0"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
            address_match = re.search(r"\binet\s+([0-9.]+)/", address_output)
            self.last_status = {
                "state": "connected" if address_match else "failed",
                "ssid": ssid,
                "ipAddress": address_match.group(1) if address_match else None,
                "error": None if address_match else "dhcp_failed",
            }
            return dict(self.last_status)

    @staticmethod
    def _scan_security(flags: str) -> str:
        flags = flags.upper()
        if "OWE" in flags:
            return "owe"
        if "PSK" in flags:
            return "wpa2-psk" if "WPA2" in flags or "RSN" in flags or "SAE" in flags else "wpa-psk"
        if "SAE" in flags:
            return "wpa3-sae"
        if "EAP" in flags:
            return "enterprise"
        if "WEP" in flags:
            return "wep"
        return "open"

    @staticmethod
    def _decode_scan_ssid(value: str) -> str | None:
        decoded = bytearray()
        index = 0
        while index < len(value):
            if index + 3 < len(value) and value[index:index + 2] == "\\x":
                try:
                    decoded.append(int(value[index + 2:index + 4], 16))
                except ValueError:
                    decoded.extend(value[index].encode("utf-8"))
                    index += 1
                    continue
                index += 4
                continue
            if index + 1 < len(value) and value[index:index + 2] == "\\\\":
                decoded.append(ord("\\"))
                index += 2
                continue
            decoded.extend(value[index].encode("utf-8"))
            index += 1
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError:
            return None

    @classmethod
    def _parse_scan_results(cls, output: str) -> list[dict[str, object]]:
        by_ssid: dict[str, dict[str, object]] = {}
        for line in output.splitlines():
            columns = line.split("\t", 4)
            if len(columns) != 5 or columns[0].lower() == "bssid":
                continue
            _, frequency_text, rssi_text, flags, encoded_ssid = columns
            ssid = cls._decode_scan_ssid(encoded_ssid)
            if ssid is None or not ssid.strip() or len(ssid.encode("utf-8")) > 32:
                continue
            try:
                frequency = int(frequency_text)
                rssi = int(rssi_text)
            except ValueError:
                continue
            security = cls._scan_security(flags)
            network: dict[str, object] = {
                "ssid": ssid,
                "rssi": rssi,
                "signal": max(0, min(100, 2 * (rssi + 100))),
                "security": security,
                "secure": security != "open",
                "frequency": frequency,
            }
            previous = by_ssid.get(ssid)
            if previous is None or rssi > int(previous["rssi"]):
                by_ssid[ssid] = network
        return sorted(by_ssid.values(), key=lambda network: (-int(network["rssi"]), str(network["ssid"]).casefold()))

    def scan(self) -> dict[str, object]:
        with self.lock:
            try:
                requested = subprocess.run(
                    ["/usr/sbin/wpa_cli", "-i", "wlan0", "scan"],
                    capture_output=True,
                    text=True,
                    timeout=3,
                    check=False,
                )
            except OSError as error:
                raise RuntimeError("wifi_scan_unavailable") from error
            except subprocess.TimeoutExpired as error:
                raise RuntimeError("wifi_scan_timeout") from error
            if requested.returncode != 0 or "OK" not in requested.stdout.splitlines():
                raise RuntimeError("wifi_scan_failed")

            time.sleep(self.SCAN_SETTLE_SECONDS)
            networks: list[dict[str, object]] = []
            received_results = False
            for attempt in range(self.SCAN_POLL_ATTEMPTS):
                try:
                    result = subprocess.run(
                        ["/usr/sbin/wpa_cli", "-i", "wlan0", "scan_results"],
                        capture_output=True,
                        text=True,
                        timeout=3,
                        check=False,
                    )
                except OSError as error:
                    raise RuntimeError("wifi_scan_unavailable") from error
                except subprocess.TimeoutExpired as error:
                    raise RuntimeError("wifi_scan_timeout") from error
                has_scan_header = any(line.lower().startswith("bssid") for line in result.stdout.splitlines())
                if result.returncode == 0 and has_scan_header:
                    received_results = True
                    networks = self._parse_scan_results(result.stdout)
                    if networks:
                        break
                if attempt + 1 < self.SCAN_POLL_ATTEMPTS:
                    time.sleep(self.SCAN_POLL_SECONDS)
            if not received_results:
                raise RuntimeError("wifi_scan_results_failed")

            return {
                "state": "completed",
                "networks": networks[:self.MAX_SCAN_NETWORKS],
                "scannedAt": int(time.time() * 1000),
                "truncated": len(networks) > self.MAX_SCAN_NETWORKS,
            }

    @staticmethod
    def _runtime_status(wpa_output: str, address_output: str) -> dict[str, object] | None:
        values: dict[str, str] = {}
        for line in wpa_output.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        wpa_state = values.get("wpa_state")
        if wpa_state is None:
            return None

        address_match = re.search(r"\binet\s+([0-9.]+)/", address_output)
        ip_address = values.get("ip_address") or (address_match.group(1) if address_match else None)
        payload: dict[str, object] = {
            "state": "idle",
            "ssid": values.get("ssid"),
            "ipAddress": ip_address,
        }
        if wpa_state == "COMPLETED":
            payload["state"] = "connected" if ip_address else "connecting"
        elif wpa_state in {"SCANNING", "ASSOCIATING", "ASSOCIATED", "4WAY_HANDSHAKE", "GROUP_HANDSHAKE"}:
            payload["state"] = "connecting"
        elif wpa_state in {"DISCONNECTED", "INACTIVE", "INTERFACE_DISABLED"}:
            payload["state"] = "disconnected"
        else:
            payload["state"] = wpa_state.lower()
        return payload

    def status(self) -> dict[str, object]:
        try:
            wpa_output = subprocess.run(
                ["/usr/sbin/wpa_cli", "-i", "wlan0", "status"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
            address_output = subprocess.run(
                ["/usr/sbin/ip", "-4", "-o", "addr", "show", "dev", "wlan0"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            ).stdout
        except (OSError, subprocess.TimeoutExpired):
            return dict(self.last_status)
        runtime = self._runtime_status(wpa_output, address_output)
        if runtime is not None:
            self.last_status = runtime
        return dict(self.last_status)


class CameraOrientationManager:
    ROTATIONS = (0, 90, 180, 270)

    def __init__(
        self,
        config_path: Path = CAMERA_CONFIGURATION_PATH,
        restart_callback=None,
    ) -> None:
        self.config_path = config_path
        self.restart_callback = restart_callback or self._restart_camera_supervisor
        self.lock = threading.Lock()

    def _rotation(self) -> int:
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            value = payload.get("rotationDegrees")
            if type(value) is int and value in self.ROTATIONS:
                return value
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
        return 0

    @staticmethod
    def _payload(rotation: int) -> dict[str, object]:
        portrait = rotation in {90, 270}
        return {
            "rotationDegrees": rotation,
            "frameRate": 60,
            "outputWidth": 1088 if portrait else 1280,
            "outputHeight": 1280 if portrait else 1088,
            "supportedRotations": [0, 90, 180, 270],
            "appliesTo": ["preview", "calibration", "capture"],
        }

    def status(self) -> dict[str, object]:
        with self.lock:
            return self._payload(self._rotation())

    def configure(self, value: object) -> dict[str, object]:
        if type(value) is not int or value not in self.ROTATIONS:
            raise ValueError("rotationDegrees must be 0, 90, 180, or 270")
        with self.lock:
            current = self._rotation()
            if value == current:
                return {**self._payload(value), "state": "active"}
            atomic_json(self.config_path, {"rotationDegrees": value})
            os.chmod(self.config_path, 0o600)
            try:
                self.restart_callback()
            except Exception:
                atomic_json(self.config_path, {"rotationDegrees": current})
                os.chmod(self.config_path, 0o600)
                raise
            return {**self._payload(value), "state": "restarting"}

    @staticmethod
    def _restart_camera_supervisor() -> None:
        try:
            pid = int(CAMERA_SUPERVISOR_PID_PATH.read_text(encoding="ascii").strip())
            command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
        except (OSError, ValueError) as error:
            raise RuntimeError("camera supervisor is not running") from error
        if b"camera_supervisor.py" not in command:
            raise RuntimeError("camera supervisor identity check failed")
        try:
            os.kill(pid, signal.SIGHUP)
        except OSError as error:
            raise RuntimeError("camera supervisor reload failed") from error


class ServiceState:
    def __init__(self, device_ip: str, camera_log: Path, data_dir: Path, claim_code_path: Path) -> None:
        self.telemetry = AdapterState(device_ip, camera_log)
        self.storage = StorageManager(data_dir, data_dir / "storage.json")
        self.capture = CaptureManager(self.storage, device_ip, self.telemetry)
        self.wifi = WifiManager()
        self.camera_orientation = CameraOrientationManager(data_dir / "camera.json")
        self.claim_code_path = claim_code_path
        ensure_claim_code(claim_code_path)

    def valid_claim(self, value: str | None) -> bool:
        try:
            expected = self.claim_code_path.read_text(encoding="ascii").strip()
        except OSError:
            return False
        return value is not None and secrets.compare_digest(value, expected)

    def offline_status(self) -> dict[str, object]:
        storage = self.storage.status()
        usb = storage.get("usb", {})
        sessions = self.capture.list_sessions()
        return {
            "state": "ready",
            "storage": {
                "target": storage.get("target"),
                "usbAvailable": usb.get("available") if isinstance(usb, dict) else False,
                "usbMounted": usb.get("mounted") if isinstance(usb, dict) else False,
                "usbLabel": usb.get("label") if isinstance(usb, dict) else None,
                "usbUuid": usb.get("uuid") if isinstance(usb, dict) else None,
                "usbTotalBytes": usb.get("totalBytes") if isinstance(usb, dict) else None,
                "usbFreeBytes": usb.get("freeBytes") if isinstance(usb, dict) else None,
            },
            "wifi": self.wifi.status(),
            "capture": self.capture.capture_status(),
            "sessionCount": len(sessions),
            "latestSession": sessions[0] if sessions else None,
        }


class Handler(BaseHTTPRequestHandler):
    state: ServiceState
    server_version = "SynCapDevice/1.0"

    def manifest_host(self) -> str:
        local_host = None
        try:
            local_host = normalize_endpoint_host(self.connection.getsockname()[0])
        except (AttributeError, IndexError, OSError, TypeError):
            pass
        if usable_endpoint_host(local_host):
            return str(local_host)

        request_host = normalize_endpoint_host(self.headers.get("Host"))
        if usable_endpoint_host(request_host):
            return str(request_host)

        route_host = routed_local_host(getattr(self, "client_address", None))
        if usable_endpoint_host(route_host):
            return str(route_host)

        configured_host = normalize_endpoint_host(getattr(self.state.telemetry, "device_ip", None))
        return str(configured_host) if configured_host is not None else "127.0.0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        try:
            if path == "/health":
                self.send_json({"ok": True, "service": "syncap-device", "version": "1.1"})
            elif path == "/v1/manifest":
                manifest = self.state.telemetry.manifest()
                apply_manifest_host(manifest, self.manifest_host())
                manifest["adapter"] = {"kind": "syncap-device-service", "security": "paired-ble"}
                manifest["capabilities"] = [
                    "camera.preview", "capture.device", "session.export", "sync.metrics",
                    "sensor.imu", "clock.sync", "exposure.phase", "network.wifi", "provisioning.ble",
                    "storage.usb", "control.ble.offline", "camera.orientation",
                ]
                if CALIBRATION_SNAPSHOT_READY.is_file():
                    manifest["capabilities"].append("calibration.raw_snapshot")
                self.send_json(manifest)
            elif path == "/v1/status":
                payload = self.state.telemetry.status()
                payload["adapter"] = "syncap-device-service"
                payload["capture"] = self.state.capture.capture_status()
                payload["wifi"] = self.state.wifi.status()
                payload["storage"] = self.state.storage.status()
                payload["cameraConfiguration"] = self.state.camera_orientation.status()
                self.send_json(payload)
            elif path == "/v1/offline/status":
                self.send_json(self.state.offline_status())
            elif path == "/v1/captures/current":
                self.send_json(self.state.capture.capture_status())
            elif path == "/v1/sessions":
                self.send_json({"sessions": self.state.capture.list_sessions()})
            elif path == "/v1/wifi/status":
                self.send_json(self.state.wifi.status())
            elif path == "/v1/wifi/scan":
                claim_code = self.headers.get("X-SynCap-Claim")
                if not self.state.valid_claim(claim_code):
                    self.send_json({"error": "claim_required"}, status=403)
                    return
                self.send_json(self.state.wifi.scan())
            elif path == "/v1/storage":
                self.send_json(self.state.storage.status())
            elif path == "/v1/camera/configuration":
                self.send_json(self.state.camera_orientation.status())
            elif match := SESSION_MANIFEST_PATH.fullmatch(path):
                self.send_json(self.state.capture.manifest(match.group("session_id")))
            elif match := SESSION_FILE_PATH.fullmatch(path):
                self.send_file(self.state.capture.export_file(match.group("session_id"), match.group("name")))
            elif match := CALIBRATION_SNAPSHOT_FILE_PATH.fullmatch(path):
                claim_code = self.headers.get("X-SynCap-Claim")
                if not self.state.valid_claim(claim_code):
                    self.send_json({"error": "claim_required"}, status=403)
                    return
                snapshot_file = (
                    CALIBRATION_SNAPSHOT_ROOT /
                    f"{match.group('snapshot_id')}_{match.group('camera_id')}.nv12"
                )
                if not snapshot_file.is_file():
                    raise KeyError("unknown calibration snapshot")
                self.send_file(snapshot_file)
            else:
                self.send_json({"error": "not_found"}, status=404)
        except KeyError as error:
            self.send_json({"error": "not_found", "message": str(error)}, status=404)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.send_json({"error": "internal_error", "message": str(error)}, status=500)

    def do_HEAD(self) -> None:
        path = urlparse(self.path).path
        try:
            match = SESSION_FILE_PATH.fullmatch(path)
            if match is None:
                self.send_error(404)
                return
            self.send_file(self.state.capture.export_file(match.group("session_id"), match.group("name")), head_only=True)
        except KeyError:
            self.send_error(404)
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        try:
            body = self.read_json()
            if path == "/v1/captures/start":
                self.send_json(self.state.capture.start(body.get("name")))
            elif match := CAPTURE_STOP_PATH.fullmatch(path):
                self.send_json(self.state.capture.stop(match.group("capture_id")))
            elif match := SESSION_EXPORT_PATH.fullmatch(path):
                self.send_json(self.state.capture.prepare_export(match.group("session_id")))
            elif path == "/v1/storage/configure":
                claim_code = self.headers.get("X-SynCap-Claim") or str(body.get("claimCode", ""))
                if not self.state.valid_claim(claim_code):
                    self.send_json({"error": "claim_required"}, status=403)
                    return
                if self.state.capture.capture_status().get("state") != "idle":
                    raise RuntimeError("storage cannot change during capture")
                self.send_json(self.state.storage.configure(body.get("target")))
            elif path == "/v1/storage/eject":
                claim_code = self.headers.get("X-SynCap-Claim") or str(body.get("claimCode", ""))
                if not self.state.valid_claim(claim_code):
                    self.send_json({"error": "claim_required"}, status=403)
                    return
                if self.state.capture.capture_status().get("state") != "idle":
                    raise RuntimeError("USB drive cannot be ejected during capture")
                self.send_json(self.state.storage.eject())
            elif path == "/v1/wifi/configure":
                claim_code = self.headers.get("X-SynCap-Claim") or str(body.get("claimCode", ""))
                if not self.state.valid_claim(claim_code):
                    self.send_json({"error": "claim_required"}, status=403)
                    return
                result = self.state.wifi.configure(
                    body.get("ssid"), body.get("password", ""), body.get("security", "wpa2-psk"),
                    bool(body.get("connectNow", True)),
                )
                self.send_json(result, status=200 if result.get("state") != "failed" else 409)
            elif path == "/v1/calibration/snapshots":
                claim_code = self.headers.get("X-SynCap-Claim") or str(body.get("claimCode", ""))
                if not self.state.valid_claim(claim_code):
                    self.send_json({"error": "claim_required"}, status=403)
                    return
                camera_ids = body.get("cameraIds")
                if not isinstance(camera_ids, list) or len(camera_ids) != 2:
                    raise ValueError("cameraIds must contain two cameras")
                snapshot = capture_synchronized_calibration_pair((str(camera_ids[0]), str(camera_ids[1])))
                snapshot["cameraConfiguration"] = self.state.camera_orientation.status()
                self.send_json(snapshot)
            elif path == "/v1/camera/configuration":
                claim_code = self.headers.get("X-SynCap-Claim") or str(body.get("claimCode", ""))
                if not self.state.valid_claim(claim_code):
                    self.send_json({"error": "claim_required"}, status=403)
                    return
                if self.state.capture.capture_status().get("state") != "idle":
                    raise RuntimeError("image orientation cannot change during capture")
                self.send_json(self.state.camera_orientation.configure(body.get("rotationDegrees")))
            else:
                self.send_json({"error": "not_found"}, status=404)
        except ValueError as error:
            self.send_json({"error": "invalid_request", "message": str(error)}, status=400)
        except KeyError as error:
            self.send_json({"error": "not_found", "message": str(error)}, status=404)
        except RuntimeError as error:
            self.send_json({"error": "conflict", "message": str(error)}, status=409)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            self.send_json({"error": "internal_error", "message": str(error)}, status=500)

    def read_json(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise ValueError("invalid content length") from error
        if length < 0 or length > 16 * 1024:
            raise ValueError("request body is too large")
        if length == 0:
            return {}
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def send_json(self, payload: object, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path, head_only: bool = False) -> None:
        size = path.stat().st_size
        try:
            requested = parse_byte_range(self.headers.get("Range"), size)
        except ValueError:
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        start, end = requested if requested is not None else (0, size - 1)
        self.send_response(206 if requested is not None else 200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
        if requested is not None:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        remaining = end - start + 1
        with path.open("rb") as source:
            source.seek(start)
            while remaining:
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    break
                self.wfile.write(block)
                remaining -= len(block)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--device-ip", default="127.0.0.1")
    parser.add_argument("--camera-log", default="/tmp/cam_demo-live.log", type=Path)
    parser.add_argument("--data-dir", default="/userdata/syncap", type=Path)
    parser.add_argument("--claim-code", default="/etc/syncap/claim-code", type=Path)
    args = parser.parse_args()

    Handler.state = ServiceState(args.device_ip, args.camera_log, args.data_dir, args.claim_code)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"SynCap device service listening on {args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
