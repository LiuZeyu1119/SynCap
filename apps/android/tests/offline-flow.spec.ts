import { expect, test, type Page } from "@playwright/test";

type WifiMockMode = "success" | "scan-timeout-once" | "connect-timeout-once" | "slow-connect"
  | "stale-host-recovery" | "stale-host-legacy" | "status-timeout"
  | "stale-host-unpaired" | "stale-host-bonding" | "stale-host-no-permission";

async function installAndroidWifiMock(page: Page, mode: WifiMockMode = "success") {
  await page.addInitScript((scenario) => {
    type NativeCall = { method: string; options: Record<string, unknown> };
    const runtime = window as typeof window & {
      __nativeCalls?: NativeCall[];
      __wifiBleScanAttempts?: number;
      __wifiConfigureAttempts?: number;
      __bleStatusAttempts?: number;
      __deviceHttpOffline?: boolean;
      __failedManifestReads?: number;
      androidBridge?: object;
      Capacitor?: Record<string, unknown>;
    };
    const calls: NativeCall[] = [];
    runtime.__nativeCalls = calls;
    runtime.__wifiBleScanAttempts = 0;
    runtime.__wifiConfigureAttempts = 0;
    runtime.__bleStatusAttempts = 0;
    runtime.__failedManifestReads = 0;
    runtime.androidBridge = {};

    const cameras = [554, 555, 556, 557].map((port, index) => ({
      id: `cam${index}`,
      direction: ["front", "right", "rear", "left"][index],
      preview: { transport: "rtsp", url: `rtsp://192.168.43.87:${port}/PRR` },
    }));
    const status = {
      v: "1.0",
      deviceTimeNs: "123",
      adapter: "syncap-device-service",
      cameraDemoRunning: true,
      cameras: cameras.map((camera, index) => ({ id: camera.id, port: 554 + index, online: true, fps: 60 })),
      sync: {
        acquisitionSkewMs: { current: 0.04, mean: 0.04, p95: 0.05, min: 0.03, max: 0.08, samples: 100 },
        previewPtsSkewMs: { current: 0, mean: 0, p95: 0, min: 0, max: 0, samples: 100 },
        previewCameraSkewMs: { current: 0.04, mean: 0.04, p95: 0.05, min: 0.03, max: 0.08, samples: 100 },
      },
      system: { uptimeSeconds: 100, temperatureC: 60, storage: { totalGiB: 22.9, availableGiB: 18.6 } },
      capture: { state: "idle" },
      wifi: { state: "connected", ssid: "Phone-Hotspot", ipAddress: "192.168.43.87" },
    };

    runtime.Capacitor = {
      PluginHeaders: [{
        name: "SynCapDevice",
        methods: ["probe", "getStatus", "getManifest", "listSessions", "scanBle", "scanWifi", "scanWifiBle", "configureWifiBle", "offlineBleCommand"]
          .map((name) => ({ name, rtype: "promise" })),
      }],
      nativePromise: async (_plugin: string, method: string, options: Record<string, unknown> = {}) => {
        calls.push({ method, options });
        if (method === "probe") {
          return {
            host: String(options.host ?? "192.168.1.12"),
            reachable: true,
            onlineCount: 4,
            streams: [554, 555, 556, 557].map((port) => ({ port, online: true, url: `rtsp://192.168.43.87:${port}/PRR` })),
          };
        }
        if (method === "getStatus") {
          if (scenario === "status-timeout") throw new Error("Read timed out");
          if ((runtime.__deviceHttpOffline || scenario.startsWith("stale-host-")) && options.host !== "10.51.121.109") {
            throw new Error("connection refused");
          }
          return status;
        }
        if (method === "getManifest") {
          if ((runtime.__deviceHttpOffline || scenario.startsWith("stale-host-")) && options.host !== "10.51.121.109") {
            runtime.__failedManifestReads = (runtime.__failedManifestReads ?? 0) + 1;
            throw new Error("connection refused");
          }
          return {
            protocolVersion: "1.0",
            device: { id: "hotspot-test", displayName: "Head Ring", model: "SynCap 4P", ipAddress: String(options.host ?? "192.168.1.12") },
            capabilities: ["camera.preview", "capture.device", "network.wifi", "provisioning.ble"],
            cameras,
          };
        }
        if (method === "listSessions") return { sessions: [] };
        if (method === "scanBle") {
          return {
            devices: [
              { id: "AA:BB:CC:DD:EE:01", address: "AA:BB:CC:DD:EE:01", name: "HeadRing-A", rssi: -60, paired: true, bonded: true },
              { id: "AA:BB:CC:DD:EE:01", address: "AA:BB:CC:DD:EE:01", name: "HeadRing-A", rssi: -42, paired: true, bonded: true },
              { id: "AA:BB:CC:DD:EE:02", address: "AA:BB:CC:DD:EE:02", name: "HeadRing-B", rssi: -55, paired: true, bonded: true },
              { id: "AA:BB:CC:DD:EE:99", address: "AA:BB:CC:DD:EE:99", name: "Unpaired-Beacon", rssi: -20, paired: false, bonded: false },
            ],
          };
        }
        if (method === "scanWifi") throw new Error("PHONE_SIDE_WIFI_SCAN_MUST_NOT_BE_USED");
        if (method === "scanWifiBle") {
          runtime.__wifiBleScanAttempts = (runtime.__wifiBleScanAttempts ?? 0) + 1;
          if (scenario === "scan-timeout-once" && runtime.__wifiBleScanAttempts === 1) {
            throw new Error("BLE operation timed out");
          }
          if (options.address === "AA:BB:CC:DD:EE:02") {
            return {
              state: "completed",
              op: "wifi.scan",
              networks: [{ ssid: "Backup-Hotspot", rssi: -46, security: "wpa2-psk", secure: true }],
              scannedAt: Date.now(),
            };
          }
          return {
            state: "completed",
            op: "wifi.scan",
            networks: [
              { ssid: "Phone-Hotspot", rssi: -68, security: "wpa2-psk", secure: true },
              { ssid: "Office-WPA3", rssi: -25, security: "wpa3-sae", secure: true },
              { ssid: "Phone-Hotspot", rssi: -38, security: "wpa2-psk", secure: true },
              { ssid: "Router-5G", rssi: -45, security: "wpa2-psk", secure: true },
              { ssid: "Cafe-Open", rssi: -55, security: "open", secure: false },
              { ssid: "", rssi: -10, security: "wpa2-psk", secure: true },
            ],
            scannedAt: Date.now(),
          };
        }
        if (method === "configureWifiBle") {
          runtime.__wifiConfigureAttempts = (runtime.__wifiConfigureAttempts ?? 0) + 1;
          if (scenario === "connect-timeout-once" && runtime.__wifiConfigureAttempts === 1) {
            return { state: "failed", ssid: options.ssid, error: "wifi_association_timeout" };
          }
          if (scenario === "slow-connect") await new Promise((resolve) => window.setTimeout(resolve, 350));
          return { state: "connected", ssid: options.ssid, ipAddress: "192.168.43.87" };
        }
        if (method === "offlineBleCommand" && options.op === "device.status") {
          runtime.__bleStatusAttempts = (runtime.__bleStatusAttempts ?? 0) + 1;
          if (["stale-host-unpaired", "stale-host-bonding", "stale-host-no-permission"].includes(scenario)) {
            if (options.backgroundRecovery !== true) throw new Error("BACKGROUND_RECOVERY_FLAG_REQUIRED");
            return { state: "skipped", op: "device.status", error: "background_recovery_requires_pairing_and_permission" };
          }
          if (scenario === "stale-host-legacy") return { state: "ready", op: "device.status" };
          return {
            state: "ready",
            op: "device.status",
            wifi: { state: "connected", ssid: "Phone-Hotspot", ipAddress: "10.51.121.109" },
          };
        }
        throw new Error(`Unexpected native method: ${method}`);
      },
    };
  }, mode);
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("nav-device").click();
});

