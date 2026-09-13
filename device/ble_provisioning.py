#!/usr/bin/env python3
"""BlueZ GATT service for encrypted SynCap provisioning and offline control."""

from __future__ import annotations

import ctypes
import json
import secrets
import signal
import struct
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import dbus
import dbus.exceptions
import dbus.mainloop.glib
import dbus.service


BLUEZ_SERVICE = "org.bluez"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHARACTERISTIC_IFACE = "org.bluez.GattCharacteristic1"
ADAPTER_IFACE = "org.bluez.Adapter1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
AGENT_IFACE = "org.bluez.Agent1"
AGENT_MANAGER_IFACE = "org.bluez.AgentManager1"

WIFI_SERVICE_UUID = "8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1"
WIFI_CONFIG_UUID = "8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1"
WIFI_STATUS_UUID = "8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1"
BLE_STATUS_MAX_BYTES = 480


class InvalidArgs(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class InvalidValueLength(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.InvalidValueLength"


class Failed(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.Failed"


class PairingAgent(dbus.service.Object):
    path = "/org/syncap/provisioning/agent"

    def __init__(self, bus: dbus.SystemBus) -> None:
        super().__init__(bus, self.path)

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="s")
    def RequestPinCode(self, _device: dbus.ObjectPath) -> str:
        return "000000"

    @dbus.service.method(AGENT_IFACE, in_signature="ou", out_signature="")
    def RequestConfirmation(self, _device: dbus.ObjectPath, _passkey: dbus.UInt32) -> None:
        return

    @dbus.service.method(AGENT_IFACE, in_signature="o", out_signature="")
    def RequestAuthorization(self, _device: dbus.ObjectPath) -> None:
        return

    @dbus.service.method(AGENT_IFACE, in_signature="os", out_signature="")
    def AuthorizeService(self, _device: dbus.ObjectPath, _uuid: str) -> None:
        return

    @dbus.service.method(AGENT_IFACE, in_signature="", out_signature="")
    def Cancel(self) -> None:
        return


class Application(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus, status: "ProvisioningStatus") -> None:
        self.path = "/org/syncap/provisioning"
        self.services: list[Service] = []
        super().__init__(bus, self.path)
        self.add_service(WifiService(bus, 0, status))

    def add_service(self, service: "Service") -> None:
        self.services.append(service)

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self) -> dict[dbus.ObjectPath, dict[str, dict[str, object]]]:
        response: dict[dbus.ObjectPath, dict[str, dict[str, object]]] = {}
        for service in self.services:
            response[service.get_path()] = service.get_properties()
            for characteristic in service.characteristics:
                response[characteristic.get_path()] = characteristic.get_properties()
        return response


class Service(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus, index: int, uuid: str, primary: bool) -> None:
        self.path = f"/org/syncap/provisioning/service{index}"
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics: list[Characteristic] = []
        super().__init__(bus, self.path)

    def get_properties(self) -> dict[str, dict[str, object]]:
        return {
            GATT_SERVICE_IFACE: {
                "UUID": self.uuid,
                "Primary": self.primary,
                "Characteristics": dbus.Array(
                    [characteristic.get_path() for characteristic in self.characteristics], signature="o"
                ),
            }
        }

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, characteristic: "Characteristic") -> None:
        self.characteristics.append(characteristic)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict[str, object]:
        if interface != GATT_SERVICE_IFACE:
            raise InvalidArgs()
        return self.get_properties()[GATT_SERVICE_IFACE]


class Characteristic(dbus.service.Object):
    def __init__(self, bus: dbus.SystemBus, index: int, uuid: str, flags: list[str], service: Service) -> None:
        self.path = f"{service.path}/char{index}"
        self.bus = bus
        self.uuid = uuid
        self.flags = flags
        self.service = service
        super().__init__(bus, self.path)

    def get_properties(self) -> dict[str, dict[str, object]]:
        return {
            GATT_CHARACTERISTIC_IFACE: {
                "Service": self.service.get_path(),
                "UUID": self.uuid,
                "Flags": dbus.Array(self.flags, signature="s"),
            }
        }

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict[str, object]:
        if interface != GATT_CHARACTERISTIC_IFACE:
            raise InvalidArgs()
        return self.get_properties()[GATT_CHARACTERISTIC_IFACE]


