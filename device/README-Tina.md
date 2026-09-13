# SynCap service for Allwinner Tina stereo cameras

`tina_service.c` adapts the discovered Allwinner/Tina stereo camera to the
same HTTP protocol used by the SynCap apps. It does not transcode or open the
preview streams during health checks.

## Runtime model

The init script runs the API service under a small watchdog, and the API service
starts exactly one persistent vendor recorder:

```text
C06_STREAM=1 /usr/bin/qgapp /tmp/syncap-ring 1600 1200 30 4000 2
```

That process publishes raw H.265 on TCP 9100 (left) and 9101 (right), and rolls
two-second MCAP files containing both cameras, `/ego/imu`, and `/audio`. qgapp
does not exit reliably, so capture start/stop never signals or restarts it:

- While idle, the service deletes only segments with a clean status sidecar and
  the MCAP closing magic.
- Start waits for the segment that was already open to close and discards it.
  `startedAtDeviceTimeNs` therefore corresponds to the next clean boundary and
  does not include up to two seconds of pre-roll.
- While recording, each newly closed pair is copied atomically to the active SD
  session and removed from the tmpfs ring only after both copies are durable.
  Size and SHA-256 are calculated during that same copy, so stop never rereads
  a long session on the small RISC-V CPU.
- Atomic publication syncs the file both before and after rename, then its
  parent directory. On this FAT32 firmware, syncing only the temporary file
  and parent left the final disk entry at size/start-cluster zero until later
  inode writeback. The ring source is retained if any commit step fails.
- Stop waits for the segment open at stop time to close, then hashes and commits
  the complete session. The normal start/stop latency is bounded by one segment.

Any terminal segment whose status is not `clean`, or whose MCAP closing magic is
invalid, fails the active capture immediately. Copy failures and recorder exits
do the same. The service writes `/tmp/syncap-ring/.syncap-quarantine` and keeps
the failing plus all later ring files even after the user presses stop or the
API service restarts. New captures remain blocked with
`capture.recovery_required` until those files are recovered and the device is
rebooted (or the marker is removed deliberately during service maintenance).
Because the ring is an 88 MiB tmpfs, entering quarantine sends `SIGKILL` to the
tracked qgapp producer after the marker is durable; the known-unreliable
`SIGTERM` path is not used. Status then reports `recoveryRequired`,
`rebootRequired`, and `producerState: "intentionally_stopped"`. The supervisor
cannot restart the producer while quarantine exists. A qgapp exit while idle
also enters this state instead of attempting a restart: on this firmware the
ISP subsequently reports `/dev/video0` unavailable until the board reboots.
Normal status requests never signal qgapp. Terminal residues found while idle
are also quarantined and preserved; the service never assumes that incomplete
data is disposable.

Preview status is determined by reading LISTEN entries in `/proc/net/tcp*`.
The service never connects to ports 9100/9101, because this firmware's raw
stream can have a single consumer and a probe connection would interrupt App
preview after a few seconds.

The measured stream rate is 29.4118 fps despite qgapp's `30` command argument.
PTP is reported as `unsupported` / `not_required`; the two sensors use the
device's hardware trigger. Stable cumulative counters do not measure exposure
timing: acquisition/preview skew values and per-camera skew remain `null`, with
zero measurement samples. `sync.hardwareTrigger`, `counterHealth`, and
`observedTriggerCount` separately report trigger configuration, counter health,
and cumulative trigger count. `hardware_trigger_active` does not claim a measured
zero timing error. No calibration capability is advertised because this unit
has no calibration file.

## Storage behavior

Production accepts only a real, writable mounted SD card at
`/rom/mnt/SDCARD` or `/mnt/extsd`, with at least 512 MiB free. Sessions are
stored at `<mount>/SynCap/ses_*`. With no SD card, preview remains available,
`storage.canCapture` is false, and capture start returns HTTP 409 with
`error: "storage.insufficient"`.

## Device installation

The stock `/etc/init.d/S20app` starts qgapp with 180-second segments and without
the raw preview listeners. Before enabling SynCap, comment only that qgapp
launch line; keep the rest of S20app because it initializes PWM and mounts the
SD card. Install the cross-built binary as
`/opt/syncap/syncap-tina-service`, install `S95syncap-tina` as
`/etc/init.d/S95syncap-tina`, and make both executable. The original S20app is
always recoverable from the read-only path `/rom/etc/init.d/S20app`.

After reboot, first verify the init state on the device:

```sh
/etc/init.d/S95syncap-tina status
```

This Tina image has no `wget` or `curl`. Verify the HTTP endpoints from the
connected development computer (choose the Tina serial if more than one ADB
device is attached):

```sh
adb -s <tina-serial> forward tcp:18080 tcp:8080
curl http://127.0.0.1:18080/health
curl http://127.0.0.1:18080/v1/status
```

There must be exactly one live qgapp process, and both preview ports 9100 and
9101 must be reported online. Do not start a second qgapp manually.

The watchdog records both PID and Linux process start time and checks the
command line before treating a pidfile as live. If the API process crashes, it
is restarted after one second and adopts the existing qgapp, so the tmpfs ring
is not left without a consumer. Three failures shorter than 15 seconds are
treated as a crash loop; the watchdog then stops every process whose
`/proc/<pid>/comm` is exactly `qgapp` to bound ring growth. The service log is
rotated at 1 MiB and retains one previous copy.

