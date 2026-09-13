import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const runtime = window as typeof window & {
      __previewOptions?: { urls: string[]; labels: string[] };
      __captureState?: "idle" | "recording";
      __captureId?: string;
      __probeOptions?: Record<string, unknown>;
      __probeCalls?: number;
      androidBridge?: object;
      Capacitor?: Record<string, unknown>;
    };
    runtime.androidBridge = {};
    runtime.__captureState = "idle";
    runtime.__probeCalls = 0;

    const cameras = [
      {
        id: "cam0",
        label: "LEFT",
        direction: "设备左目",
        mountPosition: "stereo_left",
        preview: { transport: "tcp-hevc", url: "tcp://127.0.0.1:9100" },
      },
      {
        id: "cam1",
        label: "RIGHT",
        direction: "设备右目",
        mountPosition: "stereo_right",
        preview: { transport: "tcp-hevc", url: "tcp://127.0.0.1:9101" },
      },
    ];
    const metric = { current: 0.08, mean: 0.07, p95: 0.09, min: 0.04, max: 0.12, samples: 240 };
    const emptyMetric = { current: null, mean: null, p95: null, min: null, max: null, samples: 0 };
    const storage = {
      target: "internal",
      capturePolicy: { minimumFreeBytes: 536_870_912, estimatedBytesPerSecond: 1_500_000 },
      internal: { totalBytes: 8_000_000_000, freeBytes: 6_000_000_000, canCapture: true, reason: null },
      usb: {
        available: true,
        mounted: true,
        label: "SDCARD",
        mediaType: "sd",
        displayName: "SD 卡",
        totalBytes: 64_000_000_000,
        freeBytes: 48_000_000_000,
        canCapture: true,
        reason: null,
      },
    };

    runtime.Capacitor = {
      PluginHeaders: [{
        name: "SynCapDevice",
        methods: [
          "getStatus", "getManifest", "probe", "listSessions", "configureStorage",
          "startCapture", "stopCapture", "downloadSession", "openPreview",
        ].map((name) => ({ name, rtype: "promise" })),
      }],
      nativePromise: async (_plugin: string, method: string, options: Record<string, unknown> = {}) => {
        if (method === "getManifest") {
          return {
            protocolVersion: "1.0",
            device: {
              id: "tina-stereo-fixture",
              displayName: "Tina 双目相机",
              model: "Allwinner Tina Stereo",
              type: "allwinner-tina",
              firmwareVersion: "0.1.0",
            },
            capabilities: ["camera.preview", "capture.device", "session.export", "sync.metrics", "storage.sd"],
            cameras,
          };
        }
        if (method === "getStatus") {
          const recoveryRequired = new URLSearchParams(window.location.search).has("recovery");
          const storageState = new URLSearchParams(window.location.search).get("storageState");
          const currentStorage = storageState ? {
            ...storage,
            target: "usb",
            usb: {
              ...storage.usb,
              available: storageState !== "missing",
              mounted: storageState !== "missing" && storageState !== "unmounted",
              canCapture: storageState === "ready",
              freeBytes: storageState === "ready" ? 48_000_000_000 : 0,
              reason: storageState === "missing" ? "not_available"
                : storageState === "unmounted" ? "not_mounted"
                  : storageState === "low" ? "insufficient_free_space" : null,
            },
          } : storage;
          return {
            v: "1.0",
            deviceTimeNs: "123",
            adapter: "allwinner-tina-stereo",
            cameraDemoRunning: true,
            cameras: cameras.map((camera) => ({
              id: camera.id,
              port: camera.id === "cam0" ? 9100 : 9101,
              online: !new URLSearchParams(window.location.search).has("serviceOffline"),
              fps: 30,
              groupSkewMs: new URLSearchParams(window.location.search).has("unmeasuredSync") ? null : camera.id === "cam0" ? 0 : 0.08,
            })),
            sync: {
              method: "hardware_trigger",
              state: "hardware_trigger_active",
              hardwareTrigger: true,
              counterHealth: "stable",
              observedTriggerCount: 240,
              acquisitionSkewMs: new URLSearchParams(window.location.search).has("unmeasuredSync") ? emptyMetric : metric,
              previewPtsSkewMs: emptyMetric,
              previewCameraSkewMs: emptyMetric,
            },
            system: { uptimeSeconds: 100, temperatureC: 49, storage: { totalGiB: 8, availableGiB: 6 } },
            capture: recoveryRequired
              ? {
                  state: "failed",
                  recoveryRequired: true,
                  rebootRequired: true,
                  failureReason: "qgapp exited",
                  producerState: "intentionally_stopped",
                }
              : { state: runtime.__captureState, captureId: runtime.__captureId, storage: { target: "internal" } },
            storage: currentStorage,
            wifi: { state: "connected", ssid: "Field-Hotspot", ipAddress: "192.168.1.12" },
          };
        }
        if (method === "probe") {
          runtime.__probeCalls = (runtime.__probeCalls ?? 0) + 1;
          runtime.__probeOptions = options;
          const ports = options.ports as number[];
          const paths = options.paths as string[];
          const transports = options.transports as string[];
          return {
            host: String(options.host),
            reachable: !new URLSearchParams(window.location.search).has("probeOffline"),
            onlineCount: new URLSearchParams(window.location.search).has("probeOffline") ? 0 : 2,
            streams: ports.map((port, index) => ({
              port,
              transport: transports[index],
              path: paths[index],
              online: !new URLSearchParams(window.location.search).has("probeOffline"),
              url: transports[index] === "tcp-hevc"
                ? `tcp://${options.host}:${port}`
                : `rtsp://${options.host}:${port}${paths[index]}`,
            })),
          };
        }
        if (method === "listSessions") {
          const session = {
            id: "ses_20260904_170000_abcdef",
            name: "tina-session",
            createdAt: "2026-09-04T17:00:00+08:00",
            durationMs: 10_000,
            sizeBytes: 2_000_000,
            status: "complete",
            fileCount: 3,
          };
          return {
            sessions: new URLSearchParams(window.location.search).has("damagedSessions") ? [
              { ...session, id: "damaged-metadata", name: "damaged-metadata", status: "failed", exportAvailable: false, failureReason: "session.metadata_corrupt" },
              { ...session, id: "damaged-data", name: "damaged-data", status: "failed", exportAvailable: false, failureReason: "session.data_corrupt" },
              { ...session, id: "saved-fragments", name: "saved-fragments", status: "failed", exportAvailable: true },
            ] : [session],
          };
        }
        if (method === "configureStorage") return { ...storage, target: options.target };
        if (method === "startCapture") {
          runtime.__captureState = "recording";
          runtime.__captureId = "cap_20260904_170000_abcdef";
          return {
            captureId: runtime.__captureId,
            sessionId: "ses_20260904_170000_abcdef",
            state: "recording",
            startedAtDeviceTimeNs: "123",
          };
        }
        if (method === "stopCapture") {
          runtime.__captureState = "idle";
          runtime.__captureId = undefined;
          return {
            captureId: "cap_20260904_170000_abcdef",
            sessionId: "ses_20260904_170000_abcdef",
            state: "completed",
            session: { id: "ses_20260904_170000_abcdef", status: "complete" },
          };
        }
        if (method === "downloadSession") {
          return {
            sessionId: String(options.sessionId),
            path: "/mock/SynCap/tina-session",
            bytes: 2_001_024,
            files: 4,
            verifiedFiles: 3,
            manifestSaved: true,
            manifestFile: "session.json",
            verified: true,
          };
        }
        if (method === "openPreview") {
          runtime.__previewOptions = options as { urls: string[]; labels: string[] };
          return {};
        }
        throw new Error(`Unexpected native method: ${method}`);
      },
    };
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto("/?shell=desktop");
  await expect(page.getByTestId("device-selector")).toContainText("Allwinner Tina Stereo");
});

