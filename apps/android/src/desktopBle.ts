import { invoke } from "@tauri-apps/api/core";
import { listen, type UnlistenFn } from "@tauri-apps/api/event";

export type DesktopBleTelemetry = {
  recording: boolean;
  preview: boolean;
  batteryPercent: number | null;
  voltageUv: number | null;
  charging: boolean | null;
  full: boolean;
  sdMounted: boolean;
  sdFreeMib: number;
  captureState: "idle" | "recording" | "finalizing" | "unknown";
  elapsedMs: number;
  sequence: number;
};

export type DesktopBleTelemetryEvent = {
  state: "connecting" | "connected" | "disconnected" | "error";
  address: string;
  telemetry: DesktopBleTelemetry | null;
  error: string | null;
};

export function listenDesktopBleTelemetry(
  callback: (event: DesktopBleTelemetryEvent) => void,
): Promise<UnlistenFn> {
  return listen<DesktopBleTelemetryEvent>("syncap://ble-telemetry", (event) => {
    callback(event.payload);
  });
}

export function startDesktopBleTelemetry(address: string) {
  return invoke<void>("start_ble_telemetry", { address });
}

export function stopDesktopBleTelemetry() {
  return invoke<void>("stop_ble_telemetry");
}
