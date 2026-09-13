# SynCap SDK for iOS and macOS

最新实测范围见发行包的 `RELEASE_2026_09_13.md`。新增可选 product `SynCapBluetooth`（[接入](BLUETOOTH.md)）；iOS 预览另发 `SynCapSDK-AppleMedia-0.1.0.zip`，不增加基础控制库依赖。当前 Tina 的 Classic 配网不能由此在 iPhone 使用，Apple 标定尚未实现。

可独立接入的 Swift Package：`SynCapSDK` 是由 UniFFI 生成的 Swift 异步对象接口，`SynCapSDKFFI.xcframework` 是同一份 Rust 核心的静态库。它不需要 SynCap App、Capacitor、Tauri、ADB 或第三方视频播放器。

支持 iOS 15+、macOS 12+；这些是构建最低版本，不代表已逐一运行这些系统版本。当前二进制包括：

| 平台 | 架构 |
| --- | --- |
| iOS 设备 | arm64 |
| iOS 模拟器 | arm64、x86_64 |
| macOS | arm64、x86_64 |

XCFramework 内有多个平台的完整静态库；其总大小不等于最终 App 增量大小。不要把 iOS 模拟器或 macOS slice 手动复制进 App。

## 接入

交付包应包含本目录中的 `Package.swift`、`Sources/` 和 `SynCapSDKFFI.xcframework/`。从 Git 源码取用时，先在仓库根目录运行构建脚本；拿到完整预编译包的使用者**不需要安装 Rust**。

在 Xcode 中添加本目录为本地 Swift Package，然后把 `SynCapSDK` library product 加入宿主 target；使用 `import SynCapSDK`。不要仅拖入 XCFramework 后期待 Swift 的 `DeviceClient` 已存在：Swift 对象由 Package 的源码 target 提供。

解压发行 ZIP 后，也可在解压目录直接运行 `swift test`。只读终端示例为 `swift run syncap-swift-example http://设备实际地址:8080`；不会自动开始采集。下文 `sdk/...` 路径仅用于仓库源码构建。

```swift
import SynCapSDK

let device = try DeviceClient(endpoint: "http://192.168.1.12:8080")

// 地址必须来自实际连接的设备；不猜 IP，不自动切换 Wi-Fi。
let state = try await device.currentCapture()
print(state.state, state.elapsedMs as Any)

// 显式选择存储并确认成功后再开始。
// removable 对应协议 usb 槽位，在全志设备上实际是 SD 卡。
let storage = try await device.configureStorageJson(target: .removable)
let started = try await device.startCapture(name: "Outdoor capture")
let stopped = try await device.stopCapture(captureId: started.captureId)
let sessions = try await device.sessions()
```

以上顺序展示接口，不是开始后自动立即停止的采集 UI。实际宿主应在用户选择开始/停止时调用，并串行管理同一设备的写操作。获取可用相机、存储介质名称和设备能力用 `manifestJson()` / `storageJson()`，不要写死“两路/四路”或“U 盘”。

采集、会话、导出结果有 Swift 类型；可扩展遥测和参数返回 JSON 字符串。类型化结果的 `rawJson` 保留固件原始扩展字段。设备纳秒时间戳为字符串，`elapsedMs` 为 `UInt64?`；不要用手机时间减设备时间计算录制时长。

## 导出与取消

传入宿主可写的本地绝对路径，不是 `file://` URI。建议先导出到 App 的 Documents/Application Support 会话子目录；系统文件选择器、安全作用域 URL 和导出分享由宿主负责。

```swift
import Foundation
import SynCapSDK

let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0]
let destination = documents.appendingPathComponent(started.sessionId)
let cancellation = Cancellation()

let report = try await device.exportSession(
    sessionId: started.sessionId,
    destination: destination.path,
    cancellation: cancellation
)
print(report.verified, report.bytes)

// 取消按钮调用同一个 token；每次新导出创建新的 token。
// cancellation.cancel()
```

`cancellation.cancel()` 可以从其他任务调用；本次操作以 `SdkError.Failure(kind: .cancelled, …)` 结束。不要假设 `Task.cancel()` 会自动停止 Rust 操作；需要关联 Swift Task 取消时可在 `withTaskCancellationHandler` 的 `onCancel` 中调用同一 token 的 `cancel()`。