class ProvisioningStatus:
    def __init__(self, claim_code_path: Path = Path("/etc/syncap/claim-code")) -> None:
        self.lock = threading.Lock()
        self.value: dict[str, object] = {"state": "ready"}
        self.claim_code_path = claim_code_path

    def set(self, value: dict[str, object]) -> None:
        with self.lock:
            self.value = value

    def encoded(self) -> bytes:
        with self.lock:
            return json.dumps(self.value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    def _valid_claim(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            expected = self.claim_code_path.read_text(encoding="ascii").strip()
        except OSError:
            return False
        return secrets.compare_digest(value, expected)

    @staticmethod
    def _request(
        path: str,
        method: str,
        payload: dict[str, object] | None,
        claim_code: str,
        timeout: int,
    ) -> dict[str, object]:
        body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:8080{path}",
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-SynCap-Claim": claim_code,
            },
            method=method,
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("device service returned a non-object response")
        return result

    @staticmethod
    def _compact(op: str, result: dict[str, object]) -> dict[str, object]:
        if op == "wifi.scan":
            raw_networks = result.get("networks", [])
            by_ssid: dict[str, dict[str, object]] = {}
            invalid_network = False
            if isinstance(raw_networks, list):
                for raw_network in raw_networks:
                    if not isinstance(raw_network, dict):
                        invalid_network = True
                        continue
                    ssid = raw_network.get("ssid")
                    rssi = raw_network.get("rssi")
                    if not isinstance(ssid, str) or not ssid or not isinstance(rssi, int):
                        invalid_network = True
                        continue
                    network = {
                        "ssid": ssid,
                        "rssi": rssi,
                        "security": str(raw_network.get("security", "open")),
                        "secure": bool(raw_network.get("secure", False)),
                    }
                    previous = by_ssid.get(ssid)
                    if previous is None or rssi > int(previous["rssi"]):
                        by_ssid[ssid] = network
            else:
                invalid_network = True
            networks = sorted(
                by_ssid.values(),
                key=lambda network: (-int(network["rssi"]), str(network["ssid"]).casefold()),
            )
            scanned_at = result.get("scannedAt")
            if not isinstance(scanned_at, int):
                scanned_at = int(time.time() * 1000)
            response: dict[str, object] = {
                "state": "completed",
                "op": op,
                "networks": [],
                "scannedAt": scanned_at,
            }
            truncated = bool(result.get("truncated")) or invalid_network
            included: list[dict[str, object]] = []
            for network in networks:
                candidate = {**response, "networks": [*included, network], "truncated": True}
                encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(encoded) > BLE_STATUS_MAX_BYTES:
                    truncated = True
                    break
                included.append(network)
            response["networks"] = included
            if len(included) < len(networks):
                truncated = True
            if truncated:
                response["truncated"] = True
            return response
        if op == "storage.configure":
            usb = result.get("usb", {})
            return {
                "state": "ready",
                "op": op,
                "storage": {
                    "target": result.get("target"),
                    "usbAvailable": usb.get("available") if isinstance(usb, dict) else False,
                    "usbMounted": usb.get("mounted") if isinstance(usb, dict) else False,
                    "usbLabel": usb.get("label") if isinstance(usb, dict) else None,
                    "usbUuid": usb.get("uuid") if isinstance(usb, dict) else None,
                    "usbTotalBytes": usb.get("totalBytes") if isinstance(usb, dict) else None,
                    "usbFreeBytes": usb.get("freeBytes") if isinstance(usb, dict) else None,
                },
            }
        if op == "device.status":
            latest = result.get("latestSession")
            latest_summary = None
            if isinstance(latest, dict):
                latest_summary = {
                    key: latest[key]
                    for key in ("id", "name", "createdAt", "durationMs", "sizeBytes", "status", "fileCount")
                    if key in latest and latest[key] is not None
                }
            storage = result.get("storage")
            storage_summary = None
            if isinstance(storage, dict):
                storage_summary = {
                    key: storage[key]
                    for key in (
                        "target", "usbAvailable", "usbMounted", "usbLabel", "usbUuid",
                        "usbTotalBytes", "usbFreeBytes",
                    )
                    if key in storage and storage[key] is not None
                }
            capture = result.get("capture")
            capture_summary = None
            if isinstance(capture, dict):
                capture_summary = {
                    key: capture[key]
                    for key in ("state", "captureId", "sessionId", "name", "startedAt")
                    if key in capture and capture[key] is not None
                }
            response = {
                "state": result.get("state", "ready"),
                "op": op,
                "storage": storage_summary,
                "capture": capture_summary,
                "sessionCount": result.get("sessionCount", 0),
                "latestSession": latest_summary,
            }
            wifi = result.get("wifi")
            if isinstance(wifi, dict):
                wifi_summary: dict[str, object] = {"state": str(wifi.get("state", "idle"))}
                ssid = wifi.get("ssid")
                ip_address = wifi.get("ipAddress")
                if isinstance(ssid, str) and ssid:
                    wifi_summary["ssid"] = ssid
                if isinstance(ip_address, str) and ip_address:
                    wifi_summary["ipAddress"] = ip_address
                response["wifi"] = wifi_summary
            optional_latest = ("name", "createdAt", "durationMs", "sizeBytes", "fileCount", "status", "id")
            for key in optional_latest:
                if len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= BLE_STATUS_MAX_BYTES:
                    break
                if isinstance(response.get("latestSession"), dict):
                    response["latestSession"].pop(key, None)
            if isinstance(response.get("latestSession"), dict) and not response["latestSession"]:
                response["latestSession"] = None
            if len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > BLE_STATUS_MAX_BYTES:
                response["latestSession"] = None
                if isinstance(response.get("capture"), dict):
                    response["capture"] = {"state": response["capture"].get("state", "idle")}
            if len(json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > BLE_STATUS_MAX_BYTES:
                response["storage"] = {
                    "target": storage_summary.get("target") if isinstance(storage_summary, dict) else None,
                }
            return response
        return {"op": op, **result}

    def dispatch(self, payload: dict[str, object]) -> None:
        claim_code = payload.get("claimCode")
        if not self._valid_claim(claim_code):
            self.set({"state": "failed", "error": "claim_required"})
            return
        op = str(payload.get("op") or ("wifi.configure" if "ssid" in payload else ""))
        if op not in {
            "wifi.configure", "wifi.scan", "storage.configure", "storage.eject",
            "capture.start", "capture.stop", "device.status",
        }:
            self.set({"state": "failed", "error": "unsupported_operation"})
            return
        self.set({"state": "working", "op": op})

        def run() -> None:
            try:
                if op == "wifi.configure":
                    body = {**payload, "connectNow": bool(payload.get("connectNow", True))}
                    result = self._request("/v1/wifi/configure", "POST", body, str(claim_code), 45)
                elif op == "wifi.scan":
                    result = self._request("/v1/wifi/scan", "GET", None, str(claim_code), 15)
                elif op == "storage.configure":
                    result = self._request(
                        "/v1/storage/configure", "POST", {"target": payload.get("target")}, str(claim_code), 15
                    )
                elif op == "storage.eject":
                    result = self._request("/v1/storage/eject", "POST", {}, str(claim_code), 15)
                elif op == "capture.start":
                    result = self._request(
                        "/v1/captures/start", "POST", {"name": payload.get("name")}, str(claim_code), 15
                    )
                elif op == "capture.stop":
                    capture_id = str(payload.get("captureId", ""))
                    result = self._request(
                        f"/v1/captures/{capture_id}/stop", "POST", {}, str(claim_code), 90
                    )
                else:
                    result = self._request("/v1/offline/status", "GET", None, str(claim_code), 10)
                self.set(self._compact(op, result))
            except urllib.error.HTTPError as error:
                try:
                    result = json.loads(error.read().decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    result = {"error": f"http_{error.code}"}
                self.set({"state": "failed", **result})
            except Exception as error:
                self.set({"state": "failed", "error": str(error)})

        threading.Thread(target=run, name=f"syncap-ble-{op}", daemon=True).start()


class WifiConfigCharacteristic(Characteristic):
    def __init__(self, bus: dbus.SystemBus, index: int, service: Service, status: ProvisioningStatus) -> None:
        super().__init__(bus, index, WIFI_CONFIG_UUID, ["encrypt-write"], service)
        self.status = status
        self.fragments: dict[int, dict[str, object]] = {}

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value: list[dbus.Byte], _options: dict[str, object]) -> None:
        encoded = bytes(value)
        if len(encoded) > 512 or not encoded:
            raise InvalidValueLength()
        if encoded.startswith(b"SC"):
            encoded = self._accept_fragment(encoded)
            if not encoded:
                return
        try:
            payload = json.loads(encoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Failed("invalid JSON") from error
        if not isinstance(payload, dict):
            raise Failed("JSON payload must be an object")
        self.status.dispatch(payload)

    def _accept_fragment(self, encoded: bytes) -> bytes:
        if len(encoded) < 16:
            raise InvalidValueLength()
        magic, version, flags, message_id, index, count, length, reserved = struct.unpack(">2sBBIHHHH", encoded[:16])
        payload = encoded[16:]
        if (
            magic != b"SC" or version != 1 or flags & 1 != 1 or reserved != 0 or
            count == 0 or count > 128 or index >= count or length != len(payload)
        ):
            raise Failed("invalid BLE fragment header")
        entry = self.fragments.setdefault(message_id, {"count": count, "parts": {}})
        if entry["count"] != count:
            self.fragments.pop(message_id, None)
            raise Failed("BLE fragment count changed")
        parts = entry["parts"]
        if not isinstance(parts, dict):
            raise Failed("invalid BLE fragment state")
        parts[index] = payload
        if len(parts) != count:
            return b""
        combined = b"".join(parts[position] for position in range(count))
        self.fragments.pop(message_id, None)
        if len(combined) > 4096:
            raise InvalidValueLength()
        return combined


class WifiStatusCharacteristic(Characteristic):
    def __init__(self, bus: dbus.SystemBus, index: int, service: Service, status: ProvisioningStatus) -> None:
        super().__init__(bus, index, WIFI_STATUS_UUID, ["encrypt-read"], service)
        self.status = status

    @dbus.service.method(GATT_CHARACTERISTIC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options: dict[str, object]) -> dbus.Array:
        offset = int(options.get("offset", 0))
        encoded = self.status.encoded()
        if offset < 0 or offset > len(encoded):
            raise InvalidValueLength()
        return dbus.Array([dbus.Byte(value) for value in encoded[offset:]], signature="y")


class WifiService(Service):
    def __init__(self, bus: dbus.SystemBus, index: int, status: ProvisioningStatus) -> None:
        super().__init__(bus, index, WIFI_SERVICE_UUID, True)
        self.add_characteristic(WifiConfigCharacteristic(bus, 0, self, status))
        self.add_characteristic(WifiStatusCharacteristic(bus, 1, self, status))


class Advertisement(dbus.service.Object):
    PATH_BASE = "/org/syncap/provisioning/advertisement"

    def __init__(self, bus: dbus.SystemBus, index: int, local_name: str) -> None:
        self.path = self.PATH_BASE + str(index)
        self.local_name = local_name
        super().__init__(bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def get_properties(self) -> dict[str, dict[str, object]]:
        return {
            LE_ADVERTISEMENT_IFACE: {
                "Type": "peripheral",
                "ServiceUUIDs": dbus.Array([WIFI_SERVICE_UUID], signature="s"),
                "LocalName": dbus.String(self.local_name),
                "Discoverable": dbus.Boolean(True),
                "Includes": dbus.Array(["tx-power"], signature="s"),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface: str) -> dict[str, object]:
        if interface != LE_ADVERTISEMENT_IFACE:
            raise InvalidArgs()
        return self.get_properties()[LE_ADVERTISEMENT_IFACE]

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature="", out_signature="")
    def Release(self) -> None:
        return


def find_adapter(bus: dbus.SystemBus) -> str:
    manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE, "/"), DBUS_OM_IFACE)
    objects = manager.GetManagedObjects()
    for path, interfaces in objects.items():
        if GATT_MANAGER_IFACE in interfaces and LE_ADVERTISING_MANAGER_IFACE in interfaces:
            return str(path)
    raise RuntimeError("Bluetooth adapter does not expose GATT and advertising managers")


def main() -> None:
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    bus = dbus.SystemBus()
    adapter_path = find_adapter(bus)
    adapter_object = bus.get_object(BLUEZ_SERVICE, adapter_path)
    properties = dbus.Interface(adapter_object, DBUS_PROP_IFACE)
    address = str(properties.Get(ADAPTER_IFACE, "Address"))
    local_name = f"SynCap-{address.replace(':', '')[-4:]}"
    properties.Set(ADAPTER_IFACE, "Alias", dbus.String(local_name))
    properties.Set(ADAPTER_IFACE, "Powered", dbus.Boolean(True))
    properties.Set(ADAPTER_IFACE, "Pairable", dbus.Boolean(True))

    status = ProvisioningStatus()
    pairing_agent = PairingAgent(bus)
    application = Application(bus, status)
    advertisement = Advertisement(bus, 0, local_name)
    gatt_manager = dbus.Interface(adapter_object, GATT_MANAGER_IFACE)
    advertising_manager = dbus.Interface(adapter_object, LE_ADVERTISING_MANAGER_IFACE)
    agent_manager = dbus.Interface(bus.get_object(BLUEZ_SERVICE, "/org/bluez"), AGENT_MANAGER_IFACE)
    agent_manager.RegisterAgent(pairing_agent.path, "NoInputNoOutput")
    agent_manager.RequestDefaultAgent(pairing_agent.path)
    glib = ctypes.CDLL("libglib-2.0.so.0")
    glib.g_main_loop_new.argtypes = [ctypes.c_void_p, ctypes.c_int]
    glib.g_main_loop_new.restype = ctypes.c_void_p
    glib.g_main_loop_run.argtypes = [ctypes.c_void_p]
    glib.g_main_loop_quit.argtypes = [ctypes.c_void_p]
    loop = glib.g_main_loop_new(None, False)

    registration = {"gatt": False, "advertisement": False}

    def registered(kind: str) -> None:
        registration[kind] = True
        if all(registration.values()):
            print(f"SynCap BLE provisioning active as {local_name}", flush=True)

    def registration_failed(error: object) -> None:
        print(f"BLE registration failed: {error}", flush=True)
        glib.g_main_loop_quit(loop)

    gatt_manager.RegisterApplication(
        application.get_path(), {},
        reply_handler=lambda: registered("gatt"),
        error_handler=registration_failed,
    )
    advertising_manager.RegisterAdvertisement(
        advertisement.get_path(), {},
        reply_handler=lambda: registered("advertisement"),
        error_handler=registration_failed,
    )

    def stop(_signum: int, _frame: object) -> None:
        glib.g_main_loop_quit(loop)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    glib.g_main_loop_run(loop)


if __name__ == "__main__":
    main()
