# 移动 SDK 验证记录

这是独立 Kotlin/Swift SDK 的验证记录，不是原 SynCap App 或全志固件验收。

## 最新：9 月 13 日独立手机与相机测试

手机重新接入后，独立 Android SDK 已通过一次开始、设备计时/字节增长、一次停止、12 个数据文件校验导出和预置部分文件续传；独立蓝牙 SDK 的扫描/实际配网写入也通过。当前汇总见 [交付记录](RELEASE_2026_09_13.md)。此前电脑介入停止的失败记录保留在 [独立真机验收](HARDWARE_VALIDATION.md) 的历史部分，不代表全部最新结果。

CLI 并行失败已通过延迟请求用例复现并修复测试 socket 的 nonblocking 继承问题，5 轮集成测试通过；新增回归后 Rust Release workspace 70 项通过。以下保留此前的历史结果，不能将其中“未测 Android 手机”或“并行失败未修复”视为最新状态。

## 2026-09-13 较早的补充验证与交付（历史）

本次只更新文档和重新打包，SDK 仍为 0.1.0，未变更 SDK 接口或原生库。Android App 1.0.2 的蓝牙修复尚未提取到 SDK；既有移动 App 仍使用自己的桥接，不能用 App 真机测试代替 Kotlin/Swift SDK 真机测试。

本次重新运行：

- `cargo test --offline --locked --manifest-path sdk/Cargo.toml`：**69 项通过，0 失败**。
- `cargo fmt --manifest-path sdk/Cargo.toml --all --check`：通过。
- `swift test --package-path sdk/apple`：**macOS arm64 6 项通过，0 失败**，使用当前预编译 XCFramework，测试服务器仅在本机回环地址。
- macOS arm64 CLI 从同版本源码重新执行 release 构建成功。额外 `cargo test --offline --locked --release --manifest-path sdk/Cargo.toml -p syncap-cli` 首次并行运行中，2 项 HTTP 集成测试在本地模拟服务器 `cli/tests/commands.rs:97` 读取请求时出现 `WouldBlock`；追加 `-- --test-threads=1` 后 **15 项通过**。未修改测试代码，未证明并行失败已修复；这个结果不能记为 release 并行测试全部通过，也不足以判断是实际设备链路故障。
- 本次没有重跑 Android/iOS 模拟器，也没有在 Android 手机或 iPhone 上运行独立 SDK 示例；下方移动端结果保持 9 月 10 日的历史范围。

同日无线联调使用原 Android App 在 OPPO PLP110 上开始/停止两组采集，随后用 **macOS Rust CLI** 直连头环 Wi-Fi HTTP 地址导出，没有用 ADB HTTP 转发替代无线链路：

| 会话 | 设备报告时长 | 非零数据文件 | 数据字节数 | CLI 导出结果 |
| --- | ---: | ---: | ---: | --- |
| `ses_19700101_230459_7d3ccc` | 25,188 ms | 24 | 28,165,626 | 全部大小与 SHA-256 校验通过 |
| `ses_19700101_231143_0b0872` | 70,254 ms | 68 | 77,458,054 | 全部大小与 SHA-256 校验通过 |

表中不计 `session.json` 的大小与数量。第一组另外通过 App 导出到手机并拉回校验；该操作不是 AAR 调用。开始/停止由 App 完成，不能标记为“SDK 经 Wi-Fi 开始/停止已测”。

对第一组文件另做非对齐 Range 双段请求，均返回 HTTP 206，拼接后逐字节和哈希一致；在新的测试目录预放 **761,933 字节 `.part` 副本**，CLI 经真实 Wi-Fi 续传后校验全部 24 个数据文件通过。这是模拟部分下载的恢复，不是真实断网或手机后台恢复测试，没有截断原始文件。

独立检查发现两组视频起点仍有 GOP/截段边界问题（第一组每目 720 条消息仅解码 719 帧，第二组左目 2039/2040、右目 2041/2041）。MCAP 和哈希通过不代表每帧可解码，也不代表物理曝光同步。首次预览黑屏、并发预览掉帧和原厂进程长期停滞仍未全部解决。设备日历仍为 1970，录制时长必须使用 `elapsedMs`。

完整无线报告在仓库 `docs/tina-wireless-phone-validation-2026-09-13.md`，原始证据在 `artifacts/2026-09-13-wireless-e2e/phone-test/`。发行包不包含用户录制、网络密码或固件备份。当前 Tina 成功配网使用 Classic RFCOMM；BLE GATT、iOS 配网和鸿蒙绑定不能由上述结果推断为已支持。

## 2026-09-10 历史验证

以下保留原始结果与当时的设备状态；其中“当前 failed”等描述不是 9 月 13 日的实时状态。

