# SynCap 共享 SDK

共享 Rust 控制核心及独立平台模块。基础包提供 **设备 HTTP 控制、采集、会话和校验导出**，不依赖 Tauri、Capacitor、界面、ADB 或播放器；蓝牙和媒体模块按需接入。

开发包与接入方式见 [移动端 SDK 接入](MOBILE_SDK.md)，最新验证范围见 [SDK 验证记录](MOBILE_VALIDATION.md)。[第一阶段记录](VALIDATION.md) 是历史快照，不代表当前交付状态。

最新模块与实测结果以 [交付记录](RELEASE_2026_09_13.md) 为准：Android 独立 SDK 已通过手机采集、停止、校验导出和模拟断点续传，新增蓝牙模块通过当前 Tina 的 RFCOMM 扫描/配网写入。不能扩大为整机长期稳定或所有平台验收。

基础控制 ABI 保持 **0.1.0**；Android 媒体模块为 **0.1.1**，其余模块为 0.1.0。媒体更新修复手机预览首帧阻塞，旧发行目录是历史快照。本轮没有替换 Rust 控制二进制，也没有刷设备固件。

## 当前范围

- `core/`：可被其他 Rust 程序直接引用的 `syncap-core`。
- `cli/`：终端程序 `syncap`，只调用同一份核心，不复制设备业务。
- `ffi/`：UniFFI 类型/错误/取消绑定；`android/` 与 `apple/` 提供独立 Kotlin/Swift 开发包和接入示例。
- `android/bluetooth/`：[独立 GATT/RFCOMM 模块](android/BLUETOOTH.md)。
- `android/media/`：[独立预览与原始 NV12 棋盘格标定](android/MEDIA.md)。
- `apple/Sources/SynCapBluetooth/`：[Apple CoreBluetooth 模块](apple/BLUETOOTH.md)。
- `apple-media/`：[独立 UIKit/MobileVLCKit 预览](apple-media/README.md)。
- `../apps/android/src-tauri/`：现有 macOS/Windows 桌面桥接层已经改调核心；BLE 和预览仍由原有平台代码处理。

Android/iOS App 的预览，以及 Android 标定页面已引用 SDK 同一份源码。移动 App 的 HTTP/蓝牙桥接未全部迁移；系统权限、文件选择器、前台服务和交互由宿主负责。Apple 标定和鸿蒙绑定未实现，当前 Tina 缺少原始标定快照接口，不能称为四端功能全部完成。

本阶段以仓库中 `device/syncap_service.py` 和 `device/tina_service.c` 的实际 HTTP API 为准，不宣称已经实现协议草案中的所有 TLS、CBOR、WebSocket 或发现机制。设备能力和相机数量由 `manifest()` 返回值决定。

## 构建与测试

在仓库根目录运行（需要 Rust 工具链；首次构建需要下载依赖）：

```sh
cargo test --locked --manifest-path sdk/Cargo.toml
cargo build --locked --release --manifest-path sdk/Cargo.toml -p syncap-cli
```

macOS/Linux 产物为 `sdk/target/release/syncap`；Windows 本机构建为 `syncap.exe`。提交 `sdk/Cargo.lock`，不提交 `sdk/target/`。这些依赖下载完成后可加 `--offline`。

## 命令行

每次必须明确指定当前设备地址，不自动猜 IP、切换网络或执行 ADB 命令。以下地址仅为示例，须使用设备实际地址和服务端口：

```sh
sdk/target/release/syncap --endpoint http://192.168.1.12:8080 manifest
sdk/target/release/syncap --endpoint http://192.168.1.12:8080 storage
sdk/target/release/syncap --endpoint http://192.168.1.12:8080 capture current
sdk/target/release/syncap --endpoint http://192.168.1.12:8080 capture start --storage usb --name outdoor-test
```

开始命令先确认所选存储可采集，再发送**一次**开始请求。用返回的 `captureId` 停止，用 `sessionId` 导出：

```sh
sdk/target/release/syncap --endpoint http://192.168.1.12:8080 capture stop cap_RETURNED_ID
sdk/target/release/syncap --endpoint http://192.168.1.12:8080 sessions list
sdk/target/release/syncap --endpoint http://192.168.1.12:8080 sessions export ses_RETURNED_ID --output ./exports/my-session
```

`usb` 是现有协议中“可移除存储”的槽位名；全志相机实际为 **SD 卡**，应读取 `storage.usb.mediaType/displayName` 显示真实介质。RoboBaton 可以选择 `internal` 或 `usb`；全志不支持的目标会返回错误。

连接 USB 调试中的全志设备时，可由开发者显式建立转发：

```sh
adb -s CAMERA_SERIAL forward tcp:18080 tcp:8080
sdk/target/release/syncap --endpoint http://127.0.0.1:18080 status
```

成功结果是 stdout 中的一条 JSON；请求失败的结构化错误输出到 stderr，退出码为 1；参数错误退出码为 2。`--help` 不连接设备。CLI 不是交互式计时界面，`capture current` 返回设备实际状态和可用的 `elapsedMs`。

