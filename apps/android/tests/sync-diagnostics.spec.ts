import { expect, test } from "@playwright/test";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    const runtime = window as typeof window & {
      __probeDelayMs?: number;
      __probeCalls?: number;
      __failNextStatus?: boolean;
      __statusCamerasOnline?: boolean;
      __previewOptions?: { urls: string[]; labels: string[] };
      __calibrationOptions?: Record<string, unknown>;
      __cameraOrientationOptions?: Record<string, unknown>;
      androidBridge?: object;
      Capacitor?: Record<string, unknown>;
    };
    (window as typeof window & { androidBridge?: object }).androidBridge = {};
    (window as typeof window & { Capacitor?: Record<string, unknown> }).Capacitor = {
      PluginHeaders: [{
        name: "SynCapDevice",
        methods: ["getStatus", "getManifest", "probe", "listSessions", "openPreview", "openCalibration", "getCameraConfiguration", "configureCameraOrientation"].map((name) => ({ name, rtype: "promise" })),
      }],
      nativePromise: async (_plugin: string, method: string, options: Record<string, unknown> = {}) => {
        if (method === "getManifest") {
          return {
            protocolVersion: "1.0",
            device: { id: "headring-test", displayName: "Head Ring 001", model: "SynCap 4P" },
            capabilities: ["camera.preview", "sync.metrics", "sensor.imu", "camera.orientation", "calibration.raw_snapshot"],
            cameras: [554, 555, 556, 557].map((port, index) => ({
              id: `cam${index}`,
              direction: ["front", "right", "rear", "left"][index],
              preview: { transport: "rtsp", url: `rtsp://192.168.1.12:${port}/PRR` },
            })),
          };
        }
        if (method === "getStatus") {
          if (runtime.__failNextStatus) {
            runtime.__failNextStatus = false;
            throw new Error("status unavailable");
          }
          const clockState = window.localStorage.getItem("syncap.test.clockState") ?? "listening";
          const grandmasterIdentity = window.localStorage.getItem("syncap.test.grandmasterIdentity");
          return {
            v: "1.0",
            deviceTimeNs: "123",
            adapter: "syncap-device-service",
            cameraDemoRunning: true,
            cameras: [554, 555, 556, 557].map((port, index) => ({
              id: `cam${index}`,
              port,
              online: runtime.__statusCamerasOnline !== false,
              fps: 60,
              groupSkewMs: 0.04 + index * 0.002,
            })),
            sync: {
              acquisitionSkewMs: { current: 0.043, mean: 0.044, p95: 0.052, min: 0.036, max: 0.084, samples: 937 },
              previewPtsSkewMs: { current: null, mean: null, p95: null, min: null, max: null, samples: 0 },
              previewCameraSkewMs: { current: null, mean: null, p95: null, min: null, max: null, samples: 0 },
            },
            clock: {
              source: "ptp", state: clockState, domain: 0, interface: "eth0", hardwareTimestamping: true,
              phcDevice: "/dev/ptp0", ptp4lRunning: true, phc2sysRunning: true, grandmasterIdentity,
              offsetFromMasterNs: null, meanPathDelayNs: null, lastUpdateAgeMs: null,
              systemClockDisciplined: false, sensorClockMapped: false,
            },
            system: { uptimeSeconds: 100, temperatureC: 55, storage: { totalGiB: 22.9, availableGiB: 18.6 } },
            capture: { state: "idle" },
            wifi: { state: "connected", ssid: "Test-Hotspot", ipAddress: "192.168.100.211" },
            cameraConfiguration: {
              rotationDegrees: 0, frameRate: 60, outputWidth: 1280, outputHeight: 1088,
              supportedRotations: [0, 90, 180, 270], appliesTo: ["preview", "calibration", "capture"],
            },
          };
        }
        if (method === "probe") {
          runtime.__probeCalls = (runtime.__probeCalls ?? 0) + 1;
          const delayMs = runtime.__probeDelayMs ?? 0;
          runtime.__probeDelayMs = 0;
          if (delayMs > 0) await new Promise((resolve) => setTimeout(resolve, delayMs));
          return {
            host: "192.168.1.12",
            reachable: true,
            onlineCount: 4,
            streams: [554, 555, 556, 557].map((port) => ({ port, online: true, url: `rtsp://192.168.1.12:${port}/PRR` })),
          };
        }
        if (method === "listSessions") return { sessions: [] };
        if (method === "openPreview") {
          runtime.__previewOptions = options as { urls: string[]; labels: string[] };
          return {};
        }
        if (method === "openCalibration") {
          runtime.__calibrationOptions = options;
          return {};
        }
        if (method === "configureCameraOrientation") {
          runtime.__cameraOrientationOptions = options;
          return {
            rotationDegrees: options.rotationDegrees, frameRate: 60, outputWidth: 1088, outputHeight: 1280,
            supportedRotations: [0, 90, 180, 270], appliesTo: ["preview", "calibration", "capture"], state: "restarting",
          };
        }
        throw new Error(`Unexpected native method: ${method}`);
      },
    };
  });
  await page.setViewportSize({ width: 412, height: 820 });
  await page.goto("/");
  await page.getByTestId("nav-device").click();
  await expect(page.getByRole("button", { name: /同步状态/ })).toBeVisible();
});