## 实际运行

- Rust workspace：**69 项通过**（core、CLI、FFI），`cargo fmt --check` 通过；既有 App 的 28 个受保护 runtime 文件完整性检查通过。
- Kotlin/JVM：6 项通过，直接加载本机 Rust 动态库。Gradle 已将动态库声明为测试输入，避免 Rust 更新后误用旧的通过结果。
- Android：API 36 / arm64 模拟器，使用发布 Maven 坐标安装独立示例，Debug `NativeSdkTest` **7 项通过**。通过显式 `adb -s emulator-5560 ... am instrument` 执行，并核对 `OK (7 tests)`，不以 adb 的退出码代替测试结果。未验证 Android 手机真机。
- Android R8：经压缩混淆的 release 测试宿主 **7 项通过**。仅在 `-PtestReleaseSdk=true` 时使用调试签名并保留 Kotlin/协程共享运行库 ABI，防止 AndroidX 测试 APK 调用宿主已被裁剪的成员；这不是普通 production 全部裁剪配置的运行验收。SDK AAR 自身发布保留规则未因此扩大。
- Apple：macOS arm64 **6 项通过**；iOS 27 arm64 模拟器 **6 项通过**。最终 Android 条件编译文件锁改动后，5 个 Apple target 全部重建、macOS 6 项重跑通过；iOS 运行结果来自之前同接口版本。未验证 iPhone 或 Intel Mac 实机。

本地 HTTP 测试覆盖原生库加载、类型和设备时间精度、存储选择、开始/查询/停止、错误字段与不重试、取消、Range 续传、大小/SHA-256 和原始清单校验、已完成文件复用。Android 另测同目录并发拒绝、取消后锁释放和恢复续传。

首次 Android 运行测试发现 `std::fs::File::try_lock()` 在 Android 返回不支持；现按 Android 条件编译使用 `rustix::fs::flock`，保留跨进程非阻塞互斥和描述符关闭释放语义，未跳过锁校验。

## 已连接全志相机

通过相机已有 ADB TCP 转发进行局域网 HTTP 调用；SDK 内部不执行 ADB。

- Swift 示例成功读取实际采集状态和会话列表。
- Android 独立示例的 `LiveDeviceSmokeTest` 在 Debug 和 R8 测试宿主上分别 **1 项通过**，读取状态并导出已有会话 `ses_19700105_073442_fc777e`。
- 核对 **8 个非零采集文件**的大小和 SHA-256，以及设备原始 `session.json`；总计 **10,962,256 字节**（含清单），`verified=true`。
- 设备当前采集状态仍为 `failed`，SDK 如实返回；已有的完成会话可导出。这不是“刚完成了一次新采集”。本轮未重启设备、未开始/停止录制、未清理原始数据或保护状态。

`verified` 仅指文件传输和清单一致性，不代表重新验证 MCAP/视频内容、同步精度或标定质量。全志原厂采集进程长期运行卡住的问题不在本轮修复范围内。

## 编译与兼容边界

- 独立发行目录验证：不含 `core/`、`ffi/` 或 Android `:library` 源码模块的 Android 示例 `:consumer:assembleDebug --offline` 成功；Apple 独立 Swift Package 的 `swift test` **6 项通过**。两者均使用发行包内预编译库，不链接仓库原 App。
- 两个 ZIP 的压缩数据完整性检查通过；随包目录提供 `SHA256SUMS`。SDK AAR SHA-256：`f6b34842f674136fb0326d53151b515ac46b917e332b2fdc9550c339521a99fe`。
- 既有 Tauri 桌面桥接 `cargo check --offline --locked` 通过，未在此轮替换 Android/iOS App 桥接。
- Android：arm64-v8a、x86_64 AAR；source/Maven 两种依赖方式的 R8 release consumer 均编译成功。
- SDK 与 JNA 两种 ABI 的 ELF `LOAD` 对齐为 `0x4000`，示例 APK 通过 `zipalign -c -P 16`。未进行 16 KB 页设备运行测试。
- Apple：iOS arm64、模拟器 arm64/x86_64、macOS arm64/x86_64，共 3 个 XCFramework slice；Xcode 的 iOS 设备和模拟器目标均编译成功。
- 工具链：Rust 1.97.1 / UniFFI 0.32.1；Android NDK 29.0.14206865 / AGP 8.13.0 / Kotlin 2.2.21 / JDK 21；Xcode 27 beta / Swift 6.4。
- 尚未覆盖旧系统运行、移动端后台导出、系统权限体验、应用商店签名/分发，以及既有 App 全流程。BLE 配网、预览和标定尚未提取到这个 SDK。