test("offline control uses the paired headring without asking for a claim code", async ({ page }) => {
  await page.getByRole("button", { name: /离线 U 盘采集/ }).click();

  await expect(page.getByLabel("设备认领码")).toHaveCount(0);
  await expect(page.getByPlaceholder(/6 位数字/)).toHaveCount(0);

  const confirm = page.getByRole("button", { name: "启用离线 U 盘采集" });
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.locator(".syncap-toast")).toHaveText("尚未发现 SynCap 设备，请靠近头环后重新扫描");
});

test("Bluetooth commands for one headring are serialized and a failed command releases the queue", async ({ page }) => {
  await installAndroidWifiMock(page);
  await page.goto("/");
  const result = await page.evaluate(async () => {
    const modulePath = "/src/nativeDevice.ts";
    const api = await import(modulePath);
    const bridge = (window as unknown as { Capacitor: {
      nativePromise: (plugin: string, method: string, options: Record<string, unknown>) => Promise<unknown>;
    } }).Capacitor;
    const original = bridge.nativePromise;
    const events: string[] = [];
    let releaseScan!: () => void;
    const heldScan = new Promise<void>((resolve) => { releaseScan = resolve; });
    bridge.nativePromise = async (plugin, method, options) => {
      if (method === "scanWifiBle") {
        events.push("scan:start");
        await heldScan;
        events.push("scan:failed");
        throw new Error("Unable to complete Bluetooth provisioning");
      }
      if (method === "offlineBleCommand") {
        if (options.backgroundRecovery !== undefined) throw new Error("EXPLICIT_STATUS_MUST_NOT_BE_BACKGROUND_RECOVERY");
        events.push(`status:${options.address}`);
        return { state: "ready" };
      }
      return original(plugin, method, options);
    };
    try {
      const scan = api.scanWifiNetworksOverBle("aa:bb:cc:dd:ee:01").catch(() => {});
      const status = api.getOfflineDeviceStatusOverBle("AA:BB:CC:DD:EE:01", "123456");
      await api.getOfflineDeviceStatusOverBle("AA:BB:CC:DD:EE:02", "123456");
      const beforeRelease = [...events];
      releaseScan();
      await Promise.all([scan, status]);
      return { beforeRelease, events };
    } finally {
      releaseScan();
      bridge.nativePromise = original;
    }
  });
  expect(result.beforeRelease).toEqual(["scan:start", "status:AA:BB:CC:DD:EE:02"]);
  expect(result.events).toEqual([
    "scan:start", "status:AA:BB:CC:DD:EE:02", "scan:failed", "status:AA:BB:CC:DD:EE:01",
  ]);
});

