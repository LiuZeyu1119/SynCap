import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { App as CapacitorApp } from "@capacitor/app";
import { Capacitor } from "@capacitor/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import * as Dialog from "@radix-ui/react-dialog";
import {
  ActivityLogIcon,
  ArchiveIcon,
  ArrowLeftIcon,
  CameraIcon,
  CheckCircledIcon,
  ChevronDownIcon,
  ChevronRightIcon,
  ClockIcon,
  CubeIcon,
  DashboardIcon,
  DotFilledIcon,
  DownloadIcon,
  EnterFullScreenIcon,
  EyeOpenIcon,
  FileTextIcon,
  GearIcon,
  GlobeIcon,
  LightningBoltIcon,
  LockClosedIcon,
  MagnifyingGlassIcon,
  MobileIcon,
  PlayIcon,
  ReloadIcon,
  RocketIcon,
  SewingPinIcon,
  StopIcon,
} from "@radix-ui/react-icons";
import { BottomSheet, Carousel, KeyboardInput, MobileScroll, useKeyboard } from "./mobile";
import {
  CAMERA_DISPLAY_ORDER,
  cameraMountPosition,
  isCompleteLegacyFourCameraSet,
  orderCamerasForDisplay,
} from "./cameraDisplayOrder";
import { isCameraObservedOnline, isPreviewTransportSupportedByRuntime } from "./deviceDiscovery";
import { captureTimerElapsedMs, observeCaptureTimer, type CaptureTimer } from "./captureTimer";
import {
  listenDesktopBleTelemetry,
  startDesktopBleTelemetry,
  stopDesktopBleTelemetry,
  type DesktopBleTelemetryEvent,
} from "./desktopBle";
import {
  type DeviceManifest,
  type SessionRecord,
} from "./syncap";
import {
  configureDeviceStorage,
  configureDeviceCameraOrientation,
  configureUsbStorageOverBle,
  closeNativePreview,
  configureWifiOverBle,
  downloadDeviceSession,
  ejectUsbOverBle,
  getDeviceManifest,
  getDeviceStatus,
  getOfflineDeviceStatusOverBle,
  isDesktopRuntime,
  isDeviceRuntime,
  listDeviceSessions,
  openCameraCalibration,
  openNativePreview,
  probeDeviceWithinDeadline,
  scanBleProvisioningDevices,
  scanWifiNetworksOverBle,
  startOfflineCaptureOverBle,
  startDeviceCapture,
  stopOfflineCaptureOverBle,
  stopDeviceCapture,
  type BleProvisioningDevice,
  type CaptureStorageTarget,
  type CameraCalibrationPair,
  type CameraOrientationStatus,
  type CameraRotationDegrees,
  type DeviceProbe,
  type DeviceRuntimeStatus,
  type DeviceSessionRecord,
  type DeviceStorageStatus,
  type NativeDeviceManifest,
  type NearbyWifiNetwork,
  type OfflineStorageStatus,
} from "./nativeDevice";

const desktopShell = isDesktopRuntime() || new URLSearchParams(window.location.search).get("shell") === "desktop";

if (Capacitor.isNativePlatform()) {
  document.documentElement.dataset.nativePlatform = Capacitor.getPlatform();
} else if (desktopShell) {
  document.documentElement.dataset.desktopShell = "true";
}

type AppTab = "capture" | "rtsp" | "device" | "data" | "settings";
type CaptureView = "grid" | "monitor" | "ready";
type SheetKind = "devices" | "wifi" | "offline" | "calibration" | "orientation" | "sync" | "export" | null;
type OfflineConnection = {
  address: string;
  name: string;
  claimCode: string;
  storage: OfflineStorageStatus;
};

const lastBleDeviceKey = "syncap.lastBleDevice";
const deviceHostKey = "syncap.deviceHost";
const defaultDeviceHost = "192.168.1.12";
const fixedClaimCode = "123456";
const bleHostRecoveryCooldownMs = 30_000;
const fallbackMinimumCaptureFreeBytes = 512 * 1024 * 1024;
const fallbackCaptureBytesPerSecond = 2_200_000;
const publicRtspTestUrl = "rtsp://9627b0bf2a7b.entrypoint.cloud.wowza.com:1935/app-p5260J38/66abe4b9_stream1";
const hisiRtspUrls = [0, 1, 2, 3].map((index) => `rtsp://192.168.100.100:8554/cam${index}`);

function validDeviceHost(value: string) {
  const parts = value.trim().split(".");
  return parts.length === 4 && parts.every((part) => /^\d{1,3}$/.test(part) && Number(part) <= 255);
}

function readDeviceHost() {
  const saved = window.localStorage.getItem(deviceHostKey)?.trim() ?? "";
  return validDeviceHost(saved) ? saved : defaultDeviceHost;
}

function rememberDeviceHost(value: string) {
  if (!validDeviceHost(value)) return false;
  window.localStorage.setItem(deviceHostKey, value.trim());
  return true;
}

function readLastBleDevice(): BleProvisioningDevice | null {
  try {
    const value = JSON.parse(window.localStorage.getItem(lastBleDeviceKey) ?? "null") as Partial<BleProvisioningDevice> | null;
    if (!value?.address || !value.name) return null;
    return { id: value.address, address: value.address, name: value.name, rssi: -127 };
  } catch {
    return null;
  }
}

function rememberBleDevice(device: BleProvisioningDevice) {
  window.localStorage.setItem(lastBleDeviceKey, JSON.stringify({ address: device.address, name: device.name }));
}

function defaultSessionName() {
  const now = new Date();
  const stamp = [
    now.getFullYear(),
    String(now.getMonth() + 1).padStart(2, "0"),
    String(now.getDate()).padStart(2, "0"),
    String(now.getHours()).padStart(2, "0"),
    String(now.getMinutes()).padStart(2, "0"),
  ].join("");
  return `session_${stamp}`;
}

function fallbackManifest(host: string): DeviceManifest {
  return {
    protocolVersion: "1.0",
    device: {
      id: `configured_${host.replaceAll(".", "_")}`,
      displayName: "待连接头环",
      model: "未连接",
      type: "unknown",
      firmwareVersion: "--",
      ipAddress: host,
    },
    capabilities: [],
    cameras: [554, 555, 556, 557].map((port, index) => ({
      id: `cam${index}`,
      label: `CAM ${index}`,
      direction: cameraMountPosition({ id: `cam${index}` }),
      port,
      previewUrl: `rtsp://${host}:${port}/PRR`,
      previewTransport: "rtsp",
    })),
  };
}

function normalizeManifest(value: NativeDeviceManifest, endpointHost: string): DeviceManifest {
  const usesLegacyPhysicalLayout = isCompleteLegacyFourCameraSet(value.cameras ?? []);
  const cameras = value.cameras?.map((camera, index) => {
    const previewUrl = camera.previewUrl ?? camera.preview?.url;
    const previewTransport = camera.preview?.transport === "tcp-hevc" || previewUrl?.startsWith("tcp://")
      ? "tcp-hevc"
      : camera.preview?.transport ?? "rtsp";
    let parsedPort = NaN;
    let path = previewTransport === "tcp-hevc" ? "" : "/PRR";
    try {
      const parsed = previewUrl ? new URL(previewUrl) : null;
      parsedPort = parsed ? Number(parsed.port) : NaN;
      if (parsed?.protocol === "rtsp:" || parsed?.protocol === "tcp:") path = parsed.pathname || path;
    } catch {
      // Invalid URLs from a device manifest are replaced with the verified endpoint host.
    }
    const port = camera.port ?? (Number.isFinite(parsedPort) && parsedPort > 0 ? parsedPort : 554 + index);
    const id = camera.id ?? `cam${index}`;
    const rawDirection = camera.direction ?? `通道 ${index}`;
    return {
      id,
      label: camera.label ?? `CAM ${index}`,
      direction: cameraMountPosition({
        id: usesLegacyPhysicalLayout ? id : "",
        mountPosition: camera.mountPosition,
        direction: rawDirection,
      }),
      mountPosition: camera.mountPosition,
      port,
      previewUrl: previewTransport === "tcp-hevc"
        ? `tcp://${endpointHost}:${port}${path}`
        : `rtsp://${endpointHost}:${port}${path}`,
      previewTransport,
    };
  });
  const fallback = fallbackManifest(endpointHost);
  return {
    protocolVersion: value.protocolVersion ?? "1.0",
    device: {
      id: value.device?.id ?? fallback.device.id,
      displayName: value.device?.displayName ?? "已连接头环",
      model: value.device?.model ?? "未知型号",
      type: value.device?.type ?? "unknown",
      firmwareVersion: value.device?.firmwareVersion ?? "--",
      ipAddress: endpointHost,
    },
    capabilities: value.capabilities ?? [],
    cameras: cameras?.length ? cameras : fallback.cameras,
  };
}

