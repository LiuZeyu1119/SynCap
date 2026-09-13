# SynCap Rust SDK 与终端

本包是可独立构建的 Rust workspace，包含 `core`、`cli`、`ffi`，不依赖原 App 或 ADB。功能与实测限制见同目录 `RELEASE_2026_09_13.md`。这是控制和文件导出核心，不包含跨平台蓝牙、播放器或标定算法。

## 构建

在解压目录内执行：

```sh
cargo test --locked --release
cargo build --locked --release -p syncap-cli
```

需要 Rust 工具链，首次构建需下载或预缓存依赖。本轮使用 Rust 1.97.1；只在 macOS arm64 验证，未验证 Windows/Linux 二进制。FFI 使用固定 UniFFI 版本；其移动端预编译绑定在单独的 Android/Apple 包。

## 终端操作

发行目录另附已构建的 `syncap-macos-arm64`。先运行 `./syncap-macos-arm64 --help`。以下使用源码构建的命令名称；地址和 ID 必须替换为设备实际返回值：

```sh
./target/release/syncap --endpoint http://192.168.1.12:8080 manifest
./target/release/syncap --endpoint http://192.168.1.12:8080 storage
./target/release/syncap --endpoint http://192.168.1.12:8080 capture start --storage usb --name outdoor-test
./target/release/syncap --endpoint http://192.168.1.12:8080 capture current
./target/release/syncap --endpoint http://192.168.1.12:8080 capture stop cap_RETURNED_ID
./target/release/syncap --endpoint http://192.168.1.12:8080 sessions list
./target/release/syncap --endpoint http://192.168.1.12:8080 sessions export ses_RETURNED_ID --output ./exports/session
```

`usb` 是协议中的可移除存储槽位：当前 Tina 为 SD 卡，不应在 UI 中错误标成 U 盘。读取存储响应的实际介质与可采集状态。开始和停止各发送一次，不因超时盲目重试；`outcomeUnknown=true` 时先查询当前采集状态。录制时长使用设备 `elapsedMs`，不能用主机时间减设备日历时间。

导出目录就是最终会话目录，每个会话使用独立目录。中断后在同一目录执行相同导出命令会检查并续传 `.part`；不覆盖内容不符的最终文件。成功报告表示清单、大小和 SHA-256 通过，不是曝光同步、视频逐帧或标定质量验收。不要在导出时删除正常的零字节 `.syncap-export.lock`。

标准输出为 JSON，失败的结构化错误输出到标准错误；请求失败退出码 1，参数错误退出码 2。安装后局域网操作不需要公网。现有设备使用明文 HTTP 和固定兼容认领字段，它们不构成安全认证，仅限可信局域网。

## 在 Rust 程序中复用

在调用方 `Cargo.toml` 中添加解压包内的 `core` 路径：

```toml
[dependencies]
syncap-core = { path = "/absolute/path/to/SynCapSDK-Rust-0.1.0/core" }
tokio = { version = "1", features = ["rt-multi-thread", "macros"] }
```

```rust,no_run
use syncap_core::Client;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let device = Client::new("http://192.168.1.12:8080")?;
    println!("{}", device.current_capture().await?.state);
    Ok(())
}
```

可运行的只读例子：`cargo run -p syncap-core --example inspect_device -- http://DEVICE_IP:8080`。取消客户端 future 不会停止设备录制。存储、开始、停止操作需由宿主串行管理。
