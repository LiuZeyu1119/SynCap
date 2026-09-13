export type NormalizedCameraManifest = {
  id: string;
  label?: string;
  direction?: string;
  mountPosition?: string;
  port: number;
  path: string;
  previewUrl: string;
  preview: { url: string; transport: string };
};

export type NormalizedDeviceManifest = {
  protocolVersion: string;
  device: {
    id: string;
    displayName?: string;
    model?: string;
    type?: string;
    firmwareVersion?: string;
    ipAddress: string;
  };
  capabilities: string[];
  cameras: NormalizedCameraManifest[];
};

export type NormalizedProbeStream = {
  port: number;
  path: string;
  transport: string;
  online: boolean;
  url: string;
  reason?: string;
  responseCode?: number;
  latencyMs?: number;
};

export type DeviceProbeRequest = {
  port: number;
  path: string;
  transport?: string;
  url?: string;
};

export type NormalizedDeviceProbe = {
  host: string;
  reachable: boolean;
  onlineCount: number;
  streams: NormalizedProbeStream[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function text(value: unknown) {
  return typeof value === "string" && value.trim() ? value.trim() : undefined;
}

function validPort(value: unknown) {
  const number = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isInteger(number) && number > 0 && number <= 65_535 ? number : undefined;
}

function previewParts(value: unknown) {
  const raw = text(value);
  if (!raw) return undefined;
  try {
    const url = new URL(raw);
    if (url.protocol !== "rtsp:" && url.protocol !== "tcp:") return undefined;
    return {
      protocol: url.protocol,
      port: validPort(url.port) ?? (url.protocol === "rtsp:" ? 554 : undefined),
      path: url.pathname
        ? url.pathname.startsWith("/") ? url.pathname : `/${url.pathname}`
        : "",
    };
  } catch {
    return undefined;
  }
}

function normalizeRtspPath(value: unknown, fallback = "/PRR") {
  const candidate = text(value);
  return candidate?.startsWith("/") ? candidate : fallback;
}

function normalizedPreviewTransport(value: unknown, protocol?: string) {
  const declared = text(value);
  return declared === "tcp-hevc" || protocol === "tcp:" ? "tcp-hevc" : declared ?? "rtsp";
}

function previewUrl(endpointHost: string, port: number, path: string, transport: string) {
  return transport === "tcp-hevc"
    ? `tcp://${endpointHost}:${port}${path}`
    : `rtsp://${endpointHost}:${port}${path}`;
}

function normalizeCamera(value: unknown, index: number, endpointHost: string): NormalizedCameraManifest {
  if (!isRecord(value)) throw new Error(`Camera ${index} is not an object`);
  const preview = isRecord(value.preview) ? value.preview : undefined;
  const parsed = previewParts(value.previewUrl ?? preview?.url ?? preview?.uri ?? value.uri ?? value.url);
  const transport = normalizedPreviewTransport(preview?.transport, parsed?.protocol);
  const port = validPort(value.port) ?? parsed?.port ?? 554 + index;
  const path = transport === "tcp-hevc"
    ? text(value.path) ?? parsed?.path ?? ""
    : normalizeRtspPath(value.path ?? parsed?.path);
  const normalizedPreviewUrl = previewUrl(endpointHost, port, path, transport);
  return {
    id: text(value.id) ?? `cam${index}`,
    label: text(value.label),
    direction: text(value.direction),
    mountPosition: text(value.mountPosition),
    port,
    path,
    previewUrl: normalizedPreviewUrl,
    preview: { url: normalizedPreviewUrl, transport },
  };
}

export function normalizeDeviceManifestPayload(value: unknown, endpointHost: string): NormalizedDeviceManifest {
  if (!isRecord(value)) throw new Error("Device manifest is not an object");
  const device = isRecord(value.device) ? value.device : undefined;
  const protocolVersion = text(value.protocolVersion);
  const deviceId = text(device?.id);
  if (!protocolVersion || !deviceId) throw new Error("Device manifest identity is incomplete");
  if (!Array.isArray(value.cameras) || value.cameras.length === 0 || value.cameras.length > 16) {
    throw new Error("Device manifest camera list is invalid");
  }

  const cameras = value.cameras.map((camera, index) => normalizeCamera(camera, index, endpointHost));
  if (new Set(cameras.map((camera) => camera.id)).size !== cameras.length) {
    throw new Error("Device manifest contains duplicate camera ids");
  }
  if (new Set(cameras.map((camera) => `${camera.preview.transport}:${camera.port}${camera.path}`)).size !== cameras.length) {
    throw new Error("Device manifest contains duplicate camera endpoints");
  }

  return {
    protocolVersion,
    device: {
      id: deviceId,
      displayName: text(device?.displayName),
      model: text(device?.model),
      type: text(device?.type),
      firmwareVersion: text(device?.firmwareVersion),
      ipAddress: endpointHost,
    },
    capabilities: Array.isArray(value.capabilities)
      ? value.capabilities.map(text).filter((item): item is string => item !== undefined)
      : [],
    cameras,
  };
}

export function cameraPortsFromManifest(manifest: NormalizedDeviceManifest) {
  return manifest.cameras.map((camera) => camera.port);
}

export function isCameraObservedOnline(serviceOnline?: boolean, probeOnline?: boolean, preferService = false) {
  if (preferService && serviceOnline !== undefined) return serviceOnline;
  return serviceOnline === true || probeOnline === true;
}

export function isPreviewTransportSupportedByRuntime(transport: string | undefined, desktopRuntime: boolean) {
  return !desktopRuntime || transport !== "tcp-hevc";
}

export function normalizeDeviceProbePayload(
  value: unknown,
  endpointHost: string,
  requestedStreams: Array<number | DeviceProbeRequest>,
): NormalizedDeviceProbe {
  const payload = isRecord(value) ? value : {};
  const rawStreams = Array.isArray(payload.streams) ? payload.streams : [];
  const requests = requestedStreams.map((stream): DeviceProbeRequest | undefined => {
    if (typeof stream === "number") {
      const port = validPort(stream);
      return port === undefined ? undefined : { port, path: "/PRR", transport: "rtsp" };
    }
    const port = validPort(stream.port);
    const parsed = previewParts(stream.url);
    const transport = normalizedPreviewTransport(stream.transport, parsed?.protocol);
    const path = transport === "tcp-hevc" ? parsed?.path ?? "" : normalizeRtspPath(stream.path ?? parsed?.path);
    return port === undefined ? undefined : {
      port,
      path,
      transport,
      url: previewUrl(endpointHost, port, path, transport),
    };
  }).filter((stream): stream is DeviceProbeRequest => stream !== undefined);
  const streamsByEndpoint = new Map<string, Record<string, unknown>>();
  const streamsByPort = new Map<number, Record<string, unknown>[]>();
  for (const stream of rawStreams) {
    if (!isRecord(stream)) continue;
    const port = validPort(stream.port);
    if (port === undefined) continue;
    const parsed = previewParts(stream.url);
    const transport = normalizedPreviewTransport(stream.transport, parsed?.protocol);
    const path = transport === "tcp-hevc" ? parsed?.path ?? "" : normalizeRtspPath(stream.path ?? parsed?.path);
    const endpointKey = `${transport}:${port}${path}`;
    if (!streamsByEndpoint.has(endpointKey)) streamsByEndpoint.set(endpointKey, stream);
    streamsByPort.set(port, [...(streamsByPort.get(port) ?? []), stream]);
  }

  const uniqueRequests = requests.filter((stream, index) => (
    requests.findIndex((candidate) => candidate.port === stream.port
      && candidate.path === stream.path
      && candidate.transport === stream.transport) === index
  ));
  const streams = uniqueRequests.map(({ port, path, transport = "rtsp", url }, index): NormalizedProbeStream => {
    const matchingPort = streamsByPort.get(port) ?? [];
    const source = streamsByEndpoint.get(`${transport}:${port}${path}`)
      ?? (matchingPort.length === 1 ? matchingPort[0] : isRecord(rawStreams[index]) ? rawStreams[index] : undefined);
    return {
      port,
      path,
      transport,
      online: source?.online === true,
      url: url ?? previewUrl(endpointHost, port, path, transport),
      reason: text(source?.reason),
      responseCode: typeof source?.responseCode === "number" ? source.responseCode : undefined,
      latencyMs: typeof source?.latencyMs === "number" ? source.latencyMs : undefined,
    };
  });
  const onlineCount = streams.filter((stream) => stream.online).length;
  return { host: endpointHost, reachable: onlineCount > 0, onlineCount, streams };
}