export default function Prototype() {
  const keyboard = useKeyboard();
  const [deviceHost, setDeviceHost] = useState(readDeviceHost);
  const [tab, setTab] = useState<AppTab>("capture");
  const [view, setView] = useState<CaptureView>(() => {
    const saved = window.localStorage.getItem("syncap.captureView");
    return saved === "grid" || saved === "monitor" || saved === "ready" ? saved : "grid";
  });
  const [manifest, setManifest] = useState<DeviceManifest>(() => fallbackManifest(readDeviceHost()));
  const [online, setOnline] = useState(false);
  const [checkingConnection, setCheckingConnection] = useState(false);
  const [deviceProbe, setDeviceProbe] = useState<DeviceProbe | null>(null);
  const [runtimeStatus, setRuntimeStatus] = useState<DeviceRuntimeStatus | null>(null);
  const [sheet, setSheet] = useState<SheetKind>(null);
  const [selectedCamera, setSelectedCamera] = useState<string>(CAMERA_DISPLAY_ORDER[0]);
  const [recording, setRecording] = useState(false);
  const [captureId, setCaptureId] = useState("");
  const [elapsed, setElapsed] = useState(0);
  const [sessionName, setSessionName] = useState(defaultSessionName);
  const [captureStorageTarget, setCaptureStorageTarget] = useState<CaptureStorageTarget | null>(null);
  const [configuringStorageTarget, setConfiguringStorageTarget] = useState<CaptureStorageTarget | null>(null);
  const [captureBusy, setCaptureBusy] = useState(false);
  const [toast, setToast] = useState("");
  const [sessions, setSessions] = useState<SessionRecord[]>([]);
  const [selectedSession, setSelectedSession] = useState<SessionRecord | null>(null);
  const [offlineConnection, setOfflineConnection] = useState<OfflineConnection | null>(null);
  const [desktopPreview, setDesktopPreview] = useState<{ urls: string[]; labels: string[] } | null>(null);
  const [desktopBle, setDesktopBle] = useState<DesktopBleTelemetryEvent | null>(null);
  const [desktopBleDevice, setDesktopBleDevice] = useState<BleProvisioningDevice | null>(readLastBleDevice);
  const refreshInFlight = useRef(false);
  const previewOpening = useRef(false);
  const captureActionInFlight = useRef(false);
  const captureTimer = useRef<CaptureTimer | null>(null);
  const bleHostRecoveryInFlight = useRef<Promise<void> | null>(null);
  const bleHostRecoveryLastAttempt = useRef({ host: "", startedAt: 0 });
  const deviceHostRef = useRef(deviceHost);
  const sheetRef = useRef<SheetKind>(sheet);
  deviceHostRef.current = deviceHost;
  sheetRef.current = sheet;
  const desktopBleAddress = offlineConnection?.address ?? desktopBleDevice?.address ?? "";

  const updateCaptureElapsed = (
    id: string,
    timing: { elapsedMs?: number; startedAtDeviceTimeNs?: string },
    deviceTimeNs?: string,
  ) => {
    captureTimer.current = observeCaptureTimer(captureTimer.current, id, timing, deviceTimeNs, performance.now());
    setElapsed(Math.floor(captureTimer.current.elapsedMs / 1000));
  };

  const resetCaptureElapsed = () => {
    captureTimer.current = null;
    setElapsed(0);
  };

  const cameraAvailability = useMemo(() => manifest.cameras.map((camera, index) => {
    const deviceCamera = runtimeStatus?.cameras.find((item) => item.id === camera.id)
      ?? runtimeStatus?.cameras[index];
    const samePortStreams = deviceProbe?.streams.filter((item) => item.port === camera.port) ?? [];
    const stream = deviceProbe?.streams.find((item) => item.url === camera.previewUrl)
      ?? (samePortStreams.length === 1 ? samePortStreams[0] : deviceProbe?.streams[index]);
    // The service telemetry and active transport probe update independently.
    // Either positive observation is enough to keep a real stream available;
    // a slow refresh must not make declared cameras disappear.
    return Boolean(online && isCameraObservedOnline(
      deviceCamera?.online,
      stream?.online,
      camera.previewTransport === "tcp-hevc",
    ));
  }), [manifest.cameras, runtimeStatus, deviceProbe, online]);
  const onlineCameraCount = cameraAvailability.filter(Boolean).length;

  const recoverDeviceHostOverBle = (failedHost: string, notify: boolean) => {
    if (sheetRef.current === "wifi" || sheetRef.current === "offline") return false;
    const savedDevice = readLastBleDevice();
    if (!savedDevice || !Capacitor.isNativePlatform() || Capacitor.getPlatform() !== "android") return false;
    if (bleHostRecoveryInFlight.current) return true;
    const now = Date.now();
    const lastAttempt = bleHostRecoveryLastAttempt.current;
    if (lastAttempt.host === failedHost && now - lastAttempt.startedAt < bleHostRecoveryCooldownMs) return false;
    bleHostRecoveryLastAttempt.current = { host: failedHost, startedAt: now };
    const request = (async () => {
      try {
        const result = await getOfflineDeviceStatusOverBle(savedDevice.address, fixedClaimCode, { backgroundRecovery: true });
        if (deviceHostRef.current !== failedHost) return;
        if (result.state === "skipped") {
          if (notify) setToast("没有找到头环，请检查蓝牙配对和网络连接");
          return;
        }
        const wifi = result.wifi;
        const recoveredHost = wifi?.ipAddress?.trim() ?? "";
        if (wifi?.state !== "connected" || !validDeviceHost(recoveredHost) || recoveredHost === failedHost) {
          if (notify) setToast(`未连接 ${failedHost}，蓝牙未返回新的局域网地址`);
          return;
        }
        rememberDeviceHost(recoveredHost);
        setOnline(false);
        setRuntimeStatus(null);
        setDeviceProbe(null);
        setManifest(fallbackManifest(recoveredHost));
        setToast(`已找到头环${wifi.ssid ? ` · ${wifi.ssid}` : ""}`);
        setDeviceHost(recoveredHost);
      } catch (error) {
        console.warn("[SynCap] BLE host recovery failed", error);
        if (notify && deviceHostRef.current === failedHost) {
          setToast("没有找到头环，请检查网络后重试");
        }
      }
    })();
    bleHostRecoveryInFlight.current = request;
    void request.finally(() => {
      if (bleHostRecoveryInFlight.current === request) bleHostRecoveryInFlight.current = null;
    });
    return true;
  };

  const refreshSessions = async () => {
    if (!isDeviceRuntime()) {
      setSessions([]);
      return;
    }
    try {
      const records = await listDeviceSessions(deviceHost);
      setSessions(records.map(toSessionRecord));
    } catch {
      if (!offlineConnection) setSessions([]);
    }
  };

  useEffect(() => {
    if (!isDeviceRuntime()) return;
    void refreshDevice(false);
    const timer = window.setInterval(() => void refreshDevice(false), 4000);
    return () => window.clearInterval(timer);
  }, [deviceHost]);

  useEffect(() => {
    if (!isDesktopRuntime() || desktopBleAddress) return;
    let active = true;
    let retryTimer: number | undefined;
    const scan = async () => {
      setDesktopBle({ state: "connecting", address: "", telemetry: null, error: null });
      try {
        const devices = await scanBleProvisioningDevices(10_000);
        if (!active) return;
        if (devices.length > 0) {
          const device = devices[0];
          rememberBleDevice(device);
          setDesktopBleDevice(device);
          return;
        }
        retryTimer = window.setTimeout(() => void scan(), 2000);
      } catch (error) {
        if (!active) return;
        setDesktopBle({
          state: "error",
          address: "",
          telemetry: null,
          error: error instanceof Error ? error.message : String(error),
        });
        retryTimer = window.setTimeout(() => void scan(), 5000);
      }
    };
    void scan();
    return () => {
      active = false;
      if (retryTimer != null) window.clearTimeout(retryTimer);
    };
  }, [desktopBleAddress]);

  useEffect(() => {
    if (!isDesktopRuntime() || !desktopBleAddress) return;
    let active = true;
    let unlisten: (() => void) | undefined;
    void listenDesktopBleTelemetry((event) => {
      if (!active || event.address !== desktopBleAddress) return;
      setDesktopBle(event);
      if (event.state === "connected" && event.telemetry) {
        setRecording(event.telemetry.recording);
        if (event.telemetry.recording) updateCaptureElapsed(`ble:${event.address}`, event.telemetry);
        else resetCaptureElapsed();
      }
    }).then((remove) => {
      if (!active) {
        remove();
        return;
      }
      unlisten = remove;
      void startDesktopBleTelemetry(desktopBleAddress).catch((error) => {
        setDesktopBle({
          state: "error",
          address: desktopBleAddress,
          telemetry: null,
          error: error instanceof Error ? error.message : String(error),
        });
      });
    });
    return () => {
      active = false;
      unlisten?.();
      void stopDesktopBleTelemetry();
    };
  }, [desktopBleAddress]);

  useEffect(() => {
    if (online) void refreshSessions();
    else if (!offlineConnection) setSessions([]);
  }, [online, offlineConnection]);

  useEffect(() => {
    setCaptureStorageTarget(null);
    setConfiguringStorageTarget(null);
  }, [deviceHost]);

  const refreshDevice = async (notify: boolean) => {
    if (!isDeviceRuntime()) {
      setOnline(false);
      return;
    }
    if (refreshInFlight.current) return;
    refreshInFlight.current = true;
    setCheckingConnection(true);
    const targetHost = deviceHost;

    try {
      const statusRequest = getDeviceStatus(targetHost)
        .then((status) => {
          if (!status.adapter || !Array.isArray(status.cameras) || !status.sync || !status.system) {
            throw new Error("设备返回的状态数据无法读取");
          }
          if (deviceHostRef.current === targetHost) setRuntimeStatus(status);
          return status;
        })
        .catch((error) => {
          console.warn("[SynCap] Device status refresh failed", error);
          return null;
        });
      const rawManifest = await getDeviceManifest(targetHost);
      if (deviceHostRef.current !== targetHost) return;
      if (!rawManifest.protocolVersion || !rawManifest.device?.id || !Array.isArray(rawManifest.cameras)) {
        throw new Error("无法读取设备信息，请更新设备后重试");
      }
      const nextManifest = normalizeManifest(rawManifest, targetHost);
      if (deviceHostRef.current !== targetHost) return;
      setManifest(nextManifest);
      setOnline(true);
      await statusRequest;
      if (deviceHostRef.current !== targetHost) return;
      let result: DeviceProbe | null = null;
      const probeStreams = nextManifest.cameras.map((camera, index) => {
        const port = camera.port ?? 554 + index;
        try {
          const parsed = new URL(camera.previewUrl ?? "");
          const transport = camera.previewTransport ?? (parsed.protocol === "tcp:" ? "tcp-hevc" : "rtsp");
          return {
            port,
            path: parsed.pathname || (transport === "tcp-hevc" ? "" : "/PRR"),
            transport,
            url: camera.previewUrl,
          };
        } catch {
          return { port, path: "/PRR", transport: "rtsp" };
        }
      });
      // Raw qgapp TCP streams allow one consumer. Even a connect-only health
      // probe can take that slot and interrupt the real preview, so their
      // availability comes exclusively from the device service telemetry.
      const streamsToProbe = probeStreams.filter((stream) => stream.transport !== "tcp-hevc");
      try {
        if (streamsToProbe.length > 0) {
          result = await probeDeviceWithinDeadline(targetHost, streamsToProbe);
        }
      } catch (error) {
        console.warn("[SynCap] Camera probe failed", error);
      }
      if (deviceHostRef.current !== targetHost) return;
      if (result) {
        setDeviceProbe((current) => {
          const updatedEndpoints = new Set(result.streams.map((stream) => `${stream.transport}:${stream.port}${stream.path}`));
          const retainedTcpStreams = current?.streams.filter((stream) => (
            stream.transport === "tcp-hevc"
            && !updatedEndpoints.has(`${stream.transport}:${stream.port}${stream.path}`)
          )) ?? [];
          const streams = [...result.streams, ...retainedTcpStreams];
          return {
            ...result,
            streams,
            onlineCount: streams.filter((stream) => stream.online).length,
            reachable: streams.some((stream) => stream.online),
          };
        });
      }
      if (notify) {
        setToast(result
          ? `设备已连接 · ${result.onlineCount}/${nextManifest.cameras.length} 路相机可用`
          : "设备已连接 · 正在检查相机");
      }
    } catch {
      if (deviceHostRef.current !== targetHost) return;
      setOnline(false);
      setRuntimeStatus(null);
      setDeviceProbe(null);
      const recoveringOverBle = recoverDeviceHostOverBle(targetHost, notify);
      if (notify && !recoveringOverBle) {
        setToast("没有找到头环，请确认手机和头环已连接到同一网络");
      }
    } finally {
      refreshInFlight.current = false;
      setCheckingConnection(false);
    }
  };

  const useProvisionedHost = (host: string) => {
    if (!rememberDeviceHost(host)) {
      setToast("设备返回的网络地址无效");
      return;
    }
    const normalized = host.trim();
    setOnline(false);
    setRuntimeStatus(null);
    setDeviceProbe(null);
    setManifest(fallbackManifest(normalized));
    if (normalized === deviceHost) void refreshDevice(true);
    else setDeviceHost(normalized);
  };

  useEffect(() => {
    window.localStorage.setItem("syncap.captureView", view);
  }, [view]);

  useEffect(() => {
    const scroll = document.querySelector<HTMLElement>('[data-testid="mobile-scroll"]');
    if (scroll) scroll.scrollTop = 0;
  }, [tab, view]);

  useEffect(() => {
    if (!recording) return;
    const timer = window.setInterval(() => {
      setElapsed(Math.floor(captureTimerElapsedMs(captureTimer.current, performance.now()) / 1000));
    }, 250);
    return () => window.clearInterval(timer);
  }, [recording]);

  useEffect(() => {
    if (!toast) return;
    const timer = window.setTimeout(() => setToast(""), 2200);
    return () => window.clearTimeout(timer);
  }, [toast]);

  useEffect(() => {
    if (!Capacitor.isNativePlatform()) return;
    let active = true;
    let removeListener: (() => Promise<void>) | undefined;
    void CapacitorApp.addListener("backButton", ({ canGoBack }) => {
      if (sheetRef.current !== null) {
        setSheet(null);
        return;
      }
      if (canGoBack) window.history.back();
      else void CapacitorApp.minimizeApp();
    }).then((handle) => {
      if (!active) void handle.remove();
      else removeListener = () => handle.remove();
    }).catch(() => {
      // Browser tests and non-standard shells may not install the native App plugin.
    });
    return () => {
      active = false;
      if (removeListener) void removeListener();
    };
  }, []);

  useEffect(() => {
    if (sheet !== "sync") return;
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setSheet(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [sheet]);

  const selectCaptureStorage = async (target: CaptureStorageTarget) => {
    if (!online || runtimeStatus == null || recording || captureActionInFlight.current) return;
    const requestedOption = captureStorageOption(runtimeStatus.storage, target);
    setCaptureStorageTarget(null);
    setConfiguringStorageTarget(target);
    try {
      const storage = await configureDeviceStorage(deviceHost, target);
      const option = captureStorageOption(storage, target);
      if (!option.ready) throw new Error(option.reason);
      setRuntimeStatus((current) => current == null ? current : { ...current, storage });
      setCaptureStorageTarget(target);
      setToast(`${option.label}已选定 · ${formatBytes(option.freeBytes ?? 0)} 可用`);
    } catch (error) {
      setToast(captureErrorMessage(
        error,
        `${requestedOption.label}不可用于采集`,
        undefined,
        target === "usb" ? requestedOption.label : undefined,
      ));
    } finally {
      setConfiguringStorageTarget(null);
    }
  };

  const applyCameraOrientation = async (rotationDegrees: CameraRotationDegrees) => {
    if (!online || recording) throw new Error(recording ? "采集中不能修改图像方向" : "设备未连接");
    const configuration = await configureDeviceCameraOrientation(deviceHost, rotationDegrees);
    setRuntimeStatus((current) => current == null ? current : {
      ...current,
      cameraConfiguration: configuration,
    });
    setToast(configuration.state === "active"
      ? `图像方向保持 ${rotationDegrees}°`
      : `正在重启 ${manifest.cameras.length} 路相机 · ${rotationDegrees}° 将统一用于预览、标定和采集`);
    window.setTimeout(() => void refreshDevice(false), configuration.state === "active" ? 0 : 3500);
  };

  useEffect(() => {
    if (desktopBle?.state === "connected" && desktopBle.telemetry) return;
    const activeCapture = runtimeStatus?.capture;
    if (activeCapture?.state === "recording" && activeCapture.captureId) {
      setCaptureId(activeCapture.captureId);
      setRecording(true);
      if (activeCapture.storage?.target === "internal" || activeCapture.storage?.target === "usb") {
        setCaptureStorageTarget(activeCapture.storage.target);
      }
      updateCaptureElapsed(activeCapture.captureId, activeCapture, runtimeStatus?.deviceTimeNs);
      return;
    }
    if (activeCapture && ["idle", "failed"].includes(activeCapture.state) && !captureActionInFlight.current) {
      setCaptureId("");
      setRecording(false);
      resetCaptureElapsed();
    }
  }, [runtimeStatus, desktopBle?.state, desktopBle?.telemetry?.sequence]);

  const handleCapture = async () => {
    if (captureActionInFlight.current) return;
    const actionWasStopping = recording;
    captureActionInFlight.current = true;
    setCaptureBusy(true);
    try {
      if (isDeviceRuntime() && offlineConnection) {
        if (recording) {
          const result = await stopOfflineCaptureOverBle(
            offlineConnection.address,
            offlineConnection.claimCode,
            captureId,
          );
          if (result.state !== "completed" || !result.session) {
            throw new Error(result.error ?? "离线采集停止失败");
          }
          setSessions((current) => [
            toSessionRecord(result.session as DeviceSessionRecord),
            ...current.filter((item) => item.id !== result.session?.id),
          ]);
          setRecording(false);
          resetCaptureElapsed();
          setToast("离线采集完成，数据已安全写入 U 盘");
          return;
        }
        const result = await startOfflineCaptureOverBle(
          offlineConnection.address,
          offlineConnection.claimCode,
          sessionName,
        );
        if (result.state !== "recording" || !result.captureId) {
          throw new Error(result.error ?? "离线采集启动失败");
        }
        setCaptureId(result.captureId);
        updateCaptureElapsed(result.captureId, result);
        setRecording(true);
        setToast("采集已开始，数据正在写入 U 盘");
        return;
      }
      if (!isDeviceRuntime()) throw new Error("真实采集仅在 SynCap Studio 客户端中可用");
      if (!online || runtimeStatus == null) throw new Error("设备服务未连接，不能开始采集");
      if (!manifest.capabilities.includes("capture.device")) throw new Error("当前设备未声明设备端采集能力");
      if (recording) {
        const result = await stopDeviceCapture(deviceHost, captureId);
        if (result.state !== "completed" || !result.session) throw new Error("设备未确认采集已完成");
        setRecording(false);
        setToast("采集完成，可以在数据页面查看或导出");
        resetCaptureElapsed();
        await refreshSessions();
        return;
      }

      if (onlineCameraCount !== manifest.cameras.length) {
        throw new Error(`${manifest.cameras.length} 路相机尚未全部就绪（${onlineCameraCount}/${manifest.cameras.length}），无法开始采集`);
      }
      if (captureStorageTarget == null) throw new Error("请先选择存储位置");
      let storage = runtimeStatus.storage;
      let storageOption = captureStorageOption(storage, captureStorageTarget);
      if (storage?.target !== captureStorageTarget || !storageOption.ready) {
        storage = await configureDeviceStorage(deviceHost, captureStorageTarget);
        storageOption = captureStorageOption(storage, captureStorageTarget);
        setRuntimeStatus((current) => current == null ? current : { ...current, storage });
      }
      if (!storageOption.ready) throw new Error(storageOption.reason);
      if (storage == null) throw new Error("存储状态尚未就绪");
      const result = await startDeviceCapture(deviceHost, sessionName);
      if (result.state !== "recording" || !result.captureId) throw new Error("设备未确认采集已开始");
      setCaptureId(result.captureId);
      updateCaptureElapsed(result.captureId, result);
      setRecording(true);
      setToast(`${manifest.cameras.length} 路相机${manifest.capabilities.includes("sensor.imu") ? "和运动数据" : ""}已开始写入${storageOption.label}`);
    } catch (error) {
      const selectedStorageLabel = captureStorageTarget === "usb"
        ? captureStorageOption(runtimeStatus?.storage, "usb").label
        : undefined;
      const errorMessage = captureErrorMessage(error, "设备端采集操作失败", manifest.cameras.length, selectedStorageLabel);
      setToast(errorMessage);
      if (isDeviceRuntime() && !offlineConnection) {
        try {
          const status = await getDeviceStatus(deviceHost);
          setRuntimeStatus(status);
          const activeCapture = status.capture;
          if (activeCapture?.state === "recording" && activeCapture.captureId) {
            setCaptureId(activeCapture.captureId);
            setRecording(true);
            if (activeCapture.storage?.target === "internal" || activeCapture.storage?.target === "usb") {
              setCaptureStorageTarget(activeCapture.storage.target);
            }
            updateCaptureElapsed(activeCapture.captureId, activeCapture, status.deviceTimeNs);
            setToast(actionWasStopping
              ? "设备仍在采集，请再次停止"
              : "设备已确认开始采集，无需再次点击");
          } else if (activeCapture?.state === "failed") {
            setCaptureId("");
            setRecording(false);
            resetCaptureElapsed();
            await refreshSessions();
            setToast(activeCapture.recoveryRequired
              ? "采集未完整结束；已完成片段可在数据页导出。导出后请重启设备恢复使用"
              : errorMessage);
          } else if (activeCapture?.state !== "finalizing") {
            setCaptureId("");
            setRecording(false);
            resetCaptureElapsed();
            await refreshSessions();
            if (actionWasStopping && !/完整性检查未通过/.test(errorMessage)) {
              setToast("设备已停止采集，数据页面已更新");
            }
          } else {
            setToast("设备正在完成文件写入，请稍候");
          }
        } catch {
          void refreshDevice(false);
        }
      }
    } finally {
      captureActionInFlight.current = false;
      setCaptureBusy(false);
    }
  };

  const handleLivePreview = async (cameraId?: string) => {
    if (previewOpening.current) return;
    if (!isDeviceRuntime()) {
      setToast("实时预览请在 SynCap Studio 客户端中打开");
      return;
    }

    if (!online || runtimeStatus == null) {
      setToast("设备服务未连接，无法打开实时预览");
      return;
    }
    const cameras = orderCamerasForDisplay(manifest.cameras);
    const requestedCameras = cameraId ? cameras.filter((camera) => camera.id === cameraId) : cameras;
    const onlineCameras = requestedCameras.filter((camera) => {
      const protocolIndex = manifest.cameras.findIndex((item) => item.id === camera.id);
      return protocolIndex >= 0 && cameraAvailability[protocolIndex];
    });
    const availableCameras = onlineCameras.filter((camera) => (
      isPreviewTransportSupportedByRuntime(camera.previewTransport, isDesktopRuntime())
    ));
    if (availableCameras.length === 0) {
      setToast(onlineCameras.length > 0 && isDesktopRuntime()
        ? "当前桌面版暂不支持此设备的原始 HEVC 预览，请使用移动端预览"
        : "当前没有可用的相机流");
      return;
    }

    try {
      previewOpening.current = true;
      const result = await openNativePreview(
        availableCameras.map((camera) => {
          const protocolIndex = manifest.cameras.findIndex((item) => item.id === camera.id);
          return camera.previewUrl ?? `rtsp://${manifest.device.ipAddress}:${camera.port ?? 554 + Math.max(0, protocolIndex)}/PRR`;
        }),
        availableCameras.map((camera) => `${camera.label} · ${camera.direction}`),
      );
      if (isDesktopRuntime() && result?.urls.length) setDesktopPreview(result);
    } catch {
      setToast("无法打开实时预览");
    } finally {
      previewOpening.current = false;
    }
  };

  const navigate = (next: AppTab) => {
    (document.activeElement as HTMLElement | null)?.blur();
    keyboard.hide();
    const scroll = document.querySelector<HTMLElement>('[data-testid="mobile-scroll"]');
    if (scroll) scroll.scrollTop = 0;
    setTab(next);
    setSheet(null);
  };

  const changeView = (next: CaptureView) => {
    (document.activeElement as HTMLElement | null)?.blur();
    keyboard.hide();
    const scroll = document.querySelector<HTMLElement>('[data-testid="mobile-scroll"]');
    if (scroll) scroll.scrollTop = 0;
    setView(next);
  };

  return (
    <div className="syncap-app" data-testid="syncap-app">
      <AppHeader
        manifest={manifest}
        online={online || Boolean(offlineConnection) || desktopBle?.state === "connected"}
        recording={recording}
        onOpenDevices={() => setSheet("devices")}
      />
      {desktopShell ? (
        <DesktopBleStatus
          event={desktopBle}
          onConnect={() => setSheet("offline")}
        />
      ) : null}

      <MobileScroll className="syncap-scroll">
        <main className="syncap-content" data-testid={`tab-${tab}`}>
          {tab === "capture" ? (
            <CaptureTab
              manifest={manifest}
              view={view}
              onViewChange={changeView}
              selectedCamera={selectedCamera}
              onCameraChange={setSelectedCamera}
              recording={recording}
              elapsed={elapsed}
              sessionName={sessionName}
              onSessionNameChange={setSessionName}
              onCapture={handleCapture}
              captureBusy={captureBusy}
              storageTarget={captureStorageTarget}
              configuringStorageTarget={configuringStorageTarget}
              onStorageTargetChange={(target) => void selectCaptureStorage(target)}
              online={online}
              onlineCameraCount={onlineCameraCount}
              cameraAvailability={cameraAvailability}
              runtimeStatus={runtimeStatus}
              onLivePreview={handleLivePreview}
              offlineStorage={offlineConnection?.storage ?? null}
            />
          ) : null}
          {tab === "device" ? (
            <DeviceTab
              manifest={manifest}
              online={online}
              runtimeStatus={runtimeStatus}
              onWifi={() => setSheet("wifi")}
              onOffline={() => setSheet("offline")}
              onCalibration={() => setSheet("calibration")}
              onOrientation={() => setSheet("orientation")}
              onSync={() => setSheet("sync")}
              onReconnect={() => void refreshDevice(true)}
              checkingConnection={checkingConnection}
              offlineConnection={offlineConnection}
            />
          ) : null}
          {tab === "rtsp" ? <RtspPreviewTab onToast={setToast} /> : null}
          {tab === "data" ? (
            <DataTab
              sessions={sessions}
              offlineMode={Boolean(offlineConnection)}
              onExport={(session) => {
                setSelectedSession(session);
                setSheet("export");
              }}
            />
          ) : null}
          {tab === "settings" ? (
            <SettingsTab view={view} onViewChange={changeView} manifest={manifest} />
          ) : null}
        </main>
      </MobileScroll>

      <BottomNavigation tab={tab} onChange={navigate} showRtsp={desktopShell} />
      {toast ? <div className="syncap-toast" role="status">{toast}</div> : null}
      {desktopPreview ? (
        <DesktopLivePreview
          urls={desktopPreview.urls}
          labels={desktopPreview.labels}
          onClose={() => {
            setDesktopPreview(null);
            void closeNativePreview();
          }}
        />
      ) : null}

      <AppSheet
        kind={sheet === "sync" ? null : sheet}
        manifest={manifest}
        online={online}
        selectedSession={selectedSession}
        runtimeStatus={runtimeStatus}
        onClose={() => setSheet(null)}
        onReconnect={() => refreshDevice(true)}
        onToast={setToast}
        offlineConnection={offlineConnection}
        onOfflineConnected={setOfflineConnection}
        onOfflineDisconnected={() => setOfflineConnection(null)}
        onDeviceHost={useProvisionedHost}
        recording={recording}
        onCameraOrientation={applyCameraOrientation}
      />
      {sheet === "sync" ? (
        <SyncDiagnosticsPage
          manifest={manifest}
          runtimeStatus={runtimeStatus}
          refreshing={checkingConnection}
          onRefresh={() => refreshDevice(true)}
          onClose={() => setSheet(null)}
        />
      ) : null}
    </div>
  );
}

function DesktopLivePreview({ urls, labels, onClose }: { urls: string[]; labels: string[]; onClose: () => void }) {
  return (
    <div className="desktop-preview-backdrop" role="dialog" aria-modal="true" aria-label="实时预览">
      <section className="desktop-preview-panel">
        <header>
          <div><strong>SynCap · 实时预览</strong><span>{urls.length} 路实时画面</span></div>
          <button onClick={onClose}>关闭预览</button>
        </header>
        <div className="desktop-preview-grid">
          {urls.map((url, index) => (
            <DesktopHlsStream key={url} url={url} label={labels[index] ?? `CAM ${index}`} />
          ))}
        </div>
      </section>
    </div>
  );
}

function DesktopHlsStream({ url, label }: { url: string; label: string }) {
  const video = useRef<HTMLVideoElement>(null);
  const [state, setState] = useState<"connecting" | "playing" | "failed">("connecting");

  useEffect(() => {
    const element = video.current;
    if (!element) return;
    if (element.canPlayType("application/vnd.apple.mpegurl")) {
      element.src = url;
      return () => {
        element.pause();
        element.removeAttribute("src");
        element.load();
      };
    }
    let cancelled = false;
    let player: import("hls.js").default | null = null;
    void import("hls.js").then(({ default: Hls }) => {
      if (cancelled) return;
      if (!Hls.isSupported()) {
        setState("failed");
        return;
      }
      player = new Hls({
        liveSyncDurationCount: 1,
        liveMaxLatencyDurationCount: 3,
        maxLiveSyncPlaybackRate: 1.4,
      });
      player.on(Hls.Events.MANIFEST_PARSED, () => void element.play());
      player.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) setState("failed");
      });
      player.loadSource(url);
      player.attachMedia(element);
    });
    return () => {
      cancelled = true;
      player?.destroy();
    };
  }, [url]);

  return (
    <article className="desktop-preview-stream">
      <video
        ref={video}
        autoPlay
        muted
        playsInline
        onPlaying={() => setState("playing")}
        onWaiting={() => setState("connecting")}
        onError={() => setState("failed")}
      />
      <span>{label}</span>
      <small className={state}>{state === "playing" ? "● 实时" : state === "failed" ? "连接失败" : "连接中"}</small>
    </article>
  );
}