test("network setup pauses background Bluetooth host recovery", async ({ page }) => {
  await installAndroidWifiMock(page);
  await page.evaluate(() => {
    window.localStorage.setItem("syncap.lastBleDevice", JSON.stringify({
      address: "AA:BB:CC:DD:EE:01", name: "HeadRing-A",
    }));
  });
  await page.goto("/");
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /网络设置/ }).click();
  await expect(page.getByRole("button", { name: /Phone-Hotspot/ })).toBeVisible();
  await page.evaluate(() => {
    (window as typeof window & { __deviceHttpOffline?: boolean }).__deviceHttpOffline = true;
  });
  // A manifest failure is required: a status-only timeout never enters host recovery.
  await expect.poll(() => page.evaluate(() => (
    window as typeof window & { __failedManifestReads?: number }
  ).__failedManifestReads)).toBeGreaterThan(0);
  const calls = await page.evaluate(() => (
    (window as typeof window & { __nativeCalls?: Array<{ method: string }> }).__nativeCalls ?? []
  ).filter((entry) => entry.method === "offlineBleCommand"));
  expect(calls).toEqual([]);
  await page.keyboard.press("Escape");
  await expect(page.getByRole("button", { name: /Phone-Hotspot/ })).toHaveCount(0);
  // Prove the cached device and failed HTTP probe really can trigger recovery after closing.
  await expect.poll(() => page.evaluate(() => (
    window as typeof window & { __bleStatusAttempts?: number }
  ).__bleStatusAttempts)).toBe(1);
});

