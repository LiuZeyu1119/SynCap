# Kotlin / Swift SDK 0.1.0

共享核心及可选平台模块，**不是新版本 SynCap App 安装包**。Android/iOS 原生预览及 Android 标定页面已引用 SDK；移动 App 的 HTTP/蓝牙桥接尚未全部切换。

## 当前交付状态（2026-09-13）

| 入口 | 已有产物 | 验证边界 |
| --- | --- | --- |
| Android | 控制/蓝牙/媒体 AAR、本地 Maven、独立示例 | 手机控制与真实采集/停止/导出/续传通过；RFCOMM 配网通过；媒体 0.1.1 两路前台预览 60 秒通过，设备长期稳定性仍未通过 |
| iOS / macOS | Swift Package、XCFramework、示例与测试 | macOS arm64 和 iOS 模拟器已测；未测 iPhone 真机 |
| 终端 / Rust | 共享核心、CLI 源码、macOS arm64 CLI | CLI 已通过真实 Wi-Fi 导出与模拟 partial 续传；未交付验收通过的 Windows 二进制 |
| 鸿蒙 | 尚无 ArkTS / Native SDK 发行包 | 未实现或验证，不算已支持 |

本次发行目录为仓库中的 `artifacts/2026-09-13-sdk-modular/`。基础控制二进制未改变，新增独立蓝牙/媒体模块。包内附接入说明、示例和验证记录，发行目录附 `SHA256SUMS`；`sdk-handoff` 是此前的历史快照。

最新发行内容和实测结果见 [交付记录](RELEASE_2026_09_13.md)、[独立真机验收](HARDWARE_VALIDATION.md)。旧发行目录仅代表当时快照。

新增独立 Android 蓝牙、预览/标定模块，Apple CoreBluetooth product 和独立 iOS 媒体包。当前 Tina 实测配网通道是 Classic RFCOMM，不能据此承诺 iOS 已能配网。详见各模块接入文档。

## 拿到包后怎样接入

- Android：解压 `SynCapSDK-Android-0.1.1.zip`，按包内 README 添加 `maven/` 仓库和 `com.syncap:syncap-sdk:0.1.0` 依赖；可选媒体模块使用 `com.syncap:syncap-media-android:0.1.1`。`example/` 是完整独立 Gradle 项目，只通过已发布的 Maven 坐标引用 SDK，不含 Rust 或 SDK 源码模块。
- Apple：解压 `SynCapSDK-Apple-0.1.0.zip`，在 Xcode 中把解压目录作为本地 Swift Package，选择 `SynCapSDK` product。包内同时包含生成 Swift 接口与 XCFramework，无需安装 Rust。
- 裸 AAR 另附给已有依赖管理方案的调用方；它不自动包含 Kotlin、JNA、协程等传递依赖。优先使用附带 Maven 元数据。

Android 独立例子在 `example/` 中运行 `./gradlew :consumer:assembleDebug`。首次构建仍需 Android/JDK 工具链和下载或预先缓存 Maven 依赖；开发包不承诺离线安装全部构建工具。**安装后的设备控制和数据导出无需互联网**，只需能连接实际设备的局域网地址。

## 同一份实现

`core/` 是唯一的设备 HTTP 与导出实现；`ffi/` 仅转换跨语言类型、结构化异常和显式取消；Kotlin/Swift 由固定版本 UniFFI 0.32.1 生成，不手动维护两份协议逻辑。CLI 和桌面仍直接复用 core。

可用接口包括设备能力、状态、存储查询/选择、相机旋转配置、开始/查询/停止采集、会话列表、准备导出、带大小/哈希/清单校验的断点导出。

Kotlin 的网络调用是 `suspend`，Swift 为 `async throws`。采集、会话和导出结果有原生类型；可扩展能力/遥测/参数以 JSON 字符串返回。`rawJson` 是完整模型的 JSON 序列化，保留固件扩展字段，但不承诺原始 HTTP 响应的字节、空白或键顺序。

核心纳秒时间戳继续用字符串，时长使用设备返回的 `elapsedMs`。错误保留分类、`detail`（核心消息）、HTTP 状态、设备错误码、`outcomeUnknown`。结果未知时先查 `currentCapture`，不盲目重发开始请求；关闭 SDK 客户端和取消导出均不等于停止设备录制。

导出目录必须是宿主可写的绝对本地路径；Android SAF `content://` 和 Apple `file://` URI 不能直接传入。每次导出创建独立 `Cancellation` token，并明确调用 `cancel()`，不要假设 Coroutine/Swift Task 的取消会自动停止 Rust。已排队的磁盘操作可能继续持锁完成，随后可在同目录续传。

目录中的 `.syncap-export.lock` 是正常的零字节控制文件，不是采集数据，也不计入导出报告。不要在导出期间删除它。Android 使用系统 `flock` 保持真实的目录互斥，其他平台使用标准库文件锁。

## 平台边界

- Android：API 24+ 构建最低版本，arm64-v8a / x86_64。JNA 依赖必须使用 Android AAR；SDK 与 JNA 的 native libraries 都需一起打入宿主 APK。
- Apple：iOS 15+、macOS 12+ 构建最低版本；iOS 设备 arm64，模拟器和 Mac 各 arm64/x86_64。多个 XCFramework slice 的总大小不是最终 App 体积。
- 这些构建最低版本不等于已在每个版本/架构的真实设备上运行验证。
- BLE/SPP、预览和 Android 标定为可选模块，不在基础控制 AAR/XCFramework 内。系统文件选择、后台任务和进度 UI 由宿主负责；Apple 标定和鸿蒙绑定未实现。基础核心按 manifest 能力工作，媒体模块的型号/时间基准限制见各自说明。
- 当前固件的明文 HTTP 和 `123456` 兼容字段不构成安全认证；限可信局域网，不能直接暴露公网。
- 全志原厂采集进程长时间运行后卡住的问题尚未证明修复；短时成功采集不能作为长期稳定性验收。SDK 不会清除设备保护状态。

## 从源码重建

```sh
cargo test --locked --manifest-path sdk/Cargo.toml
cargo build --locked --release --manifest-path sdk/Cargo.toml -p syncap-cli
bash sdk/scripts/build-android.sh
bash sdk/scripts/build-apple.sh
bash sdk/apple/test.sh
bash sdk/scripts/build-apple-media.sh
bash sdk/scripts/package-mobile.sh
```

Android 脚本需要显式的 `ANDROID_HOME`、可用 JDK 和 NDK 29；Apple 需要完整 Xcode 与相应 Rust targets。仓库内详细调用及权限要求见 `sdk/android/README.md` 和 `sdk/apple/README.md`；发行 zip 中见各自 `README.md`。验证范围见 [移动 SDK 验证记录](MOBILE_VALIDATION.md)。

iOS 媒体构建需要先按 `sdk/apple-media/README.md` 准备 MobileVLCKit 运行库；全部模块的发行脚本在 macOS arm64 上执行，并附已构建 CLI。只使用控制模块时无需媒体依赖。

本机构建使用 Rust 1.97.1 和 Xcode 27 beta。针对其 Mach-O debug-strip 对齐问题，workspace 对 `syncap-ffi` 设置 `strip="none"`，Android 打包仍单独 strip ELF；没有手改生成的二进制。相关上游记录：[Rust issue 157750](https://github.com/rust-lang/rust/issues/157750)。异步绑定/取消依据：[UniFFI async 文档](https://mozilla.github.io/uniffi-rs/latest/futures.html)。
