# Android SDK 独立复测

以下命令在发行 Android ZIP 的解压根目录执行。`example/` 只消费已发布 Maven 坐标；`source/` 是可编辑的三个库及其测试。需要 JDK、Android SDK 与依赖缓存/网络，不需要原 App。工具应使用调用方自己的路径。

## 无物理相机的测试

```sh
./example/gradlew -p example :consumer:assembleDebug :consumer:assembleDebugAndroidTest
./source/gradlew -p source :library:assembleRelease :bluetooth:assembleDebugAndroidTest :media:assembleDebugAndroidTest
```

明确选中要测试的 Android 设备，再运行：

```sh
export ANDROID_SERIAL=YOUR_TEST_PHONE_SERIAL
./example/gradlew -p example :consumer:connectedDebugAndroidTest
./source/gradlew -p source :bluetooth:connectedDebugAndroidTest :media:connectedDebugAndroidTest
```

默认跳过外部相机/蓝牙操作；核心 7 项使用手机内的本地 HTTP fixture，蓝牙验证协议边界，媒体验证原始 NV12 输入和合成双目标定。后台管理较严格的手机请解锁并保持测试页前台。不要把这些测试当作物理曝光同步、实拍标定或全链路验收。

若离线环境只有编译依赖、缺少 Gradle connected 使用的 UTP 依赖，可直接安装同一个媒体测试 APK 执行，仍运行真实 instrumentation：

```sh
adb -s "$ANDROID_SERIAL" install -r source/media/build/outputs/apk/androidTest/debug/media-debug-androidTest.apk
adb -s "$ANDROID_SERIAL" shell am instrument -w -r \
  com.syncap.sdk.media.test/androidx.test.runner.AndroidJUnitRunner
```

R8 测试为 `./example/gradlew -p example :consumer:connectedReleaseAndroidTest -PtestReleaseSdk=true`，使用专用测试宿主保留规则；**不要在 R8 版本启用 LivePreviewTest**，它的测试取证依赖 Debug 内部字段。

## 实际采集、停止、导出与模拟断点

这会实际录制约 10 秒，占用设备和手机空间；测试完成后不删除数据。手机与头环需网络可达，不能已有其他采集，故障保护状态会直接拒绝。安装的是独立示例，不是 SynCap App：

```sh
adb -s "$ANDROID_SERIAL" install -r example/consumer/build/outputs/apk/debug/consumer-debug.apk
adb -s "$ANDROID_SERIAL" install -r example/consumer/build/outputs/apk/androidTest/debug/consumer-debug-androidTest.apk
adb -s "$ANDROID_SERIAL" shell am instrument -w -r \
  -e class com.syncap.example.LiveCaptureTest#captureStopExportAndResume \
  -e syncap.endpoint http://DEVICE_IP:8080 \
  -e syncap.allowCapture true -e syncap.captureSeconds 10 \
  com.syncap.example.test/androidx.test.runner.AndroidJUnitRunner
```

成功标准：一次开始/停止，设备时长和字节增长，completed，完整文件清单非零且大小/SHA-256 匹配，新目录预置部分文件后再次导出通过。证据位于示例私有目录 `files/sdk-live-UUID/`，输出会返回实际路径；包括 `evidence.json`、`export/` 和 `resume/`。模拟 partial 不等于实际断网/系统杀进程恢复。

## 真实预览

退出所有其他相机预览客户端，使用上述 Debug 安装包，解锁并让预览保持前台约 80 秒：

```sh
adb -s "$ANDROID_SERIAL" shell am instrument -w -r \
  -e class com.syncap.example.LivePreviewTest \
  -e syncap.endpoint http://DEVICE_IP:8080 -e syncap.allowPreview true \
  -e syncap.previewSeconds 60 \
  com.syncap.example.test/androidx.test.runner.AndroidJUnitRunner
```

检查两路首帧、随后显示帧增长及关闭后的播放器资源释放；观察时长默认 12 秒，可设 12–120 秒。来电/切后台会明确失败，不算解码失败或通过。2026-09-13 媒体 0.1.1 在 OPPO PLP110 / Tina 上通过三次冷启动及一轮 Maven 包 60 秒测试；这是实际渲染计数，不是 TCP 连通性测试。该设备另有长时 IMU/编码器停滞，不能据此宣称长期稳定，详见发行报告。

## 实际蓝牙扫描与配置

在 Android 系统正常配对、授予“附近设备”权限，不输入认领码。先构建并安装 `source/bluetooth/build/outputs/apk/androidTest/debug/bluetooth-debug-androidTest.apk`，再显式只读扫描：

```sh
adb -s "$ANDROID_SERIAL" install -r source/bluetooth/build/outputs/apk/androidTest/debug/bluetooth-debug-androidTest.apk
adb -s "$ANDROID_SERIAL" shell am instrument -w -r \
  -e class com.syncap.sdk.bluetooth.ProvisioningTest#livePairedDevice \
  -e syncap.allowBluetooth true -e syncap.bluetoothAddress YOUR_DEVICE_MAC \
  com.syncap.sdk.bluetooth.test/androidx.test.runner.AndroidJUnitRunner
```

实写配置还需显式传入 `syncap.allowProvision=true`、`syncap.ssid`、`syncap.password`。这些值由调用方提供，不能放到仓库或公共日志；含密码的 ADB 参数也可能留在 shell history/进程命令行，面向用户应通过 `BluetoothWorkflow` 的输入界面调用 SDK。不要默认执行配置来证明“扫描通过”。本轮 Tina 实际通道为 RFCOMM；GATT 和 iPhone 不在此硬件结论内。

若 `outcomeUnknown=true`，先查实际状态，不能重发开始/配置碰运气。测试不刷固件、不清除保护标记、不重启头环，也不替代真实 App 的后台任务与系统生命周期管理。
