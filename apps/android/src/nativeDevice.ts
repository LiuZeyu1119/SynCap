import { Capacitor, registerPlugin } from "@capacitor/core";
import { invoke } from "@tauri-apps/api/core";
import {
  normalizeDeviceManifestPayload,
  normalizeDeviceProbePayload,
  type DeviceProbeRequest,
  type NormalizedDeviceManifest,
} from "./deviceDiscovery";

export type DeviceProbeStream = {
  port: number;
  path: string;
  transport: string;
  online: boolean;
  url: string;
  reason?: string;
  responseCode?: number;
  latencyMs?: number;
};

export type DeviceProbe = {
  host: string;
  reachable: boolean;
  onlineCount: number;
  streams: DeviceProbeStream[];
};

export type NativeDeviceManifest = NormalizedDeviceManifest;

type MetricStats = {
  current: number | null;
  mean: number | null;
  p95: number | null;
  min: number | null;
  max: number | null;
  samples: number;
};

export type CaptureStorageTarget = "internal" | "usb";
export type CameraRotationDegrees = 0 | 90 | 180 | 270;

export type CameraOrientationStatus = {
  rotationDegrees: CameraRotationDegrees;
  frameRate: number;
  outputWidth: number;
  outputHeight: number;
  supportedRotations: CameraRotationDegrees[];
  appliesTo: Array<"preview" | "calibration" | "capture" | string>;
  state?: "active" | "restarting" | string;
};

export type DeviceStorageVolume = {
  mediaType?: "usb" | "sd" | "internal";
  displayName?: string;
  available?: boolean;
  mounted?: boolean;
  label?: string;
  uuid?: string;
  filesystem?: string;
  totalBytes?: number;
  freeBytes?: number;
  canCapture?: boolean;
  reason?: string | null;
};

export type DeviceStorageStatus = {
  target: CaptureStorageTarget | string;
  internal: DeviceStorageVolume;
  usb: DeviceStorageVolume;
  capturePolicy?: {
    minimumFreeBytes: number;
    estimatedBytesPerSecond: number;
  };
};

export type DeviceRuntimeStatus = {
  v: string;
  deviceTimeNs: string;
  adapter: string;
  cameraDemoRunning: boolean;
  cameras: Array<{
    id: string;
    port: number;
    online: boolean;
    fps?: number;
    groupSkewMs?: number | null;
    queue?: string;
    fullWaits?: number;
    pipelineDelayMs?: number;
  }>;
  sync: {
    method?: string;
    state?: string;
    hardwareTrigger?: boolean | null;
    counterHealth?: "stable" | "unverified";
    observedTriggerCount?: number | null;
    acquisitionSkewMs: MetricStats;
    previewPtsSkewMs: MetricStats;
    previewCameraSkewMs: MetricStats;
  };
  clock?: {
    source: "ptp" | "free_running" | string;
    state: "locked" | "locking" | "listening" | "faulty" | "unsupported" | "unknown" | string;
    domain: number | null;
    interface: string | null;
    hardwareTimestamping: boolean;
    phcDevice: string | null;
    ptp4lRunning: boolean;
    phc2sysRunning: boolean;
    grandmasterPresent?: boolean;
    statusReason?: string;
    requiredForCapture?: boolean;
    grandmasterIdentity: string | null;
    offsetFromMasterNs: number | null;
    meanPathDelayNs: number | null;
    lastUpdateAgeMs: number | null;
    systemClockDisciplined: boolean;
    sensorClockMapped: boolean;
  };
  exposureSync?: {
    phaseLockedToGrandmaster: boolean;
    targetPeriodNs: string | null;
    phaseErrorNs: number | null;
    timestampTraceable: boolean;
    timestampDomain: string;
    timestampSource: string;
    measurementPoint: string;
    physicalExposureDelayCalibrated: boolean;
    physicalExposureDelayNs: number | null;
  };
  cameraImuSync?: {
    physicalTimeOffsetCalibrated: boolean;
    timeOffsetNs: number | null;
    clockDomainMapped: boolean;
  };
  system: {
    uptimeSeconds: number;
    temperatureC: number | null;
    storage: { totalGiB: number; availableGiB: number };
  };
  capture?: {
    state: "idle" | "recording" | "finalizing" | "failed" | string;
    captureId?: string;
    sessionId?: string;
    name?: string;
    startedAt?: string;
    startedAtDeviceTimeNs?: string;
    elapsedMs?: number;
    storage?: { target?: CaptureStorageTarget | string };
    recoveryRequired?: boolean;
    rebootRequired?: boolean;
    failureReason?: string;
    producerState?: string;
  };
  storage?: DeviceStorageStatus;
  cameraConfiguration?: CameraOrientationStatus;
  wifi?: {
    state: "connected" | "disconnected" | string;
    ssid?: string | null;
    ipAddress?: string | null;
  };
  power?: {
    battery?: {
      available: boolean;
      online?: boolean | null;
      capacityPercent?: number | null;
      voltageUv?: number | null;
      chargingState: "charging" | "full" | "discharging" | "not_charging" | "unknown" | "unavailable" | string;
      chargingStateSource?: "power_supply" | "no_charger_status_route" | "battery_unavailable" | string;
    };
  };
};