function RtspPreviewTab({ onToast }: { onToast: (message: string) => void }) {
  const [inputs, setInputs] = useState(["", "", "", ""]);
  const [preview, setPreview] = useState<{ urls: string[]; labels: string[] } | null>(null);
  const [sourceUrls, setSourceUrls] = useState<string[]>([]);
  const [focusedStream, setFocusedStream] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => () => {
    void closeNativePreview();
  }, []);

  const updateInput = (index: number, value: string) => {
    setInputs((current) => current.map((item, itemIndex) => itemIndex === index ? value : item));
  };

  const stopPreview = async () => {
    setBusy(true);
    try {
      await closeNativePreview();
      setPreview(null);
      setSourceUrls([]);
      setFocusedStream(null);
    } finally {
      setBusy(false);
    }
  };

  const startPreview = async () => {
    const requested = inputs.map((value) => value.trim()).filter(Boolean);
    if (requested.length === 0) {
      onToast("请至少输入一路 RTSP 地址");
      return;
    }
    const invalid = requested.find((value) => {
      try {
        const parsed = new URL(value);
        return parsed.protocol !== "rtsp:";
      } catch {
        return true;
      }
    });
    if (invalid) {
      onToast("地址格式无效，请输入 rtsp:// 地址");
      return;
    }
    if (!isDesktopRuntime()) {
      onToast("RTSP 解码预览请在 SynCap Studio Mac 客户端中打开");
      return;
    }

    setBusy(true);
    try {
      await closeNativePreview();
      const labels = requested.map((_, index) => `STREAM ${String(index + 1).padStart(2, "0")}`);
      const result = await openNativePreview(requested, labels);
      if (!result?.urls.length) throw new Error("预览桥没有返回可播放地址");
      setSourceUrls(requested);
      setPreview(result);
      setFocusedStream(null);
      onToast(`已连接 ${result.urls.length} 路 RTSP 流`);
    } catch (error) {
      setPreview(null);
      setSourceUrls([]);
      onToast(error instanceof Error
        ? error.message
        : typeof error === "string" && error.trim()
          ? error
          : "RTSP 预览启动失败");
    } finally {
      setBusy(false);
    }
  };

  const visibleStreams = preview == null
    ? []
    : preview.urls.map((url, index) => ({ url, index })).filter((item) => focusedStream == null || item.index === focusedStream);

  return (
    <section className="page rtsp-preview-page" data-testid="rtsp-preview-page">
      <header className="rtsp-page-hero">
        <div>
          <span>DESKTOP LIVE VIEW</span>
          <h1>RTSP 预览</h1>
          <p>输入任意 RTSP 地址，通过本机媒体桥低延迟解码；支持一路聚焦或四路并看。</p>
        </div>
        <div className={preview ? "rtsp-live-pill live" : "rtsp-live-pill"}>
          <DotFilledIcon />
          {preview ? `${preview.urls.length} 路在线` : "等待连接"}
        </div>
      </header>

      <div className={preview ? "rtsp-workspace is-live" : "rtsp-workspace"}>
        <aside className="rtsp-source-panel">
          <div className="rtsp-panel-heading">
            <span><GlobeIcon /></span>
            <div><strong>信号源</strong><small>最多同时预览四路</small></div>
          </div>
          <div className="rtsp-address-list">
            {inputs.map((value, index) => (
              <label className="rtsp-address-row" key={index}>
                <span>{String(index + 1).padStart(2, "0")}</span>
                <KeyboardInput
                  aria-label={`RTSP 地址 ${index + 1}`}
                  data-testid={`rtsp-url-${index}`}
                  inputMode="url"
                  placeholder={index === 0 ? "rtsp://host:8554/cam0" : "可选"}
                  value={value}
                  onChange={(event) => updateInput(index, event.target.value)}
                  disabled={busy || preview != null}
                />
              </label>
            ))}
          </div>
          <div className="rtsp-presets">
            <button type="button" onClick={() => setInputs([publicRtspTestUrl, "", "", ""])} disabled={busy || preview != null} data-testid="rtsp-preset-public">
              <GlobeIcon /><span><strong>公开测试流</strong><small>Wowza 官方循环画面</small></span>
            </button>
            <button type="button" onClick={() => setInputs(hisiRtspUrls)} disabled={busy || preview != null} data-testid="rtsp-preset-hisi">
              <CameraIcon /><span><strong>HiSi 四路</strong><small>192.168.100.100 · cam0—3</small></span>
            </button>
          </div>
          {preview ? (
            <button type="button" className="rtsp-connect-button stop" onClick={() => void stopPreview()} disabled={busy}>
              <StopIcon />{busy ? "正在断开" : "停止预览"}
            </button>
          ) : (
            <button type="button" className="rtsp-connect-button" onClick={() => void startPreview()} disabled={busy} data-testid="rtsp-connect">
              {busy ? <ReloadIcon className="spin" /> : <PlayIcon />}{busy ? "正在建立媒体桥" : "开始预览"}
            </button>
          )}
          <p className="rtsp-privacy-note"><LockClosedIcon />地址只在当前窗口内使用，不保存账号或密码。</p>
        </aside>

        <section className="rtsp-viewer" aria-label="RTSP 画面">
          {preview ? (
            <>
              <header className="rtsp-viewer-toolbar">
                <div><EyeOpenIcon /><span><strong>LIVE MATRIX</strong><small>{focusedStream == null ? "多画面监看" : preview.labels[focusedStream]}</small></span></div>
                {focusedStream != null ? <button type="button" onClick={() => setFocusedStream(null)}><DashboardIcon />返回矩阵</button> : null}
              </header>
              <div className={focusedStream == null ? "rtsp-video-matrix" : "rtsp-video-matrix focused"}>
                {visibleStreams.map(({ url, index }) => (
                  <button className="rtsp-video-cell" type="button" onClick={() => setFocusedStream(focusedStream === index ? null : index)} key={`${url}-${index}`}>
                    <DesktopHlsStream url={url} label={preview.labels[index] ?? `STREAM ${index + 1}`} />
                    <span className="rtsp-source-caption">{sourceUrls[index]}</span>
                    <EnterFullScreenIcon className="rtsp-focus-icon" />
                  </button>
                ))}
              </div>
            </>
          ) : (
            <div className="rtsp-empty-state">
              <div className="rtsp-orbit"><span /><EyeOpenIcon /></div>
              <strong>画面将在这里出现</strong>
              <p>输入一路或四路地址，然后开始预览。</p>
              <div><span>RTSP</span><span>H.264 / H.265</span><span>TCP BRIDGE</span></div>
            </div>
          )}
        </section>
      </div>
    </section>
  );
}

type DesktopWindowAction = "minimize" | "maximize" | "close";

async function runDesktopWindowAction(action: DesktopWindowAction) {
  if (!isDesktopRuntime()) return;
  const window = getCurrentWindow();
  if (action === "minimize") await window.minimize();
  if (action === "maximize") await window.toggleMaximize();
  if (action === "close") await window.close();
}

function DesktopWindowControls() {
  return (
    <div className="desktop-window-controls" aria-label="窗口控制">
      <button type="button" className="minimize" aria-label="最小化" onClick={() => void runDesktopWindowAction("minimize")}><span /></button>
      <button type="button" className="maximize" aria-label="最大化或还原" onClick={() => void runDesktopWindowAction("maximize")}><span /></button>
      <button type="button" className="close" aria-label="关闭窗口" onClick={() => void runDesktopWindowAction("close")}><span /></button>
    </div>
  );
}

function DesktopBleStatus({
  event,
  onConnect,
}: {
  event: DesktopBleTelemetryEvent | null;
  onConnect: () => void;
}) {
  const telemetry = event?.telemetry;
  const connected = event?.state === "connected" && telemetry != null;
  const captureLabel = telemetry?.captureState === "finalizing"
    ? "正在安全封装"
    : telemetry?.recording
      ? `REC ${formatElapsed(Math.floor(telemetry.elapsedMs / 1000))}`
      : telemetry?.preview
        ? "实时预览"
        : "待机";
  const chargingLabel = telemetry?.full
    ? "已充满"
    : telemetry?.charging === true
      ? "充电中"
      : telemetry?.charging === false
        ? "未充电"
        : "充电状态未知";
  return (
    <aside className={telemetry?.recording ? "desktop-ble-status recording" : "desktop-ble-status"} aria-live="polite">
      <div className="desktop-ble-identity">
        <span className={connected ? "ble-link-dot connected" : "ble-link-dot"} />
        <div>
          <strong>{connected ? "HISI 蓝牙已连接" : event?.state === "connecting" ? "正在连接 HISI" : "HISI 蓝牙未连接"}</strong>
          <small>{event?.state === "error" ? event.error ?? "连接失败" : connected ? "实时状态通知 · 0.5 s" : "长按头环按键后连接"}</small>
        </div>
      </div>
      {connected ? (
        <div className="desktop-ble-metrics">
          <span className={telemetry.recording ? "capture recording" : "capture"}><DotFilledIcon />{captureLabel}</span>
          <span><LightningBoltIcon />{telemetry.batteryPercent == null ? "电量 --" : `电量 ${telemetry.batteryPercent}%`}</span>
          <span>{chargingLabel}</span>
          <span>{telemetry.voltageUv == null ? "电压 --" : `${(telemetry.voltageUv / 1_000_000).toFixed(2)} V`}</span>
          <span>{telemetry.sdMounted ? `SD ${telemetry.sdFreeMib} MiB 可用` : "SD 未挂载"}</span>
        </div>
      ) : (
        <button type="button" onClick={onConnect}>连接头环</button>
      )}
    </aside>
  );
}

function AppHeader({
  manifest,
  online,
  recording,
  onOpenDevices,
}: {
  manifest: DeviceManifest;
  online: boolean;
  recording: boolean;
  onOpenDevices: () => void;
}) {
  return (
    <header className="syncap-header">
      <div className="brand-lockup" aria-label="SynCap Studio">
        <img src="/assets/syncap/syncap-mark.png" alt="" draggable={false} />
        <span>SynCap</span>
      </div>
      <div className="syncap-header-actions">
        <button className="device-chip" onClick={onOpenDevices} data-testid="device-selector">
          <DotFilledIcon className={recording ? "dot recording" : online ? "dot healthy" : "dot offline"} />
          <span>{manifest.device.model}</span>
          <ChevronDownIcon />
        </button>
        {desktopShell ? <DesktopWindowControls /> : null}
      </div>
    </header>
  );
}

function CaptureTab({
  manifest,
  view,
  onViewChange,
  selectedCamera,
  onCameraChange,
  recording,
  elapsed,
  sessionName,
  onSessionNameChange,
  onCapture,
  captureBusy,
  storageTarget,
  configuringStorageTarget,
  onStorageTargetChange,
  online,
  onlineCameraCount,
  cameraAvailability,
  runtimeStatus,
  onLivePreview,
  offlineStorage,
}: {
  manifest: DeviceManifest;
  view: CaptureView;
  onViewChange: (view: CaptureView) => void;
  selectedCamera: string;
  onCameraChange: (id: string) => void;
  recording: boolean;
  elapsed: number;
  sessionName: string;
  onSessionNameChange: (name: string) => void;
  onCapture: () => void;
  captureBusy: boolean;
  storageTarget: CaptureStorageTarget | null;
  configuringStorageTarget: CaptureStorageTarget | null;
  onStorageTargetChange: (target: CaptureStorageTarget) => void;
  online: boolean;
  onlineCameraCount: number;
  cameraAvailability: boolean[];
  runtimeStatus: DeviceRuntimeStatus | null;
  onLivePreview: (cameraId?: string) => void;
  offlineStorage: OfflineStorageStatus | null;
}) {
  const displayCameras = useMemo(() => orderCamerasForDisplay(manifest.cameras), [manifest.cameras]);
  const displayCameraAvailability = useMemo(() => displayCameras.map((camera) => {
    const protocolIndex = manifest.cameras.findIndex((item) => item.id === camera.id);
    return protocolIndex >= 0 && Boolean(cameraAvailability[protocolIndex]);
  }), [displayCameras, manifest.cameras, cameraAvailability]);
  const storageOption = storageTarget == null ? null : captureStorageOption(runtimeStatus?.storage, storageTarget);
  const allCamerasReady = manifest.cameras.length > 0 && onlineCameraCount === manifest.cameras.length;
  const captureRecoveryRequired = runtimeStatus?.capture?.recoveryRequired === true;
  const captureEnabled = !captureBusy && (
    recording
    || offlineStorage != null
    || Boolean(
      online
      && !captureRecoveryRequired
      && manifest.capabilities.includes("capture.device")
      && allCamerasReady
      && storageTarget != null
      && storageOption?.ready
      && configuringStorageTarget == null
    )
  );
  const captureBlockedLabel = !online && offlineStorage == null
    ? "设备未连接"
    : captureBusy
      ? recording ? "正在停止采集" : "正在开始采集"
      : captureRecoveryRequired
        ? "请重启设备后再采集"
      : configuringStorageTarget != null
        ? "正在检查存储"
        : !allCamerasReady && offlineStorage == null
          ? `等待相机全部就绪 · ${onlineCameraCount}/${manifest.cameras.length}`
          : storageTarget == null
            ? "请先选择存储位置"
            : storageOption?.reason ?? "存储不可用于采集";
  return (
    <section className="page capture-page">
      <ViewSwitcher value={view} onChange={onViewChange} cameraCount={manifest.cameras.length} />
      {captureRecoveryRequired ? (
        <div className="offline-mode-banner recovery-mode-banner" role="alert" data-testid="capture-recovery-banner">
          <ReloadIcon />
          <span>
            <strong>设备需要重启</strong>
            <small>上次采集未完整结束；已保存片段可在数据页导出，重启设备后可继续采集</small>
          </span>
        </div>
      ) : null}
      {offlineStorage ? (
        <div className="offline-mode-banner">
          <ArchiveIcon />
          <span><strong>离线 U 盘采集</strong><small>{offlineStorage.usbLabel ?? "SYNCAP"} · {offlineStorage.usbFreeBytes == null ? "U 盘已就绪" : `${formatBytes(offlineStorage.usbFreeBytes)} 可用`}</small></span>
          <CheckCircledIcon />
        </div>
      ) : online ? (
        <CaptureStorageSelector
          status={runtimeStatus?.storage}
          selected={storageTarget}
          configuring={configuringStorageTarget}
          disabled={recording || captureBusy}
          onSelect={onStorageTargetChange}
        />
      ) : null}

      {view === "grid" ? (
        <GridCapture
          cameras={displayCameras}
          recording={recording}
          elapsed={elapsed}
          onCapture={onCapture}
          captureEnabled={captureEnabled}
          captureBlockedLabel={captureBlockedLabel}
          storageTarget={storageTarget}
          online={online}
          onlineCameraCount={onlineCameraCount}
          cameraAvailability={displayCameraAvailability}
          runtimeStatus={runtimeStatus}
          onLivePreview={onLivePreview}
        />
      ) : null}
      {view === "monitor" ? (
        <MonitorCapture
          cameras={displayCameras}
          selectedCamera={selectedCamera}
          onCameraChange={onCameraChange}
          recording={recording}
          elapsed={elapsed}
          onCapture={onCapture}
          captureEnabled={captureEnabled}
          captureBlockedLabel={captureBlockedLabel}
          storageTarget={storageTarget}
          cameraAvailability={displayCameraAvailability}
          runtimeStatus={runtimeStatus}
          onLivePreview={onLivePreview}
        />
      ) : null}
      {view === "ready" ? (
        <ReadyCapture
          cameras={displayCameras}
          sessionName={sessionName}
          onSessionNameChange={onSessionNameChange}
          recording={recording}
          elapsed={elapsed}
          onCapture={onCapture}
          online={online}
          onlineCameraCount={onlineCameraCount}
          cameraAvailability={displayCameraAvailability}
          captureEnabled={captureEnabled}
          captureBlockedLabel={captureBlockedLabel}
          storageTarget={storageTarget}
          runtimeStatus={runtimeStatus}
          hasImu={manifest.capabilities.includes("sensor.imu")}
        />
      ) : null}
    </section>
  );
}

