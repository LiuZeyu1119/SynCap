# SynCap device service

The RoboBaton-4P adapter keeps the vendor camera demo running without replacing it. It records the four local RTSP streams with FFmpeg stream copy and records the IMU through `/root/demo/imu_reader_demo`.

## Installed layout

- `/opt/syncap/syncap_service.py`: status, capture, session, USB storage, Wi-Fi and Range download API
- `/opt/syncap/legacy_adapter.py`: legacy camera/PTP telemetry parser
- `/opt/syncap/camera_supervisor.py`: camera-demo lifecycle and bounded diagnostics log
- `/opt/syncap/ble_provisioning.py`: encrypted BlueZ GATT provisioning service
- `/opt/syncap/wifi_supervisor.py`: non-blocking saved Wi-Fi association and DHCP recovery
- `/opt/syncap/mediamtx`: persistent arm64 RTSP normalization relay (MediaMTX v1.20.0)
- `/opt/syncap/mediamtx.yml`: loopback-only relay configuration for the four camera streams
- `/etc/init.d/S96syncap`: boot service
- `/etc/syncap/claim-code`: internal protocol-compatibility value (`123456` in the development image), mode `0600`
- `/userdata/syncap/sessions`: persistent session data
- `/userdata/syncap/storage.json`: selected capture target (`internal` or `usb`)
- `/media/syncap-usb/SynCap/sessions`: offline sessions on a `SYNCAP` exFAT volume
- `/var/log/syncap`: service logs

`S96syncap` starts the camera supervisor before the API and restarts `cam_demo` after an unexpected exit. The live diagnostics log is capped and rotated in `/tmp`, so status and synchronization metrics remain current without consuming unbounded storage. `/v1/status` reports `cameraState` as `stopped`, `starting`, `degraded`, or `ready`; static camera entries in `/v1/manifest` describe capabilities and must not be treated as online state.

Camera entries also expose `mountPosition` in wearer coordinates. RoboBaton-4P maps CAM3/CAM1/CAM2/CAM0 to `wearer_left_outer`, `wearer_left_inner`, `wearer_right_inner`, and `wearer_right_outer`; protocol IDs and RTSP ports do not change.

`GET /v1/camera/configuration` returns the active image orientation. `POST /v1/camera/configuration` accepts the internal claim plus `rotationDegrees` (`0`, `90`, `180`, or `270`) and restarts the supervised camera process. The v1 runtime keeps the sensor at its full-rate mounting orientation and applies the selected rotation in each hardware encoder, so preview and captured MP4 files use the same 60 fps output. Raw calibration is exposed only by runtimes that provide the full-resolution raw-snapshot capability and must record the same rotation in its metadata.

When `/userdata/wpa_supplicant.conf` is a root-owned regular mode-`0600` file, `S96syncap` starts the Wi-Fi supervisor only after the camera, API, and BLE processes. The supervisor runs independently of boot, retries saved station association every five seconds, removes stale `wlan0` addresses while disconnected, and runs a bounded DHCP request after association. A short-lived cross-process marker keeps it out of the way while BLE provisioning replaces the saved network. A missing hotspot therefore does not delay camera or control startup and can appear later without another reboot.

Wi-Fi status is read from `wpa_supplicant` and the live `wlan0` address on every request. Restarting the device service therefore does not reset an established connection to a false `idle` state.

The current development headring advertises as `SynCap-DF62`. After system Bluetooth pairing, clients send the fixed compatibility value `123456` internally. They must never ask the user to enter or confirm it. This value preserves the existing protocol field; it is not proof of physical ownership and must be replaced by device-specific authenticated provisioning before production multi-device deployment.

## Capture and export

```text
POST /v1/captures/start
GET  /v1/captures/current
POST /v1/captures/{captureId}/stop
GET  /v1/sessions
GET  /v1/sessions/{sessionId}/manifest
POST /v1/sessions/{sessionId}/prepare-export
GET  /v1/sessions/{sessionId}/files/{name}
GET  /v1/storage
POST /v1/storage/configure
POST /v1/storage/eject
GET  /v1/camera/configuration
POST /v1/camera/configuration
```

