export type DeviceManifest = {
  protocolVersion: string;
  device: {
    id: string;
    displayName: string;
    model: string;
    type?: string;
    firmwareVersion: string;
    ipAddress: string;
  };
  capabilities: string[];
  cameras: Array<{ id: string; label: string; direction: string; mountPosition?: string; previewUrl?: string; previewTransport?: string; port?: number }>;
};

export type SessionRecord = {
  id: string;
  name: string;
  createdAt: string;
  duration: string;
  size: string;
  status: "complete" | "recording" | "failed";
  sizeBytes?: number;
  fileCount?: number;
  exportAvailable?: boolean;
  failureReason?: string;
};