function ViewSwitcher({ value, onChange, cameraCount }: { value: CaptureView; onChange: (value: CaptureView) => void; cameraCount: number }) {
  const modes: Array<{ id: CaptureView; label: string; icon: ReactNode }> = [
    { id: "grid", label: `${cameraCount} 路总览`, icon: <DashboardIcon /> },
    { id: "monitor", label: "单路监看", icon: <EyeOpenIcon /> },
    { id: "ready", label: "采集准备", icon: <CheckCircledIcon /> },
  ];

  return (
    <div className="view-switcher" role="tablist" aria-label="采集显示方式">
      {modes.map((mode) => (
        <button
          key={mode.id}
          className={value === mode.id ? "active" : ""}
          onClick={() => onChange(mode.id)}
          role="tab"
          aria-selected={value === mode.id}
          data-testid={`view-${mode.id}`}
        >
          {mode.icon}
          <span>{mode.label}</span>
        </button>
      ))}
    </div>
  );
}

function CaptureStorageSelector({
  status,
  selected,
  configuring,
  disabled,
  onSelect,
}: {
  status: DeviceStorageStatus | undefined;
  selected: CaptureStorageTarget | null;
  configuring: CaptureStorageTarget | null;
  disabled: boolean;
  onSelect: (target: CaptureStorageTarget) => void;
}) {
  const internal = captureStorageOption(status, "internal");
  const usb = captureStorageOption(status, "usb");
  const selectedOption = selected === "internal" ? internal : selected === "usb" ? usb : null;
  const message = configuring != null
    ? `正在检查${configuring === "usb" ? usb.label : internal.label}空间…`
    : selectedOption?.ready
      ? `${selectedOption.label}已就绪，预计可录制 ${selectedOption.estimatedDuration}`
      : selectedOption?.reason ?? "开始采集前必须选择存储位置";

  return (
    <section className="capture-storage-selector" data-testid="capture-storage-selector">
      <header><span><ArchiveIcon />采集存储</span><small>不会自动切换位置</small></header>
      <div className="capture-storage-options" role="group" aria-label="选择采集存储位置">
        {[internal, usb].map((option) => {
          const active = selected === option.target;
          const pending = configuring === option.target;
          return (
            <button
              type="button"
              className={active ? "selected" : ""}
              aria-pressed={active}
              data-testid={`storage-${option.target}`}
              disabled={disabled || configuring != null || !option.selectable}
              onClick={() => onSelect(option.target)}
              key={option.target}
            >
              {option.target === "usb" ? <ArchiveIcon /> : <CubeIcon />}
              <span>
                <strong>{option.label}</strong>
                <small>{pending ? "正在检查…" : !option.ready ? option.reason : option.freeBytes == null ? "等待容量" : `${formatBytes(option.freeBytes)} 可用`}</small>
              </span>
              {active && option.ready ? <CheckCircledIcon /> : <ChevronRightIcon />}
            </button>
          );
        })}
      </div>
      <p className={selectedOption?.ready ? "storage-preflight ready" : "storage-preflight warning"} role="status">
        {selectedOption?.ready ? <CheckCircledIcon /> : <DotFilledIcon />}{message}
      </p>
    </section>
  );
}

function GridCapture({
  cameras,
  recording,
  elapsed,
  onCapture,
  captureEnabled,
  captureBlockedLabel,
  storageTarget,
  online,
  onlineCameraCount,
  cameraAvailability,
  runtimeStatus,
  onLivePreview,
}: {
  cameras: DeviceManifest["cameras"];
  recording: boolean;
  elapsed: number;
  onCapture: () => void;
  captureEnabled: boolean;
  captureBlockedLabel: string;
  storageTarget: CaptureStorageTarget | null;
  online: boolean;
  onlineCameraCount: number;
  cameraAvailability: boolean[];
  runtimeStatus: DeviceRuntimeStatus | null;
  onLivePreview: () => void;
}) {
  const storage = configuredCaptureStorage(runtimeStatus, storageTarget);
  const previewCameraCount = cameras.filter((camera, index) => (
    cameraAvailability[index]
    && isPreviewTransportSupportedByRuntime(camera.previewTransport, isDesktopRuntime())
  )).length;
  const previewUnavailableOnDesktop = onlineCameraCount > 0 && previewCameraCount === 0 && isDesktopRuntime();
  return (
    <div className="capture-mode grid-mode" data-testid="capture-grid">
      <MetricStrip online={online} cameraCount={cameras.length} onlineCameraCount={onlineCameraCount} runtimeStatus={runtimeStatus} />
      <div className={`camera-grid camera-count-${Math.min(4, Math.max(1, cameras.length))}`}>
        {cameras.map((camera, index) => (
          <CameraTile camera={camera} online={cameraAvailability[index]} key={camera.id} compact />
        ))}
      </div>
      <button className="live-preview-button" onClick={() => onLivePreview()} disabled={previewCameraCount === 0}><PlayIcon />{previewUnavailableOnDesktop ? "桌面版暂不支持此设备预览" : previewCameraCount > 0 ? `打开实时预览 · ${previewCameraCount} 路可用` : "没有可用实时流"}</button>
      <div className="storage-summary">
        <div><ClockIcon /><span>预计可录制<strong>{storage.estimatedDuration}</strong></span></div>
        <div><ArchiveIcon /><span>{storage.label}<strong>{storage.freeBytes == null ? "--" : formatBytes(storage.freeBytes)}</strong></span></div>
      </div>
      <CaptureButton recording={recording} elapsed={elapsed} onClick={onCapture} disabled={!captureEnabled} disabledLabel={captureBlockedLabel} />
    </div>
  );
}

function MonitorCapture({
  cameras,
  selectedCamera,
  onCameraChange,
  recording,
  elapsed,
  onCapture,
  captureEnabled,
  captureBlockedLabel,
  storageTarget,
  cameraAvailability,
  runtimeStatus,
  onLivePreview,
}: {
  cameras: DeviceManifest["cameras"];
  selectedCamera: string;
  onCameraChange: (id: string) => void;
  recording: boolean;
  elapsed: number;
  onCapture: () => void;
  captureEnabled: boolean;
  captureBlockedLabel: string;
  storageTarget: CaptureStorageTarget | null;
  cameraAvailability: boolean[];
  runtimeStatus: DeviceRuntimeStatus | null;
  onLivePreview: (cameraId?: string) => void;
}) {
  const camera = cameras.find((item) => item.id === selectedCamera) ?? cameras[0];
  const acquisitionSkew = runtimeStatus?.sync.acquisitionSkewMs.current;
  const clock = runtimeStatus?.clock;
  const selectedIndex = Math.max(0, cameras.findIndex((item) => item.id === camera.id));
  const cameraOnline = cameraAvailability[selectedIndex];
  const previewSupported = isPreviewTransportSupportedByRuntime(camera.previewTransport, isDesktopRuntime());
  const storage = configuredCaptureStorage(runtimeStatus, storageTarget);
  return (
    <div className="capture-mode monitor-mode" data-testid="capture-monitor">
      <div className="sync-ribbon"><ActivityLogIcon />相机同步误差 <strong>{acquisitionSkew == null ? "--" : `${acquisitionSkew.toFixed(3)} ms`}</strong><span className={`clock-state ${clockTone(clock)}`}>{clockSummary(clock)}</span></div>
      <div className="monitor-heading"><span><DotFilledIcon className={`dot ${cameraOnline ? "healthy" : "offline"}`} />{camera.label} · {camera.direction}</span><EnterFullScreenIcon /></div>
      <div className={`hero-feed feed-state ${cameraOnline ? "available" : "offline"}`}>
        <CameraIcon />
        <strong>{cameraOnline ? "画面已就绪" : "画面未连接"}</strong>
        <span>{cameraOnline ? "点击下方按钮打开实时画面" : "连接头环后才会显示画面"}</span>
      </div>
      <Carousel ariaLabel="切换相机" className="camera-carousel" contentClassName="camera-carousel-track">
        {cameras.map((item, index) => (
          <button
            className={item.id === camera.id ? "camera-thumb active" : "camera-thumb"}
            key={item.id}
            onClick={() => onCameraChange(item.id)}
            aria-label={`${item.label} · ${item.direction}`}
          >
            <CameraIcon />
            <span>{item.direction}<small>{cameraAvailability[index] ? "在线" : "离线"}</small></span>
          </button>
        ))}
      </Carousel>
      <button className="live-preview-button" onClick={() => onLivePreview(camera.id)} disabled={!cameraOnline || !previewSupported}><PlayIcon />{cameraOnline && !previewSupported ? "桌面版暂不支持此设备预览" : cameraOnline ? `打开 ${camera.label} 实时监看` : `${camera.label} 当前不可用`}</button>
      <div className="preflight-row"><CheckCircledIcon />采集前检查 · <strong>{cameraOnline ? "相机同步已就绪" : "相机未连接"}</strong></div>
      <div className="monitor-controls">
        <div><span>{storage.label}</span><strong>{storage.freeBytes == null ? "--" : formatBytes(storage.freeBytes)}</strong></div>
        <button className={recording ? "record-orb recording" : "record-orb"} onClick={onCapture} disabled={!captureEnabled} data-testid="monitor-record">
          {recording ? <StopIcon /> : <DotFilledIcon />}
          <span>{recording ? captureEnabled ? formatElapsed(elapsed) : captureBlockedLabel : captureEnabled ? "开始采集" : captureBlockedLabel}</span>
        </button>
        <div className="icon-action readonly"><ActivityLogIcon /><span>{cameraOnline ? "遥测" : "离线"}</span></div>
      </div>
    </div>
  );
}

function ReadyCapture({
  cameras,
  sessionName,
  onSessionNameChange,
  recording,
  elapsed,
  onCapture,
  online,
  onlineCameraCount,
  cameraAvailability,
  captureEnabled,
  captureBlockedLabel,
  storageTarget,
  runtimeStatus,
  hasImu,
}: {
  cameras: DeviceManifest["cameras"];
  sessionName: string;
  onSessionNameChange: (name: string) => void;
  recording: boolean;
  elapsed: number;
  onCapture: () => void;
  online: boolean;
  onlineCameraCount: number;
  cameraAvailability: boolean[];
  captureEnabled: boolean;
  captureBlockedLabel: string;
  storageTarget: CaptureStorageTarget | null;
  runtimeStatus: DeviceRuntimeStatus | null;
  hasImu: boolean;
}) {
  const clock = runtimeStatus?.clock;
  const network = deviceNetworkSummary(online, runtimeStatus);
  const cameraFps = runtimeStatus?.cameras.map((camera) => camera.fps).filter((fps): fps is number => fps != null) ?? [];
  const averageFps = cameraFps.length ? cameraFps.reduce((sum, fps) => sum + fps, 0) / cameraFps.length : null;
  const localReady = online && onlineCameraCount === cameras.length;
  const externalClock = externalClockStatus(clock);
  const externalClockReady = externalClock === "connected";
  const storage = configuredCaptureStorage(runtimeStatus, storageTarget);
  const checks: Array<{ label: string; value: string; icon: ReactNode; status: "healthy" | "warning" | "optional" }> = [
    { label: "网络连接", value: online ? `${network.label} · ${network.detail}` : network.detail, icon: <GlobeIcon />, status: online ? "healthy" : "warning" },
    {
      label: "外部时间同步",
      value: externalClockReady
        ? "已连接"
        : externalClock === "connecting"
          ? "正在连接（可选）"
          : externalClock === "needs-attention"
            ? "需要检查（不影响本机采集）"
            : "未连接（可选）",
      icon: <ClockIcon />,
      status: externalClockReady ? "healthy" : externalClock === "needs-attention" ? "warning" : "optional",
    },
    { label: "相机", value: `${cameras.length} 路 · ${onlineCameraCount} 实时${averageFps == null ? "" : ` · ${averageFps.toFixed(1)} fps`}`, icon: <CameraIcon />, status: onlineCameraCount === cameras.length ? "healthy" : "warning" },
  ];
  if (hasImu) checks.push({ label: "运动传感器", value: "采集时自动记录", icon: <ActivityLogIcon />, status: "healthy" });
  checks.push({ label: "存储", value: storage.freeBytes == null ? "请选择可用存储" : `${storage.label} · ${formatBytes(storage.freeBytes)}`, icon: <ArchiveIcon />, status: storage.freeBytes == null ? "warning" : "healthy" });

  return (
    <div className="capture-mode ready-mode" data-testid="capture-ready">
      <div className={`ready-hero ${localReady ? "healthy" : "warning"}`}>
        {localReady ? <CheckCircledIcon /> : <ClockIcon />}
        <div><h2>{recording ? "正在同步采集" : localReady ? "可以开始采集" : "设备尚未就绪"}</h2><p>{recording ? formatElapsed(elapsed) : localReady ? `${cameras.length} 路相机内部同步已就绪` : `请等待 ${cameras.length} 路相机全部连接`}</p></div>
      </div>
      <div className={`camera-contact-sheet camera-count-${Math.min(4, Math.max(1, cameras.length))}`}>
        {cameras.map((camera, index) => (
          <div className={cameraAvailability[index] ? "available" : "offline"} key={camera.id}>
            <span><DotFilledIcon />{camera.label} · {camera.direction}</span>
            <CameraIcon />
            <small>{cameraAvailability[index] ? "已就绪" : "未连接"}</small>
          </div>
        ))}
      </div>
      <div className="readiness-list">
        {checks.map((check) => (
          <div className={`readiness-row ${check.status}`} key={check.label}>
            <span className="readiness-icon">{check.icon}</span>
            <span className="readiness-label">{check.label}</span>
            <strong>{check.value}</strong>
            {check.status === "healthy" ? <CheckCircledIcon className="check" /> : <DotFilledIcon className={check.status === "optional" ? "optional" : "pending"} />}
          </div>
        ))}
      </div>
      <label className="session-field" htmlFor="session-name">
        <KeyboardInput id="session-name" value={sessionName} onChange={(event) => onSessionNameChange(event.target.value)} />
        <SewingPinIcon />
      </label>
      <CaptureButton recording={recording} elapsed={elapsed} onClick={onCapture} disabled={!captureEnabled} disabledLabel={captureBlockedLabel} />
    </div>
  );
}

function MetricStrip({
  online,
  cameraCount,
  onlineCameraCount,
  runtimeStatus,
}: {
  online: boolean;
  cameraCount: number;
  onlineCameraCount: number;
  runtimeStatus: DeviceRuntimeStatus | null;
}) {
  const syncSkew = runtimeStatus?.sync.acquisitionSkewMs.current;
  const network = deviceNetworkSummary(online, runtimeStatus);
  return (
    <div className="metric-strip">
      <div><span>同步</span><strong>{syncSkew == null ? "--" : `${syncSkew.toFixed(3)} ms`}</strong><small>{syncSkew == null ? runtimeStatus?.sync.method === "hardware_trigger" ? "误差未测量" : "等待遥测" : "实时帧组"}</small></div>
      <div><span>相机</span><strong>{online ? `${cameraCount} 路` : "--"}</strong><small>{!online ? "设备未连接" : cameraCount > 0 && onlineCameraCount === cameraCount ? "全部可用" : `${onlineCameraCount}/${cameraCount} 路可用`}</small></div>
      <div><span>网络</span><strong>{network.label}</strong><small>{network.detail}</small></div>
    </div>
  );
}

function CameraTile({ camera, online, compact = false }: { camera: DeviceManifest["cameras"][number]; online: boolean; compact?: boolean }) {
  return (
    <article className={`${compact ? "camera-tile compact" : "camera-tile"} ${online ? "available" : "offline"}`}>
      <header><span><DotFilledIcon className={`dot ${online ? "healthy" : "offline"}`} />{camera.label} · {camera.direction}</span><EnterFullScreenIcon /></header>
      <div className="camera-tile-state"><CameraIcon /><strong>{online ? "已就绪" : "未连接"}</strong><small>{online ? "打开实时预览" : "连接头环后显示画面"}</small></div>
    </article>
  );
}

function CaptureButton({
  recording,
  elapsed,
  onClick,
  disabled = false,
  disabledLabel = "设备未连接",
}: {
  recording: boolean;
  elapsed: number;
  onClick: () => void;
  disabled?: boolean;
  disabledLabel?: string;
}) {
  return (
    <button className={recording ? "capture-button recording" : "capture-button"} onClick={onClick} disabled={disabled} data-testid="capture-button">
      {recording ? <StopIcon /> : <DotFilledIcon />}
      <span>{recording ? disabled ? disabledLabel : `停止采集 · ${formatElapsed(elapsed)}` : disabled ? disabledLabel : "开始采集"}</span>
    </button>
  );
}

