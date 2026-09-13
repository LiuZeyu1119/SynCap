#!/usr/bin/env python3
"""Sample a PTP hardware clock between two CLOCK_MONOTONIC_RAW reads."""

from __future__ import annotations

import argparse
import os
import time


CLOCK_MONOTONIC_RAW = 4
CLOCKFD = 3


def fd_to_clock_id(fd: int) -> int:
    return ((~fd) << 3) | CLOCKFD


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--device", default="/dev/ptp0")
    parser.add_argument("--duration", type=float, default=90.0)
    parser.add_argument("--rate", type=float, default=200.0)
    args = parser.parse_args()
    if args.duration <= 0 or args.rate <= 0:
        parser.error("duration and rate must be positive")

    fd = os.open(args.device, os.O_RDONLY)
    phc_clock_id = fd_to_clock_id(fd)
    period = 1.0 / args.rate
    started = time.monotonic()
    deadline = started + args.duration
    next_sample = started
    print("raw_before_ns,phc_ns,raw_after_ns,raw_mid_ns,bracket_ns,phc_minus_raw_ns", flush=True)
    try:
        while time.monotonic() < deadline:
            raw_before = time.clock_gettime_ns(CLOCK_MONOTONIC_RAW)
            phc = time.clock_gettime_ns(phc_clock_id)
            raw_after = time.clock_gettime_ns(CLOCK_MONOTONIC_RAW)
            raw_mid = (raw_before + raw_after) // 2
            print(
                f"{raw_before},{phc},{raw_after},{raw_mid},"
                f"{raw_after - raw_before},{phc - raw_mid}",
                flush=False,
            )
            next_sample += period
            remaining = next_sample - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
    finally:
        os.close(fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
