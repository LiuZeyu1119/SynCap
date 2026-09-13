# 开发与用法导航

先看 [README](../README.md) 的能力边界和 [AGENTS](../AGENTS.md) 的开发约定。命令从仓库根目录执行，除非明确 `cd`。

## 直接使用开发包

不需要编译 Rust 时，优先从 [Releases](https://github.com/LiuZeyu1119/SynCap/releases) 下载 SDK ZIP。Android 解压后有本地 `maven/`、独立 `example/`、可编辑 `source/`；按 [Android README](../sdk/android/README.md) 引入基础控制 0.1.0、可选蓝牙 0.1.0 / 媒体 0.1.1。Apple ZIP 含已生成 Swift 接口与 XCFramework。

SDK 独立示例 `sdk/android/consumer/` 的 `MainActivity.kt` 演示状态查询与预览，`CaptureWorkflow.kt` 演示开始/停止/导出，`BluetoothWorkflow.kt` 演示配网。示例 APK 不会在启动时自动录制；完整交互在 SynCap App。

## Android App 源码构建

需要 Node.js 22+、JDK 21 或可兼容的新版本、Android SDK Platform 36 与对应 Build Tools。配置自己的 `JAVA_HOME`、`ANDROID_HOME`，不要复制某台电脑的绝对路径。

```sh
npm ci --prefix apps/android
npm --prefix apps/android run android:sync
apps/android/android/gradlew -p apps/android/android :app:assembleDebug :app:testDebugUnitTest
```

输出 `apps/android/android/app/build/outputs/apk/debug/app-debug.apk`。此 App 直接引用 `sdk/android/media/`，不需要先编译 Rust 控制绑定。Android `settings.gradle` 中三个 `..` 的相对路径依赖当前 monorepo 结构，不能只复制 `apps/android/android/`。

使用自己的授权测试设备：

```sh
adb devices -l
adb -s YOUR_PHONE_SERIAL install -r apps/android/android/app/build/outputs/apk/debug/app-debug.apk
```

Debug 签名不是商店签名。已有安装签名不匹配时不要自动卸载/清数据；先导出用户数据并确定升级策略。

## Rust 核心与终端

```sh
cargo test --locked --manifest-path sdk/Cargo.toml
cargo build --locked --release --manifest-path sdk/Cargo.toml -p syncap-cli
sdk/target/release/syncap --help
sdk/target/release/syncap --endpoint http://DEVICE_IP:8080 status
```

这里的 `DEVICE_IP` 必须替换为设备实际地址。第一条查询只读；真实录制与导出命令见 [TERMINAL](../sdk/TERMINAL.md)。Windows 使用对应 `.exe`；本次未提供 Windows 预编译文件。

## 从源码生成 Android SDK

macOS / Linux：安装 Rust、JDK、Android SDK 36、NDK 29.0.14206865（或设置 `ANDROID_NDK_HOME` 指向兼容 NDK）。

```sh
rustup target add aarch64-linux-android x86_64-linux-android
bash sdk/scripts/build-android.sh
sdk/android/gradlew -p sdk/android :consumer:assembleDebug -PusePublishedSdk=true
```

脚本生成 Kotlin 接口、arm64/x86_64 Rust JNI、本地 Maven 的控制/蓝牙/媒体包与测试；不是把原 App 打成 SDK。输出位于 `sdk/android/build/maven/`。独立 SDK 示例 APK 在 `sdk/android/consumer/build/outputs/apk/debug/consumer-debug.apk`。只有依赖缓存齐全时才追加 `--offline`。

## Apple / iOS

需要 macOS、完整 Xcode、Rust Apple targets。控制/BLE 包：

```sh
rustup target add aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios aarch64-apple-darwin x86_64-apple-darwin
bash sdk/scripts/build-apple.sh
bash sdk/apple/test.sh
```

可选 iOS 媒体依赖恢复见 [apple-media/README](../sdk/apple-media/README.md)，再执行 `bash sdk/scripts/build-apple-media.sh`。App 的 `npm --prefix apps/android run ios:sync` 会准备 Capacitor 和媒体依赖；随后在 Xcode 打开 `apps/android/ios/App/App.xcodeproj`。真机签名由接入者配置，不提交私钥或 provisioning profile。

鸿蒙绑定尚未实现，没有可运行的鸿蒙安装步骤。不要把 Swift/Android 二进制标为鸿蒙 SDK。

## 共享界面与桌面

```sh
npm --prefix apps/android run dev
npm --prefix apps/android run build
npm --prefix apps/android run desktop:check
```

预览端口见 Vite 输出。桌面额外需要 Rust 和相应平台开发工具；安装包构建及平台限制见 [PLATFORMS](../apps/android/PLATFORMS.md)。MediaMTX 使用仓库脚本恢复，不提交下载的二进制。界面预览/mock 测试不是物理设备验收。

## 设备服务

- RoboBaton：Python 服务、Wi-Fi 与 BLE 见 [device/README](../device/README.md)。原厂 demo 仅以外部仓库引用。
- Tina：C 服务、RV32 交叉编译与固件前提见 [README-Tina](../device/README-Tina.md)。需厂商匹配的工具链、系统库和原厂采集程序；本仓库不提供固件镜像。
- 主机回归入口是 `device/test_*.py`；依赖随具体模块而定。构建/安装服务不会修复闭源 IMU 驱动，不能把部署到错误型号视为正常测试。

只读诊断与修改设备是不同操作。安装服务、Wi-Fi 写入、采集或重启前必须确认明确授权与真实目标。