function DeviceTab({
  manifest,
  online,
  runtimeStatus,
  onWifi,
  onOffline,
  onCalibration,
  onOrientation,
  onSync,
  onReconnect,
  checkingConnection,
  offlineConnection,
}: {
  manifest: DeviceManifest;
  online: boolean;
  runtimeStatus: DeviceRuntimeStatus | null;
  onWifi: () => void;
  onOffline: () => void;
  onCalibration: () => void;
  onOrientation: () => void;
  onSync: () => void;
  onReconnect: () => void;
  checkingConnection: boolean;
  offlineConnection: OfflineConnection | null;
}) {
  const displayCameras = orderCamerasForDisplay(manifest.cameras);
  const cameraCount = manifest.cameras.length;
  const sync = runtimeStatus?.sync.acquisitionSkewMs;
  const clock = runtimeStatus?.clock;
  const network = deviceNetworkSummary(online, runtimeStatus);
  const battery = runtimeStatus?.power?.battery;
  const deviceConnected = online || Boolean(offlineConnection);
  const activeStorageTarget = runtimeStatus?.storage?.target === "internal" || runtimeStatus?.storage?.target === "usb"
    ? runtimeStatus.storage.target
    : null;
  const activeStorage = configuredCaptureStorage(runtimeStatus, activeStorageTarget);
  const activeStorageVolume = activeStorageTarget == null ? undefined : runtimeStatus?.storage?.[activeStorageTarget];
  const activeStorageState = activeStorageTarget == null ? null
    : activeStorageVolume == null ? "未就绪"
      : activeStorageTarget === "usb" && !activeStorageVolume.available ? "未插入"
        : activeStorageTarget === "usb" && !activeStorageVolume.mounted ? "未就绪"
          : activeStorageVolume.canCapture === false
            ? activeStorageVolume.reason === "insufficient_free_space" ? "空间不足" : "未就绪"
            : null;
  const externalStorageName = storageVolumeDisplayName(runtimeStatus?.storage?.usb, "usb");
  const usesLegacyCalibrationPairs = isCompleteLegacyFourCameraSet(manifest.cameras);
  const hasRawCalibration = manifest.capabilities.includes("calibration.raw_snapshot");
  const calibrationReady = online
    && usesLegacyCalibrationPairs
    && hasRawCalibration;
  const orientationReady = online && manifest.capabilities.includes("camera.orientation");
  const syncReady = online && manifest.capabilities.includes("sync.metrics");
  const bluetoothProvisioning = manifest.capabilities.some((capability) =>
    capability === "provisioning.ble" || capability === "provisioning.bluetooth");
  const offlineReady = Boolean(offlineConnection)
    || !online
    || (manifest.capabilities.includes("storage.usb")
      && bluetoothProvisioning);
  const wifiReady = !online
    || (manifest.capabilities.includes("network.wifi")
      && bluetoothProvisioning);
  const rotation = runtimeStatus?.cameraConfiguration?.rotationDegrees ?? 0;
  const actions: Array<{ label: string; detail: string; icon: ReactNode; action: () => void; disabled?: boolean }> = [
    {
      label: "离线 U 盘采集",
      detail: offlineConnection
        ? `${offlineConnection.storage.usbLabel ?? "SYNCAP"} · ${offlineConnection.storage.usbFreeBytes == null ? "已就绪" : `${formatBytes(offlineConnection.storage.usbFreeBytes)} 可用`}`
        : online && !offlineReady
          ? "当前设备暂不支持离线 U 盘控制"
          : "没有网络时也可将数据保存到 U 盘",
      icon: <ArchiveIcon />,
      action: onOffline,
      disabled: !offlineReady,
    },
    {
      label: "网络设置",
      detail: online && !wifiReady
        ? "当前设备暂不支持 App 配网"
        : "连接普通 Wi-Fi 或手机热点",
      icon: <GlobeIcon />,
      action: onWifi,
      disabled: !wifiReady,
    },
    {
      label: "相机位置标定",
      detail: !usesLegacyCalibrationPairs && !hasRawCalibration
        ? "当前设备暂不支持全分辨率标定"
        : !online
          ? "连接头环后可使用棋盘格标定"
          : !hasRawCalibration
            ? "请先更新头环相机服务"
            : !usesLegacyCalibrationPairs
              ? "当前设备未声明可标定相机组"
              : "棋盘格 · 1–4 / 2–3 两组",
      icon: <SewingPinIcon />,
      action: onCalibration,
      disabled: !calibrationReady,
    },
    {
      label: "图像方向",
      detail: !online
        ? "连接头环后可设置"
        : !manifest.capabilities.includes("camera.orientation")
          ? "当前设备暂不支持"
          : `${rotation}° · 预览、标定和采集保持一致`,
      icon: <ReloadIcon />,
      action: onOrientation,
      disabled: !orientationReady,
    },
    {
      label: "同步状态",
      detail: !online
        ? `连接头环后查看 ${cameraCount} 路相机同步状态`
        : !manifest.capabilities.includes("sync.metrics")
          ? "当前设备暂不支持"
          : sync?.p95 == null
            ? runtimeStatus?.sync.method === "hardware_trigger" ? "时间误差未测量" : "等待同步数据"
            : `稳定误差 ${sync.p95.toFixed(3)} ms`,
      icon: <ActivityLogIcon />,
      action: onSync,
      disabled: !syncReady,
    },
  ];

  return (
    <section className="page device-page">
      <PageTitle eyebrow="设备" title={manifest.device.displayName} tone={deviceConnected ? "healthy" : "offline"} />
      <div className={`device-hero-card ${deviceConnected ? "connected" : "disconnected"}`}>
        <img src="/assets/syncap/syncap-mark.png" alt="" draggable={false} />
        <div><span>{manifest.device.model} · {deviceTypeLabel(manifest.device.type)}</span><strong>{online ? `${network.label} 在线` : offlineConnection ? "离线采集已连接" : "离线"}</strong><small>{offlineConnection && !online ? "数据将保存到 U 盘" : online ? network.detail : "请连接网络或使用离线采集"}</small></div>
        <button onClick={onReconnect} disabled={checkingConnection} aria-label="重新检测设备"><ReloadIcon className={checkingConnection ? "spin" : ""} /></button>
      </div>
      <div className="device-vitals">
        <div><span>温度</span><strong>{runtimeStatus?.system.temperatureC == null ? "--" : `${runtimeStatus.system.temperatureC.toFixed(1)}°C`}</strong></div>
        <div data-testid="device-storage-status"><span>{offlineConnection ? "U 盘可用" : activeStorageTarget == null ? "采集存储" : activeStorageState ? activeStorage.label : `${activeStorage.label}可用`}</span><strong>{offlineConnection?.storage.usbFreeBytes != null ? formatBytes(offlineConnection.storage.usbFreeBytes) : activeStorageState ?? (activeStorage.freeBytes == null ? "--" : formatBytes(activeStorage.freeBytes))}</strong></div>
        <div><span>固件</span><strong>{manifest.device.firmwareVersion}</strong></div>
      </div>
      {online && battery ? <div className="device-power-status" data-testid="device-power-status">
        <div><span>电量</span><strong>{batteryLevelLabel(battery)}</strong><small>{battery.voltageUv == null ? "电压不可用" : `${(battery.voltageUv / 1_000_000).toFixed(2)} V`}</small></div>
        <div><span>充电状态</span><strong>{chargingStateLabel(battery.chargingState)}</strong><small>{battery.chargingStateSource === "no_charger_status_route" ? "硬件状态脚未接入" : battery.available ? "来自电源管理接口" : "电量计不可用"}</small></div>
      </div> : null}
      <SectionHeading title="设备状态" detail={online ? `${manifest.cameras.length} 路相机` : "等待连接"} />
      {online ? <div className="capability-strip">
        {manifest.capabilities.includes("camera.preview") ? <span><CameraIcon />{manifest.cameras.length} 路相机</span> : null}
        {manifest.capabilities.includes("sensor.imu") ? <span><ActivityLogIcon />运动传感器</span> : null}
        {manifest.capabilities.some((capability) => capability === "storage.usb" || capability === "storage.sd") ? <span><ArchiveIcon />{externalStorageName}采集</span> : null}
        {manifest.capabilities.includes("power.battery") ? <span><LightningBoltIcon />电源状态</span> : null}
      </div> : <div className="capability-empty">连接成功后显示相机、存储和传感器状态</div>}
      <SectionHeading title="设备设置" />
      <div className="action-list">
        {actions.map((item) => (
          <button key={item.label} onClick={item.action} disabled={item.disabled}>
            <span className="action-icon">{item.icon}</span>
            <span><strong>{item.label}</strong><small>{item.detail}</small></span>
            <ChevronRightIcon />
          </button>
        ))}
      </div>
      <SectionHeading title={`${cameraCount} 路相机同步`} detail="最近 10 秒" />
      <div className="sync-detail">
        <div><span>当前误差</span><strong>{sync?.current == null ? "--" : `${sync.current.toFixed(3)} ms`}</strong></div>
        <div><span>稳定误差</span><strong>{sync?.p95 == null ? "--" : `${sync.p95.toFixed(3)} ms`}</strong></div>
        <div><span>检测次数</span><strong>{sync?.samples ?? 0}</strong></div>
        <div className={`camera-deltas camera-count-${Math.min(4, Math.max(1, cameraCount))}`}>{displayCameras.flatMap((camera) => {
          const protocolIndex = manifest.cameras.findIndex((item) => item.id === camera.id);
          const status = runtimeStatus?.cameras.find((item) => item.id === camera.id)
            ?? (protocolIndex >= 0 ? runtimeStatus?.cameras[protocolIndex] : undefined);
          return [
            <span key={`${camera.id}-label`}>{camera.label} · {camera.direction}</span>,
            <b key={`${camera.id}-value`}>{status?.groupSkewMs == null ? "--" : `${status.groupSkewMs.toFixed(3)} ms`}</b>,
          ];
        })}</div>
      </div>
    </section>
  );
}

function DataTab({ sessions, offlineMode, onExport }: { sessions: SessionRecord[]; offlineMode: boolean; onExport: (session: SessionRecord) => void }) {
  const totalBytes = sessions.reduce((sum, session) => sum + (session.sizeBytes ?? 0), 0);
  return (
    <section className="page data-page">
      <PageTitle eyebrow="数据" title="采集会话" />
      <div className="data-summary">
        <div><strong>{sessions.length}</strong><span>本机可见会话</span></div>
        <div><strong>{formatBytes(totalBytes)}</strong><span>{offlineMode ? "U 盘会话" : "设备端数据"}</span></div>
      </div>
      <SectionHeading title="最近采集" detail="设备端" />
      <div className="session-list">
        {sessions.map((session) => {
          const partialExportAvailable = session.exportAvailable !== false && session.status === "failed" && (session.fileCount ?? 0) > 0;
          const canExport = session.exportAvailable !== false && (session.status === "complete" || partialExportAvailable);
          const unavailableReason = session.exportAvailable === false
            ? session.failureReason === "session.metadata_corrupt" ? "清单损坏"
              : session.failureReason === "session.data_corrupt" ? "文件损坏" : "暂不可导出"
            : null;
          return <article key={session.id}>
            <span className="session-icon"><FileTextIcon /></span>
            <div><strong>{session.name}</strong><small>{session.createdAt} · {session.duration} · {session.size}</small><span>{session.status === "complete" && !unavailableReason ? <CheckCircledIcon /> : <ClockIcon />}{unavailableReason ?? (session.status === "complete" ? "清单与校验完成" : session.status === "recording" ? "正在设备端采集" : partialExportAvailable ? "采集未完成 · 可导出已保存文件" : "采集未完成")}</span></div>
            <button onClick={() => onExport(session)} disabled={!canExport || offlineMode} aria-label={`导出 ${session.name}`}><DownloadIcon /></button>
          </article>;
        })}
        {sessions.length === 0 ? <div className="session-empty"><ArchiveIcon /><strong>还没有采集数据</strong><span>{offlineMode ? "U 盘会话不经手机读取；安全弹出后直接访问 U 盘" : "连接设备服务后会从设备端加载会话清单"}</span></div> : null}
      </div>
      <div className="export-note"><LockClosedIcon /><span>{offlineMode ? "数据已在 U 盘；请到设备页安全弹出后直接读取" : "导出中断后可以继续，完成时会自动检查文件"}</span></div>
    </section>
  );
}

function SettingsTab({ view, onViewChange, manifest }: { view: CaptureView; onViewChange: (view: CaptureView) => void; manifest: DeviceManifest }) {
  return (
    <section className="page settings-page">
      <PageTitle eyebrow="设置" title="SynCap Studio" />
      <SectionHeading title="默认采集视图" detail="已自动保存" />
      <ViewSwitcher value={view} onChange={onViewChange} cameraCount={manifest.cameras.length} />
      <SectionHeading title="关于" />
      <div className="protocol-card">
        <div><RocketIcon /><span>App 版本<strong>SynCap Studio 1.0</strong></span></div>
        <div><GlobeIcon /><span>设备连接<strong>支持普通 Wi-Fi、手机热点和离线采集</strong></span></div>
        <div><LockClosedIcon /><span>隐私与安全<strong>网络密码只用于连接，不会保存在 App 中</strong></span></div>
      </div>
      <p className="settings-footnote">相机标定、图像方向和同步状态等入口会一直显示；暂不可用时会直接说明原因。</p>
    </section>
  );
}

function AppSheet({
  kind,
  manifest,
  online,
  selectedSession,
  runtimeStatus,
  onClose,
  onReconnect,
  onToast,
  offlineConnection,
  onOfflineConnected,
  onOfflineDisconnected,
  onDeviceHost,
  recording,
  onCameraOrientation,
}: {
  kind: SheetKind;
  manifest: DeviceManifest;
  online: boolean;
  selectedSession: SessionRecord | null;
  runtimeStatus: DeviceRuntimeStatus | null;
  onClose: () => void;
  onReconnect: () => Promise<void>;
  onToast: (message: string) => void;
  offlineConnection: OfflineConnection | null;
  onOfflineConnected: (connection: OfflineConnection) => void;
  onOfflineDisconnected: () => void;
  onDeviceHost: (host: string) => void;
  recording: boolean;
  onCameraOrientation: (rotationDegrees: CameraRotationDegrees) => Promise<void>;
}) {
  const meta = sheetMeta(kind, selectedSession);
  return (
    <BottomSheet
      open={kind !== null}
      onOpenChange={(open) => !open && onClose()}
      title={meta.title}
      description={meta.description}
      snap={meta.snap}
      contentClassName={kind === "wifi"
        ? "wifi-sheet-content"
        : kind === "calibration"
          ? "calibration-sheet-content"
          : undefined}
    >
      {kind === "devices" ? <DeviceSheet manifest={manifest} online={online} offline={Boolean(offlineConnection)} onReconnect={onReconnect} /> : null}
      {kind === "wifi" ? <WifiSheet onClose={onClose} onToast={onToast} onDeviceHost={onDeviceHost} /> : null}
      {kind === "offline" ? (
        <OfflineStorageSheet
          connection={offlineConnection}
          onConnected={onOfflineConnected}
          onDisconnected={onOfflineDisconnected}
          onClose={onClose}
          onToast={onToast}
        />
      ) : null}
      {kind === "calibration" ? (
        <CalibrationSheet
          manifest={manifest}
          online={online}
          onClose={onClose}
          onToast={onToast}
        />
      ) : null}
      {kind === "orientation" ? (
        <CameraOrientationSheet
          current={runtimeStatus?.cameraConfiguration}
          cameraCount={manifest.cameras.length}
          recording={recording}
          onApply={onCameraOrientation}
          onClose={onClose}
          onToast={onToast}
        />
      ) : null}
      {kind === "export" && selectedSession ? <ExportSheet session={selectedSession} host={manifest.device.ipAddress} cameraCount={manifest.cameras.length} hasImu={manifest.capabilities.includes("sensor.imu")} onClose={onClose} onToast={onToast} /> : null}
    </BottomSheet>
  );
}

function CameraOrientationSheet({
  current,
  cameraCount,
  recording,
  onApply,
  onClose,
  onToast,
}: {
  current: CameraOrientationStatus | undefined;
  cameraCount: number;
  recording: boolean;
  onApply: (rotationDegrees: CameraRotationDegrees) => Promise<void>;
  onClose: () => void;
  onToast: (message: string) => void;
}) {
  const currentRotation = current?.rotationDegrees ?? 0;
  const [selected, setSelected] = useState<CameraRotationDegrees>(currentRotation);
  const [busy, setBusy] = useState(false);
  const options: Array<{ degrees: CameraRotationDegrees; label: string; detail: string }> = [
    { degrees: 0, label: "标准方向", detail: "1280 × 1088 · 60 fps" },
    { degrees: 90, label: "顺时针 90°", detail: "1088 × 1280 · 60 fps" },
    { degrees: 180, label: "旋转 180°", detail: "1280 × 1088 · 60 fps" },
    { degrees: 270, label: "逆时针 90°", detail: "1088 × 1280 · 60 fps" },
  ];

  const apply = async () => {
    if (busy || recording || selected === currentRotation) return;
    setBusy(true);
    try {
      await onApply(selected);
      onClose();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "无法修改图像方向");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="orientation-sheet" data-testid="orientation-sheet">
      <div className="orientation-source-note">
        <CameraIcon />
        <span><strong>统一调整 {cameraCount} 路图像方向</strong><small>预览、棋盘格标定和正式采集文件会同时修改并保持一致。</small></span>
      </div>
      <div className="orientation-options" role="radiogroup" aria-label="图像方向">
        {options.map((option) => (
          <button
            type="button"
            role="radio"
            aria-checked={selected === option.degrees}
            className={selected === option.degrees ? "selected" : ""}
            onClick={() => setSelected(option.degrees)}
            key={option.degrees}
          >
            <span className="orientation-glyph"><i style={{ transform: `rotate(${option.degrees}deg)` }}><CameraIcon /></i></span>
            <span><strong>{option.label}</strong><small>{option.detail}</small></span>
            {selected === option.degrees ? <CheckCircledIcon /> : null}
          </button>
        ))}
      </div>
      <p className="orientation-warning">
        四个方向均保持 60 fps。应用后 {cameraCount} 路相机会自动重启，通常需要几秒恢复；采集中禁止修改。
      </p>
      <button
        type="button"
        className="sheet-action primary"
        onClick={() => void apply()}
        disabled={busy || recording || selected === currentRotation}
      >
        {busy ? <ReloadIcon className="spin" /> : <CheckCircledIcon />}
        {recording ? "请先停止采集" : busy ? "正在应用并重启相机" : selected === currentRotation ? `当前已是 ${currentRotation}°` : "应用到预览、标定与采集"}
      </button>
    </div>
  );
}