取消导出**不会停止设备采集**。已完成文件及 `.part` 保留；用新 token、同一会话和同一目录重试可续传。不能以“Swift Task 已取消”为由重复发送采集开始请求。已提交的磁盘操作仍可能短暂继续，完成前保留目录锁。

导出校验大小、SHA-256、Range 和完整文件清单，不覆盖冲突的已有最终文件。`report.files`、`report.bytes` 包含保存的 `session.json`；`verifiedFiles` 是校验的采集文件数。`verified=true` 只表示传输完整性，不表示重新验证视频语义、同步或标定质量。

## 处理真实错误

```swift
do {
    _ = try await device.startCapture(name: "Capture")
} catch let SdkError.Failure(kind, detail, httpStatus, deviceCode, outcomeUnknown) {
    print(kind, detail, httpStatus as Any, deviceCode as Any)
    if outcomeUnknown {
        // 请求可能已被设备执行；先查实际状态，不盲目重试。
        let actual = try await device.currentCapture()
        print(actual.state)
    }
}
```

`state=failed`、`rebootRequired` 等设备故障保留在结果中；SDK 不会把它们伪装成在线/空闲，也不会自动清除保护标记或重启设备。

## 宿主网络权限与边界

宿主访问局域网时，在 App 的 `Info.plist` 添加 `NSLocalNetworkUsageDescription`，例如“连接采集设备，预览和导出您的采集数据”。权限拒绝要由宿主引导用户恢复，库不能绕过。开启 App Sandbox 的 macOS App 还需 outgoing network client entitlement。参见 [Apple 本地网络隐私说明](https://developer.apple.com/documentation/technotes/tn3179-understanding-local-network-privacy) 与 [网络客户端 entitlement](https://developer.apple.com/documentation/bundleresources/entitlements/com.apple.security.network.client)。

基础 `SynCapSDK` product 明确指定设备地址，不做 Bonjour/mDNS 或 BLE 扫描；仅接入它时不需要蓝牙权限。新增的 `SynCapBluetooth` 有自己的系统授权要求，见 BLUETOOTH.md。现有设备使用明文 HTTP 和兼容认领字段，限可信局域网；库支持 HTTPS 不等于设备实现了 HTTPS，不要全局关闭宿主网络安全保护。

蓝牙是本包的独立 product；预览/解码在另发的 iOS 媒体包中。Apple 标定尚未实现，后台任务和系统授权 UI 由宿主负责。Android SPP 不能直接当作 iOS BLE。本包不代表完整 iOS App 或原厂固件稳定性已验收。

## 源码构建与验证

需要 macOS、完整 Xcode、Rust，以及下面五个 target。脚本只检查依赖，不自动安装/更新工具链，也不接触设备数据：

```sh
rustup target add aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios \
  aarch64-apple-darwin x86_64-apple-darwin
bash sdk/scripts/build-apple.sh
bash sdk/apple/test.sh
```

脚本执行固定 Cargo.lock 的 Rust 构建、同版本 UniFFI 生成、lipo、`xcodebuild -create-xcframework`。只在新框架全部成功后替换旧生成产物；旧框架保留在 `sdk/target/apple-package/` 本轮 staging 下。不要手改生成的 `Sources/SynCapSDK/SynCapSDK.swift` 或 FFI 头文件。

测试默认运行 macOS 的六项本地 HTTP 测试，并编译 iOS 设备/模拟器 target。指定已有 iOS 模拟器 UUID 可额外运行同一组 XCTest：

```sh
xcrun simctl list devices available
SYNCAP_IOS_SIMULATOR_ID=YOUR_SIMULATOR_UUID bash sdk/apple/test.sh
```

可独立运行的只读 Swift 示例（地址仅为示例）：

```sh
swift run --package-path sdk/apple syncap-swift-example http://127.0.0.1:18080
```

测试覆盖真实 Swift → Rust FFI 调用：采集生命周期和设备时钟、结构化设备错误且不重试、无效 URL、故障透传、显式取消、断点续传及原始清单保留。测试服务仅监听本机回环地址，不控制相机。