test("device page labels the observed Wi-Fi network instead of assuming Ethernet", async ({ page }) => {
  const hero = page.locator(".device-hero-card");
  await expect(hero).toContainText("Wi-Fi 在线");
  await expect(hero).toContainText("Test-Hotspot");
  await expect(page.getByText("有线网络")).toHaveCount(0);
});

test("camera surfaces use the physical order while preserving camera stream identity", async ({ page }) => {
  const expectedLabels = ["CAM 3 · 左外侧", "CAM 1 · 左内侧", "CAM 2 · 右内侧", "CAM 0 · 右外侧"];

  await expect(page.locator(".camera-deltas span")).toHaveText(expectedLabels);
  await expect(page.locator(".camera-deltas b")).toHaveText(["0.046 ms", "0.042 ms", "0.044 ms", "0.040 ms"]);

  await page.getByRole("button", { name: /同步状态/ }).click();
  await expect(page.locator(".camera-sync-list > div > span")).toHaveText([
    "CAM 3 · 左外侧",
    "CAM 1 · 左内侧",
    "CAM 2 · 右内侧",
    "CAM 0 · 右外侧",
  ]);
  await page.getByRole("button", { name: "关闭同步诊断" }).click();

  await page.getByTestId("nav-capture").click();
  await expect(page.locator(".camera-grid .camera-tile header span")).toHaveText([
    "CAM 3 · 左外侧",
    "CAM 1 · 左内侧",
    "CAM 2 · 右内侧",
    "CAM 0 · 右外侧",
  ]);

  await page.getByRole("button", { name: /打开实时预览 · 4 路可用/ }).click();
  await expect.poll(() => page.evaluate(() => (
    (window as typeof window & { __previewOptions?: { urls: string[]; labels: string[] } }).__previewOptions
  ))).toEqual({
    urls: [
      "rtsp://192.168.1.12:557/PRR",
      "rtsp://192.168.1.12:555/PRR",
      "rtsp://192.168.1.12:556/PRR",
      "rtsp://192.168.1.12:554/PRR",
    ],
    labels: ["CAM 3 · 左外侧", "CAM 1 · 左内侧", "CAM 2 · 右内侧", "CAM 0 · 右外侧"],
  });

  await page.getByTestId("view-monitor").click();
  await expect(page.locator(".monitor-heading")).toContainText("CAM 3 · 左外侧");
  expect(await page.locator(".camera-thumb").evaluateAll((buttons) => (
    buttons.map((button) => button.getAttribute("aria-label"))
  ))).toEqual([
    "CAM 3 · 左外侧",
    "CAM 1 · 左内侧",
    "CAM 2 · 右内侧",
    "CAM 0 · 右外侧",
  ]);

  await page.getByTestId("view-ready").click();
  await expect(page.locator(".camera-contact-sheet > div > span")).toHaveText([
    "CAM 3 · 左外侧",
    "CAM 1 · 左内侧",
    "CAM 2 · 右内侧",
    "CAM 0 · 右外侧",
  ]);
});

