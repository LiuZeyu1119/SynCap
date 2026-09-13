import assert from "node:assert/strict";
import test from "node:test";

import {
  cameraPortsFromManifest,
  isCameraObservedOnline,
  isPreviewTransportSupportedByRuntime,
  normalizeDeviceManifestPayload,
  normalizeDeviceProbePayload,
} from "../src/deviceDiscovery.ts";
import { cameraMountPosition, orderCamerasForDisplay } from "../src/cameraDisplayOrder.ts";

const liveManifest = {
  protocolVersion: "1.0",
  device: {
    id: "dev_rb4p_a4df62",
    displayName: "Head Ring 001",
    model: "RoboBaton-4P",
    ipAddress: "192.168.1.12",
  },
  capabilities: ["camera.preview", "sync.metrics", "sensor.imu"],
  cameras: [
    { id: "cam0", direction: "front", preview: { transport: "rtsp", url: "rtsp://192.168.1.12:554/PRR" } },
    { id: "cam1", direction: "right", preview: { transport: "rtsp", url: "rtsp://192.168.1.12:555/PRR" } },
    { id: "cam2", direction: "rear", preview: { transport: "rtsp", url: "rtsp://192.168.1.12:556/PRR" } },
    { id: "cam3", direction: "left", preview: { transport: "rtsp", url: "rtsp://192.168.1.12:557/PRR" } },
  ],
};

test("parses the four live 192.168.1.12 camera endpoints", () => {
  const manifest = normalizeDeviceManifestPayload(liveManifest, "192.168.1.12");
  assert.deepEqual(cameraPortsFromManifest(manifest), [554, 555, 556, 557]);
  assert.deepEqual(
    manifest.cameras.map((camera) => camera.previewUrl),
    [554, 555, 556, 557].map((port) => `rtsp://192.168.1.12:${port}/PRR`),
  );
});

test("uses the verified endpoint host instead of a stale advertised host", () => {
  const manifest = normalizeDeviceManifestPayload(liveManifest, "192.168.100.211");
  assert.equal(manifest.device.ipAddress, "192.168.100.211");
  assert.equal(manifest.cameras[0].previewUrl, "rtsp://192.168.100.211:554/PRR");
});

test("accepts Hisi cameras that share one RTSP port and differ by path", () => {
  const manifest = normalizeDeviceManifestPayload({
    protocolVersion: "1.0",
    device: { id: "hisi_hi3559av100", model: "Hi3559AV100-4P", type: "hisi" },
    capabilities: ["camera.preview", "power.battery"],
    cameras: [0, 1, 2, 3].map((index) => ({
      id: `cam${index}`,
      preview: { transport: "rtsp", url: `rtsp://127.0.0.1:8554/cam${index}` },
    })),
  }, "192.168.100.100");

  assert.equal(manifest.device.type, "hisi");
  assert.deepEqual(manifest.cameras.map((camera) => [camera.port, camera.path]), [
    [8554, "/cam0"], [8554, "/cam1"], [8554, "/cam2"], [8554, "/cam3"],
  ]);
  assert.equal(manifest.cameras[2].previewUrl, "rtsp://192.168.100.100:8554/cam2");
});

test("preserves Tina raw HEVC TCP transports while replacing the advertised host", () => {
  const manifest = normalizeDeviceManifestPayload({
    protocolVersion: "1.0",
    device: { id: "tina_stereo_fixture", model: "Tina Stereo", type: "allwinner-tina" },
    capabilities: ["camera.preview"],
    cameras: [0, 1].map((index) => ({
      id: index === 0 ? "left" : "right",
      preview: { transport: "tcp-hevc", url: `tcp://127.0.0.1:${9100 + index}` },
    })),
  }, "192.168.1.13");

  assert.deepEqual(manifest.cameras.map((camera) => ({
    port: camera.port,
    path: camera.path,
    previewUrl: camera.previewUrl,
    transport: camera.preview.transport,
  })), [
    { port: 9100, path: "", previewUrl: "tcp://192.168.1.13:9100", transport: "tcp-hevc" },
    { port: 9101, path: "", previewUrl: "tcp://192.168.1.13:9101", transport: "tcp-hevc" },
  ]);
});

