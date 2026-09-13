#!/usr/bin/env python3
"""Capture timestamped H.264 access units from the four RoboBaton RTSP streams."""

from __future__ import annotations

import argparse
import base64
import csv
import re
import socket
import struct
import threading
import time
from pathlib import Path


class RtspCapture:
    def __init__(
        self,
        url: str,
        output_dir: Path,
        channel: int,
        duration_s: float,
    ):
        match = re.fullmatch(r"rtsp://([^/:]+)(?::(\d+))?(/.*)", url)
        if match is None:
            raise ValueError(f"invalid RTSP URL: {url}")
        self.host = match.group(1)
        self.port = int(match.group(2) or 554)
        self.path = match.group(3)
        self.url = url
        self.output_dir = output_dir
        self.channel = channel
        self.duration_s = duration_s
        self.frames = 0
        self.error: Exception | None = None

    @staticmethod
    def _rtp_payload(packet: bytes) -> tuple[int, bool, bytes]:
        if len(packet) < 12 or packet[0] >> 6 != 2:
            raise ValueError("invalid RTP packet")
        cc = packet[0] & 0x0F
        offset = 12 + cc * 4
        if packet[0] & 0x10:
            if len(packet) < offset + 4:
                raise ValueError("truncated RTP extension")
            words = struct.unpack("!H", packet[offset + 2 : offset + 4])[0]
            offset += 4 + words * 4
        end = len(packet)
        if packet[0] & 0x20:
            end -= packet[-1]
        timestamp = struct.unpack("!I", packet[4:8])[0]
        return timestamp, bool(packet[1] & 0x80), packet[offset:end]

    @staticmethod
    def _append_payload(access_unit: bytearray, payload: bytes) -> tuple[bool, bool]:
        if not payload:
            return False, False
        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            access_unit.extend(b"\x00\x00\x00\x01")
            access_unit.extend(payload)
            return nal_type == 5, 1 <= nal_type <= 5
        if nal_type == 24:
            offset = 1
            contains_idr = False
            contains_vcl = False
            while offset + 2 <= len(payload):
                size = struct.unpack("!H", payload[offset : offset + 2])[0]
                offset += 2
                nal = payload[offset : offset + size]
                offset += size
                if len(nal) != size:
                    break
                access_unit.extend(b"\x00\x00\x00\x01")
                access_unit.extend(nal)
                contains_idr |= bool(nal and (nal[0] & 0x1F) == 5)
                contains_vcl |= bool(nal and 1 <= (nal[0] & 0x1F) <= 5)
            return contains_idr, contains_vcl
        if nal_type == 28 and len(payload) >= 2:
            fu_header = payload[1]
            reconstructed_type = fu_header & 0x1F
            is_start = bool(fu_header & 0x80)
            if is_start:
                access_unit.extend(b"\x00\x00\x00\x01")
                access_unit.append((payload[0] & 0xE0) | reconstructed_type)
            access_unit.extend(payload[2:])
            return (
                is_start and reconstructed_type == 5,
                is_start and 1 <= reconstructed_type <= 5,
            )
        return False, False

    def run(self) -> None:
        try:
            self._run()
        except Exception as exc:  # Preserve errors until all capture threads finish.
            self.error = exc

    def _run(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=5)
        sock.settimeout(5)
        buffer = bytearray()
        cseq = 0
        session: str | None = None

        def receive_exact(size: int) -> None:
            while len(buffer) < size:
                chunk = sock.recv(1 << 16)
                if not chunk:
                    raise ConnectionError("RTSP server closed the connection")
                buffer.extend(chunk)

        def request(method: str, url: str, extra: dict[str, str] | None = None) -> bytes:
            nonlocal cseq, session
            cseq += 1
            headers = {"CSeq": str(cseq), "User-Agent": "sync-measure/1.0"}
            if session is not None:
                headers["Session"] = session
            if extra:
                headers.update(extra)
            message = (
                f"{method} {url} RTSP/1.0\r\n"
                + "".join(f"{key}: {value}\r\n" for key, value in headers.items())
                + "\r\n"
            )
            sock.sendall(message.encode("ascii"))
            while b"\r\n\r\n" not in buffer:
                receive_exact(len(buffer) + 1)
            split = buffer.find(b"\r\n\r\n")
            header = bytes(buffer[:split])
            del buffer[: split + 4]
            status = header.splitlines()[0]
            if b" 200 " not in status:
                raise RuntimeError(status.decode("ascii", errors="replace"))
            length_match = re.search(br"Content-Length:\s*(\d+)", header, re.I)
            length = int(length_match.group(1)) if length_match else 0
            receive_exact(length)
            body = bytes(buffer[:length])
            del buffer[:length]
            session_match = re.search(br"Session:\s*([^;\r\n]+)", header, re.I)
            if session_match:
                session = session_match.group(1).decode("ascii")
            return body

        request("OPTIONS", self.url)
        sdp = request("DESCRIBE", self.url, {"Accept": "application/sdp"}).decode("ascii")
        controls = re.findall(r"^a=control:(.+)\r?$", sdp, re.M)
        if not controls:
            raise RuntimeError("SDP does not contain a media control URL")
        control = controls[-1].strip()
        track_url = control if control.startswith("rtsp://") else f"{self.url}/{control}"
        request("SETUP", track_url, {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"})
        request("PLAY", self.url, {"Range": "npt=0.000-"})

        parameter_sets: list[bytes] = []
        fmtp = re.search(r"sprop-parameter-sets=([^;\r\n]+)", sdp)
        if fmtp:
            parameter_sets = [base64.b64decode(value) for value in fmtp.group(1).split(",")]

        video_path = self.output_dir / f"cam{self.channel}.h264"
        csv_path = self.output_dir / f"cam{self.channel}_frames.csv"
        started = False
        access_unit = bytearray()
        access_unit_timestamp: int | None = None
        access_unit_has_idr = False
        access_unit_has_vcl = False
        deadline = time.monotonic() + self.duration_s
        with video_path.open("wb") as video, csv_path.open("w", newline="") as metadata:
            writer = csv.writer(metadata)
            writer.writerow(["frame_index", "rtp_timestamp", "board_timestamp_ns"])
            while time.monotonic() < deadline:
                receive_exact(4)
                if buffer[0] != 0x24:
                    marker = buffer.find(0x24)
                    if marker < 0:
                        buffer.clear()
                    else:
                        del buffer[:marker]
                    continue
                packet_channel = buffer[1]
                packet_size = struct.unpack("!H", buffer[2:4])[0]
                receive_exact(4 + packet_size)
                packet = bytes(buffer[4 : 4 + packet_size])
                del buffer[: 4 + packet_size]
                if packet_channel != 0:
                    continue
                timestamp, marker, payload = self._rtp_payload(packet)
                if access_unit_timestamp is None:
                    access_unit_timestamp = timestamp
                elif timestamp != access_unit_timestamp:
                    access_unit.clear()
                    access_unit_timestamp = timestamp
                    access_unit_has_idr = False
                    access_unit_has_vcl = False
                payload_has_idr, payload_has_vcl = self._append_payload(access_unit, payload)
                access_unit_has_idr |= payload_has_idr
                access_unit_has_vcl |= payload_has_vcl
                if not marker:
                    continue
                if not started and access_unit_has_idr:
                    for parameter_set in parameter_sets:
                        video.write(b"\x00\x00\x00\x01" + parameter_set)
                    started = True
                if started and access_unit and access_unit_has_vcl:
                    video.write(access_unit)
                    board_timestamp_ns = round(access_unit_timestamp * 1_000_000_000 / 90_000)
                    writer.writerow([self.frames, access_unit_timestamp, board_timestamp_ns])
                    self.frames += 1
                access_unit.clear()
                access_unit_timestamp = None
                access_unit_has_idr = False
                access_unit_has_vcl = False
        sock.close()

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.1.12")
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    captures = [
        RtspCapture(f"rtsp://{args.host}:{554 + channel}/PRR", args.output, channel, args.duration)
        for channel in range(4)
    ]
    threads = [threading.Thread(target=capture.run, name=f"cam{capture.channel}") for capture in captures]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for capture in captures:
        if capture.error is not None:
            raise capture.error
        print(f"cam{capture.channel}: {capture.frames} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