test("chessboard calibration explains the motion and opens the requested physical pair", async ({ page }) => {
  await page.getByRole("button", { name: /相机位置标定/ }).click();
  const sheet = page.getByTestId("calibration-sheet");
  await expect(sheet).toBeVisible();
  await expect(sheet.getByText("让两台相机同时看清完整棋盘格")).toBeVisible();
  await expect(sheet.getByText("中央正对")).toBeVisible();
  await expect(sheet.getByText("覆盖四角")).toBeVisible();
  await expect(sheet.getByText("远近变化")).toBeVisible();
  await expect(sheet.getByText("多向倾斜")).toBeVisible();
  await expect(sheet.locator(".calibration-board-demo")).toHaveCSS("animation-name", "calibration-board-tour");

  await expect(sheet.getByText("检测 9 × 6 个内部角点")).toBeVisible();
  const longInput = sheet.locator("#calibration-long");
  await longInput.fill("3");
  await expect(sheet.getByRole("button", { name: /开始拍摄标定照片/ })).toBeDisabled();
  await longInput.fill("11");
  await sheet.locator("#calibration-wide").fill("8");
  await sheet.locator("#calibration-square-size").fill("25.5");
  await sheet.getByRole("button", { name: /2–3 内侧组/ }).click();
  await sheet.getByRole("button", { name: /开始拍摄标定照片/ }).click();

  await expect.poll(() => page.evaluate(() => (
    (window as typeof window & { __calibrationOptions?: Record<string, unknown> }).__calibrationOptions
  ))).toEqual({
    pair: "pair23",
    cameraIds: ["cam1", "cam2"],
    urls: ["rtsp://192.168.1.12:555/PRR", "rtsp://192.168.1.12:556/PRR"],
    labels: ["CAM 1 · 左内侧", "CAM 2 · 右内侧"],
    squaresLong: 11,
    squaresWide: 8,
    squareSizeMm: 25.5,
  });
});

test("image orientation is one device source setting for preview calibration and capture", async ({ page }) => {
  const button = page.getByRole("button", { name: /图像方向/ });
  await expect(button).toContainText("0° · 预览、标定和采集保持一致");
  await button.click();
  const sheet = page.getByTestId("orientation-sheet");
  await expect(sheet).toContainText("统一调整 4 路图像方向");
  await expect(sheet).toContainText("预览、棋盘格标定和正式采集文件会同时修改并保持一致");
  await expect(sheet.getByRole("radio", { name: /旋转 180°/ })).toContainText("60 fps");
  await expect(sheet).toContainText("四个方向均保持 60 fps");
  await sheet.getByRole("radio", { name: /顺时针 90°/ }).click();
  await sheet.getByRole("button", { name: "应用到预览、标定与采集" }).click();
  await expect.poll(() => page.evaluate(() => (
    (window as typeof window & { __cameraOrientationOptions?: Record<string, unknown> }).__cameraOrientationOptions
  ))).toEqual({ host: "192.168.1.12", port: 8080, rotationDegrees: 90 });
});

test("sync diagnostics separates healthy internal sync from unconfigured and uncalibrated features", async ({ page }) => {
  await page.getByRole("button", { name: /同步状态/ }).click();
  const diagnostics = page.getByTestId("sync-diagnostics-page");
  await expect(diagnostics).toContainText("4 路相机内部同步正常");
  await expect(diagnostics.locator(".sync-context-card")).toContainText("未连接外部时间基准");
  await expect(diagnostics.locator(".sync-context-card > b")).toHaveText("可选");
  await expect(diagnostics.getByText("Grandmaster")).toHaveCount(0);
  await expect(diagnostics.locator(".trace-chain .uncalibrated")).toHaveCount(2);
  await expect(diagnostics.locator(".trace-chain .uncalibrated > b")).toHaveText(["未标定", "未标定"]);
  await expect(diagnostics.locator(".trace-chain .warning")).toHaveCount(0);
});

test("missing external time stays optional for legacy faulty and unsupported reports", async ({ page }) => {
  for (const state of ["faulty", "unsupported"]) {
    await page.evaluate((clockState) => {
      window.localStorage.setItem("syncap.test.clockState", clockState);
      window.localStorage.removeItem("syncap.test.grandmasterIdentity");
    }, state);
    await page.reload();
    await page.getByTestId("nav-device").click();
    await page.getByRole("button", { name: /同步状态/ }).click();

    const diagnostics = page.getByTestId("sync-diagnostics-page");
    await expect(diagnostics.locator(".sync-context-card")).toHaveClass(/unconfigured/);
    await expect(diagnostics.locator(".sync-context-card")).toContainText("未连接外部时间基准");
    await expect(diagnostics.locator(".sync-context-card > b")).toHaveText("可选");
    await expect(diagnostics.locator(".trace-chain .warning")).toHaveCount(0);
    await page.getByRole("button", { name: "关闭同步诊断" }).click();
  }

  await page.getByTestId("nav-capture").click();
  await page.getByTestId("view-ready").click();
  await expect(page.locator(".ready-hero")).toHaveClass(/healthy/);
  await expect(page.locator(".ready-hero")).toContainText("4 路相机内部同步已就绪");
  const optionalClock = page.locator(".readiness-row", { hasText: "外部时间同步" });
  await expect(optionalClock).toHaveClass(/optional/);
  await expect(optionalClock).toContainText("未连接（可选）");
});

