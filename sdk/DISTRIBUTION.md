# SynCap SDK 开发包

日期：2026-09-13。这是 SDK，不是新版 SynCap App APK。先阅读 [实测结果及未通过项](RELEASE_2026_09_13.md)，不能将构建成功等同于全平台真机可用。

| 文件 | 用途 |
| --- | --- |
| `SynCapSDK-Android-0.1.1.zip` | 本地 Maven、三模块 AAR/源码 JAR、独立示例、可编辑库工程、测试与文档；媒体模块修复预览首帧阻塞 |
| `SynCapSDK-Apple-0.1.0.zip` | Swift Package、控制 XCFramework、独立 CoreBluetooth、测试与文档 |
| `SynCapSDK-AppleMedia-0.1.0.zip` | 可选 iOS 预览模块与 MobileVLCKit 多架构运行库，体积不是最终 App 增量 |
| `SynCapSDK-Rust-0.1.0.zip` | 独立可编译核心、CLI、FFI 源码与测试 |
| `syncap-macos-arm64` | 本轮构建并验证的 Apple Silicon 终端程序 |
| `syncap-sdk-0.1.0.aar` | 裸 Android 控制库；需自行补齐传递依赖，优先用 Maven 包 |

控制和蓝牙不依赖视频/标定模块。仅需控制的项目不要引入媒体依赖。Android ZIP 中的示例为了演示三个模块会包含大型 VLC/OpenCV 依赖，不是精简生产 App 的体积基准。

基础控制、Android 蓝牙、Apple 与 Rust 仍为 0.1.0；本次仅 Android 媒体库升级至 0.1.1，示例引用新坐标。旧 Maven 版本保留，不覆盖已发布二进制。

解压 Android 包后按 `README.md` 添加 Maven 仓库，在 `example/` 中运行 `./gradlew :consumer:assembleDebug`。Apple 包以本地 Swift Package 接入；预览另选可选媒体包。Rust 包在根目录执行 `cargo test --locked --release`。

第一次构建需要工具链与依赖下载/预缓存；安装后的蓝牙配网、局域网采集与导出不依赖公网。Android/iOS 的权限、文件选择和后台生命周期由宿主处理。鸿蒙绑定、Apple 标定尚未实现；当前 Tina 的 Classic 配网不适用于 iPhone CoreBluetooth。

在本目录校验发行文件：

```sh
shasum -a 256 -c SHA256SUMS
./syncap-macos-arm64 --help
```

发行包不包含设备固件、备份、用户录像或网络密码；`package.*` 目录只是本机解包验证工作区，不必复制给接入方。
