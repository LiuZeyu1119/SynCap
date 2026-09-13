export const CAMERA_DISPLAY_ORDER = ["cam3", "cam1", "cam2", "cam0"] as const;

export const CAMERA_MOUNT_POSITIONS = {
  cam3: { code: "wearer_left_outer", label: "左外侧" },
  cam1: { code: "wearer_left_inner", label: "左内侧" },
  cam2: { code: "wearer_right_inner", label: "右内侧" },
  cam0: { code: "wearer_right_outer", label: "右外侧" },
} as const;

const cameraDisplayRank = new Map<string, number>(
  CAMERA_DISPLAY_ORDER.map((cameraId, index) => [cameraId, index]),
);

export function isCompleteLegacyFourCameraSet(cameras: readonly { id: string }[]) {
  return cameras.length === CAMERA_DISPLAY_ORDER.length
    && CAMERA_DISPLAY_ORDER.every((cameraId) => cameras.some((camera) => camera.id === cameraId));
}

export function orderCamerasForDisplay<T extends { id: string }>(cameras: readonly T[]) {
  if (!isCompleteLegacyFourCameraSet(cameras)) return [...cameras];
  return cameras
    .map((camera, index) => ({ camera, index }))
    .sort((left, right) => {
      const leftRank = cameraDisplayRank.get(left.camera.id);
      const rightRank = cameraDisplayRank.get(right.camera.id);
      if (leftRank == null && rightRank == null) return left.index - right.index;
      if (leftRank == null) return 1;
      if (rightRank == null) return -1;
      return leftRank - rightRank;
    })
    .map(({ camera }) => camera);
}

export function cameraMountPosition(camera: { id: string; mountPosition?: string; direction?: string }) {
  const known = CAMERA_MOUNT_POSITIONS[camera.id as keyof typeof CAMERA_MOUNT_POSITIONS];
  if (known) return known.label;
  const mountCode = camera.mountPosition?.trim();
  if (mountCode === "stereo_left") return "左目";
  if (mountCode === "stereo_right") return "右目";
  const byCode = Object.values(CAMERA_MOUNT_POSITIONS).find((position) => position.code === mountCode);
  return byCode?.label ?? mountCode ?? camera.direction?.trim() ?? camera.id.toUpperCase();
}