test("hardware trigger health is not presented as a measured zero timing error", async ({ page }) => {
  await page.goto("/?unmeasuredSync");
  await expect(page.getByText("误差未测量")).toBeVisible();
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /同步状态/ }).click();
  const diagnostics = page.getByTestId("sync-diagnostics-page");
  await expect(diagnostics).toContainText("硬件触发工作中");
  await expect(diagnostics).toContainText("时间误差未测量，不能据此判断曝光同步精度");
  await expect(diagnostics).not.toContainText("0.000 ms");
  await expect(diagnostics).not.toContainText("2 路相机内部同步正常");
});

test("two-camera devices preserve manifest order and fill the multi-camera layouts", async ({ page }) => {
  await expect(page.getByTestId("view-grid")).toContainText("2 路总览");
  await expect(page.getByTestId("storage-usb")).toContainText("SD 卡");
  await expect(page.getByTestId("storage-usb")).not.toContainText("U 盘");
  await expect(page.locator(".camera-grid .camera-tile header span")).toHaveText([
    "LEFT · 左目",
    "RIGHT · 右目",
  ]);
  await page.waitForTimeout(4_300);
  expect(await page.evaluate(() => (
    (window as typeof window & { __probeCalls?: number }).__probeCalls
  ))).toBe(0);

  const grid = await page.locator(".camera-grid").boundingBox();
  const gridTiles = await page.locator(".camera-grid .camera-tile").evaluateAll((tiles) => (
    tiles.map((tile) => ({ left: tile.getBoundingClientRect().left, right: tile.getBoundingClientRect().right }))
  ));
  expect(grid).not.toBeNull();
  expect(gridTiles).toHaveLength(2);
  expect(gridTiles[0].left).toBeCloseTo(grid!.x, 0);
  expect(gridTiles[1].right).toBeCloseTo(grid!.x + grid!.width, 0);

  await page.getByRole("button", { name: /打开实时预览 · 2 路可用/ }).click();
  await expect.poll(() => page.evaluate(() => (
    (window as typeof window & { __previewOptions?: { urls: string[]; labels: string[] } }).__previewOptions
  ))).toEqual({
    urls: ["tcp://192.168.1.12:9100", "tcp://192.168.1.12:9101"],
    labels: ["LEFT · 左目", "RIGHT · 右目"],
  });

  await page.getByTestId("view-ready").evaluate((button) => (button as HTMLButtonElement).click());
  await expect(page.locator(".ready-hero")).toContainText("2 路相机内部同步已就绪");
  await expect(page.locator(".readiness-row", { hasText: "运动传感器" })).toHaveCount(0);
  const contactSheet = await page.locator(".camera-contact-sheet").boundingBox();
  const contactTiles = await page.locator(".camera-contact-sheet > div").evaluateAll((tiles) => (
    tiles.map((tile) => ({ left: tile.getBoundingClientRect().left, right: tile.getBoundingClientRect().right }))
  ));
  expect(contactSheet).not.toBeNull();
  expect(Math.abs(contactTiles[0].left - contactSheet!.x)).toBeLessThanOrEqual(1);
  expect(Math.abs(contactTiles[1].right - (contactSheet!.x + contactSheet!.width))).toBeLessThanOrEqual(1);
});