test("network setup uses plain language and never asks for technical identifiers", async ({ page }) => {
  await page.getByRole("button", { name: /网络设置/ }).click();

  await expect(page.getByText("Lab-5G")).toHaveCount(0);
  await expect(page.getByLabel("Wi-Fi 名称（SSID）")).toHaveCount(0);
  await expect(page.getByLabel("设备认领码")).toHaveCount(0);
  await expect(page.getByText("请使用 Android 手机设置网络。")).toBeVisible();
  await expect(page.getByText("支持普通 Wi-Fi 和手机热点")).toBeVisible();
  await expect(page.getByText(/BLE|SSID|dBm|个人热点/, { exact: false })).toHaveCount(0);

  const confirm = page.getByRole("button", { name: "连接", exact: true });
  await expect(confirm).toBeDisabled();
});

test("same-phone hotspot is discovered by the selected headring and becomes the new device host", async ({ page }) => {
  await installAndroidWifiMock(page);

  await page.goto("/");
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /网络设置/ }).click();
  await expect(page.getByText("已配对").first()).toBeVisible();
  await expect(page.getByText("Unpaired-Beacon")).toHaveCount(0);
  await expect(page.getByRole("button", { name: /Phone-Hotspot/ })).toHaveCount(1);

  const networkRows = page.locator(".nearby-wifi-list > button");
  await expect(networkRows).toHaveCount(4);
  await expect(networkRows.nth(0)).toContainText("Phone-Hotspot");
  await expect(networkRows.nth(1)).toContainText("Router-5G");
  await expect(networkRows.nth(2)).toContainText("Cafe-Open");
  await expect(networkRows.nth(3)).toContainText("Office-WPA3");
  await expect(networkRows.nth(3)).toBeDisabled();

  const scansBeforeConnect = await page.evaluate(() => (
    (window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }).__nativeCalls ?? []
  ).filter((entry) => entry.method === "scanWifi" || entry.method === "scanWifiBle"));
  expect(scansBeforeConnect.filter((entry) => entry.method === "scanWifi")).toHaveLength(0);
  expect(scansBeforeConnect.filter((entry) => entry.method === "scanWifiBle")).toEqual([
    { method: "scanWifiBle", options: { address: "AA:BB:CC:DD:EE:01" } },
  ]);

  await page.getByRole("button", { name: /Phone-Hotspot/ }).click();

  const password = page.getByLabel("Phone-Hotspot 的密码");
  await password.fill("hotspot-password");
  await expect(page.getByTestId("keyboard-dock")).toHaveAttribute("data-visible", "false");

  await page.setViewportSize({ width: 412, height: 520 });
  const confirm = page.getByRole("button", { name: "连接", exact: true });
  await expect(confirm).toBeVisible();
  await expect(confirm).toBeInViewport();
  const bounds = await confirm.boundingBox();
  expect(bounds).not.toBeNull();
  expect((bounds?.y ?? 0) + (bounds?.height ?? 0)).toBeLessThanOrEqual(520);

  await confirm.click();
  await expect(page.locator(".syncap-toast")).toContainText("头环已连接 Phone-Hotspot");
  const configureCall = await page.evaluate(() => (
    (window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }).__nativeCalls
      ?.find((entry) => entry.method === "configureWifiBle")
  ));
  expect(configureCall?.options).toMatchObject({
    address: "AA:BB:CC:DD:EE:01",
    ssid: "Phone-Hotspot",
    password: "hotspot-password",
    claimCode: "123456",
  });
  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("syncap.deviceHost"))).toBe("192.168.43.87");
  await expect.poll(() => page.evaluate(() => (
    (window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }).__nativeCalls ?? []
  ).some((entry) => entry.method === "getStatus" && entry.options.host === "192.168.43.87"))).toBe(true);
});

