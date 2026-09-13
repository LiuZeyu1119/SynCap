# SDK 独立真机验收 — 2026-09-13

最新结论见 [本轮交付记录](RELEASE_2026_09_13.md)。手机重新接入后，单次采集/停止/导出/断点续传已通过；本文下方保留此前未通过轮次的记录，不能把历史末态当成当前设备状态。

## 重新接入后的结果

- 独立 Android SDK，会话 `ses_19700102_011552_d77997`，一次开始、两次数据增长采样、一次停止，设备时长 **12,953 ms**。
- **12 个非零数据文件 / 9,601,483 B**；含清单导出 **9,604,118 B**。大小和 SHA-256 验证通过；新目录预置 131,071 字节部分文件后再次导出同样全部通过。这是模拟断点，不是真实断网。
- 独立 MCAP 检查：6 个 MCAP 结构完整，左 360 帧、右 359 帧、IMU 9738 条、音频 191 条；359 组左右时间戳对应，结束边界多 1 个左目消息。不等于严格全帧物理同步，设备日期仍为 1970。
- 较早的本次会话 `ses_19700102_011431_fa756e` 采集/停止成功，但测试错误地要求可选字段 `exportAvailable` 必须出现，验收失败。改为只拒绝明确 false；实际导出仍要求完整清单、非零大小和全部哈希，然后通过以上轮次。
- 独立蓝牙 SDK 自动选择 RFCOMM，扫描到 5 个网络，实际配网写入返回 connected 和 IP。权限通过 Android 系统界面授予，不由 SDK 绕过。
- 硬件测试的宿主前台运行，并持有最长 240 秒 CPU 唤醒锁。基础 SDK 不持锁，结果不能证明系统挂起/杀进程后的后台可靠性。

证据位于 `artifacts/2026-09-13-sdk-completion/`，不打入开发包。预览、Apple、打包和剩余限制见交付记录。

## 较早轮次（历史记录）

## 环境与边界

- Android：OPPO PLP110（公开记录省略设备序列号）。独立 `com.syncap.example` / `com.syncap.example.test`，不是 SynCap App 的桥接调用。
- Kotlin 示例使用 `-PusePublishedSdk=true`，引用本地 Maven AAR `com.syncap:syncap-sdk:0.1.0`。AAR SHA-256 为 `f6b34842f674136fb0326d53151b515ac46b917e332b2fdc9550c339521a99fe`。
- 相机：全志双目，服务 0.2.5，实际 Wi-Fi HTTP 地址为 `192.168.11.36:8080`；这只是本轮地址，不应硬编码进接入程序。
- ADB 用于安装、运行测试和保留故障文件，没有用 HTTP 转发冒充无线链路。本轮未重新配置 Wi-Fi、未刷固件、未删除用户录制、未清空手机数据。

## 已通过

1. Android 手机 Debug `NativeSdkTest` **7/7**：真实加载 AAR 的 Rust/JNA 库，测试类型与设备时间、存储/开始/停止、错误且不重试、无效输入、显式取消、Range/哈希/清单、目录互斥和取消后恢复。此组使用手机内的 HTTP fixture，不代表物理相机采集通过。
2. 手机经真实 Wi-Fi 执行 `LiveCaptureTest.recoveryStateIsPreserved` **1/1**：当设备处于故障隔离时，SDK 返回 `failed`、`recoveryRequired=true`、`rebootRequired=true`，没有伪装为空闲或清除保护。
3. `LiveCaptureTest.queryAfterCoroutineDelay` **1/1**：真实 Wi-Fi 查询、协程延时 1 秒、再次查询返回。不能据此判断先前锁屏停顿的根因已解决。
4. macOS arm64 独立 Swift SDK `LiveDeviceTests.testExportExistingSession` **1/1**：真实 Wi-Fi 导出下面的测试录制，耗时 **30.642 秒**，132 个采集文件、含清单 **154,769,891 B**；Swift 用 CryptoKit 再次逐文件检查非零大小和 SHA-256。
5. Rust Release workspace **70/70**。修复 CLI 测试服务器在 macOS 上接受的 socket 继承 nonblocking 模式导致 `WouldBlock` 的问题：先新增延迟发送请求的测试并确认失败，再显式设置 accepted socket 为 blocking，保留读写超时。CLI HTTP 集成测试随后连续 5 轮 **7/7**，完整 CLI 为 **16/16**。只改测试 fixture，没有放宽 SDK 超时或自动重试规则。

## 未通过的真实采集链路