for (const [state, label] of [
  ["missing", "未插入"],
  ["unmounted", "未就绪"],
  ["low", "空间不足"],
  ["ready", "SD 卡可用"],
]) {
  test(`device storage reports SD card ${state} truthfully`, async ({ page }) => {
    await page.goto(`/?shell=desktop&storageState=${state}`);
    await page.getByTestId("nav-device").click();
    const storageStatus = page.getByTestId("device-storage-status");
    await expect(storageStatus).toContainText("SD 卡");
    await expect(storageStatus).toContainText(label);
    if (state !== "ready") {
      await expect(storageStatus).not.toContainText("可用");
      await expect(storageStatus).not.toContainText("0 B");
    } else {
      await expect(storageStatus).toContainText("44.7 GB");
    }
  });
}

test("raw TCP preview ports are never actively probed when service telemetry is unavailable", async ({ page }) => {
  await page.goto("/?shell=desktop&serviceOffline=1&probeOffline=1");
  await expect(page.getByTestId("device-selector")).toContainText("Allwinner Tina Stereo");
  await page.waitForTimeout(4_300);
  expect(await page.evaluate(() => (
    (window as typeof window & { __probeCalls?: number }).__probeCalls
  ))).toBe(0);
  await expect(page.getByRole("button", { name: "没有可用实时流" })).toBeDisabled();
});

