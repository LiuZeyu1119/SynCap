# SDK 第一阶段验证记录 — 2026-09-10

本文件保留第一阶段的历史结果。后续已生成 Android AAR 与 Apple XCFramework；当前交付与验证范围分别见 [移动 SDK 接入](MOBILE_SDK.md) 和 [最新验证记录](MOBILE_VALIDATION.md)。下文“尚未交付”“targets 未安装”仅描述当时状态。

## 交付边界

已完成共享 Rust 核心、CLI、桌面 Tauri 控制/导出接入。Android/iOS 原生插件、BLE、预览、标定和设备服务源码本轮未修改；尚未交付 AAR 或 XCFramework。

测试环境为 macOS arm64、Rust 1.97.1。CLI 的依赖树不包含 Tauri 或 Capacitor。当前本机发布二进制为 `artifacts/2026-09-10-sdk/syncap-macos-arm64`（约 4.6 MiB）：

```text
SHA-256 49e7440ffafa0d5a7af295c858c613ce33e6a047d2da95644b4f60f7e3b5c310
```

## 自动化和构建

- `cargo test --offline --locked --manifest-path sdk/Cargo.toml`：55 项通过。
  - CLI 参数解析 9 项，进程级 HTTP 集成 6 项。
  - 导出与会话清单校验 23 项。
  - 采集成功语义 7 项，HTTP 控制 10 项。
- `cargo check --offline --locked --manifest-path sdk/Cargo.toml --all-targets`：本机编译通过，包含只读 Rust 接入示例。
- `cargo build --offline --locked --release --manifest-path sdk/Cargo.toml -p syncap-cli`：通过。
- SDK `cargo fmt --all --check`：通过。
- 桌面 `cargo test --offline --lib`：8 项通过；`cargo check --offline` 通过。
- App `npm run check:runtime`：28 个受保护运行时文件通过。
- `git diff --check`：通过。现有工作区中的其他修改未清理或提交。
- Clippy 未安装，本轮未执行。

错误回归覆盖：HTTP200 但存储不可采集、开始返回 failed/空 ID、停止返回其他 captureId、丢失开始响应不重试、空/坏 JSON、会话失败字段保留、重定向拒绝、零字节/坏哈希、错误 Range、导出计划漏文件、原文件冲突、跨进程导出互斥，以及取消后后台写入/提交仍保持目录锁。

## 全志真机

USB ADB 连接全志相机（公开记录省略设备序列号），服务 `syncap-tina 0.2.1`。显式建立本机 `18080 → 8080` 转发，无手机参与。客户端只通过 HTTP 调用设备，没有把 ADB 加入 SDK。

1. 读取真实状态、SD 卡空间和会话列表。
2. 已有完整会话 `ses_19700105_052332_cb45c2` 导出通过：2 个数据文件，原始 `session.json` 保留。
3. 原损坏会话 `ses_19700116_202251_01e182` 被设备以 HTTP409 / `session.metadata_corrupt` 拒绝；CLI 非零退出，未创建成功导出，也未删除原始数据。
4. 重启后第一次开始请求明确返回 `camera.unavailable`（仍在暖机）。检查到计数稳定、相机在线、capture=idle 后，再显式发送开始请求并成功；CLI 内部没有自动重试。
5. 新会话 `ses_19700105_073442_fc777e`（名称 `SDK-integration-6s`）完成开始、状态查询、停止和导出：
   - 开始：`state=recording`，`elapsedMs=0`。
   - 约等待 6 秒后查询：`elapsedMs=7228`，`sizeBytes=7938128`，6 个已落盘文件。
   - 停止：`state=completed`，设备报告会话 `durationMs=9283`，8 个数据文件，总计 `10960258` 字节。
   - SDK 导出：8 个数据文件全部校验，另有原始 `session.json`，共 `10962256` 字节。
   - MCAP 独立 CRC/读取检查：4 个 MCAP，左目 240 条、右目 241 条、IMU 6510 条、音频 128 条。这只验证数据可读取，不代表左右帧数严格相等或已验证同步精度；会话时长也是设备报告值，不是逐视频帧重算结果。
6. 对最终发布版 CLI 做真机续传：预放首个 MCAP 的 65536 字节前缀，经只转发 HTTP 的本地观察代理确认实际请求 `Range: bytes=65536-`；设备返回剩余内容后，8 个文件大小和 SHA-256 均与原始清单一致。
7. 重复导出同一目录：成功复用已校验文件，观察到 0 次数据文件下载请求；原始 `session.json` 字节一致。

本机证据保存在被 gitignore 排除的 `artifacts/2026-09-10-sdk/`：`live-session/`、`resume-result.json`、`verify-resume.mjs` 和故障日志/临时片段备份。新增短时测试会话留在设备 SD 卡；没有删除用户会话，也没有格式化存储。

## 尚未解决或验证的事项

- 全志闭源 `qgapp` 仍有运行一段时间后卡住的问题。本轮在一次重启后约 4 分钟再次出现不完整片段并被服务隔离，SDK 正确返回 `capture.recovery_required`。故障证据已备份后再重启做短时测试；**短时链路通过不等于固件长时间稳定性已修复**。本轮结束前只读示例返回 idle，未留下主动采集任务。
- 当前设备标识和会话中的 1970 日期来自设备未校时，不把它与手机/电脑当前日期相减；SDK 保留设备时间域。
- 没有连接 RoboBaton 实机；其 HTTP 合同依据当前设备服务源码和本地响应测试核对。
- Windows Rust target 已安装，但交叉编译在 C 依赖处缺 Windows CRT `assert.h`。尝试现有 cargo-xwin 时发现仍需下载 CRT，已停止；Windows 完整编译未通过验收。
- Android/iOS Rust targets 未安装，未进行对应编译、签名、打包或真机 SDK 接入。移动端绑定是下一阶段，不能将本轮称为三端 SDK 已全部完成。
- 没有做桌面安装包/界面启动验收；桌面变化仅有控制/导出桥接及测试，UI 代码没有改变。
