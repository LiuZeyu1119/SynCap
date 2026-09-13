#!/usr/bin/env python3
"""Scan and exercise the SynCap BLE provisioning characteristics with Bleak."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import struct

from bleak import BleakClient, BleakScanner


SERVICE_UUID = "8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1"
CONFIG_UUID = "8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1"
STATUS_UUID = "8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1"


async def run(
    claim_code: str,
    ssid: str,
    scan_seconds: float,
    fragment_size: int,
    command: dict[str, object] | None,
) -> None:
    discovered = await BleakScanner.discover(timeout=scan_seconds, return_adv=True)
    match = None
    for device, advertisement in discovered.values():
        uuids = {value.lower() for value in advertisement.service_uuids}
        if SERVICE_UUID in uuids or (device.name or "").startswith("SynCap-"):
            match = device
            break
    if match is None:
        raise RuntimeError("SynCap BLE advertisement was not found")

    async with BleakClient(match) as client:
        payload_object = command or {
                "ssid": ssid,
                "password": "",
                "security": "open",
                "connectNow": False,
            }
        payload_object["claimCode"] = claim_code
        payload = json.dumps(
            payload_object,
            separators=(",", ":"),
        ).encode("utf-8")
        message_id = secrets.randbits(32)
        fragments = [payload[offset:offset + fragment_size] for offset in range(0, len(payload), fragment_size)]
        for index, fragment in enumerate(fragments):
            header = struct.pack(">2sBBIHHHH", b"SC", 1, 1, message_id, index, len(fragments), len(fragment), 0)
            await client.write_gatt_char(CONFIG_UUID, header + fragment, response=True)
        status: dict[str, object] = {"state": "working"}
        for _ in range(75):
            await asyncio.sleep(0.8)
            status = json.loads((await client.read_gatt_char(STATUS_UUID)).decode("utf-8"))
            if status.get("state") != "working":
                break
        print(json.dumps({"device": match.name, "address": match.address, "status": status}, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--claim-code", required=True)
    parser.add_argument("--ssid", default="SynCap-BLE-Smoke")
    parser.add_argument("--scan-seconds", type=float, default=8)
    parser.add_argument("--fragment-size", type=int, default=4)
    parser.add_argument("--command", help='JSON command, for example {"op":"storage.configure","target":"usb"}')
    args = parser.parse_args()
    command = json.loads(args.command) if args.command else None
    if command is not None and not isinstance(command, dict):
        parser.error("--command must be a JSON object")
    asyncio.run(run(args.claim_code, args.ssid, args.scan_seconds, max(1, args.fragment_size), command))


if __name__ == "__main__":
    main()
