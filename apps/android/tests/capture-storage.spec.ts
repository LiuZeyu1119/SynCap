import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const runtime = window as typeof window & {
      __captureStartCalls?: number;
      __captureState?: "idle" | "recording";
      __captureId?: string;
      __captureStartedAtMs?: number;
      __captureElapsedMs?: number;
      __captureClockFieldsMissing?: boolean;
      __statusCalls?: number;
      __configureStorageCalls?: number;
      __configuredStorage?: "internal" | "usb";
      __startResponseLost?: boolean;
      __stopResponseLost?: boolean;
      __stopFailure?: boolean;
      androidBridge?: object;
      Capacitor?: Record<string, unknown>;
    };
    runtime.androidBridge = {};
    runtime.__captureStartCalls = 0;
    runtime.__captureState = "idle";
    runtime.__statusCalls = 0;
    runtime.__configureStorageCalls = 0;
    runtime.__configuredStorage = "usb";

    const storage = (target: "internal" | "usb") => {
      const lowInternalSpace = window.localStorage.getItem("syncap.test.lowStorage") === "1";
      return ({
      target,
      capturePolicy: { minimumFreeBytes: 536_870_912, estimatedBytesPerSecond: 2_250_000 },
      internal: {
        totalBytes: 4_966_912_000,
        freeBytes: lowInternalSpace ? 100_000_000 : 4_043_055_104,
        canCapture: !lowInternalSpace,
        reason: lowInternalSpace ? "insufficient_free_space" : null,
      },
      usb: {
        available: false,
        mounted: false,
        label: "SYNCAP",
        canCapture: false,
        reason: "not_available",
      },
      });
    };

    runtime.Capacitor = {
      PluginHeaders: [{
        name: "SynCapDevice",
        methods: ["getStatus", "getManifest", "probe", "listSessions", "configureStorage", "startCapture", "stopCapture"]
          .map((name) => ({ name, rtype: "promise" })),
      }],
      nativePromise: async (_plugin: string, method: string, options: Record<string, unknown>) => {
        if (method === "getManifest") {
          return {
            protocolVersion: "1.0",
            device: { id: "capture-test", displayName: "Head Ring", model: "SynCap 4P" },
            capabilities: ["camera.preview", "capture.device", "session.export"],
            cameras: [554, 555, 556, 557].map((port, index) => ({
              id: `cam${index}`,
              direction: ["front", "right", "rear", "left"][index],
              preview: { transport: "rtsp", url: `rtsp://192.168.100.211:${port}/PRR` },
            })),
          };
        }
        if (method === "getStatus") {
          runtime.__statusCalls = (runtime.__statusCalls ?? 0) + 1;
          const missingCamera = window.localStorage.getItem("syncap.test.missingCamera") === "1";
          return {
            v: "1.0",
            deviceTimeNs: String(Math.round((343_237 + performance.now()) * 1_000_000)),
            adapter: "syncap-device-service",
            cameraDemoRunning: true,
            cameras: [554, 555, 556, 557].map((port, index) => ({ id: `cam${index}`, port, online: !(missingCamera && index === 2), fps: 60 })),
            sync: {
              acquisitionSkewMs: { current: 0.04, mean: 0.04, p95: 0.05, min: 0.03, max: 0.08, samples: 100 },
              previewPtsSkewMs: { current: null, mean: null, p95: null, min: null, max: null, samples: 0 },
              previewCameraSkewMs: { current: null, mean: null, p95: null, min: null, max: null, samples: 0 },
            },
            system: { uptimeSeconds: 100, temperatureC: 60, storage: { totalGiB: 22.9, availableGiB: 18.6 } },
            capture: {
              state: runtime.__captureState ?? "idle",
              captureId: runtime.__captureId,
              startedAt: runtime.__captureState === "recording" ? "1970-01-05T00:00:00Z" : undefined,
              startedAtDeviceTimeNs: runtime.__captureClockFieldsMissing || runtime.__captureStartedAtMs == null
                ? undefined : String(Math.round((343_237 + runtime.__captureStartedAtMs) * 1_000_000)),
              elapsedMs: runtime.__captureElapsedMs,
              storage: { target: runtime.__configuredStorage ?? "usb" },
            },
            storage: storage(runtime.__configuredStorage ?? "usb"),
            wifi: { state: "connected", ssid: "HUAWEI_PURA_X", ipAddress: "192.168.100.211" },
          };
        }
        if (method === "probe") {
          const missingCamera = window.localStorage.getItem("syncap.test.missingCamera") === "1";
          return {
            host: "192.168.100.211",
            reachable: true,
            onlineCount: 4,
            streams: [554, 555, 556, 557].map((port, index) => ({ port, online: !(missingCamera && index === 2), url: `rtsp://192.168.100.211:${port}/PRR` })),
          };
        }
        if (method === "listSessions") return { sessions: [] };
        if (method === "configureStorage") {
          runtime.__configureStorageCalls = (runtime.__configureStorageCalls ?? 0) + 1;
          if (options.target === "usb") throw new Error("SYNCAP USB drive is not available");
          runtime.__configuredStorage = "internal";
          return storage("internal");
        }
        if (method === "startCapture") {
          runtime.__captureStartCalls = (runtime.__captureStartCalls ?? 0) + 1;
          runtime.__captureState = "recording";
          runtime.__captureId = "cap_20260814_151500_abcdef";
          runtime.__captureStartedAtMs = performance.now();
          if (runtime.__startResponseLost) throw new Error("Unable to start capture");
          return {
            captureId: "cap_20260814_151500_abcdef", sessionId: "ses_20260814_151500_abcdef", state: "recording",
            startedAtDeviceTimeNs: String(Math.round((343_237 + runtime.__captureStartedAtMs) * 1_000_000)),
          };
        }
        if (method === "stopCapture") {
          runtime.__captureState = "idle";
          runtime.__captureId = undefined;
          if (runtime.__stopResponseLost) throw new Error("Unable to stop capture");
          if (runtime.__stopFailure) {
            throw new Error("capture_finalization_failed: video integrity check failed");
          }
          return {
            captureId: "cap_20260814_151500_abcdef",
            sessionId: "ses_20260814_151500_abcdef",
            state: "completed",
            session: { id: "ses_20260814_151500_abcdef", status: "complete" },
          };
        }
        throw new Error(`Unexpected native method: ${method}`);
      },
    };
  });
  await page.setViewportSize({ width: 412, height: 820 });
  await page.goto("/");
  await expect(page.getByTestId("capture-storage-selector")).toBeVisible();
});