Every completed session contains four fragmented MP4 files with copied H.264 streams, `imu.log`, `diagnostics.json`, and `session.json`. IMU capture uses the official RoboBaton runtime 1.1.0 (`libicm42688` ABI 2.1) at its supported 30 Hz rate and writes every sample plus timing metrics; the session manifest reports the same rate and the `system_realtime` timestamp domain. The relay keeps one persistent upstream connection to each vendor RTSP endpoint and repacketizes oversized RTP before the board's FFmpeg 4.4.2 recorder sees it. Recorders connect only to `127.0.0.1:8554/cam0` through `cam3`; port 8554 is not exposed to the LAN.

Stopping a capture parses the fragmented-MP4 timing tables on the device. A session is marked `complete` only when all four files have continuous decode time (maximum frame interval 100 ms), at least 95% of their nominal frame rate, duration within 2.5 seconds of the requested capture, and cross-camera duration spread no greater than 250 ms. RTSP/NAL/DTS errors in recorder logs also fail validation. The metrics and exact failure reasons are stored in `videoValidation` in both `session.json` and `diagnostics.json`; failed files remain on the selected volume for forensic recovery but are never reported as a successful session.

The deployed relay is the official `mediamtx_v1.20.0_linux_arm64.tar.gz` asset. Its archive SHA-256 is `6aa3c03da7b6477f1e110c8e18e819cf9ef121e8981b52b8f8219982dae35f2f`; the extracted binary SHA-256 is `2da379972ba86627632aa7e3f779c680ba04a5ee26ef2a20dc61cefcc24f73b8`. MediaMTX is distributed under the MIT license in `LICENSE-MediaMTX`.

Export files support HTTP Range and are accepted by the client only after size and SHA-256 verification.

`GET /v1/storage` and the `storage` object in `GET /v1/status` preserve the existing
`target`, `internal`, and `usb` fields and add capture readiness metadata:

```json
{
  "target": "internal",
  "capturePolicy": {
    "minimumFreeBytes": 536870912,
    "estimatedBytesPerSecond": 2250000
  },
  "internal": {
    "totalBytes": 4966912000,
    "freeBytes": 4043055104,
    "canCapture": true,
    "reason": null
  },
  "usb": {
    "available": false,
    "label": "SYNCAP",
    "canCapture": false,
    "reason": "not_available"
  }
}
```

Both internal and USB capture targets require at least 512 MiB free at selection
and again immediately before a session starts. `reason` is `not_available`,
`not_mounted`, or `insufficient_free_space` when `canCapture` is false. The byte
rate is a conservative planning estimate for four 4 Mbps H.264 streams plus IMU
and container overhead; it is not a storage reservation. A selected USB target
never falls back to internal storage if the volume disappears or becomes full.

## BLE provisioning and offline control

- Service: `8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1`
- Encrypted configuration write: `8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1`
- Encrypted status read: `8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1`

The Android client fragments JSON using the 16-byte SynCap BLE header after negotiating ATT MTU. The device reassembles up to 128 fragments and validates the internal six-digit compatibility value before every write operation.

The same encrypted command characteristic accepts `wifi.scan`, `wifi.configure`, `storage.configure`, `storage.eject`, `capture.start`, `capture.stop`, and `device.status`. Selecting USB storage requires a removable exFAT volume labelled `SYNCAP`; the service mounts it when needed and never silently falls back to internal storage. A completed offline session is self-contained and can be opened directly on macOS, Windows, Android, or iOS after safe eject.

`wifi.scan` runs the scan on the headring's `wlan0`, which lets a phone discover and select its own personal hotspot even though Android cannot enumerate its active SoftAP through the normal app Wi-Fi API. The client writes:

```json
{"op":"wifi.scan","claimCode":"123456"}
```

The final encrypted status response is minified JSON, contains no password or BSSID, and is capped at 480 UTF-8 bytes. Networks are deduplicated by SSID and ordered strongest first; `truncated` is set when weaker entries do not fit:

```json
{"state":"completed","op":"wifi.scan","networks":[{"ssid":"HUAWEI_PURA_X","rssi":-42,"security":"wpa2-psk","secure":true}],"scannedAt":1786700000000}
```
