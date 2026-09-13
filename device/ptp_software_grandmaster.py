#!/usr/bin/env python3
"""Minimal two-step PTPv2 software grandmaster for direct-link validation."""

from __future__ import annotations

import argparse
import select
import signal
import socket
import struct
import time


PTP_MULTICAST = "224.0.1.129"
EVENT_PORT = 319
GENERAL_PORT = 320
TWO_STEP_FLAG = 0x0200


def timestamp(value_ns: int) -> bytes:
    seconds, nanoseconds = divmod(value_ns, 1_000_000_000)
    return seconds.to_bytes(6, "big") + struct.pack("!I", nanoseconds)


class Grandmaster:
    def __init__(self, interface_ip: str, mac: str, sync_hz: float):
        self.interface_ip = interface_ip
        octets = bytes.fromhex(mac.replace(":", ""))
        if len(octets) != 6:
            raise ValueError("MAC address must contain six octets")
        self.clock_identity = octets[:3] + b"\xff\xfe" + octets[3:]
        self.port_identity = self.clock_identity + struct.pack("!H", 1)
        self.sync_period = 1.0 / sync_hz
        self.sync_sequence = 0
        self.announce_sequence = 0
        self.delay_responses = 0
        self.running = True
        self.event_socket = self._make_socket(EVENT_PORT)
        self.general_socket = self._make_socket(GENERAL_PORT)

    def _make_socket(self, port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(self.interface_ip))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)
        sock.bind(("", port))
        membership = socket.inet_aton(PTP_MULTICAST) + socket.inet_aton(self.interface_ip)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
        return sock

    def header(
        self,
        message_type: int,
        length: int,
        sequence: int,
        control: int,
        log_interval: int,
        flags: int = 0,
    ) -> bytes:
        return struct.pack(
            "!BBHBBHqI8sHHBb",
            message_type,
            2,
            length,
            0,
            0,
            flags,
            0,
            0,
            self.clock_identity,
            1,
            sequence,
            control,
            log_interval,
        )

    def send_announce(self) -> None:
        now_ns = time.time_ns()
        payload = timestamp(now_ns) + struct.pack(
            "!hBBBBHB8sHB",
            0,
            0,
            100,
            248,
            0xFE,
            0xFFFF,
            100,
            self.clock_identity,
            0,
            0xA0,
        )
        message = self.header(0x0B, 64, self.announce_sequence, 5, 0) + payload
        self.general_socket.sendto(message, (PTP_MULTICAST, GENERAL_PORT))
        self.announce_sequence = (self.announce_sequence + 1) & 0xFFFF

    def send_sync(self) -> None:
        sequence = self.sync_sequence
        sync = self.header(0x00, 44, sequence, 0, -3, TWO_STEP_FLAG) + timestamp(0)
        before_ns = time.time_ns()
        self.event_socket.sendto(sync, (PTP_MULTICAST, EVENT_PORT))
        after_ns = time.time_ns()
        precise_origin_ns = (before_ns + after_ns) // 2
        follow_up = self.header(0x08, 44, sequence, 2, -3) + timestamp(precise_origin_ns)
        self.general_socket.sendto(follow_up, (PTP_MULTICAST, GENERAL_PORT))
        self.sync_sequence = (sequence + 1) & 0xFFFF

    def receive_delay_request(self) -> None:
        packet, _ = self.event_socket.recvfrom(2048)
        receive_ns = time.time_ns()
        if len(packet) < 44 or packet[0] & 0x0F != 0x01 or packet[1] & 0x0F != 2:
            return
        sequence = struct.unpack("!H", packet[30:32])[0]
        requesting_port_identity = packet[20:30]
        response = (
            self.header(0x09, 54, sequence, 3, 0x7F)
            + timestamp(receive_ns)
            + requesting_port_identity
        )
        self.general_socket.sendto(response, (PTP_MULTICAST, GENERAL_PORT))
        self.delay_responses += 1

    def run(self, duration_s: float) -> None:
        started = time.monotonic()
        next_sync = started
        next_announce = started
        while self.running and time.monotonic() - started < duration_s:
            now = time.monotonic()
            if now >= next_announce:
                self.send_announce()
                next_announce += 1.0
            if now >= next_sync:
                self.send_sync()
                next_sync += self.sync_period
            timeout = max(0.0, min(next_sync, next_announce) - time.monotonic())
            readable, _, _ = select.select([self.event_socket], [], [], min(timeout, 0.1))
            if readable:
                self.receive_delay_request()
        print(
            f"sent_sync={self.sync_sequence} sent_announce={self.announce_sequence} "
            f"delay_responses={self.delay_responses}",
            flush=True,
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface-ip", required=True)
    parser.add_argument("--mac", required=True)
    parser.add_argument("--sync-hz", type=float, default=8.0)
    parser.add_argument("--duration", type=float, default=90.0)
    args = parser.parse_args()
    grandmaster = Grandmaster(args.interface_ip, args.mac, args.sync_hz)
    signal.signal(signal.SIGTERM, lambda *_: setattr(grandmaster, "running", False))
    signal.signal(signal.SIGINT, lambda *_: setattr(grandmaster, "running", False))
    grandmaster.run(args.duration)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
