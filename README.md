# SynCap

多相机头环采集 App、可复用 SDK 与设备服务。支持按设备能力发现相机、蓝牙配网、局域网预览、设备端采集、会话管理及带 SHA-256 校验的断点导出。

共享界面使用 React / TypeScript；Android、iOS 通过 Capacitor 承载，macOS、Windows 通过 Tauri 承载；控制与导出核心使用 Rust。Android 预览/标定与 Apple 预览为独立原生模块。

> 当前是开发预览版，不是四端全部功能或设备长期稳定性已验收的正式版。请先读[验证结果与已知限制](sdk/RELEASE_2026_09_13.md)。

## 下载与快速体验

在 [GitHub Releases](https://github.com/LiuZeyu1119/SynCap/releases) 下载：

- **SynCap-1.0.4-android-arm64.apk**：完整 App，Android 7.0 / API 24 以上，arm64。基于已真机验证的 1.0.3，仅移除未使用的私有实拍素材并更新版本号；开发调试签名，不是应用商店发行包，无需安装 SDK 即可体验。
- **SynCapSDKExample-android.apk**：独立 SDK 接入示例，不依赖安装原 App；用于开发者查询设备状态和打开原生预览。完整 SDK 示例包含媒体依赖，体积不代表基础控制库大小。
- **SynCapSDK-Android-0.1.1.zip**：本地 Maven、三模块源码、独立消费工程与测试。
- **SynCapSDK-Apple-0.1.0.zip**、**SynCapSDK-AppleMedia-0.1.0.zip**：Swift 控制/蓝牙与可选 iOS 媒体模块。
- **SynCapSDK-Rust-0.1.0.zip**、**syncap-macos-arm64**：独立 Rust 源码及 Apple Silicon CLI。

下载后用 Release 中的 `SHA256SUMS` 校验。设备需运行对应 SynCap 服务；这不是任意 USB 摄像头即插即用 App。相机可以连接普通 Wi-Fi，也可以连接手机自己发出的热点；手机与相机必须网络可达。蓝牙承担发现/配网，视频和采集控制走局域网。首次开发构建需下载依赖，部署后的局域网工作不要求公网。

开发版仅用于可信局域网，不要把设备控制端口直接暴露到公网。

## 从这里开始

| 你要做什么 | 入口 |
| --- | --- |
| 查看 App 源码 | [共享界面与各端宿主](apps/android/)、[共用媒体模块](sdk/android/MEDIA.md) |
| 查看架构与模块关系 | [架构图、App / SDK 依赖图与源码导航](docs/ARCHITECTURE.md) |
| 让 AI Agent 接手开发 | [AGENTS.md](AGENTS.md)：模块导航、约束、验证与硬件边界 |
| 从源码运行 App、SDK | [开发指南](docs/DEVELOPMENT.md) |
| Kotlin / Swift 接入 | [SDK 总览](sdk/README.md)、[移动 SDK](sdk/MOBILE_SDK.md) |
| Android 蓝牙配网 | [Bluetooth SDK](sdk/android/BLUETOOTH.md) |
| Android 预览 / 原始棋盘格标定 | [Media SDK](sdk/android/MEDIA.md) |
| Android 独立真机测试 | [测试命令与成功标准](sdk/android/TESTING.md) |
| Apple 接入 | [Swift SDK](sdk/apple/README.md)、[CoreBluetooth](sdk/apple/BLUETOOTH.md)、[iOS 媒体](sdk/apple-media/README.md) |
| 终端使用 | [CLI 指南](sdk/TERMINAL.md) |
| 实现一个新设备 | [设备协议](docs/syncap-device-protocol-v1.md)、[Tina 服务](device/README-Tina.md)、[RoboBaton 服务](device/README.md) |
| 打包或公开发布 | [发布指南](docs/RELEASING.md) |

## 源码结构

```text
apps/android/       共享响应式界面 + Android / iOS / Tauri 宿主（历史目录名）
sdk/core/           Rust HTTP 控制、会话、导出与数据校验
sdk/cli/            终端工具
sdk/ffi/            UniFFI 绑定与公共数据类型
sdk/android/        Kotlin 控制库、蓝牙、媒体、独立消费示例
sdk/apple/          Swift 控制 / GATT、示例与测试
sdk/apple-media/    可选 iOS MobileVLCKit 预览
device/             RoboBaton Python 服务、Tina C 服务及主机测试
docs/               协议、构建、发布与历史验证记录
scripts/            提交前公开发布检查
```

源码仓库不含录像、固件镜像、密钥、用户配置、编译缓存或大型第三方运行库。SDK 生成绑定和二进制由构建脚本恢复，或从 Release 使用预构建开发包；不要用 `git add -f` 塞入缓存。

## 当前边界

- Android：双目预览、关闭重开、采集开始/停止与校验导出已做真机验证；真实配网通道取决于设备 GATT / Classic RFCOMM 能力。
- Tina 原厂采集程序仍可能长时间运行后发生 IMU/I2C/编码停滞；录像起点有参考帧缺失的已知情况。短时通过不代表长期稳定或每帧无损。
- Tina 没有同步原始 NV12 标定快照接口，不能用预览截图标定；PTP 不支持，不能显示成零误差或已锁定。
- Apple 已有构建与部分主机测试，iPhone 真机全链路未验收；CoreBluetooth 不能代替当前 Tina 的 Classic 配网。Apple 标定、鸿蒙绑定尚未实现。
- macOS/Windows 共享宿主在仓库中，但本次发布不提供未经验证的安装包。查看各模块报告，不将构建成功等同于所有平台功能可用。

## 许可与第三方

本次公开源码尚未选定项目整体许可证，不在此擅自授予统一 MIT/Apache 许可。第三方代码和运行库保留各自声明：LibVLC/MobileVLCKit、OpenCV、MediaMTX 等不因本仓库公开而改变其许可。见 [Android 媒体声明](sdk/android/media/src/main/assets/licenses/THIRD_PARTY_NOTICES.txt)、[MobileVLCKit 许可](sdk/apple-media/Vendor/MobileVLCKit-COPYING.txt) 和 [MediaMTX 许可](device/LICENSE-MediaMTX)。原厂闭源 qgapp、厂商工具链与固件不随仓库再分发。