test("a failed previously discovered external time source still asks for inspection", async ({ page }) => {
  await page.evaluate(() => {
    window.localStorage.setItem("syncap.test.clockState", "faulty");
    window.localStorage.setItem("syncap.test.grandmasterIdentity", "001122.fffe.334455");
  });
  await page.reload();
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /同步状态/ }).click();

  const diagnostics = page.getByTestId("sync-diagnostics-page");
  await expect(diagnostics.locator(".sync-context-card")).toHaveClass(/warning/);
  await expect(diagnostics.locator(".sync-context-card")).toContainText("外部时间同步需要检查");
  await expect(diagnostics.locator(".sync-context-card > b")).toHaveText("检查");
});

test("sync diagnostics is a safe full-screen page with explicit close and scroll", async ({ page }) => {
  await page.getByRole("button", { name: /同步状态/ }).click();

  const diagnostics = page.getByTestId("sync-diagnostics-page");
  const close = page.getByRole("button", { name: "关闭同步诊断" });
  await expect(diagnostics).toBeVisible();
  await expect(close).toBeVisible();
  await expect(close).toBeFocused();

  const refresh = page.getByRole("button", { name: "刷新同步诊断" });
  await page.keyboard.press("Shift+Tab");
  await expect(refresh).toBeFocused();
  await page.keyboard.press("Tab");
  await expect(close).toBeFocused();

  const pageBounds = await diagnostics.boundingBox();
  const closeBounds = await close.boundingBox();
  expect(pageBounds).not.toBeNull();
  expect(closeBounds).not.toBeNull();
  expect(pageBounds?.y).toBeGreaterThanOrEqual(0);
  expect(closeBounds?.y).toBeGreaterThanOrEqual(pageBounds?.y ?? 0);
  expect(closeBounds?.height).toBeGreaterThanOrEqual(44);

  const scroll = page.getByTestId("sync-diagnostics-scroll");
  await expect(scroll.getByText("曝光与运动传感器")).toBeAttached();
  await scroll.evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await expect(scroll.getByText("相机与运动传感器时间偏移")).toBeInViewport();

  await close.click();
  await expect(diagnostics).toHaveCount(0);
  await expect(page.getByTestId("tab-device")).toBeVisible();
});

test("Escape closes sync diagnostics without leaving the device page", async ({ page }) => {
  await page.getByRole("button", { name: /同步状态/ }).click();
  await expect(page.getByTestId("sync-diagnostics-page")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("sync-diagnostics-page")).toHaveCount(0);
  await expect(page.getByTestId("tab-device")).toBeVisible();
});

test("slow RTSP probing has a deadline and keeps a valid device service online", async ({ page }) => {
  await page.getByRole("button", { name: /同步状态/ }).click();
  const refresh = page.getByRole("button", { name: "刷新同步诊断" });
  await expect(refresh).toBeEnabled();
  const initialProbeCalls = await page.evaluate(() => {
    const runtime = window as typeof window & { __probeDelayMs?: number; __probeCalls?: number; __statusCamerasOnline?: boolean };
    runtime.__probeDelayMs = 10_000;
    runtime.__statusCamerasOnline = false;
    return (window as typeof window & { __probeCalls?: number }).__probeCalls ?? 0;
  });

  const startedAt = Date.now();
  await refresh.click();
  await expect(refresh).toBeDisabled();
  await expect(refresh).toBeEnabled({ timeout: 4_000 });
  expect(Date.now() - startedAt).toBeLessThan(3_600);
  await expect(page.getByTestId("sync-diagnostics-page")).toBeVisible();
  await expect(page.getByText("设备已连接 · 正在检查相机")).toBeVisible();
  await page.waitForTimeout(1_500);
  const probeCalls = await page.evaluate(() => (
    (window as typeof window & { __probeCalls?: number }).__probeCalls ?? 0
  ));
  expect(probeCalls).toBe(initialProbeCalls + 1);
  await page.getByRole("button", { name: "关闭同步诊断" }).click();
  await page.getByTestId("nav-capture").click();
  await expect(page.getByRole("button", { name: /打开实时预览 · 4 路可用/ })).toBeVisible();
});

test("failed status refresh keeps a manifest-and-probe connection and restores the refresh action", async ({ page }) => {
  await page.getByRole("button", { name: /同步状态/ }).click();
  const refresh = page.getByRole("button", { name: "刷新同步诊断" });
  await expect(refresh).toBeEnabled();
  await page.evaluate(() => {
    (window as typeof window & { __failNextStatus?: boolean }).__failNextStatus = true;
  });

  await refresh.click();
  await expect(refresh).toBeEnabled();
  await expect(page.getByText("设备已连接 · 4/4 路相机可用")).toBeVisible();
});