function CalibrationSheet({
  manifest,
  online,
  onClose,
  onToast,
}: {
  manifest: DeviceManifest;
  online: boolean;
  onClose: () => void;
  onToast: (message: string) => void;
}) {
  const [pair, setPair] = useState<CameraCalibrationPair>("pair14");
  const [squaresLong, setSquaresLong] = useState("10");
  const [squaresWide, setSquaresWide] = useState("7");
  const [squareSizeMm, setSquareSizeMm] = useState("30");
  const [busy, setBusy] = useState(false);

  const longCount = Number(squaresLong);
  const wideCount = Number(squaresWide);
  const sideLength = Number(squareSizeMm);
  const dimensionsValid = Number.isInteger(longCount)
    && Number.isInteger(wideCount)
    && longCount >= 4
    && longCount <= 20
    && wideCount >= 4
    && wideCount <= 20
    && Number.isFinite(sideLength)
    && sideLength >= 1
    && sideLength <= 200;
  const pairCameraIds: [string, string] = pair === "pair14" ? ["cam3", "cam0"] : ["cam1", "cam2"];
  const pairCameras = pairCameraIds.map((id) => manifest.cameras.find((camera) => camera.id === id));
  const camerasReady = pairCameras.every(Boolean);
  const canStart = online && camerasReady && dimensionsValid && !busy;

  const start = async () => {
    if (!canStart) return;
    setBusy(true);
    try {
      const first = pairCameras[0];
      const second = pairCameras[1];
      if (!first || !second) throw new Error("标定相机不存在");
      const previewUrl = (camera: DeviceManifest["cameras"][number]) => {
        const protocolIndex = manifest.cameras.findIndex((item) => item.id === camera.id);
        return camera.previewUrl
          ?? `rtsp://${manifest.device.ipAddress}:${camera.port ?? 554 + Math.max(0, protocolIndex)}/PRR`;
      };
      await openCameraCalibration({
        pair,
        cameraIds: [first.id, second.id],
        urls: [previewUrl(first), previewUrl(second)],
        labels: [`${first.label ?? first.id.toUpperCase()} · ${first.direction}`, `${second.label ?? second.id.toUpperCase()} · ${second.direction}`],
        squaresLong: longCount,
        squaresWide: wideCount,
        squareSizeMm: sideLength,
      });
      onClose();
    } catch (error) {
      console.warn("[SynCap] Unable to open camera calibration", error);
      onToast(error instanceof Error ? error.message : "无法打开相机标定");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="calibration-sheet" data-testid="calibration-sheet">
      <div className="calibration-example" aria-label="棋盘格标定动作示例">
        <div className="calibration-lens left"><span>{pairCameras[0]?.direction ?? "左侧相机"}</span></div>
        <div className="calibration-lens right"><span>{pairCameras[1]?.direction ?? "右侧相机"}</span></div>
        <div className="calibration-board-demo" aria-hidden="true" />
        <div className="calibration-guide-copy"><strong>让两台相机同时看清完整棋盘格</strong><span>依次移动到中央、四角、远近，并向不同方向倾斜</span></div>
      </div>

      <div className="calibration-pose-strip" aria-label="标定拍摄姿态">
        <span><b>1</b>中央正对</span>
        <span><b>2</b>覆盖四角</span>
        <span><b>3</b>远近变化</span>
        <span><b>4</b>多向倾斜</span>
      </div>

      <div className="option-group calibration-pair-options">
        <span>选择双目组</span>
        <div>
          <button type="button" className={pair === "pair14" ? "selected" : ""} onClick={() => setPair("pair14")}>
            <strong>1–4 外侧组</strong><small>CAM3 左外侧 → CAM0 右外侧</small>
          </button>
          <button type="button" className={pair === "pair23" ? "selected" : ""} onClick={() => setPair("pair23")}>
            <strong>2–3 内侧组</strong><small>CAM1 左内侧 → CAM2 右内侧</small>
          </button>
        </div>
      </div>

      <div className="calibration-board-fields">
        <label className="sheet-field" htmlFor="calibration-long">
          <span>长边格数</span>
          <KeyboardInput id="calibration-long" type="number" inputMode="numeric" min="4" max="20" value={squaresLong} onChange={(event) => setSquaresLong(event.target.value)} />
        </label>
        <label className="sheet-field" htmlFor="calibration-wide">
          <span>宽边格数</span>
          <KeyboardInput id="calibration-wide" type="number" inputMode="numeric" min="4" max="20" value={squaresWide} onChange={(event) => setSquaresWide(event.target.value)} />
        </label>
        <label className="sheet-field" htmlFor="calibration-square-size">
          <span>格边长（mm）</span>
          <KeyboardInput id="calibration-square-size" type="number" inputMode="decimal" min="1" max="200" step="0.1" value={squareSizeMm} onChange={(event) => setSquareSizeMm(event.target.value)} />
        </label>
      </div>
      <p className={`calibration-board-note ${dimensionsValid ? "" : "error"}`}>
        {dimensionsValid
          ? `检测 ${longCount - 1} × ${wideCount - 1} 个内部角点；请输入黑白方格数量，不是角点数量。`
          : "格数需为 4–20 的整数，边长需为 1–200 mm。"}
      </p>

      <div className="calibration-auto-note"><CameraIcon /><span><strong>建议拍摄 24 组以上</strong><small>预览只用于构图；标定始终使用头环拍摄的全分辨率原图。</small></span></div>
      <button type="button" className="sheet-action primary calibration-start" onClick={() => void start()} disabled={!canStart}>
        {busy ? <ReloadIcon className="spin" /> : <CameraIcon />}
        {busy ? "正在打开双路标定" : !online ? "设备未连接" : "开始拍摄标定照片"}
      </button>
    </div>
  );
}

function SyncDiagnosticsPage({
  manifest,
  runtimeStatus,
  refreshing,
  onRefresh,
  onClose,
}: {
  manifest: DeviceManifest;
  runtimeStatus: DeviceRuntimeStatus | null;
  refreshing: boolean;
  onRefresh: () => Promise<void>;
  onClose: () => void;
}) {
  const displayCameras = orderCamerasForDisplay(manifest.cameras);
  const cameraCount = manifest.cameras.length;
  const hasImu = manifest.capabilities.includes("sensor.imu");
  const usesLegacyPhysicalOrder = isCompleteLegacyFourCameraSet(manifest.cameras);
  const clock = runtimeStatus?.clock;
  const exposure = runtimeStatus?.exposureSync;
  const cameraImu = runtimeStatus?.cameraImuSync;
  const acquisition = runtimeStatus?.sync.acquisitionSkewMs;
  const externalClock = externalClockStatus(clock);
  const clockLocked = externalClock === "connected";
  const clockConnecting = externalClock === "connecting";
  const clockNeedsAttention = externalClock === "needs-attention";
  const onlineCameraCount = runtimeStatus?.cameras.filter((camera) => camera.online).length ?? 0;
  const allCamerasOnline = manifest.cameras.length > 0 && onlineCameraCount === manifest.cameras.length;
  const internalSyncMeasured = Boolean(acquisition && acquisition.samples > 0 && acquisition.p95 != null);
  const hardwareTriggerActive = runtimeStatus?.sync.state === "hardware_trigger_active";
  const internalSyncHealthy = allCamerasOnline && internalSyncMeasured && (acquisition?.p95 ?? Infinity) <= 1;
  const internalSyncTone = !allCamerasOnline || (internalSyncMeasured && !internalSyncHealthy)
    ? "faulty"
    : internalSyncHealthy ? "healthy" : "pending";
  const externalClockActive = clockLocked || clockConnecting || clockNeedsAttention;

  return (
    <Dialog.Root open onOpenChange={(open) => { if (!open) onClose(); }}>
      <Dialog.Portal>
        <Dialog.Content asChild>
    <section className="sync-diagnostics-page" aria-labelledby="sync-diagnostics-title" data-testid="sync-diagnostics-page">
      <header className="sync-diagnostics-toolbar">
        <button type="button" className="sync-diagnostics-back" onClick={onClose} aria-label="关闭同步诊断"><ArrowLeftIcon /><span>返回</span></button>
        <div><span>设备状态</span><h2 id="sync-diagnostics-title">同步状态</h2></div>
        <button type="button" className="sync-diagnostics-refresh" onClick={() => void onRefresh()} disabled={refreshing} aria-label="刷新同步诊断"><ReloadIcon className={refreshing ? "spin" : ""} /><span>刷新</span></button>
      </header>

      <div className="sync-diagnostics-scroll" data-testid="sync-diagnostics-scroll">
        <div className={`sync-overview-card ${internalSyncTone}`}>
          <div className="sync-overview-copy"><span>{cameraCount} 路相机同步</span><strong>{internalSyncHealthy ? `${cameraCount} 路相机内部同步正常` : !allCamerasOnline ? "相机未全部就绪" : internalSyncMeasured ? "同步误差超过 1 ms" : hardwareTriggerActive ? "硬件触发工作中" : "等待同步数据"}</strong><small>{internalSyncHealthy ? `稳定误差 ${formatMilliseconds(acquisition?.p95)} · 正式采集使用同一组画面` : hardwareTriggerActive ? "时间误差未测量，不能据此判断曝光同步精度" : "外部时间同步是可选功能，不影响单台头环内部同步"}</small></div>
          <div className="sync-camera-count"><strong>{onlineCameraCount}/{manifest.cameras.length}</strong><span>相机在线</span></div>
        </div>

        <div className="diagnostic-metrics sync-summary-metrics">
          <div><span>当前误差</span><strong>{formatMilliseconds(acquisition?.current)}</strong></div>
          <div><span>稳定误差</span><strong>{formatMilliseconds(acquisition?.p95)}</strong></div>
          <div><span>检测次数</span><strong>{acquisition?.samples ?? 0}</strong></div>
        </div>

        <div className={`sync-context-card ${clockLocked ? "healthy" : clockNeedsAttention ? "warning" : "unconfigured"}`}>
          <ClockIcon />
          <span>
            <strong>{clockLocked ? "已连接外部时间基准" : clockConnecting ? "正在连接外部时间基准" : clockNeedsAttention ? "外部时间同步需要检查" : "未连接外部时间基准"}</strong>
            <small>{clockLocked ? "可用于多台头环的时间对齐" : clockNeedsAttention ? "只影响多台设备对时，不影响本机采集。" : `可选，不影响 ${cameraCount} 路相机同步和采集。`}</small>
          </span>
          <b>{clockLocked ? "正常" : clockConnecting ? "连接中" : clockNeedsAttention ? "检查" : "可选"}</b>
        </div>

        <section className="diagnostic-section">
          <div className="diagnostic-heading"><span><ActivityLogIcon />相机组内部同步</span><small>实时帧组</small></div>
          <div className="camera-sync-list">
            {displayCameras.map((camera) => {
              const protocolIndex = manifest.cameras.findIndex((item) => item.id === camera.id);
              const status = runtimeStatus?.cameras.find((item) => item.id === camera.id)
                ?? (protocolIndex >= 0 ? runtimeStatus?.cameras[protocolIndex] : undefined);
              return (
                <div className={status?.online ? "online" : "offline"} key={camera.id}>
                  <span><DotFilledIcon />{camera.label} · {camera.direction}</span>
                  <strong>{formatMilliseconds(status?.groupSkewMs)}</strong>
                </div>
              );
            })}
          </div>
          <p className="diagnostic-note">{usesLegacyPhysicalOrder ? "位置按佩戴者视角排列：左外侧、左内侧、右内侧、右外侧。" : "位置按设备清单顺序排列。"}这里显示的是相机实际采集时间差，不是预览画面到达手机的时间差。</p>
        </section>

        <section className="diagnostic-section">
          <div className="diagnostic-heading"><span><ClockIcon />外部时间同步（可选）</span><small>{clockLocked && clock?.lastUpdateAgeMs != null ? `${clock.lastUpdateAgeMs.toFixed(0)} ms 前更新` : clockConnecting ? "连接中" : clockNeedsAttention ? "需要检查" : "未连接"}</small></div>
          <div className="trace-chain">
            <DiagnosticState label="设备硬件时钟" detail={clock?.hardwareTimestamping ? "可用于外部对时" : "本机采集不需要"} tone={clock?.hardwareTimestamping ? "healthy" : "unconfigured"} status={clock?.hardwareTimestamping ? undefined : "可选"} />
            <DiagnosticState
              label="连接外部时间基准"
              detail={clockLocked ? "已稳定连接" : clockConnecting ? "正在连接" : clockNeedsAttention ? "连接异常，请检查对时设备和网络" : "当前没有外部时间基准"}
              tone={clockLocked ? "healthy" : clockConnecting ? "pending" : clockNeedsAttention ? "warning" : "unconfigured"}
              status={clockConnecting ? "连接中" : !externalClockActive ? "可选" : undefined}
            />
            <DiagnosticState label="校准设备时间" detail={clock?.systemClockDisciplined ? "设备时间已对齐" : clockConnecting ? "正在等待稳定" : clockNeedsAttention ? "外部对时未完成" : "仅多台设备对时时需要"} tone={clock?.systemClockDisciplined ? "healthy" : clockConnecting ? "pending" : clockNeedsAttention ? "warning" : "unconfigured"} status={!externalClockActive ? "可选" : undefined} />
            <DiagnosticState label="相机时间对齐" detail={clock?.sensorClockMapped ? "相机时间已对齐到外部基准" : `${cameraCount} 路相机仍保持设备内部同步`} tone={clock?.sensorClockMapped ? "healthy" : clockConnecting ? "pending" : clockNeedsAttention ? "warning" : "unconfigured"} status={!externalClockActive ? "可选" : undefined} />
          </div>
        </section>

        <section className="diagnostic-section">
          <div className="diagnostic-heading"><span><LightningBoltIcon />{hasImu ? "曝光与运动传感器" : "曝光"}</span><small>实验室标定</small></div>
          <div className="trace-chain">
            <DiagnosticState label="曝光与外部时间对齐" detail={exposure?.phaseLockedToGrandmaster ? formatNanoseconds(exposure.phaseErrorNs) : "仅多设备精确对时时需要"} tone={exposure?.phaseLockedToGrandmaster ? "healthy" : clockLocked ? "warning" : "unconfigured"} />
            <DiagnosticState label="实际曝光延迟" detail={exposure?.physicalExposureDelayCalibrated ? formatNanoseconds(exposure.physicalExposureDelayNs) : "需要专用实验设备测量，不影响日常采集"} tone={exposure?.physicalExposureDelayCalibrated ? "healthy" : "uncalibrated"} />
            {hasImu ? <DiagnosticState label="相机与运动传感器时间偏移" detail={cameraImu?.physicalTimeOffsetCalibrated ? formatNanoseconds(cameraImu.timeOffsetNs) : `未做实验室标定，不影响 ${cameraCount} 路相机同步`} tone={cameraImu?.physicalTimeOffsetCalibrated ? "healthy" : "uncalibrated"} /> : null}
          </div>
        </section>
      </div>
    </section>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}

type DiagnosticTone = "healthy" | "warning" | "pending" | "unconfigured" | "uncalibrated";

function DiagnosticState({ label, detail, tone, status: statusOverride }: { label: string; detail: string; tone: DiagnosticTone; status?: string }) {
  const status = statusOverride ?? (tone === "healthy" ? "正常" : tone === "warning" ? "检查" : tone === "pending" ? "连接中" : tone === "uncalibrated" ? "未标定" : "未配置");
  return (
    <div className={tone}>
      {tone === "healthy" ? <CheckCircledIcon /> : tone === "uncalibrated" ? <SewingPinIcon /> : <DotFilledIcon />}
      <span><strong>{label}</strong><small>{detail}</small></span>
      <b>{status}</b>
    </div>
  );
}

function DeviceSheet({ manifest, online, offline, onReconnect }: { manifest: DeviceManifest; online: boolean; offline: boolean; onReconnect: () => Promise<void> }) {
  const [scanning, setScanning] = useState(false);
  const scan = async () => {
    setScanning(true);
    try {
      await onReconnect();
    } finally {
      setScanning(false);
    }
  };
  return (
    <div className="sheet-stack">
      <div className={`device-result ${online || offline ? "selected" : ""}`}>
        <img src="/assets/syncap/syncap-mark.png" alt="" draggable={false} />
        <span><strong>{manifest.device.displayName}</strong><small>{online ? "已通过网络连接" : offline ? "离线采集已连接" : "未连接"}</small></span>
        {online || offline ? <CheckCircledIcon /> : <DotFilledIcon className="dot offline" />}
      </div>
      <button className="sheet-action secondary" onClick={() => void scan()} disabled={scanning}>{scanning ? <ReloadIcon className="spin" /> : <MagnifyingGlassIcon />}{scanning ? "正在检查设备连接" : "重新检查"}</button>
      <p className="sheet-hint"><LockClosedIcon />需要更换网络或使用离线采集，请前往设备页面。</p>
    </div>
  );
}

function WifiSheet({ onClose, onToast, onDeviceHost }: { onClose: () => void; onToast: (message: string) => void; onDeviceHost: (host: string) => void }) {
  const [savedBleDevice] = useState<BleProvisioningDevice | null>(() => readLastBleDevice());
  const [password, setPassword] = useState("");
  const [bleDevices, setBleDevices] = useState<BleProvisioningDevice[]>([]);
  const [selectedBle, setSelectedBle] = useState("");
  const [wifiNetworks, setWifiNetworks] = useState<NearbyWifiNetwork[]>([]);
  const [selectedWifi, setSelectedWifi] = useState("");
  const [bleScanning, setBleScanning] = useState(false);
  const [wifiScanning, setWifiScanning] = useState(false);
  const [wifiScanCompleted, setWifiScanCompleted] = useState(false);
  const [wifiScanError, setWifiScanError] = useState("");
  const [wifiScanTruncated, setWifiScanTruncated] = useState(false);
  const [busy, setBusy] = useState(false);
  const headringScanInFlight = useRef<Promise<BleProvisioningDevice[]> | null>(null);
  const wifiScanInFlight = useRef<{ address: string; request: Promise<NearbyWifiNetwork[]> } | null>(null);
  const selectedBleRef = useRef("");
  const wifiOwnerRef = useRef("");
  const connectInFlight = useRef(false);
  const androidNative = Capacitor.isNativePlatform() && Capacitor.getPlatform() === "android";
  const selectedDevice = bleDevices.find((device) => device.address === selectedBle) ?? null;
  const selectedNetwork = wifiNetworks.find((network) => network.ssid === selectedWifi) ?? null;
  const passwordReady = selectedNetwork != null && (!selectedNetwork.secure || (password.length >= 8 && password.length <= 63));
  const canConnect = Boolean(
    androidNative
    && selectedDevice
    && selectedNetwork
    && wifiNetworkSupported(selectedNetwork)
    && passwordReady
    && !bleScanning
    && !wifiScanning
    && !busy
  );

  const scanHeadrings = (): Promise<BleProvisioningDevice[]> => {
    if (!androidNative) {
      return Promise.resolve([]);
    }
    if (headringScanInFlight.current) return headringScanInFlight.current;
    setBleScanning(true);
    const request = (async () => {
      try {
        const devices = await scanBleProvisioningDevices(2600);
        const available = pairedBleDevicesForWifi(devices, savedBleDevice?.address);
        setBleDevices(available);
        const preferred = available.find((device) => device.address === selectedBleRef.current) ?? available[0];
        if (preferred) rememberBleDevice(preferred);
        const address = preferred?.address ?? "";
        selectedBleRef.current = address;
        setSelectedBle(address);
        if (!address) {
          wifiOwnerRef.current = "";
          setWifiNetworks([]);
          setSelectedWifi("");
          setPassword("");
          setWifiScanCompleted(false);
          setWifiScanError("");
          setWifiScanTruncated(false);
        }
        return available;
      } catch (error) {
        selectedBleRef.current = "";
        setBleDevices([]);
        setSelectedBle("");
        setWifiNetworks([]);
        setSelectedWifi("");
        setPassword("");
        const message = wifiProvisioningErrorMessage(error, "未找到已配对头环");
        onToast(message);
        return [];
      }
    })();
    headringScanInFlight.current = request;
    void request.finally(() => {
      if (headringScanInFlight.current === request) headringScanInFlight.current = null;
      setBleScanning(false);
    });
    return request;
  };

  const scanWifi = (address = selectedBleRef.current): Promise<NearbyWifiNetwork[]> => {
    if (!androidNative || !address) return Promise.resolve([]);
    if (wifiScanInFlight.current?.address === address) return wifiScanInFlight.current.request;
    const ownerChanged = wifiOwnerRef.current !== address;
    wifiOwnerRef.current = address;
    if (ownerChanged) {
      setWifiNetworks([]);
      setSelectedWifi("");
      setPassword("");
    }
    setWifiScanning(true);
    setWifiScanCompleted(false);
    setWifiScanError("");
    setWifiScanTruncated(false);
    const request = (async () => {
      try {
        const result = await withDeadline(
          scanWifiNetworksOverBle(address),
          55_000,
          "头环扫描 Wi-Fi 超时，请确认头环仍在附近后重试",
        );
        if (result.state !== "completed" || result.op !== "wifi.scan") {
          throw new Error(result.error ?? "头环没有完成 Wi-Fi 扫描");
        }
        if (!Array.isArray(result.networks)) throw new Error("头环返回的 Wi-Fi 列表无效");
        const networks = normalizeBleWifiNetworks(result.networks);
        if (selectedBleRef.current !== address) return networks;
        setWifiNetworks(networks);
        setSelectedWifi((current) => networks.some((network) => network.ssid === current) ? current : "");
        if (!networks.some((network) => network.ssid === selectedWifi)) setPassword("");
        setWifiScanCompleted(true);
        setWifiScanTruncated(Boolean(result.truncated));
        return networks;
      } catch (error) {
        const message = wifiProvisioningErrorMessage(error, "无法让头环扫描附近 Wi-Fi");
        if (selectedBleRef.current === address) {
          setWifiScanCompleted(true);
          setWifiScanError(message);
          setWifiScanTruncated(false);
          onToast(message);
        }
        return [];
      }
    })();
    wifiScanInFlight.current = { address, request };
    void request.finally(() => {
      if (wifiScanInFlight.current?.request === request) wifiScanInFlight.current = null;
      if (selectedBleRef.current === address) setWifiScanning(false);
    });
    return request;
  };

  const chooseHeadring = (address: string) => {
    if (address === selectedBleRef.current || wifiScanning || busy) return;
    selectedBleRef.current = address;
    setSelectedBle(address);
    setWifiNetworks([]);
    setSelectedWifi("");
    setPassword("");
    setWifiScanCompleted(false);
    setWifiScanError("");
    setWifiScanTruncated(false);
    void scanWifi(address);
  };

  const refreshHeadrings = async () => {
    const available = await scanHeadrings();
    const address = selectedBleRef.current || available[0]?.address || "";
    if (address) await scanWifi(address);
  };

  useEffect(() => {
    let cancelled = false;
    const prepareProvisioning = async () => {
      if (!androidNative) return;
      const available = await scanHeadrings();
      if (cancelled) return;
      const address = selectedBleRef.current || available[0]?.address || "";
      if (address) await scanWifi(address);
    };
    void prepareProvisioning();
    return () => { cancelled = true; };
  }, []);

  const connect = async () => {
    if (!androidNative) return onToast("请使用 Android 手机设置网络");
    if (connectInFlight.current) return;
    if (!selectedDevice) return onToast("请先选择头环");
    if (!selectedNetwork) return onToast("请选择要连接的网络");
    if (!wifiNetworkSupported(selectedNetwork)) return onToast("该网络暂不支持，请选择其他网络");
    if (selectedNetwork.secure && (password.length < 8 || password.length > 63)) return onToast("请输入正确的网络密码");
    connectInFlight.current = true;
    setBusy(true);
    try {
      const result = await withDeadline(
        configureWifiOverBle(selectedDevice.address, selectedNetwork.ssid, password, fixedClaimCode, selectedNetwork.security),
        130_000,
        "连接超时，请确认网络仍可用后重试",
      );
      if (result.state !== "connected") throw new Error(result.error ?? "设备未能连接网络");
      if (result.ssid && result.ssid !== selectedNetwork.ssid) throw new Error("设备连接到了其他网络，请重试");
      if (!result.ipAddress || !validDeviceHost(result.ipAddress)) throw new Error("网络已连接，但设备暂时无法使用");
      rememberBleDevice(selectedDevice);
      onDeviceHost(result.ipAddress);
      setPassword("");
      onToast(`头环已连接 ${result.ssid ?? selectedNetwork.ssid}`);
      onClose();
    } catch (error) {
      onToast(wifiProvisioningErrorMessage(error, "连接网络失败"));
    } finally {
      connectInFlight.current = false;
      setBusy(false);
    }
  };
  return (
    <div className="sheet-stack wifi-setup-sheet">
      <div className="wifi-setup-scroll">
        {bleScanning || wifiScanning || busy ? (
          <div className="sheet-loading"><ReloadIcon className="spin" />{busy ? "正在连接网络" : bleScanning ? "正在查找头环" : "正在搜索附近网络"}</div>
        ) : null}

        <div className="setup-step-heading"><span>1</span><div><strong>选择头环</strong><small>选择需要联网的设备</small></div></div>
        <div className="wifi-list headring-list">
          {bleDevices.map((device) => (
            <button type="button" className={device.address === selectedBle ? "selected" : ""} key={device.address} onClick={() => chooseHeadring(device.address)} aria-pressed={device.address === selectedBle} disabled={bleScanning || wifiScanning || busy}>
              <MobileIcon /><span><strong>{device.name}</strong><small>已配对</small></span>{device.address === selectedBle ? <CheckCircledIcon /> : <ChevronRightIcon />}
            </button>
          ))}
          {!bleScanning && bleDevices.length === 0 ? <div className="empty-scan">没有找到已配对的头环，请先在手机设置中完成配对。</div> : null}
        </div>
        <button type="button" className="sheet-action secondary compact-action" onClick={() => void refreshHeadrings()} disabled={!androidNative || bleScanning || wifiScanning || busy}><ReloadIcon className={bleScanning ? "spin" : ""} />刷新设备</button>

        <div className="setup-step-heading"><span>2</span><div><strong>选择网络</strong><small>支持普通 Wi-Fi 和手机热点</small></div></div>
        <p className="sheet-hint"><GlobeIcon />如果使用手机热点，请先在手机设置中开启热点。</p>
        <div className="wifi-list nearby-wifi-list">
          {wifiNetworks.map((network) => (
            <button type="button" className={network.ssid === selectedWifi ? "selected" : ""} key={network.ssid} onClick={() => { setSelectedWifi(network.ssid); setPassword(""); }} aria-pressed={network.ssid === selectedWifi} disabled={!wifiNetworkSupported(network)}>
              <GlobeIcon /><span><strong>{network.ssid}</strong><small>{wifiNetworkSupported(network) ? wifiSecurityLabel(network.security) : "暂不支持"} · {wifiSignalLabel(network.rssi)}</small></span>{network.ssid === selectedWifi ? <CheckCircledIcon /> : <ChevronRightIcon />}
            </button>
          ))}
          {wifiScanError ? <div className="empty-scan" role="alert">{wifiScanError}</div> : null}
          {!wifiScanning && wifiNetworks.length === 0 && !wifiScanError ? (
            <div className="empty-scan">{!androidNative ? "请使用 Android 手机设置网络。" : !selectedDevice ? "请先选择头环。" : wifiScanCompleted ? "没有找到可用网络，请靠近路由器，或确认手机热点已开启。" : "等待搜索网络。"}</div>
          ) : null}
        </div>
        {wifiScanTruncated ? <p className="sheet-hint"><DotFilledIcon />附近网络较多，只显示信号最强的一部分。</p> : null}
        <button type="button" className="sheet-action secondary compact-action" onClick={() => void scanWifi()} disabled={!androidNative || !selectedDevice || wifiScanning || busy}><ReloadIcon className={wifiScanning ? "spin" : ""} />重新搜索网络</button>

        {selectedNetwork?.secure ? (
          <label className="sheet-field" htmlFor="wifi-password"><span>{selectedNetwork.ssid} 的密码</span><KeyboardInput id="wifi-password" type="password" value={password} onChange={(event) => setPassword(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter") void connect(); }} enterKeyHint="done" autoComplete="current-password" placeholder="请输入网络密码" /></label>
        ) : selectedNetwork ? (
          <p className="sheet-hint"><LockClosedIcon />{selectedNetwork.ssid} 是开放网络，无需输入密码。</p>
        ) : null}
        <p className="sheet-hint"><LockClosedIcon />网络密码不会保存在 App 中。</p>
      </div>
      <button className="sheet-action primary confirm-action wifi-confirm-action" onClick={() => void connect()} disabled={!canConnect}><LightningBoltIcon />{busy ? "正在连接" : "连接"}</button>
    </div>
  );
}

function OfflineStorageSheet({
  connection,
  onConnected,
  onDisconnected,
  onClose,
  onToast,
}: {
  connection: OfflineConnection | null;
  onConnected: (connection: OfflineConnection) => void;
  onDisconnected: () => void;
  onClose: () => void;
  onToast: (message: string) => void;
}) {
  const native = isDeviceRuntime();
  const [savedBleDevice] = useState<BleProvisioningDevice | null>(() => readLastBleDevice());
  const [devices, setDevices] = useState<BleProvisioningDevice[]>(() => savedBleDevice ? [savedBleDevice] : []);
  const [selected, setSelected] = useState(connection?.address ?? savedBleDevice?.address ?? "");
  const [scanning, setScanning] = useState(false);
  const [busy, setBusy] = useState(false);
  const scanInFlight = useRef<Promise<BleProvisioningDevice[]> | null>(null);

  const scan = (): Promise<BleProvisioningDevice[]> => {
    if (!native) {
      onToast("请在 Android 手机上使用离线采集");
      return Promise.resolve([]);
    }
    if (scanInFlight.current) return scanInFlight.current;
    setScanning(true);
    const request = (async () => {
      try {
        const found = await scanBleProvisioningDevices(8000);
        const available = found.length > 0 ? found : savedBleDevice ? [savedBleDevice] : [];
        setDevices(available);
        if (found[0]) rememberBleDevice(found[0]);
        setSelected((current) => current || available[0]?.address || "");
        return available;
      } catch (error) {
        onToast(error instanceof Error ? wifiProvisioningErrorMessage(error, "没有找到头环") : "没有找到头环");
        return savedBleDevice ? [savedBleDevice] : [];
      }
    })();
    scanInFlight.current = request;
    void request.finally(() => {
      if (scanInFlight.current === request) scanInFlight.current = null;
      setScanning(false);
    });
    return request;
  };

  useEffect(() => {
    if (!connection) void scan();
  }, []);

  const enable = async () => {
    setBusy(true);
    try {
      let address = selected;
      if (!address) {
        const found = await scan();
        address = found[0]?.address ?? "";
        if (!address) return onToast("尚未发现 SynCap 设备，请靠近头环后重新扫描");
      }
      const result = await configureUsbStorageOverBle(address, fixedClaimCode);
      if (result.state !== "ready" || result.storage?.target !== "usb" || !result.storage.usbMounted) {
        throw new Error(result.error ?? "设备未能启用 U 盘存储");
      }
      const device = devices.find((item) => item.address === address) ?? savedBleDevice;
      if (device) rememberBleDevice(device);
      onConnected({
        address,
        name: device?.name ?? connection?.name ?? "SynCap",
        claimCode: fixedClaimCode,
        storage: result.storage,
      });
      onToast(`离线模式已启用 · ${result.storage.usbLabel ?? "SYNCAP"}`);
      onClose();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "无法启用离线 U 盘模式");
    } finally {
      setBusy(false);
    }
  };

  const refresh = async () => {
    if (!connection) return;
    setBusy(true);
    try {
      const result = await getOfflineDeviceStatusOverBle(connection.address, connection.claimCode);
      if (!result.storage) throw new Error(result.error ?? "设备未返回存储状态");
      onConnected({ ...connection, storage: result.storage });
      onToast(`U 盘剩余 ${result.storage.usbFreeBytes == null ? "--" : formatBytes(result.storage.usbFreeBytes)}`);
    } catch (error) {
      onToast(error instanceof Error ? error.message : "无法读取离线设备状态");
    } finally {
      setBusy(false);
    }
  };

  const eject = async () => {
    if (!connection) return;
    setBusy(true);
    try {
      const result = await ejectUsbOverBle(connection.address, connection.claimCode);
      if (result.state !== "ejected") throw new Error(result.error ?? "设备拒绝弹出 U 盘");
      onDisconnected();
      onToast("U 盘已同步并安全卸载，现在可以拔出");
      onClose();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "安全弹出失败");
    } finally {
      setBusy(false);
    }
  };

  if (connection) {
    return (
      <div className="sheet-stack">
        <div className="offline-storage-card">
          <ArchiveIcon />
          <span><strong>{connection.storage.usbLabel ?? "SYNCAP"} U 盘</strong><small>{connection.name} · 已连接</small></span>
          <CheckCircledIcon />
        </div>
        <div className="diagnostic-metrics">
          <div><span>状态</span><strong>{connection.storage.usbMounted ? "已挂载" : "未挂载"}</strong></div>
          <div><span>剩余空间</span><strong>{connection.storage.usbFreeBytes == null ? "--" : formatBytes(connection.storage.usbFreeBytes)}</strong></div>
          <div><span>卷标</span><strong>{connection.storage.usbLabel ?? "--"}</strong></div>
        </div>
        <button className="sheet-action secondary" onClick={() => void refresh()} disabled={busy}><ReloadIcon className={busy ? "spin" : ""} />刷新离线状态</button>
        <button className="sheet-action primary" onClick={() => void eject()} disabled={busy}><ArchiveIcon />同步并安全弹出 U 盘</button>
        <p className="sheet-hint"><LockClosedIcon />采集中禁止弹出；停止后会写入会话清单并同步文件系统。</p>
      </div>
    );
  }

  return (
    <div className="sheet-stack">
      {scanning || busy ? <div className="sheet-loading"><ReloadIcon className="spin" />{scanning ? "正在查找头环" : "正在连接设备"}</div> : null}
      <div className="wifi-list">
        {devices.map((device) => (
          <button className={device.address === selected ? "selected" : ""} key={device.address} onClick={() => setSelected(device.address)}>
            <MobileIcon /><span><strong>{device.name}</strong><small>{device.rssi === -127 ? "已配对" : wifiSignalLabel(device.rssi)}</small></span>{device.address === selected ? <CheckCircledIcon /> : <ChevronRightIcon />}
          </button>
        ))}
        {!scanning && devices.length === 0 ? <div className="empty-scan">没有找到头环，请确认设备已开机并靠近手机。</div> : null}
      </div>
      <button className="sheet-action secondary" onClick={() => void scan()} disabled={scanning || busy}><ReloadIcon className={scanning ? "spin" : ""} />重新扫描</button>
      <button className="sheet-action primary confirm-action" onClick={() => void enable()} disabled={busy}><ArchiveIcon />启用离线 U 盘采集</button>
      <p className="sheet-hint"><LockClosedIcon />数据会直接保存在头环连接的 U 盘中。</p>
    </div>
  );
}

function ExportSheet({
  session,
  host,
  cameraCount,
  hasImu,
  onClose,
  onToast,
}: {
  session: SessionRecord;
  host: string;
  cameraCount: number;
  hasImu: boolean;
  onClose: () => void;
  onToast: (message: string) => void;
}) {
  const [exporting, setExporting] = useState(false);
  const run = async () => {
    try {
      setExporting(true);
      if (!isDeviceRuntime()) throw new Error("真实导出仅在 SynCap Studio 客户端中可用");
      const result = await downloadDeviceSession(host, session.id);
      if (!result.verified || !result.manifestSaved) throw new Error("导出完成，但完整性校验未通过");
      onToast(`会话清单已保存 · ${result.verifiedFiles} 个数据文件通过 SHA-256 校验 · ${formatBytes(result.bytes)}`);
      onClose();
    } catch (error) {
      onToast(error instanceof Error ? error.message : "会话导出失败");
    } finally {
      setExporting(false);
    }
  };
  const partial = session.status === "failed";
  return (
    <div className="sheet-stack">
      <div className="export-session"><FileTextIcon /><span><strong>{session.name}</strong><small>{session.duration} · {session.size}</small></span></div>
      <div className="export-mode"><ArchiveIcon /><span><strong>{partial ? "导出已保存数据" : "导出完整会话"}</strong><small>{partial ? "仅包含设备已完成写入并通过校验的片段" : `包含 ${cameraCount} 路视频${hasImu ? "、运动数据" : ""}和会话信息`}</small></span></div>
      <div className="export-checks"><span><CheckCircledIcon />原始文件</span><span><CheckCircledIcon />会话清单</span><span><CheckCircledIcon />数据文件 SHA-256</span></div>
      <p className="sheet-hint"><LockClosedIcon />{partial ? "未完整结束的最后片段不会冒充完整数据。" : "导出完成后会自动检查文件是否完整。"}</p>
      <button className="sheet-action primary" onClick={run} disabled={exporting}>{exporting ? <ReloadIcon className="spin" /> : <DownloadIcon />}{exporting ? "正在导出" : "开始导出"}</button>
    </div>
  );
}

function BottomNavigation({ tab, onChange, showRtsp }: { tab: AppTab; onChange: (tab: AppTab) => void; showRtsp: boolean }) {
  const items: Array<{ id: AppTab; label: string; icon: ReactNode }> = [
    { id: "capture", label: "采集", icon: <CameraIcon /> },
    ...(showRtsp ? [{ id: "rtsp" as const, label: "RTSP 预览", icon: <EyeOpenIcon /> }] : []),
    { id: "device", label: "设备", icon: <CubeIcon /> },
    { id: "data", label: "数据", icon: <ArchiveIcon /> },
    { id: "settings", label: "设置", icon: <GearIcon /> },
  ];
  return (
    <nav className="bottom-navigation" aria-label="主导航">
      {items.map((item) => (
        <button className={tab === item.id ? "active" : ""} key={item.id} onClick={() => onChange(item.id)} data-testid={`nav-${item.id}`}>
          {item.icon}<span>{item.label}</span>
        </button>
      ))}
    </nav>
  );
}

function PageTitle({ eyebrow, title, tone }: { eyebrow: string; title: string; tone?: "healthy" | "recording" | "offline" }) {
  return <div className="page-title"><span className={tone ?? ""}>{eyebrow}</span><h1>{title}</h1></div>;
}

function SectionHeading({ title, detail }: { title: string; detail?: string }) {
  return <div className="section-heading"><h2>{title}</h2>{detail ? <span>{detail}</span> : null}</div>;
}

function sheetMeta(kind: SheetKind, session: SessionRecord | null) {
  switch (kind) {
    case "devices": return { title: "设备连接", description: "查看当前连接状态", snap: 0.48 };
    case "wifi": return { title: "连接网络", description: "选择 Wi-Fi 或手机热点，输入密码", snap: 0.9 };
    case "offline": return { title: "离线 U 盘采集", description: "无网络时，数据直接保存到 U 盘", snap: 0.78 };
    case "calibration": return { title: "相机位置标定", description: "棋盘格双目标定 · 自动筛选有效照片", snap: 0.94 };
    case "orientation": return { title: "图像方向", description: "预览、标定和采集同时生效", snap: 0.78 };
    case "export": return { title: "导出会话", description: session?.name ?? "", snap: 0.66 };
    default: return { title: "", description: "", snap: 0.6 };
  }
}

type CaptureStorageOption = {
  target: CaptureStorageTarget;
  label: string;
  freeBytes: number | null;
  ready: boolean;
  selectable: boolean;
  reason: string;
  estimatedDuration: string;
};

function storageVolumeDisplayName(
  volume: DeviceStorageStatus["usb"] | DeviceStorageStatus["internal"] | undefined,
  target: CaptureStorageTarget,
) {
  const declaredName = volume?.displayName?.trim();
  if (declaredName) return declaredName;
  if (target === "internal" || volume?.mediaType === "internal") return "设备本地";
  if (volume?.mediaType === "sd") return "SD 卡";
  return `${volume?.label ?? "SYNCAP"} U 盘`;
}

function captureStorageOption(status: DeviceStorageStatus | undefined, target: CaptureStorageTarget): CaptureStorageOption {
  const volume = status?.[target];
  const label = storageVolumeDisplayName(volume, target);
  const minimumFreeBytes = status?.capturePolicy?.minimumFreeBytes ?? fallbackMinimumCaptureFreeBytes;
  const estimatedBytesPerSecond = status?.capturePolicy?.estimatedBytesPerSecond ?? fallbackCaptureBytesPerSecond;
  const freeBytes = typeof volume?.freeBytes === "number" ? volume.freeBytes : null;
  const enoughSpace = freeBytes != null && freeBytes >= minimumFreeBytes;
  const available = target === "internal" ? true : Boolean(volume?.available);
  const mounted = target === "internal" ? true : Boolean(volume?.mounted);
  const ready = volume?.canCapture ?? (available && mounted && enoughSpace);
  const selectable = target === "internal" ? enoughSpace : available && (enoughSpace || !mounted);
  const reasonCode = volume?.reason;
  let reason = "存储状态尚未就绪";
  if (reasonCode === "not_available" || (target === "usb" && !available)) {
    reason = target === "usb" && volume?.mediaType == null && !volume?.displayName
      ? "未检测到标记为 SYNCAP 的 exFAT U 盘"
      : `未检测到${label}`;
  } else if (reasonCode === "not_mounted" || (target === "usb" && available && !mounted)) {
    reason = `${label}尚未挂载，点击后检查并挂载`;
  } else if (reasonCode === "insufficient_free_space" || (freeBytes != null && !enoughSpace)) {
    reason = `可用空间不足，至少需保留 ${formatBytes(minimumFreeBytes)}`;
  } else if (ready) {
    reason = "存储已就绪";
  }
  return {
    target,
    label,
    freeBytes,
    ready,
    selectable,
    reason,
    estimatedDuration: formatEstimatedCaptureDuration(freeBytes, minimumFreeBytes, estimatedBytesPerSecond),
  };
}

function configuredCaptureStorage(runtimeStatus: DeviceRuntimeStatus | null, selectedTarget: CaptureStorageTarget | null) {
  const status = runtimeStatus?.storage;
  if (selectedTarget == null) return { label: "未选择存储", freeBytes: null, estimatedDuration: "--" };
  const option = captureStorageOption(status, selectedTarget);
  return {
    label: status == null ? "可用空间" : option.label,
    freeBytes: status == null ? null : option.freeBytes,
    estimatedDuration: status == null ? "--" : option.estimatedDuration,
  };
}

function formatEstimatedCaptureDuration(freeBytes: number | null, minimumFreeBytes: number, bytesPerSecond: number) {
  if (freeBytes == null) return "--";
  const seconds = Math.floor(Math.max(0, freeBytes - minimumFreeBytes) / Math.max(1, bytesPerSecond));
  if (seconds < 60) return seconds === 0 ? "空间不足" : "不足 1 分钟";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return hours > 0 ? `约 ${hours} 小时 ${minutes} 分钟` : `约 ${minutes} 分钟`;
}

function captureErrorMessage(error: unknown, fallback: string, cameraCount?: number, externalStorageName?: string) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  const code = typeof error === "object" && error !== null && "code" in error
    ? String((error as { code?: unknown }).code ?? "")
    : "";
  const diagnostic = `${code} ${message}`;
  if (/capture[._-]recovery[._-]required/i.test(diagnostic)) {
    return "设备保留了上次未完整结束的数据；请先导出可用文件，再重启设备";
  }
  if (/capture[._-]interrupted|segment status .* incomplete|terminal segment/i.test(diagnostic)) {
    return "采集未完整结束，已完成片段仍可在数据页面导出";
  }
  if (/USB drive is not (available|mounted)|not_available|not_mounted/i.test(diagnostic)) {
    return externalStorageName
      ? `未检测到可用的${externalStorageName}，请检查后重新选择`
      : "未检测到可用的 SYNCAP U 盘，请插入后重新选择";
  }
  if (/less than 512 MiB|insufficient[._-]free[._-]space|storage[._-]insufficient|space.*insufficient/i.test(diagnostic)) {
    return "存储空间不足，至少需要保留 512 MB 空闲空间";
  }
  if (/camera[._-]unavailable|camera streams unavailable|persistent qgapp is not running|preview listeners are unavailable/i.test(diagnostic)) return `${cameraCount == null ? "所有" : `${cameraCount} 路`}相机尚未全部就绪，无法开始采集`;
  if (/already recording|capture[._-]already[._-]running/i.test(diagnostic)) return "设备已有采集任务正在运行";
  if (/segment[._-]boundary[._-]timeout/i.test(diagnostic)) return "相机正在准备下一段数据，请稍后再试";
  if (/capture[._-]finalization[._-]failed|video integrity check failed/i.test(diagnostic)) {
    return "采集已停止，但文件完整性检查未通过，请在数据页面查看记录";
  }
  if (/timed? ?out|failed to connect|unable to resolve host/i.test(diagnostic)) return "设备连接超时，请检查当前 Wi-Fi";
  return message.trim() && !/^Unable to (start|stop) capture$/i.test(message) ? message : fallback;
}

function formatElapsed(value: number) {
  const hours = Math.floor(value / 3600);
  const minutes = Math.floor((value % 3600) / 60);
  const seconds = value % 60;
  return [hours, minutes, seconds].map((item) => String(item).padStart(2, "0")).join(":");
}

function toSessionRecord(record: DeviceSessionRecord): SessionRecord {
  const created = new Date(record.createdAt);
  const createdAt = Number.isNaN(created.getTime())
    ? record.createdAt
    : new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }).format(created);
  const durationSeconds = Math.max(0, Math.round((record.durationMs ?? 0) / 1000));
  const status: SessionRecord["status"] = record.status === "complete" || record.status === "recording" ? record.status : "failed";
  return {
    id: record.id,
    name: record.name,
    createdAt,
    duration: formatElapsed(durationSeconds),
    size: formatBytes(record.sizeBytes),
    sizeBytes: record.sizeBytes,
    fileCount: record.fileCount,
    exportAvailable: record.exportAvailable,
    failureReason: record.failureReason,
    status,
  };
}