test("maps same-port Hisi probe results by RTSP path", () => {
  const requests = [0, 1, 2, 3].map((index) => ({ port: 8554, path: `/cam${index}` }));
  const probe = normalizeDeviceProbePayload({
    streams: [
      { port: 8554, path: "/cam2", online: true, url: "rtsp://192.168.100.100:8554/cam2" },
      { port: 8554, path: "/cam0", online: false, url: "rtsp://192.168.100.100:8554/cam0" },
    ],
  }, "192.168.100.100", requests);

  assert.deepEqual(probe.streams.map((stream) => [stream.path, stream.online]), [
    ["/cam0", false], ["/cam1", false], ["/cam2", true], ["/cam3", false],
  ]);
  assert.equal(probe.streams[2].url, "rtsp://192.168.100.100:8554/cam2");
});

test("keeps tcp-hevc probe identity and reconstructs the verified endpoint URL", () => {
  const probe = normalizeDeviceProbePayload({
    streams: [
      { port: 9100, path: "", transport: "tcp-hevc", online: true, url: "tcp://127.0.0.1:9100" },
      { port: 9101, path: "", transport: "tcp-hevc", online: false, reason: "connection_refused" },
    ],
  }, "192.168.1.13", [
    { port: 9100, path: "", transport: "tcp-hevc", url: "tcp://127.0.0.1:9100" },
    { port: 9101, path: "", transport: "tcp-hevc", url: "tcp://127.0.0.1:9101" },
  ]);

  assert.deepEqual(probe.streams.map((stream) => ({
    transport: stream.transport,
    url: stream.url,
    online: stream.online,
  })), [
    { transport: "tcp-hevc", url: "tcp://192.168.1.13:9100", online: true },
    { transport: "tcp-hevc", url: "tcp://192.168.1.13:9101", online: false },
  ]);
});

test("combines camera telemetry and probing while allowing raw TCP to prefer service truth", () => {
  assert.equal(isCameraObservedOnline(true, false), true);
  assert.equal(isCameraObservedOnline(false, true), true);
  assert.equal(isCameraObservedOnline(false, false), false);
  assert.equal(isCameraObservedOnline(undefined, undefined), false);
  assert.equal(isCameraObservedOnline(false, true, true), false);
  assert.equal(isCameraObservedOnline(undefined, true, true), true);
});

test("disables unsupported raw HEVC preview only in the desktop runtime", () => {
  assert.equal(isPreviewTransportSupportedByRuntime("tcp-hevc", true), false);
  assert.equal(isPreviewTransportSupportedByRuntime("tcp-hevc", false), true);
  assert.equal(isPreviewTransportSupportedByRuntime("rtsp", true), true);
  assert.equal(isPreviewTransportSupportedByRuntime(undefined, true), true);
});

test("maps partial and out-of-order probe results by port without inventing online streams", () => {
  const probe = normalizeDeviceProbePayload({
    streams: [
      { port: 557, online: true, reason: "online", responseCode: 200 },
      { port: 554, online: false, reason: "connection_refused" },
    ],
  }, "192.168.1.12", [554, 555, 556, 557]);

  assert.deepEqual(probe.streams.map((stream) => [stream.port, stream.online]), [
    [554, false], [555, false], [556, false], [557, true],
  ]);
  assert.equal(probe.onlineCount, 1);
  assert.equal(probe.reachable, true);
});

test("rejects manifests that cannot identify a real device", () => {
  assert.throws(
    () => normalizeDeviceManifestPayload({ cameras: liveManifest.cameras }, "192.168.1.12"),
    /identity is incomplete/,
  );
});

test("preserves manifest order for devices that are not the complete legacy four-camera set", () => {
  assert.deepEqual(
    orderCamerasForDisplay([{ id: "cam0" }, { id: "cam1" }]).map((camera) => camera.id),
    ["cam0", "cam1"],
  );
  assert.deepEqual(
    orderCamerasForDisplay([{ id: "left" }, { id: "right" }]).map((camera) => camera.id),
    ["left", "right"],
  );
});

test("keeps the physical display order for the complete legacy four-camera set", () => {
  assert.deepEqual(
    orderCamerasForDisplay([0, 1, 2, 3].map((index) => ({ id: `cam${index}` }))).map((camera) => camera.id),
    ["cam3", "cam1", "cam2", "cam0"],
  );
});

test("localizes stereo mount positions without changing legacy physical labels", () => {
  assert.equal(cameraMountPosition({ id: "left", mountPosition: "stereo_left" }), "左目");
  assert.equal(cameraMountPosition({ id: "right", mountPosition: "stereo_right" }), "右目");
  assert.deepEqual(
    ["cam3", "cam1", "cam2", "cam0"].map((id) => cameraMountPosition({ id })),
    ["左外侧", "左内侧", "右内侧", "右外侧"],
  );
});