The v1 client historically calls its removable-storage slot `usb`. The service
therefore keeps `target: "usb"` for wire compatibility but returns
`mediaType: "sd"`, `displayName: "SD 卡"`, and advertises `storage.sd`.
It never claims that internal storage can capture.

Wi-Fi scanning and configuration are performed by the device itself, so the
same flow works for an ordinary access point or a hotspot hosted by the phone.
The password is passed directly as an argument to Tina's `/bin/wifi` manager,
is never interpolated into a shell command, and is cleared from the service's
working buffers after the attempt.

For a temporary QA directory only, set both variables; setting a root alone
does not bypass the real-mount check:

```sh
export SYNCAP_TINA_STORAGE_ROOT=/tmp/syncap-qa-sd
export SYNCAP_TINA_ALLOW_UNMOUNTED_STORAGE=1
```

## Build and install

Use the matching static musl toolchain from the Tina SDK. Board-specific
`-march` and `-mabi` flags can be passed through `TINA_CFLAGS`.

```sh
TINA_TOOLCHAIN=/path/to/tina/toolchain \
TINA_CFLAGS='-march=rv32imafdc -mabi=ilp32d' \
./device/build_tina_service.sh
```

Alternatively set `TINA_CC`, `TINA_SYSROOT`, `TINA_STRIP`, and `TINA_OUTPUT`
directly. Verify the result reports the same RISC-V class and musl ABI as the
device before installing it.

Expected target layout:

```text
/opt/syncap/syncap-tina-service
/etc/init.d/S95syncap-tina
```

`S95syncap-tina restart` stops and replaces only the watchdog/API process; qgapp
is left running briefly and the new API process adopts it. Use restart only
while idle. `S95syncap-tina stop` is different: after stopping the watchdog and
API, it verifies `/proc/<pid>/comm` for every candidate and sends `SIGKILL` only
to processes named exactly `qgapp`. This firmware does not terminate qgapp
reliably with `SIGTERM`, and leaving it alive without the API service would fill
the 88 MiB tmpfs. Because the camera stack may not reopen after qgapp exits, a
subsequent start can require a board reboot. Stop a capture through the App
before stopping the service.

At runtime, a qgapp exit is not spliced into a session or automatically
replaced. If it dies during capture, that session is marked failed with its
already-closed segments retained. If it dies while idle, the service enters the
recovery/reboot-required state. A live PID whose preview listeners disappear is
reported unavailable and is never killed by a status request.

qgapp output defaults to `/dev/null` so verbose vendor logs cannot consume the
camera's small tmpfs. Set `--qgapp-log /tmp/qgapp.log` only for bounded QA runs.

Since service 0.2.5, active ring housekeeping transfers at most one closed
segment per pass so queued SD writes yield to the control socket. Stop fixes
the last segment belonging to the capture, including the close/open race, and
does not append later segments. The 8-second camera-close deadline is separate
from the 45-second total stop/drain deadline: a boundary that closed during an
SD write must not be reported as a stalled camera. The producer watchdog also
re-observes the ring after a slow copy instead of charging its own write time
as missing camera output. A blocking filesystem call can still exceed these
cooperative deadlines. Footer, sensor-counter, durable-write and export hash
checks remain mandatory; this does not repair vendor IMU or frame-counter
matching faults. `test_tina_stop_backpressure.py` exercises the slow-write
race with a simulated clock and an owned child producer.

## Bluetooth provisioning

The Tina helper advertises LE provisioning only after successful GATT application
registration. Firmware without `GattManager1` keeps Classic discovery and the
encrypted RFCOMM SPP service on channel 7, without advertising an absent GATT
service. Android can use cached SPP capabilities on an already-paired dual-mode
device to choose RFCOMM directly. Pairing and encryption remain required.

## HTTP API

```text
GET  /health
GET  /v1/manifest
GET  /v1/status
GET  /v1/wifi/scan
GET  /v1/wifi/status
POST /v1/wifi/configure
GET  /v1/storage
POST /v1/storage/configure
GET  /v1/captures/current
POST /v1/captures/start
POST /v1/captures/{captureId}/stop
GET  /v1/sessions
GET  /v1/sessions/{sessionId}/manifest
POST /v1/sessions/{sessionId}/prepare-export
GET  /v1/sessions/{sessionId}/files/{name}
```

Downloads support `HEAD` and single HTTP byte ranges. Export manifests contain
the exact size and SHA-256 of every MCAP and clean status sidecar.
Empty/corrupt session metadata remains visible as a failed session with
`exportAvailable: false`; corrupt manifests and missing/size-mismatched files
are rejected by manifest/prepare-export requests, without rewriting originals.

Active capture status includes `elapsedMs`, `startedAtDeviceTimeNs`,
`sizeBytes`, and `fileCount`. Device nanoseconds use the monotonic clock and
must never be subtracted from the client's calendar clock. `startedAt` is
calendar metadata only and can be in 1970 when the device has not been set.

Run the host protocol tests with:

```sh
python3 -m unittest device.test_tina_service
```