export type DeviceSessionRecord = {
  id: string;
  name: string;
  createdAt: string;
  durationMs: number | null;
  sizeBytes: number;
  status: "complete" | "recording" | "failed" | string;
  fileCount: number;
  exportAvailable?: boolean;
  failureReason?: string;
};

export type DeviceCaptureStart = {
  captureId: string;
  sessionId: string;
  state: string;
  startedAtDeviceTimeNs: string;
  elapsedMs?: number;
};

export type DeviceCaptureStop = {
  captureId: string;
  sessionId: string;
  state: string;
  session: DeviceSessionRecord;
};

export type DeviceExportManifest = {
  sessionId: string;
  rangeSupported: boolean;
  totalBytes: number;
  manifestUrl: string;
  files: Array<{ name: string; sizeBytes: number; sha256: string; url: string }>;
};

export type DeviceDownloadResult = {
  sessionId: string;
  path: string;
  bytes: number;
  files: number;
  verifiedFiles: number;
  manifestSaved: boolean;
  manifestFile: string;
  verified: boolean;
};

export type NativePreviewResult = {
  urls: string[];
  labels: string[];
};

export type BleProvisioningDevice = {
  id: string;
  address: string;
  name: string;
  rssi: number;
  paired?: boolean;
  bonded?: boolean;
};

export type NearbyWifiNetwork = {
  ssid: string;
  rssi: number;
  signal?: number;
  security: string;
  secure: boolean;
  connected?: boolean;
};

export type BleWifiScanResponse = {
  state: "completed" | string;
  op: "wifi.scan" | string;
  networks: NearbyWifiNetwork[];
  scannedAt: number;
  truncated?: boolean;
  error?: string;
};

export type CameraCalibrationPair = "pair14" | "pair23";

export type CameraCalibrationRequest = {
  pair: CameraCalibrationPair;
  cameraIds: [string, string];
  urls: [string, string];
  labels: [string, string];
  squaresLong: number;
  squaresWide: number;
  squareSizeMm: number;
};

export type OfflineStorageStatus = {
  target: "internal" | "usb" | string;
  usbAvailable: boolean;
  usbMounted: boolean;
  usbLabel?: string;
  usbUuid?: string;
  usbTotalBytes?: number;
  usbFreeBytes?: number;
};

export type OfflineBleResponse = {
  state: string;
  op?: string;
  error?: string;
  wifi?: {
    state: "idle" | "connecting" | "connected" | "failed" | string;
    ssid?: string;
    ipAddress?: string;
  };
  storage?: OfflineStorageStatus;
  capture?: DeviceRuntimeStatus["capture"];
  captureId?: string;
  sessionId?: string;
  startedAtDeviceTimeNs?: string;
  elapsedMs?: number;
  session?: DeviceSessionRecord;
  sessionCount?: number;
  latestSession?: DeviceSessionRecord | null;
};

type SynCapDevicePlugin = {
  probe(options: { host: string; ports: number[]; paths: string[]; transports: string[] }): Promise<DeviceProbe>;
  getManifest(options: { host: string; port: number }): Promise<NativeDeviceManifest>;
  getStatus(options: { host: string; port: number }): Promise<DeviceRuntimeStatus>;
  getStorage(options: { host: string; port: number }): Promise<DeviceStorageStatus>;
  configureStorage(options: { host: string; port: number; target: CaptureStorageTarget }): Promise<DeviceStorageStatus>;
  getCameraConfiguration(options: { host: string; port: number }): Promise<CameraOrientationStatus>;
  configureCameraOrientation(options: { host: string; port: number; rotationDegrees: CameraRotationDegrees }): Promise<CameraOrientationStatus>;
  startCapture(options: { host: string; port: number; name: string }): Promise<DeviceCaptureStart>;
  stopCapture(options: { host: string; port: number; captureId: string }): Promise<DeviceCaptureStop>;
  listSessions(options: { host: string; port: number }): Promise<{ sessions: DeviceSessionRecord[] }>;
  prepareExport(options: { host: string; port: number; sessionId: string }): Promise<DeviceExportManifest>;
  downloadSession(options: { host: string; port: number; sessionId: string }): Promise<DeviceDownloadResult>;
  scanBle(options: { durationMs: number }): Promise<{ devices: BleProvisioningDevice[] }>;
  scanWifi(): Promise<{ networks: NearbyWifiNetwork[]; fresh?: boolean; scannedAt?: number }>;
  scanWifiBle(options: { address: string }): Promise<BleWifiScanResponse>;
  configureWifiBle(options: {
    address: string;
    ssid: string;
    password: string;
    security: string;
    claimCode: string;
  }): Promise<{ state: string; ssid?: string; ipAddress?: string; error?: string }>;
  offlineBleCommand(options: {
    address: string;
    claimCode: string;
    op: "storage.configure" | "storage.eject" | "capture.start" | "capture.stop" | "device.status";
    backgroundRecovery?: boolean;
    target?: "usb" | "internal";
    name?: string;
    captureId?: string;
  }): Promise<OfflineBleResponse>;
  openPreview(options: { urls: string[]; labels: string[] }): Promise<void>;
  openCalibration(options: CameraCalibrationRequest): Promise<void>;
};

