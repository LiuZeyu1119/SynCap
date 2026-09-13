import { expect, test } from "@playwright/test";

test("recognizes a Hisi headband, displays truthful power state, and reuses BLE Wi-Fi setup", async ({ page }) => {
  await page.addInitScript(() => {
    const runtime = window as typeof window & {
      androidBridge?: object;
      Capacitor?: Record<string, unknown>;
      __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }>;
    };
    runtime.androidBridge = {};
    runtime.__nativeCalls = [];
    const cameras = [0, 1, 2, 3].map((index) => ({
      id: `cam${index}`,
      label: ["U5 黑白", "U6 彩色", "U7 彩色", "U8 黑白"][index],
      preview: { transport: "rtsp", url: `rtsp://127.0.0.1:8554/cam${index}` },
    }));
    const emptyMetric = { current: null, mean: null, p95: null, min: null, max: null, samples: 0 };

    runtime.Capacitor = {
      PluginHeaders: [{
        name: "SynCapDevice",
        methods: ["probe", "getStatus", "getManifest", "listSessions", "scanBle", "scanWifiBle"].map((name) => ({ name, rtype: "promise" })),
      }],
      nativePromise: async (_plugin: string, method: string, options: Record<string, unknown> = {}) => {
        runtime.__nativeCalls?.push({ method, options });
        if (method === "getManifest") {
          return {
            protocolVersion: "1.0",
            device: {
              id: "hisi_hi3559av100",
              displayName: "Hisi 四目头环",
              model: "Hi3559AV100-4P",
              type: "hisi",
              firmwareVersion: "0.1.0-port20260820",
            },
            capabilities: [
              "camera.preview",
              "sensor.imu",
              "power.battery",
              "network.wifi",
              "provisioning.ble",
            ],
            cameras,
          };
        }
        if (method === "getStatus") {
          return {
            v: "1.0",
            deviceTimeNs: "123",
            adapter: "hisi-hi3559av100",
            cameraDemoRunning: true,
            cameras: cameras.map((camera) => ({ id: camera.id, port: 8554, online: true })),
            sync: {
              acquisitionSkewMs: emptyMetric,
              previewPtsSkewMs: emptyMetric,
              previewCameraSkewMs: emptyMetric,
            },
            system: { uptimeSeconds: 123, temperatureC: 51.5, storage: { totalGiB: 8, availableGiB: 6 } },
            power: {
              battery: {
                available: true,
                online: true,
                capacityPercent: 73,
                voltageUv: 3920000,
                chargingState: "unknown",
                chargingStateSource: "no_charger_status_route",
              },
            },
          };
        }
        if (method === "probe") {
          const paths = options.paths as string[];
          return {
            host: String(options.host),
            reachable: true,
            onlineCount: 4,
            streams: paths.map((path) => ({ port: 8554, path, online: true, url: `rtsp://${options.host}:8554${path}` })),
          };
        }
        if (method === "scanBle") {
          return {
            devices: [{
              id: "28:2D:06:0A:C8:C3",
              address: "28:2D:06:0A:C8:C3",
              name: "SynCap-C8C3",
              rssi: -42,
              paired: true,
              bonded: true,
              advertising: true,
            }],
          };
        }
        if (method === "scanWifiBle") {
          return {
            state: "completed",
            op: "wifi.scan",
            networks: [{ ssid: "Office-2G", rssi: -48, security: "wpa2-psk", secure: true }],
          };
        }
        if (method === "listSessions") return { sessions: [] };
        throw new Error(`Unexpected native method: ${method}`);
      },
    };
  });

  await page.goto("/");
  await expect(page.getByTestId("device-selector")).toContainText("Hi3559AV100-4P");
  await page.getByTestId("nav-device").click();

  const power = page.getByTestId("device-power-status");
  await expect(page.getByTestId("tab-device")).toContainText("Hisi 四目头环");
  await expect(page.getByTestId("tab-device")).toContainText("Hi3559AV100-4P · Hisi");
  await expect(power).toContainText("73%");
  await expect(power).toContainText("3.92 V");
  await expect(power).toContainText("未知");
  await expect(power).toContainText("硬件状态脚未接入");
  await expect(page.getByRole("button", { name: /离线 U 盘采集/ })).toBeDisabled();
  await expect(page.getByRole("button", { name: /离线 U 盘采集/ })).toContainText("当前设备暂不支持离线 U 盘控制");
  const networkSetup = page.getByRole("button", { name: /网络设置/ });
  await expect(networkSetup).toBeEnabled();
  await expect(networkSetup).toContainText("连接普通 Wi-Fi 或手机热点");
  await networkSetup.click();
  await expect(page.getByRole("button", { name: /SynCap-C8C3/ })).toBeVisible();
  await expect(page.getByRole("button", { name: /Office-2G/ })).toBeVisible();

  const probeCall = await page.evaluate(() => (
    window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }
  ).__nativeCalls?.find((call) => call.method === "probe"));
  expect(probeCall?.options.ports).toEqual([8554, 8554, 8554, 8554]);
  expect(probeCall?.options.paths).toEqual(["/cam0", "/cam1", "/cam2", "/cam3"]);
  const wifiScanCall = await page.evaluate(() => (
    window as typeof window & { __nativeCalls?: Array<{ method: string; options: Record<string, unknown> }> }
  ).__nativeCalls?.find((call) => call.method === "scanWifiBle"));
  expect(wifiScanCall?.options).toEqual({ address: "28:2D:06:0A:C8:C3" });
});