function formatBytes(value: number) {
  if (!Number.isFinite(value) || value <= 0) return "0 B";
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  if (value < 1024 * 1024 * 1024) return `${(value / 1024 / 1024).toFixed(1)} MB`;
  return `${(value / 1024 / 1024 / 1024).toFixed(1)} GB`;
}

function pairedBleDevicesForWifi(devices: BleProvisioningDevice[], savedAddress?: string) {
  const byAddress = new Map<string, BleProvisioningDevice>();
  devices.forEach((device) => {
    if (!device.address || (!device.paired && !device.bonded)) return;
    const key = device.address.toUpperCase();
    const existing = byAddress.get(key);
    const rssi = Number.isFinite(device.rssi) ? device.rssi : -127;
    if (!existing || rssi > existing.rssi) {
      byAddress.set(key, { ...device, rssi, paired: Boolean(device.paired || existing?.paired), bonded: Boolean(device.bonded || existing?.bonded) });
    } else {
      byAddress.set(key, { ...existing, paired: Boolean(existing.paired || device.paired), bonded: Boolean(existing.bonded || device.bonded) });
    }
  });
  const preferred = savedAddress?.toUpperCase();
  return [...byAddress.values()].sort((left, right) => (
    Number(right.address.toUpperCase() === preferred) - Number(left.address.toUpperCase() === preferred)
    || right.rssi - left.rssi
    || left.name.localeCompare(right.name, "zh-CN")
  ));
}

