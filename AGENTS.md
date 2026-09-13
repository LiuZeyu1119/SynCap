# AI Agent 导航与执行约定

先读 [README](README.md)、[最新验证记录](sdk/RELEASE_2026_09_13.md) 和当前任务所在模块。这个仓库是 SynCap，不是仅适配某一型号的 RoboBaton App。用户目标是可实际采集的数据链路，不是有按钮的演示界面。

## 工作流程

1. 编码前明确目标、输入输出、影响范围及可验证的完成标准；有阻塞歧义先问，非阻塞问题采用最小假设并说明。
2. 先沿相关调用链读代码、测试和下级 `AGENTS.md`，复现问题，再做最小必要修改；不顺手重构。
3. 保留用户已有改动与设备数据。不要 reset/checkout 覆盖工作区，不未经要求提交、推送或发布。
4. 为新增分支覆盖正常与错误情况，运行相关测试与构建。报告实际结果、未验证平台和剩余风险，不把 mock、编译、模拟器或 ADB 转发说成真机无线通过。
5. 搜索优先 `rg`。日志、测试录像和临时文件放在被忽略的 `artifacts/`；不得提交密码、令牌、设备标识、证书私钥、固件备份和用户录像。

## 按任务定位

| 任务 | 先读 / 修改入口 | 验证 |
| --- | --- | --- |
| UI、配网页、计时 | `apps/android/AGENTS.md`、`src/Prototype.tsx`、`src/prototype.css`、`src/captureTimer.ts` | runtime 校验、对应 Node / Playwright 测试、App 构建 |
| Android 宿主 / HTTP 桥接 | `apps/android/android/app/src/main/java/com/syncap/studio/SynCapDevicePlugin.java` | App JVM 测试；网络与生命周期修改还要真机 |
| Android 预览 / 标定 | `sdk/android/MEDIA.md`、`sdk/android/media/src/main/java/com/syncap/studio/` | SDK instrumentation；真实首帧、连续帧增长、关闭释放 |
| 蓝牙发现 / 配网 | `sdk/android/BLUETOOTH.md`、`sdk/android/bluetooth/`、`device/tina_ble.c` 或 `device/ble_provisioning.py` | 协议测试；扫描不等于配置成功；硬件写入必须显式允许 |
| 跨端采集 / 导出 | `sdk/core/src/client.rs`、`export.rs`、`export_manifest.rs`、`sdk/ffi/` | Rust workspace 测试及相应平台绑定测试 |
| Apple | `sdk/apple/README.md`、`sdk/apple/BLUETOOTH.md`、`sdk/apple-media/README.md` | Swift 测试 / Xcode 构建；不能声称已测 iPhone |
| 桌面 | `apps/android/PLATFORMS.md`、`apps/android/src-tauri/src/lib.rs` | Cargo 检查与目标平台构建 |
| Tina 服务 | `device/README-Tina.md`、`device/tina_service.c`、`device/test_tina_*.py` | 主机回归；经授权后验证设备实际开始、增长、停止和导出 |
| RoboBaton 服务 | `device/README.md`、`device/syncap_service.py`、`legacy_adapter.py` | `device/test_*.py` 对应测试；勿把 Tina 结果移植为四摄验收 |
| 新设备适配 | `docs/syncap-device-protocol-v1.md`、manifest 与 SDK models | 契约 / 能力分支 / 错误语义测试 |
| 发布 | `docs/RELEASING.md`、`scripts/check-publication.mjs` | 暂存区检查、干净检出构建、Release 校验和 |

上表的 `src/` 相对 `apps/android/`。构建命令和依赖恢复见 [DEVELOPMENT](docs/DEVELOPMENT.md)。不要手改生成的 Kotlin / Swift UniFFI 文件；修改 Rust FFI 后用脚本重新生成。App 和 SDK 共用媒体源码，不复制出第二份播放器/标定引擎。

## 不可破坏的产品与数据约束

- 相机数量、存储、网络与协议按 manifest 能力展示。RoboBaton 已确认排列为 cam3 左外侧、cam1 左内侧、cam2 右内侧、cam0 右外侧；Tina 为左目/右目。不能泛化成前后左右或硬编码所有设备四路。
- 已系统配对不再要求认领码；协议兼容值不是用户输入步骤。Wi-Fi 与手机自身热点都应可配，不显示伪造网络或固定连接标签。
- 在线、实时、同步、采集成功必须有对应证据。不能以端口连通替代真实出帧，不能把未知误差变成 0，不能把 PTP unsupported 写成 faulty / locked。
- 计时优先设备 `elapsedMs` / 同域单调时间，不把设备 1970 年墙钟与手机当前时间相减。
- 采集、标定与预览来源分开：不能拿压缩预览帧代替原始同步标定快照；不得静默降低采集质量。方向为旋转，预览/标定/录制保持一致。
- 开始/停止/配网等有副作用请求不能无条件自动重发。结果未知先查状态；零字节、清单损坏、Range/哈希不符必须保留失败。
- 不关闭文件完整性校验、不删除故障片段、不清空 quarantine 来假装恢复。

## 设备操作边界

默认只做主机测试。连接物理设备后先只读确认型号、地址、存储和采集状态；不得猜 IP、选错手机或启动第二个 qgapp。Tina 裸流可能只支持单客户端，不能用额外探测连接抢走 App 预览。

用户没有明确授权时，不配网、不录制、不重启、不安装服务。**当前设备明确要求不刷固件**；即便已提供镜像，也不能烧录。部署前备份确切目标并保留校验和；故障恢复前先保存日志与片段。现有固件 IMU/I2C 卡住尚未根治，不用降低完整性要求或反复重启冒充修复。

## 推荐最小验证

```sh
node scripts/check-publication.mjs
cargo test --locked --manifest-path sdk/Cargo.toml
npm --prefix apps/android run check:runtime
node --experimental-strip-types --test apps/android/tests/device-discovery.test.ts apps/android/tests/capture-timer.test.ts
git diff --check
```

根据修改范围追加构建与模块测试。硬件 opt-in 参数、方法及通过标准见 `sdk/android/TESTING.md`；不能把测试宿主自身 wake lock 宣称为生产后台服务。公开报告只保留匿名化必要信息，原始证据留在本机，不能发布用户网络凭据。
