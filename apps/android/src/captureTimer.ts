type CaptureTiming = {
  elapsedMs?: number;
  startedAtDeviceTimeNs?: string;
};

export type CaptureTimer = {
  captureId: string;
  elapsedMs: number;
  sampledAtMs: number;
  startedAtDeviceTimeNs?: string;
};

export function captureTimerElapsedMs(timer: CaptureTimer | null, nowMs: number) {
  return timer == null ? 0 : timer.elapsedMs + Math.max(0, nowMs - timer.sampledAtMs);
}

export function observeCaptureTimer(
  previous: CaptureTimer | null,
  captureId: string,
  timing: CaptureTiming,
  deviceTimeNs: string | undefined,
  nowMs: number,
): CaptureTimer {
  const current = previous?.captureId === captureId ? previous : null;
  const startedAtDeviceTimeNs = timing.startedAtDeviceTimeNs ?? current?.startedAtDeviceTimeNs;
  let elapsedMs = timing.elapsedMs;
  if (elapsedMs == null || !Number.isFinite(elapsedMs) || elapsedMs < 0) {
    elapsedMs = undefined;
    if (deviceTimeNs && startedAtDeviceTimeNs && /^\d+$/.test(deviceTimeNs) && /^\d+$/.test(startedAtDeviceTimeNs)) {
      // Both values belong to the device clock, not the phone's calendar clock.
      const differenceNs = BigInt(deviceTimeNs) - BigInt(startedAtDeviceTimeNs);
      const differenceMs = Number(differenceNs) / 1_000_000;
      if (differenceNs >= 0n && Number.isFinite(differenceMs)) elapsedMs = differenceMs;
    }
  }
  return {
    captureId,
    elapsedMs: Math.max(captureTimerElapsedMs(current, nowMs), elapsedMs ?? 0),
    sampledAtMs: nowMs,
    startedAtDeviceTimeNs,
  };
}