function wifiSecurityRank(security: string) {
  switch (security) {
    case "wpa2-psk": return 3;
    case "wpa-psk": return 2;
    case "open": return 1;
    default: return 0;
  }
}

function normalizeBleWifiNetworks(networks: NearbyWifiNetwork[]) {
  const bySsid = new Map<string, NearbyWifiNetwork>();
  networks.forEach((network) => {
    if (typeof network.ssid !== "string" || new TextEncoder().encode(network.ssid).length === 0 || new TextEncoder().encode(network.ssid).length > 32) return;
    const security = typeof network.security === "string" ? network.security.toLowerCase() : "unknown";
    const candidate: NearbyWifiNetwork = {
      ...network,
      security,
      secure: security !== "open",
      rssi: Number.isFinite(network.rssi) ? network.rssi : -127,
    };
    const existing = bySsid.get(candidate.ssid);
    if (!existing) {
      bySsid.set(candidate.ssid, candidate);
      return;
    }
    const candidateRank = wifiSecurityRank(candidate.security);
    const existingRank = wifiSecurityRank(existing.security);
    if (candidateRank > existingRank || (candidateRank === existingRank && candidate.rssi > existing.rssi)) {
      bySsid.set(candidate.ssid, candidate);
    }
  });
  return [...bySsid.values()].sort((left, right) => (
    Number(wifiNetworkSupported(right)) - Number(wifiNetworkSupported(left))
    || right.rssi - left.rssi
    || left.ssid.localeCompare(right.ssid, "zh-CN")
  ));
}

async function withDeadline<T>(request: Promise<T>, timeoutMs: number, message: string) {
  let timeoutId: ReturnType<typeof window.setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timeoutId = window.setTimeout(() => reject(new Error(message)), timeoutMs);
  });
  try {
    return await Promise.race([request, deadline]);
  } finally {
    if (timeoutId != null) window.clearTimeout(timeoutId);
  }
}

function wifiProvisioningErrorMessage(error: unknown, fallback: string) {
  const message = error instanceof Error ? error.message : String(error ?? "");
  if (/wifi_association_timeout|wrong[_ -]?password|authentication.*fail/i.test(message)) {
    return "密码不正确，或网络已关闭，请检查后重试";
  }
  if (/dhcp_failed/i.test(message)) return "网络已连接，但设备暂时无法使用，请重开网络后重试";
  if (/unsupported_operation|does not expose|characteristics are incomplete/i.test(message)) {
    return "当前头环版本不支持网络设置，请先更新设备";
  }
  if (/bluetooth permission|location permission/i.test(message)) return "请允许 App 查找附近设备后重试";
  if (/timed? ?out|超时/i.test(message)) {
    return fallback.includes("扫描") || fallback.includes("搜索") ? "搜索网络超时，请靠近头环后重试" : "连接超时，请确认网络仍可用后重试";
  }
  if (/BLE device disconnected|BLE connection failed|Unable to create BLE connection|Unable to complete Bluetooth provisioning/i.test(message)) {
    return "与头环的连接已断开，请靠近头环后重试";
  }
  return message.trim() || fallback;
}

function wifiNetworkSupported(network: NearbyWifiNetwork) {
  return ["open", "wpa-psk", "wpa2-psk"].includes(network.security);
}

function wifiSecurityLabel(value: string) {
  switch (value) {
    case "open": return "无需密码";
    case "wpa-psk":
    case "wpa2-psk": return "需要密码";
    default: return "暂不支持";
  }
}

function wifiSignalLabel(rssi: number) {
  if (rssi >= -55) return "信号强";
  if (rssi >= -70) return "信号良好";
  return "信号较弱";
}

function deviceTypeLabel(type: string | undefined) {
  if (type === "hisi") return "Hisi";
  if (type === "robobaton") return "RoboBaton";
  return "标准设备";
}

type RuntimeBattery = NonNullable<NonNullable<DeviceRuntimeStatus["power"]>["battery"]>;

function batteryLevelLabel(battery: RuntimeBattery) {
  if (!battery.available || battery.capacityPercent == null) return "--";
  return `${Math.max(0, Math.min(100, Math.round(battery.capacityPercent)))}%`;
}

function chargingStateLabel(state: RuntimeBattery["chargingState"]) {
  switch (state) {
    case "charging": return "充电中";
    case "full": return "已充满";
    case "discharging": return "未充电";
    case "not_charging": return "未充电";
    case "unavailable": return "不可用";
    default: return "未知";
  }
}

function deviceNetworkSummary(online: boolean, runtimeStatus: DeviceRuntimeStatus | null) {
  if (!online) return { label: "--", detail: "设备离线" };
  if (runtimeStatus?.wifi?.state === "connected") {
    return {
      label: "Wi-Fi",
      detail: runtimeStatus.wifi.ssid?.trim() || "设备服务已连接",
    };
  }
  return { label: "局域网", detail: "设备服务已连接" };
}

type RuntimeClock = NonNullable<DeviceRuntimeStatus["clock"]>;

function clockSummary(clock: RuntimeClock | undefined) {
  switch (externalClockStatus(clock)) {
    case "connected": return `外部时间已同步 · ${formatNanoseconds(clock?.offsetFromMasterNs)}`;
    case "connecting": return "外部时间正在同步";
    case "needs-attention": return "外部时间同步需要检查";
    default: return clock ? "外部时间未连接 · 可选" : "使用设备内部时间";
  }
}

type ExternalClockStatus = "connected" | "connecting" | "needs-attention" | "optional";

function externalClockStatus(clock: RuntimeClock | undefined): ExternalClockStatus {
  if (clock?.state === "locked") return "connected";
  if (clock?.state === "locking") return "connecting";
  const hasExternalClockEvidence = Boolean(clock?.grandmasterIdentity?.trim())
    || clock?.offsetFromMasterNs != null
    || clock?.meanPathDelayNs != null
    || clock?.systemClockDisciplined === true
    || clock?.sensorClockMapped === true;
  if (clock?.state === "faulty" && hasExternalClockEvidence) return "needs-attention";
  return "optional";
}

function clockTone(clock: RuntimeClock | undefined) {
  const status = externalClockStatus(clock);
  if (status === "connected") return "healthy";
  if (status === "needs-attention") return "faulty";
  return "optional";
}

function formatNanoseconds(value: number | null | undefined) {
  if (value == null) return "--";
  const absolute = Math.abs(value);
  if (absolute < 1000) return `${value.toFixed(0)} ns`;
  if (absolute < 1_000_000) return `${(value / 1000).toFixed(1)} µs`;
  return `${(value / 1_000_000).toFixed(3)} ms`;
}

function formatMilliseconds(value: number | null | undefined) {
  return value == null ? "--" : `${value.toFixed(3)} ms`;
}