test("slow telemetry does not turn a reachable Wi-Fi device offline", async ({ page }) => {
  await installAndroidWifiMock(page, "status-timeout");
  await page.goto("/");

  await expect(page.getByTestId("device-selector")).toContainText("SynCap 4P");
  await expect(page.getByText("已就绪", { exact: true })).toHaveCount(4);
  await expect(page.getByRole("button", { name: "打开实时预览 · 4 路可用" })).toBeEnabled();
  await expect(page.getByText("设备服务已连接", { exact: true })).toBeVisible();
  await expect(page.getByText("设备未连接")).toHaveCount(0);
});

test("a stale saved host is recovered once through the selected paired headring", async ({ page }) => {
  await installAndroidWifiMock(page, "stale-host-recovery");
  await page.evaluate(() => {
    window.localStorage.setItem("syncap.deviceHost", "10.51.121.88");
    window.localStorage.setItem("syncap.lastBleDevice", JSON.stringify({
      address: "AA:BB:CC:DD:EE:01",
      name: "HeadRing-A",
    }));
  });
  await page.goto("/");

  await expect.poll(() => page.evaluate(() => window.localStorage.getItem("syncap.deviceHost"))).toBe("10.51.121.109");
  await expect.poll(() => page.evaluate(() => (
    (window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }).__nativeCalls ?? []
  ).some((entry) => entry.method === "getStatus" && entry.options.host === "10.51.121.109"))).toBe(true);
  const recoveryCalls = await page.evaluate(() => (
    (window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }).__nativeCalls ?? []
  ).filter((entry) => entry.method === "offlineBleCommand" || entry.method === "configureWifiBle"));
  expect(recoveryCalls).toEqual([{
    method: "offlineBleCommand",
    options: { address: "AA:BB:CC:DD:EE:01", claimCode: "123456", op: "device.status", backgroundRecovery: true },
  }]);
});

for (const mode of ["stale-host-unpaired", "stale-host-bonding", "stale-host-no-permission"] as const) {
  test(`background host recovery is safely skipped for ${mode}`, async ({ page }) => {
    await installAndroidWifiMock(page, mode);
    await page.evaluate(() => {
      window.localStorage.setItem("syncap.deviceHost", "10.51.121.88");
      window.localStorage.setItem("syncap.lastBleDevice", JSON.stringify({
        address: "AA:BB:CC:DD:EE:01", name: "HeadRing-A",
      }));
    });
    await page.goto("/");
    await expect.poll(() => page.evaluate(() => (
      window as typeof window & { __bleStatusAttempts?: number }
    ).__bleStatusAttempts)).toBe(1);
    await page.waitForTimeout(4500);
    expect(await page.evaluate(() => window.localStorage.getItem("syncap.deviceHost"))).toBe("10.51.121.88");
    const calls = await page.evaluate(() => (
      (window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }).__nativeCalls ?? []
    ).filter((entry) => entry.method === "offlineBleCommand" || entry.method === "configureWifiBle"));
    expect(calls).toEqual([{
      method: "offlineBleCommand",
      options: { address: "AA:BB:CC:DD:EE:01", claimCode: "123456", op: "device.status", backgroundRecovery: true },
    }]);
    await expect(page.locator(".syncap-toast")).toHaveCount(0);
  });
}

test("legacy device status without Wi-Fi stays on the saved host and observes the recovery cooldown", async ({ page }) => {
  await installAndroidWifiMock(page, "stale-host-legacy");
  await page.evaluate(() => {
    window.localStorage.setItem("syncap.deviceHost", "10.51.121.88");
    window.localStorage.setItem("syncap.lastBleDevice", JSON.stringify({
      address: "AA:BB:CC:DD:EE:01",
      name: "HeadRing-A",
    }));
  });
  await page.goto("/");

  await expect.poll(() => page.evaluate(() => (
    window as typeof window & { __bleStatusAttempts?: number }
  ).__bleStatusAttempts)).toBe(1);
  await page.waitForTimeout(4500);
  expect(await page.evaluate(() => window.localStorage.getItem("syncap.deviceHost"))).toBe("10.51.121.88");
  expect(await page.evaluate(() => (
    window as typeof window & { __bleStatusAttempts?: number }
  ).__bleStatusAttempts)).toBe(1);
});