手机 SDK 单次选择 SD 卡并开始成功，会话 `ses_19700101_235248_b76068`。但测试在开始后的等待阶段没有继续输出状态；当时手机锁屏。等待超出计划后，电脑确认该会话仍在录制，然后终止**独立测试 App** 并用 CLI 显式停止这个已确认属于测试的 captureId。

- 设备报告时长 **135,343 ms**，132 个数据文件、**154,749,013 B**。
- 所以它是“手机 SDK 开始、电脑 CLI 介入停止、Swift SDK 导出”，**不是手机 SDK 开始/停止/导出全链路通过**。
- 独立 MCAP 检查：66 个 MCAP，结束标记完整，每片都有双目/IMU/音频；左右各 3960 条、IMU 107329 条。相同时间戳配对 3959 组，两路各有一个会话边界未配对消息，不能称为严格全帧同步。
- 锁屏、宿主生命周期和协程停顿之间的因果尚未证实。没有通过更换时长计算、隐藏失败或重复发送开始请求解决它。

后续前台测试被手机锁屏检查阻止，在开始录制前失败。测试宿主已改为在解锁时显示独立示例，并给硬件测试增加 **240 秒上限的 CPU 唤醒锁**；测试结束释放，不关闭系统锁屏或更改系统休眠设置。新宿主编译完成，但安装时手机已从 ADB 消失，**这一版尚未在手机上运行，不能称作锁屏问题已修复**。唤醒锁只在独立示例中，不加入基础 SDK AAR；它也不是生产后台服务的替代方案。

## 设备故障与当前阻塞

本轮设备原厂 `qgapp` 多次在空闲阶段停滞。保留故障日志与片段后执行过两次正常重启；最后仍为故障隔离状态，没有进行第三次盲目重启。当前没有主动录制，已有会话保留。

最后 ADB 仅识别到全志设备，没有手机。需要重新连接、解锁安卓手机，继续独立 SDK 开始、计时/数据增长、停止、导出/续传验收。当前既无 Android 真机 R8 新一轮验收，也无 iPhone 真机验收。

完整 SDK 仍缺独立配网、预览、标定模块及移动 App 接入；鸿蒙绑定也未实现。本次新增的是可复现的硬件测试与失败修复，**没有新增这些功能或发布完整 SDK 版本**。

## 复现入口

在仓库根目录配置可用 JDK 17+ 和 Android SDK 后，构建已发布 AAR 的独立测试宿主：

```sh
sdk/android/gradlew -p sdk/android :consumer:assembleDebug :consumer:assembleDebugAndroidTest -PusePublishedSdk=true
adb -s PHONE_SERIAL install -r sdk/android/consumer/build/outputs/apk/debug/consumer-debug.apk
adb -s PHONE_SERIAL install -r sdk/android/consumer/build/outputs/apk/androidTest/debug/consumer-debug-androidTest.apk
adb -s PHONE_SERIAL shell am instrument -w -r -e class com.syncap.example.NativeSdkTest com.syncap.example.test/androidx.test.runner.AndroidJUnitRunner
```

硬件测试默认不自动录制。只有指定实际地址和 `syncap.allowCapture=true` 才开始；必须先确认设备空闲、有足够存储，并在旁监控，不能无人值守运行尚未通过的测试。手机进程被杀死不等于设备停止录制，异常时根据日志中的实际 captureId 查询/显式停止。

```sh
adb -s PHONE_SERIAL shell am instrument -w -r \
  -e class com.syncap.example.LiveCaptureTest#captureStopExportAndResume \
  -e syncap.endpoint http://ACTUAL_DEVICE_IP:8080 \
  -e syncap.allowCapture true -e syncap.captureSeconds 20 \
  com.syncap.example.test/androidx.test.runner.AndroidJUnitRunner
```

Apple 的物理设备测试只读取并导出已有会话，不开始/停止或重启：

```sh
SYNCAP_LIVE_ENDPOINT=http://ACTUAL_DEVICE_IP:8080 \
SYNCAP_LIVE_SESSION_ID=ses_EXISTING_ID \
SYNCAP_LIVE_EXPORT_DIRECTORY=/absolute/new/export-directory \
swift test --package-path sdk/apple --filter LiveDeviceTests
```

测试数据和证据：仓库 `artifacts/2026-09-13-sdk-device-validation/`，包括 Android instrumentation 日志、电脑介入停止的结果、设备故障片段、Swift 导出与独立校验 JSON。它们被 gitignore 排除，不混入 SDK 发行 ZIP。