test("capture requires an explicit usable storage choice and never falls back from USB", async ({ page }) => {
  const capture = page.getByTestId("capture-button");
  await expect(capture).toBeDisabled();
  await expect(capture).toContainText("请先选择存储位置");
  await expect(page.getByTestId("storage-usb")).toBeDisabled();
  await expect(page.getByTestId("storage-usb")).toContainText("未检测到标记为 SYNCAP");

  await page.getByTestId("storage-internal").click();
  await expect(page.getByTestId("storage-internal")).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByText(/设备本地已就绪，预计可录制/)).toBeVisible();
  await expect(capture).toBeEnabled();
  await page.evaluate(() => {
    (window as typeof window & { __configureStorageCalls?: number }).__configureStorageCalls = 0;
  });

  await capture.click();
  await expect(capture).toContainText("停止采集");
  const calls = await page.evaluate(() => {
    const runtime = window as typeof window & {
      __captureStartCalls?: number;
      __configureStorageCalls?: number;
    };
    return {
      start: runtime.__captureStartCalls ?? 0,
      configureStorage: runtime.__configureStorageCalls ?? 0,
    };
  });
  expect(calls).toEqual({ start: 1, configureStorage: 0 });
});

test("an externally stopped capture returns the app to idle", async ({ page }) => {
  await page.getByTestId("storage-internal").click();
  await page.getByTestId("capture-button").click();
  await expect(page.getByTestId("capture-button")).toContainText("停止采集");
  const statusCalls = await page.evaluate(() => (window as typeof window & { __statusCalls?: number }).__statusCalls ?? 0);
  await page.waitForFunction((minimum) => (
    ((window as typeof window & { __statusCalls?: number }).__statusCalls ?? 0) > minimum
  ), statusCalls, { timeout: 5_500 });
  await page.evaluate(() => {
    const runtime = window as typeof window & { __captureState?: "idle" | "recording"; __captureId?: string };
    runtime.__captureState = "idle";
    runtime.__captureId = undefined;
  });
  await expect(page.getByTestId("capture-button")).toContainText("开始采集", { timeout: 5_500 });
});

test("an unsynchronized device calendar cannot turn recording time into decades", async ({ page }) => {
  await page.getByTestId("storage-internal").click();
  const capture = page.getByTestId("capture-button");
  await capture.click();
  await expect(capture).toContainText("停止采集 · 00:00:00");
  await expect(capture).toContainText(/停止采集 · 00:00:0[4-8]/, { timeout: 9_000 });
});