test("capture recovery is persistent, explicit and cannot be bypassed", async ({ page }) => {
  await page.goto("/?shell=desktop&recovery=1");
  await expect(page.getByTestId("capture-recovery-banner")).toContainText("设备需要重启");
  await expect(page.getByTestId("capture-recovery-banner")).toContainText("已保存片段可在数据页导出");
  await expect(page.getByTestId("capture-button")).toBeDisabled();
  await expect(page.getByTestId("capture-button")).toContainText("请重启设备后再采集");
});

test("two-camera capture, synchronization and export copy reflect declared capabilities", async ({ page }) => {
  await page.getByTestId("storage-usb").click();
  await page.getByTestId("capture-button").click();
  await expect(page.getByText("2 路相机已开始写入SD 卡")).toBeVisible();

  await page.getByTestId("nav-device").click();
  const calibrationAction = page.getByRole("button", { name: /相机位置标定/ });
  await expect(calibrationAction).toBeDisabled();
  await expect(calibrationAction).toContainText("当前设备暂不支持全分辨率标定");
  await expect(calibrationAction).not.toContainText("需要四路相机");
  await expect(page.getByText("2 路相机同步")).toBeVisible();
  await page.getByRole("button", { name: /同步状态/ }).click();
  await expect(page.getByTestId("sync-diagnostics-page")).toContainText("2 路相机内部同步正常");
  await page.getByRole("button", { name: "关闭同步诊断" }).click();

  await page.getByTestId("nav-data").click();
  await page.getByRole("button", { name: "导出 tina-session" }).click();
  await expect(page.getByText("包含 2 路视频和会话信息")).toBeVisible();
  await page.getByRole("button", { name: "开始导出" }).click();
  await expect(page.getByText(/会话清单已保存 · 3 个数据文件通过 SHA-256 校验/)).toBeVisible();
});

test("corrupt sessions explain the damage and disable export even when files are listed", async ({ page }) => {
  await page.goto("/?shell=desktop&damagedSessions");
  await page.getByTestId("nav-data").click();
  const metadata = page.locator(".session-list article", { hasText: "damaged-metadata" });
  const data = page.locator(".session-list article", { hasText: "damaged-data" });
  await expect(metadata).toContainText("清单损坏");
  await expect(data).toContainText("文件损坏");
  await expect(metadata.getByRole("button", { name: "导出 damaged-metadata" })).toBeDisabled();
  await expect(data.getByRole("button", { name: "导出 damaged-data" })).toBeDisabled();
  await expect(metadata).not.toContainText("可导出已保存文件");
  await expect(data).not.toContainText("可导出已保存文件");
});

test("failed captures with valid saved fragments remain exportable", async ({ page }) => {
  await page.goto("/?shell=desktop&damagedSessions");
  await page.getByTestId("nav-data").click();
  const partial = page.locator(".session-list article", { hasText: "saved-fragments" });
  await expect(partial).toContainText("采集未完成 · 可导出已保存文件");
  await partial.getByRole("button", { name: "导出 saved-fragments" }).click();
  await expect(page.getByRole("button", { name: "开始导出" })).toBeEnabled();
});