test("switching the paired headring replaces its BLE-side Wi-Fi results", async ({ page }) => {
  await installAndroidWifiMock(page);
  await page.goto("/");
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /网络设置/ }).click();
  await expect(page.getByRole("button", { name: /Phone-Hotspot/ })).toBeVisible();

  await page.getByRole("button", { name: /HeadRing-B/ }).click();
  await expect(page.getByRole("button", { name: /Backup-Hotspot/ })).toBeVisible();
  await expect(page.locator(".nearby-wifi-list").getByText("Phone-Hotspot", { exact: true })).toHaveCount(0);
  const scanAddresses = await page.evaluate(() => (
    (window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }).__nativeCalls ?? []
  ).filter((entry) => entry.method === "scanWifiBle").map((entry) => entry.options.address));
  expect(scanAddresses).toEqual(["AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"]);
});

test("BLE-side scan timeout clears loading and can be retried", async ({ page }) => {
  await installAndroidWifiMock(page, "scan-timeout-once");
  await page.goto("/");
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /网络设置/ }).click();

  await expect(page.getByRole("alert")).toContainText("搜索网络超时");
  const refresh = page.getByRole("button", { name: "重新搜索网络" });
  await expect(refresh).toBeEnabled();
  await refresh.click();
  await expect(page.getByRole("button", { name: /Phone-Hotspot/ })).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  const attempts = await page.evaluate(() => (window as typeof window & { __wifiBleScanAttempts?: number }).__wifiBleScanAttempts);
  expect(attempts).toBe(2);
});

test("connection failure preserves the password and allows a successful retry", async ({ page }) => {
  await installAndroidWifiMock(page, "connect-timeout-once");
  await page.goto("/");
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /网络设置/ }).click();
  await page.getByRole("button", { name: /Phone-Hotspot/ }).click();
  const password = page.getByLabel("Phone-Hotspot 的密码");
  await password.fill("hotspot-password");
  const confirm = page.getByRole("button", { name: "连接", exact: true });

  await confirm.click();
  await expect(page.locator(".syncap-toast")).toHaveText("密码不正确，或网络已关闭，请检查后重试");
  await expect(password).toHaveValue("hotspot-password");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(page.locator(".syncap-toast")).toContainText("头环已连接 Phone-Hotspot");
  const attempts = await page.evaluate(() => (window as typeof window & { __wifiConfigureAttempts?: number }).__wifiConfigureAttempts);
  expect(attempts).toBe(2);
});

test("keyboard Enter and the fixed confirmation action remain single-flight", async ({ page }) => {
  await installAndroidWifiMock(page, "slow-connect");
  await page.goto("/");
  await page.getByTestId("nav-device").click();
  await page.getByRole("button", { name: /网络设置/ }).click();
  await page.getByRole("button", { name: /Phone-Hotspot/ }).click();
  const password = page.getByLabel("Phone-Hotspot 的密码");
  await password.fill("hotspot-password");
  await page.setViewportSize({ width: 412, height: 520 });

  await password.press("Enter");
  await password.press("Enter");
  const busy = page.getByRole("button", { name: "正在连接" });
  await expect(busy).toBeDisabled();
  await expect(busy).toBeInViewport();
  await expect(page.locator(".syncap-toast")).toContainText("头环已连接 Phone-Hotspot");
  const attempts = await page.evaluate(() => (window as typeof window & { __wifiConfigureAttempts?: number }).__wifiConfigureAttempts);
  expect(attempts).toBe(1);
});