test("reconnecting adopts device elapsed time and continues between status polls", async ({ page }) => {
  await page.evaluate(() => {
    const runtime = window as typeof window & {
      __captureState?: string; __captureId?: string; __captureElapsedMs?: number;
    };
    runtime.__captureState = "recording";
    runtime.__captureId = "externally_started";
    runtime.__captureElapsedMs = 65_000;
  });
  const capture = page.getByTestId("capture-button");
  await expect(capture).toContainText("停止采集 · 00:01:05", { timeout: 6_000 });
  await expect(capture).toContainText(/停止采集 · 00:01:0[6-9]/, { timeout: 5_000 });
});

test("a lost start response with an old device clock still starts at zero", async ({ page }) => {
  await page.getByTestId("storage-internal").click();
  await page.evaluate(() => {
    const runtime = window as typeof window & { __startResponseLost?: boolean; __captureClockFieldsMissing?: boolean };
    runtime.__startResponseLost = true;
    runtime.__captureClockFieldsMissing = true;
  });
  const capture = page.getByTestId("capture-button");
  await capture.click();
  await expect(capture).toContainText("停止采集 · 00:00:00");
  await expect(capture).toContainText(/停止采集 · 00:00:0[1-3]/);
});

test("a lost start response adopts the active device capture without another click", async ({ page }) => {
  await page.getByTestId("storage-internal").click();
  await page.evaluate(() => {
    (window as typeof window & { __startResponseLost?: boolean }).__startResponseLost = true;
  });

  const capture = page.getByTestId("capture-button");
  await capture.click();

  await expect(capture).toContainText("停止采集");
  await expect(page.getByText("设备已确认开始采集，无需再次点击")).toBeVisible();
  const calls = await page.evaluate(() => (
    window as typeof window & { __captureStartCalls?: number }
  ).__captureStartCalls);
  expect(calls).toBe(1);
});

test("a lost stop response adopts the idle device state", async ({ page }) => {
  await page.getByTestId("storage-internal").click();
  const capture = page.getByTestId("capture-button");
  await capture.click();
  await expect(capture).toContainText("停止采集");
  await page.evaluate(() => {
    (window as typeof window & { __stopResponseLost?: boolean }).__stopResponseLost = true;
  });

  await capture.click();

  await expect(capture).toContainText("开始采集");
  await expect(page.getByText("设备已停止采集，数据页面已更新")).toBeVisible();
});

test("a failed stop response follows device truth and leaves recording mode", async ({ page }) => {
  await page.getByTestId("storage-internal").click();
  const capture = page.getByTestId("capture-button");
  await capture.click();
  await expect(capture).toContainText("停止采集");
  await page.evaluate(() => {
    (window as typeof window & { __stopFailure?: boolean }).__stopFailure = true;
  });

  await capture.click();

  await expect(capture).toContainText("开始采集");
  await expect(page.getByText("采集已停止，但文件完整性检查未通过，请在数据页面查看记录")).toBeVisible();
});

test("legacy system free space is not shown as the unavailable USB capacity", async ({ page }) => {
  await expect(page.getByTestId("capture-grid")).not.toContainText("18.6 GB");
  await expect(page.getByTestId("capture-grid")).toContainText("预计可录制--");
});

test("insufficient local and USB space is explained before capture", async ({ page }) => {
  await page.evaluate(() => window.localStorage.setItem("syncap.test.lowStorage", "1"));
  await page.reload();
  await expect(page.getByTestId("storage-internal")).toBeDisabled();
  await expect(page.getByTestId("storage-internal")).toContainText("可用空间不足，至少需保留 512.0 MB");
  await expect(page.getByTestId("storage-usb")).toBeDisabled();
  await expect(page.getByTestId("capture-button")).toBeDisabled();
});

test("capture stays blocked until all four cameras are ready", async ({ page }) => {
  await page.evaluate(() => window.localStorage.setItem("syncap.test.missingCamera", "1"));
  await page.reload();
  await page.getByTestId("storage-internal").click();

  const capture = page.getByTestId("capture-button");
  await expect(capture).toBeDisabled();
  await expect(capture).toContainText("等待相机全部就绪 · 3/4");
  const calls = await page.evaluate(() => (window as typeof window & { __captureStartCalls?: number }).__captureStartCalls);
  expect(calls).toBe(0);
});
