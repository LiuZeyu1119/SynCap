# Android 预览与标定 SDK

独立坐标：`com.syncap:syncap-media-android:0.1.1`。从原 App 移出的原生预览、棋盘格采集页面和 OpenCV 双目标定引擎，原 Android App 现在引用同一个模块；不是通过启动原 App 来实现功能。

为保留既有 App 与测试兼容性，公共 Java 类仍位于 `com.syncap.studio`。库不依赖 Capacitor、Tauri 或 React。

## 预览

```kotlin
import com.syncap.studio.SynCapMedia

// 从设备 manifest.cameras[*].preview.url 读取 URL，标签采用真实安装位置。
startActivity(SynCapMedia.previewIntent(context, cameraUrls, cameraLabels))
```

支持 1–8 路 RTSP/TCP-HEVC、原生关闭和页面生命周期释放。Tina 原始 HEVC 的时间基准当前为 29.4118 fps；RTSP 使用流自身时间戳。媒体权限、主题、非导出 Activity 和独立 FileProvider 随 AAR 合并。预览 Activity 必须前台显示；切到其他 App 时暂停/释放流属于预期行为。

0.1.1 禁用相机预览不需要的 VLC 字幕、OSD 和字体渲染器，避免字体初始化阻塞首帧；相机名称/状态仍由原生 Android View 绘制。未切换软件解码、改变码率/分辨率或重编码采集数据。真实测试范围见随包验收记录。

该模块依赖 LibVLC 3.7.5 和 OpenCV 4.9.0。基础控制 AAR 不引入它们；只需控制/导出的应用不要添加媒体模块。示例同时打包两个 ABI 和两套媒体依赖，比纯控制示例大很多，不代表基础 SDK 变大。

当前验证组合有重复的 `libc++_shared.so`，完整示例/原 App 使用下列配置；它只适用于本文已验证的依赖组合。接入其他 C++ 库时需重新验证，不可用它掩盖任意 ABI/版本冲突：

```kotlin
android {
    defaultConfig { ndk { abiFilters += listOf("arm64-v8a", "x86_64") } }
    packaging { jniLibs { pickFirsts += "lib/**/libc++_shared.so" } }
}
```

## 标定计算（不依赖内置页面）

```kotlin
import com.syncap.studio.StereoCalibrationSession
import org.json.JSONObject

StereoCalibrationSession(10, 7, 30.0, "left", "right").use { calibration ->
    // 输入横向 10 格、纵向 7 格，每格边长 30 mm，不是内角点数量。
    // 每次不同位姿，传入设备原始同步快照及其原始元数据；在工作线程调用。
    val sample = calibration.addRawPair(leftNv12Bytes, rightNv12Bytes, snapshotMetadata)
    // sample.accepted / reason / sampleCount / canSolve 用于显示采样反馈。
    if (calibration.canSolve()) {
        val parameters: JSONObject = calibration.solve("cal_session_id", "stereo")
        // 宿主保存参数；检查 quality / RMS，不以采样数量代替标定质量。
    }
}
```

一个会话要反复 `addRawPair`，不能每张图重建对象。最低 18 对、至少 5 个区域，页面目标 24 对；是否可解算还取决于覆盖条件。使用现有 **OpenCV fisheye** 模型，不支持随意指定其他镜头模型。

输入必须为**完整尺寸原始 NV12**。元数据要求：

```json
{
  "source": "synchronized_raw_nv12",
  "syncErrorNs": "1000",
  "cameraConfiguration": { "rotationDegrees": 0 },
  "cameras": [
    { "id": "left", "format": "nv12", "width": 1280, "height": 1088, "sizeBytes": 2088960, "sha256": "实际左帧 SHA-256" },
    { "id": "right", "format": "nv12", "width": 1280, "height": 1088, "sizeBytes": 2088960, "sha256": "实际右帧 SHA-256" }
  ]
}
```

校验每帧大小与哈希、左右尺寸、设备声明的时间差 ≤ 2 ms、同一会话方向/尺寸不变；拒绝预览来源、空白棋盘、重复姿态。方向只能 0/90/180/270 度，原始帧应已按设备配置旋转；SDK 不在求解前私自再次旋转。元数据校验不是独立物理曝光同步测量，宿主不能从预览截图伪造上述字段。

`SynCapMedia.calibrationIntent(...)` 可打开既有 RoboBaton 原始快照标定页面（同设备双 RTSP，pair14/pair23）；页面下载原始 NV12，而不是从播放器截图。算法 API 可用其他相机 ID 和原始尺寸，不受页面的既有配对命名限制。

**当前 Tina 固件没有原始同步标定快照接口，因此不能用它完成这套实拍标定。** SDK 不会用预览代替。原始输入校验及合成 120 mm 已知基线测试可验证程序行为，但不是当前镜头实拍 RMS 验收。Apple 对应标定模块尚未实现。

LibVLC LGPL 授权文本和来源说明随 AAR assets/licenses 提供；OpenCV 授权随其发行依赖，发布宿主应用时应一并保留适用声明。
