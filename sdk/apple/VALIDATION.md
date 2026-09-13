# Apple SDK 验证记录（2026-09-10）

本记录针对 `sdk/apple` 的独立 Swift Package，不是现有 iOS App 验收。

最新模块记录：`RELEASE_2026_09_13.md`。9 月 13 日本轮新增 CoreBluetooth 模块，4 项协议/取消测试通过；控制核心 6 项重跑通过，独立包再构建，iOS/模拟器编译通过。以下保留较早阶段的结果，不能据此推断已测 iPhone。

2026-09-13 补充：现有 XCFramework 的 macOS arm64 `swift test --package-path sdk/apple` 重跑 **6 项通过，0 失败**；没有重建原生库或重跑 iOS 模拟器/真机。本节以下保留 9 月 10 日历史结果。各平台的最新汇总在源码 `sdk/MOBILE_VALIDATION.md`（发行包根目录同名文件）。

- 构建环境：macOS 27、Xcode 27 beta、Swift 6.4（Package Swift 5 language mode）、Rust 1.97.1、UniFFI 0.32.1。
- 重新生成最终 `detail` 错误字段的 Swift/FFI 绑定，并使用 workspace 最终 release profile（`syncap-ffi strip=none`）打包；没有混用旧生成源码。
- `bash sdk/scripts/build-apple.sh`：成功；5 个 Rust target 合并为 iOS 设备 arm64、iOS 模拟器 arm64/x86_64、macOS arm64/x86_64 的 3 个 XCFramework slice。
- `swift test --package-path sdk/apple`：macOS arm64 **6 项通过，0 失败**。
- 14:43 对最终 Android 条件编译 `rustix` 文件锁改动后的源码重新生成绑定、构建全部 5 个 Apple target，并重跑 macOS Swift 测试：**6 项通过，0 失败**。Apple 仍使用标准库文件锁；本次未再次运行 iOS 模拟器，以下 iOS 结果来自前一轮同接口验证。
- `xcodebuild ... -destination generic/platform=iOS build`：iOS arm64 编译成功。
- `xcodebuild ... -destination 'generic/platform=iOS Simulator' build`：模拟器 arm64/x86_64 编译成功。
- `SYNCAP_IOS_SIMULATOR_ID=895A0ADC-F169-481B-998A-C879E55E571E bash sdk/apple/test.sh`：iPhone 17 Pro / iOS 27.0 / arm64 模拟器 **6 项通过，0 失败**。由 `xcresulttool get test-results summary` 核实，并非仅凭构建退出码判断运行通过。

最后一次 iOS 测试结果位于本机生成目录：

```text
sdk/apple/.build/xcode/Logs/Test/Test-SynCapSDK-Package-2026.09.10_14-22-44-+0800.xcresult
```

六项均经过真实 Swift → UniFFI → Rust 核心 → 本地 HTTP server 调用，覆盖采集开始/当前/停止/会话、设备时间和扩展字段、结构化错误且不重试、URL 参数错误、设备故障透传、取消导出但不停止相机、Range 续传、SHA-256 校验、原始清单字节和已有文件复用。

未验证：iPhone 真机及其局域网权限体验、Intel Mac 实机、旧版 iOS/macOS 系统、后台导出和上架签名。测试未操作已连接相机，不包含长时间采集固件回归。