const plugin = registerPlugin<SynCapDevicePlugin>("SynCapDevice");
const activeDeviceProbes = new Map<string, Promise<DeviceProbe>>();

export function isDesktopRuntime() {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export function isDeviceRuntime() {
  return Capacitor.isNativePlatform() || isDesktopRuntime();
}

export function probeDevice(host: string, requestedStreams: Array<number | DeviceProbeRequest>) {
  const streams = requestedStreams.map((stream): DeviceProbeRequest => (
    typeof stream === "number" ? { port: stream, path: "/PRR", transport: "rtsp" } : stream
  ));
  const ports = streams.map((stream) => stream.port);
  const paths = streams.map((stream) => stream.path);
  const transports = streams.map((stream) => stream.transport ?? (stream.url?.startsWith("tcp://") ? "tcp-hevc" : "rtsp"));
  const key = `${host}:${streams.map((stream, index) => `${transports[index]}:${stream.port}${stream.path}`).join(",")}`;
  const active = activeDeviceProbes.get(key);
  if (active) return active;
  const nativeRequest = isDesktopRuntime()
    ? invoke<DeviceProbe>("probe_device", { host, ports })
    : plugin.probe({ host, ports, paths, transports });
  const request = nativeRequest.then((result) => normalizeDeviceProbePayload(result, host, streams));
  activeDeviceProbes.set(key, request);
  const clear = () => {
    if (activeDeviceProbes.get(key) === request) activeDeviceProbes.delete(key);
  };
  request.then(clear, clear);
  return request;
}

export async function probeDeviceWithinDeadline(
  host: string,
  streams: Array<number | DeviceProbeRequest>,
  timeoutMs = 2_800,
) {
  let timeoutId: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<null>((resolve) => {
    timeoutId = setTimeout(() => resolve(null), timeoutMs);
  });
  try {
    return await Promise.race([probeDevice(host, streams), deadline]);
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
  }
}

export function openNativePreview(urls: string[], labels: string[]) {
  if (isDesktopRuntime()) return invoke<NativePreviewResult>("open_preview", { urls, labels });
  return plugin.openPreview({ urls, labels });
}

export function closeNativePreview() {
  if (isDesktopRuntime()) return invoke<void>("close_preview");
  return Promise.resolve();
}

export function openCameraCalibration(options: CameraCalibrationRequest) {
  if (isDesktopRuntime()) {
    return Promise.reject(new Error("棋盘格标定当前先支持 Android 客户端"));
  }
  return plugin.openCalibration(options);
}

export function getDeviceStatus(host: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceRuntimeStatus>("get_device_status", { host, port });
  return plugin.getStatus({ host, port });
}

export function getDeviceManifest(host: string, port = 8080) {
  const request = isDesktopRuntime()
    ? invoke<unknown>("get_device_manifest", { host, port })
    : plugin.getManifest({ host, port });
  return request.then((result) => normalizeDeviceManifestPayload(result, host));
}

export function getDeviceStorage(host: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceStorageStatus>("get_device_storage", { host, port });
  return plugin.getStorage({ host, port });
}

export function configureDeviceStorage(host: string, target: CaptureStorageTarget, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceStorageStatus>("configure_device_storage", { host, port, target });
  return plugin.configureStorage({ host, port, target });
}

export function getDeviceCameraOrientation(host: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<CameraOrientationStatus>("get_camera_configuration", { host, port });
  return plugin.getCameraConfiguration({ host, port });
}

export function configureDeviceCameraOrientation(
  host: string,
  rotationDegrees: CameraRotationDegrees,
  port = 8080,
) {
  if (isDesktopRuntime()) {
    return invoke<CameraOrientationStatus>("configure_camera_orientation", { host, port, rotationDegrees });
  }
  return plugin.configureCameraOrientation({ host, port, rotationDegrees });
}

export function startDeviceCapture(host: string, name: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceCaptureStart>("start_device_capture", { host, port, name });
  return plugin.startCapture({ host, port, name });
}

export function stopDeviceCapture(host: string, captureId: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceCaptureStop>("stop_device_capture", { host, port, captureId });
  return plugin.stopCapture({ host, port, captureId });
}

export function listDeviceSessions(host: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceSessionRecord[]>("list_device_sessions", { host, port });
  return plugin.listSessions({ host, port }).then((result) => result.sessions);
}

export function prepareDeviceExport(host: string, sessionId: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceExportManifest>("prepare_device_export", { host, port, sessionId });
  return plugin.prepareExport({ host, port, sessionId });
}

export function downloadDeviceSession(host: string, sessionId: string, port = 8080) {
  if (isDesktopRuntime()) return invoke<DeviceDownloadResult>("download_device_session", { host, port, sessionId });
  return plugin.downloadSession({ host, port, sessionId });
}

export function scanBleProvisioningDevices(durationMs = 4500) {
  if (isDesktopRuntime()) return invoke<BleProvisioningDevice[]>("scan_ble_devices", { durationMs });
  return plugin.scanBle({ durationMs }).then((result) => result.devices);
}

export function scanNearbyWifiNetworks() {
  if (isDesktopRuntime()) {
    return Promise.reject(new Error("附近 Wi-Fi 列表当前仅支持 Android 客户端"));
  }
  return plugin.scanWifi().then((result) => result.networks);
}

// A headring accepts one provisioning transaction at a time, including status reads.
const bluetoothCommands = new Map<string, Promise<void>>();

function queueBluetoothCommand<T>(address: string, command: () => Promise<T>): Promise<T> {
  const key = address.trim().toUpperCase();
  const previous = bluetoothCommands.get(key) ?? Promise.resolve();
  const result = previous.then(command);
  const settled = result.then(() => {}, () => {});
  bluetoothCommands.set(key, settled);
  void settled.then(() => {
    if (bluetoothCommands.get(key) === settled) bluetoothCommands.delete(key);
  });
  return result;
}

export function scanWifiNetworksOverBle(address: string) {
  if (isDesktopRuntime()) {
    return Promise.reject(new Error("头环侧 Wi-Fi 扫描当前仅支持 Android 客户端"));
  }
  return queueBluetoothCommand(address, () => plugin.scanWifiBle({ address }));
}

export function configureWifiOverBle(
  address: string,
  ssid: string,
  password: string,
  claimCode: string,
  security = "wpa2-psk",
) {
  if (isDesktopRuntime()) {
    return invoke<{ state: string; ssid?: string; ipAddress?: string; error?: string }>("configure_wifi_ble", {
      address, ssid, password, security, claimCode,
    });
  }
  return queueBluetoothCommand(address, () => plugin.configureWifiBle({ address, ssid, password, security, claimCode }));
}

export function configureUsbStorageOverBle(address: string, claimCode: string) {
  if (isDesktopRuntime()) return invoke<OfflineBleResponse>("offline_ble_command", { address, claimCode, op: "storage.configure", target: "usb" });
  return queueBluetoothCommand(address, () => plugin.offlineBleCommand({ address, claimCode, op: "storage.configure", target: "usb" }));
}

export function getOfflineDeviceStatusOverBle(
  address: string,
  claimCode: string,
  options: { backgroundRecovery?: boolean } = {},
) {
  if (isDesktopRuntime()) return invoke<OfflineBleResponse>("offline_ble_command", { address, claimCode, op: "device.status" });
  return queueBluetoothCommand(address, () => plugin.offlineBleCommand({
    address, claimCode, op: "device.status",
    ...(options.backgroundRecovery ? { backgroundRecovery: true } : {}),
  }));
}

export function startOfflineCaptureOverBle(address: string, claimCode: string, name: string) {
  if (isDesktopRuntime()) return invoke<OfflineBleResponse>("offline_ble_command", { address, claimCode, op: "capture.start", name });
  return queueBluetoothCommand(address, () => plugin.offlineBleCommand({ address, claimCode, op: "capture.start", name }));
}

export function stopOfflineCaptureOverBle(address: string, claimCode: string, captureId: string) {
  if (isDesktopRuntime()) return invoke<OfflineBleResponse>("offline_ble_command", { address, claimCode, op: "capture.stop", captureId });
  return queueBluetoothCommand(address, () => plugin.offlineBleCommand({ address, claimCode, op: "capture.stop", captureId }));
}

export function ejectUsbOverBle(address: string, claimCode: string) {
  if (isDesktopRuntime()) return invoke<OfflineBleResponse>("offline_ble_command", { address, claimCode, op: "storage.eject" });
  return queueBluetoothCommand(address, () => plugin.offlineBleCommand({ address, claimCode, op: "storage.eject" }));
}