## 在其他程序中复用

其他 Rust 项目的 `Cargo.toml`：

```toml
[dependencies]
syncap-core = { path = "/absolute/path/to/robobaton4p/sdk/core" }
tokio = { version = "1", features = ["rt-multi-thread", "macros"] }
```

调用者管理运行时、UI 状态和导出目录，不需要启动整个 App：

```rust,no_run
use syncap_core::Client;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let device = Client::new("http://192.168.1.12:8080")?;
    let state = device.current_capture().await?;
    println!("{}", state.state);
    let sessions = device.sessions().await?;
    println!("{} sessions", sessions.len());
    Ok(())
}
```

仓库中可编译的只读示例：

```sh
cargo run --manifest-path sdk/Cargo.toml -p syncap-core --example inspect_device -- http://127.0.0.1:18080
```

主要 API：`manifest`、`status`、`storage`、`configure_storage`、`camera_configuration`、`configure_camera_orientation`、`start_capture`、`current_capture`、`stop_capture`、`sessions`、`prepare_export`、`export_session`。

采集和会话提供类型化结果并保留未知扩展字段；状态名保持字符串，以兼容固件新增状态。manifest、遥测、存储、相机配置暂保留 JSON，尚不作为稳定的跨语言 ABI。设备纳秒时间戳保留字符串，避免 JavaScript 整数精度损失；不要拿手机当前时间减设备时间来计算录制时长。

## 采集错误与取消

- 改变设备状态的请求不自动重试。`outcomeUnknown=true` 表示请求结果不确定，**并不表示设备没有开始录制**；先调用 `current_capture()`，再决定是否停止或提示用户。
- 请求的 HTTP 状态、设备错误码和原始消息分别在 `httpStatus`、`deviceCode`、`message` 中。网络/协议错误也有分类，不应翻译成“全部在线”。
- `state=failed`、`recoveryRequired`、`rebootRequired` 等故障原样保留。SDK 不会清除设备保护标记或自动重启设备。
- 取消 Rust future 只取消本次客户端操作，不向设备发送停止命令。停止采集必须显式调用 `stop_capture()`。
- 同一设备的存储配置、开始和停止应由调用方串行执行。当前协议没有原子“选存储并开始”事务，也不能阻止另一台控制端同时改动设备。

## 导出约定

- `export_session(session_id, destination)` 的目录就是最终会话目录，**不会再追加 sessionId**。每个会话使用独立目录。跨进程目录锁会拒绝同目录的并发导出；锁文件 `.syncap-export.lock` 是正常的零字节控制文件，不是采集数据，不计入导出报告，也不要在导出期间手动删除。
- 分块下载，不把视频整体加载进内存；中断保留 `文件名.part`。重复执行同一导出命令可续传，完成的文件重新校验后复用。
- 校验每个文件非零大小、总大小和 SHA-256；同时核对原始会话清单和导出计划，防止只导出文件子集。续传核验 `Content-Range`。服务端不支持 Range 时可从头重下当前 partial。
- 校验失败不产生成功报告，不用坏文件替换最终文件。完整且正确的 `.part` 原子提交；文件系统不支持无覆盖原子提交时会保留 partial 并报错。
- 拒绝文件路径越界、重复/保留文件名、符号链接、跨设备 URL 和重定向；导出目录应由调用者独占管理。已有最终文件内容不同时拒绝覆盖。
- 保留设备原始 `session.json` 字节。该文件必须包含匹配的会话身份；如本地已有不同版本，换一个空目录导出，避免覆盖旧元数据。
- `.part` 为 SDK 保留文件；hash 错误的 partial 保留用于检查，不会自动丢弃。确认不再需要后可移走该 partial，再重试。
- `verified=true` 表示**传输文件的大小和哈希已验证**，不是重新验证 MCAP/视频语义、相机同步或标定质量。
- 取消 future 会停止后续网络传输，但已提交的磁盘操作可能继续完成；这些操作完成前仍持有目录锁。取消不是磁盘操作已停止的同步屏障，稍后可重试并复用 partial 或已校验的最终文件。
- 当前尚未提供进度回调、移动端后台任务或导出前目标磁盘容量估算；磁盘写入失败会返回 IO 错误，保留可恢复文件。

## 网络与后续移动端边界

SDK 接受 HTTP/HTTPS 的设备 origin，不接受 URL 中的账号、查询参数或子路径；设备请求禁用系统代理和重定向。现有设备使用 HTTP 和兼容字段 `claimCode=123456`，这**不是安全认证**；仅在可信局域网/显式本地转发使用，不应暴露到公网。HTTPS 客户端支持并不等于设备已提供 HTTPS。

Kotlin/Swift 绑定提供对象、异步调用、结构化错误和显式取消；进度回调尚未实现。移动 App 控制/导出桥接未全部迁移。蓝牙与硬件解码已有独立平台模块，仍须按设备能力/系统权限接入，不能假设 iOS 可以使用 Android SPP。构建/模拟器通过不等于全部真机验收。
