# SynCap Android SDK

独立 Kotlin / AAR 接口，调用 `syncap-core` 同一份 Rust 设备逻辑，不依赖现有 App、Capacitor、Tauri、React 或 ADB。这里不是重新打包旧 App，也尚未替换旧 App 的 Android 桥接实现。

基础控制 AAR 保持独立；可选 [蓝牙模块](BLUETOOTH.md) 与 [预览/标定模块](MEDIA.md) 已加入本地 Maven 仓库。原 App 的预览/标定已引用媒体 SDK。最新实测边界见发行包的 `RELEASE_2026_09_13.md`。

## 接入发布包

解压 `SynCapSDK-Android-0.1.1.zip` 后，包根目录包含 `maven/` 和独立 `example/` 工程。基础控制和蓝牙坐标仍为 0.1.0，媒体为 0.1.1。示例默认通过 Maven 坐标引用旁边的发布库，不需要 Rust、NDK 或原 SynCap App 的源码。在解压后的包根目录运行：

```sh
# 先设置本机 JAVA_HOME（JDK 17+）与 ANDROID_HOME（已安装 Android SDK platform 36）。
./example/gradlew -p example :consumer:assembleDebug
```

Windows 使用 `example\gradlew.bat -p example :consumer:assembleDebug`。输出位于 `example/consumer/build/outputs/apk/debug/consumer-debug.apk`。首次构建仍需要下载 Gradle、Android 构建插件和公开依赖；发布 ZIP 不包含这些工具和完整依赖缓存。已有缓存时可以追加 `--offline`；App 与设备在局域网工作不需要公网。

优先使用附带的本地 Maven 仓库，而不是只复制 AAR。把发布包的 `maven/` 放到自己的项目中，在 `settings.gradle.kts` 加入：

```kotlin
dependencyResolutionManagement {
    repositories {
        google()
        mavenCentral()
        maven { url = uri("maven") }
    }
}
```

应用模块 `build.gradle.kts`：

```kotlin
android {
    defaultConfig {
        minSdk = 24
        ndk { abiFilters += listOf("arm64-v8a", "x86_64") }
    }
}

dependencies {
    implementation("com.syncap:syncap-sdk:0.1.0")
    // 只有使用 Dispatchers.Main 的 Android UI 才需要这一项。
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.10.2")
}
```

发布元数据自动引入 Kotlin 标准库 2.2.21、coroutines-core 1.10.2 和 **JNA 5.18.1 的 Android AAR**。单独 `implementation(files("syncap-sdk.aar"))` 不会解析这些依赖，需要调用方自行补齐，尤其不能把 JNA 的普通桌面 JAR 当作 Android AAR 使用。接入应用的 Kotlin 编译器应为 2.2.21 或兼容该元数据的版本。

SDK 当前只发布 64 位 ARM 与 x86；限制应用的 ABI 可以避免 JNA 自带的其他架构让 APK 宣称支持实际不存在的 SynCap 原生库。不要在现有多架构 App 中用 `pickFirst` 掩盖版本或架构冲突。

## Kotlin 调用

所有网络方法是 `suspend` 函数。实例持有 Rust 原生资源，退出连接生命周期后调用 `close()`，或者在有限范围内使用 `use`；不要在业务调用尚未结束时关闭它。

```kotlin
import com.syncap.sdk.DeviceClient
import com.syncap.sdk.StorageTarget
import com.syncap.sdk.SdkException

suspend fun begin(endpoint: String, name: String) {
    DeviceClient(endpoint).use { device ->
        // 读 manifestJson()/storageJson() 显示实际能力和存储介质。
        // REMOVABLE 对 RoboBaton 是 U 盘，对当前全志设备是 SD 卡。
        device.configureStorageJson(StorageTarget.REMOVABLE)
        try {
            val capture = device.startCapture(name)
            println(capture.captureId)
            println(capture.elapsedMs) // 设备时长；可能暂时没有，不可换成手机时钟相减。
        } catch (failure: SdkException.Failure) {
            if (failure.outcomeUnknown) {
                val actual = device.currentCapture()
                println(actual.state) // 先查实际状态，不盲目重发开始。
            }
            throw failure
        }
    }
}
```

以上函数不会替调用方自动停止已经开始的录制。停止必须显式 `stopCapture(captureId)`；保存返回的 `sessionId` 后调用 `exportSession(sessionId, absoluteDirectory, cancellation)`。目录参数是最终会话目录，不会再追加会话 ID。原始响应扩展字段保留在类型化结果的 `rawJson` 中；设备纳秒时戳仍是字符串，避免转换损失精度。

错误为 `SdkException.Failure`，包含 `kind`、`detail`、`httpStatus`、`deviceCode` 和 `outcomeUnknown`。`detail` 是服务端/核心消息，不是面向用户的最终文案。由宿主 App 根据错误类型显示解释，不要把失败映射成在线或成功。

## 导出与取消

```kotlin
import com.syncap.sdk.Cancellation
import java.io.File

// 在协程中调用。调用方保存 token，并在明确取消时调用 token.cancel()。
val token = Cancellation()
try {
    val report = device.exportSession(
        sessionId,
        File(context.filesDir, "exports/$sessionId").absolutePath,
        token,
    )
    check(report.verified)
} finally {
    token.close()
}
```

每次导出创建新的 token；取消后保留 `.part`，用同一个会话和目录再次导出即可断点续传。取消导出不会停止设备录制，也不是已提交磁盘写操作完成的同步屏障。`verified` 只代表传输文件的大小和哈希通过，不代表视频内容、同步或标定质量已重新验证。`bytes` 包含会话清单，`files` 包含 `session.json`，`verifiedFiles` 是依据设备 SHA-256 校验的采集文件数量。

同一目录的导出持有文件锁，第二个并发导出会失败，不能靠删除 `.syncap-export.lock` 绕过。Android 的 Rust `std::fs::File::try_lock()` 尚不支持该平台，因此核心在 Android 上改用 `rustix::fs::flock` 调用内核的非阻塞独占锁，锁随文件描述符生命周期释放；这不是忽略锁错误或取消并发保护。其他平台继续使用标准库文件锁。

必须传应用拥有的真实文件系统目录。Android 的 `content://` / SAF 文档树不能直接当成本地路径传入；先导出到应用目录，再由宿主通过系统文件接口复制/分享。不要申请全盘管理权限来绕过这个边界。前台服务、后台长任务、目标盘容量预估、导出进度 UI 都仍由宿主应用处理。

## 网络与范围

- AAR 合并 `INTERNET` 权限，不申请蓝牙、定位或存储权限。
- 使用设备当前实际网络地址，不假设是路由器、有线网络或手机热点。SDK 不猜 IP、不自动切网或执行 ADB。
- 当前设备 HTTP 只适合可信局域网，`claimCode=123456` 是兼容字段，不是认证。原生 Rust HTTP 客户端不依赖 Android 的 Java 网络栈，不能把 `usesCleartextTraffic` 当作它的安全隔离；生产端需自己限制允许地址并按设备支持配置 HTTPS。
- 接入示例显式声明明文局域网用途。HTTPS 客户端不等于当前固件已支持 HTTPS。
- BLE 配网、蓝牙 SPP、视频解码、预览组件、标定计算与系统选择器不包含在这个 AAR 中。
- 存储选择和开始/停止应串行执行。现有设备协议没有原子“选存储并开始”事务，SDK不会自动重试状态改变请求。

## 从源码构建

本节路径均相对完整 Git 仓库根目录，不适用于仅含 AAR 与独立示例的发布 ZIP。

需要 JDK 17+、Rust、Android SDK platform 36、NDK 29.0.14206865，以及已安装的 Rust Android targets。使用项目自己的 `JAVA_HOME` / `ANDROID_HOME`，不修改系统配置。

```sh
rustup target add aarch64-linux-android x86_64-linux-android
# 在仓库根目录，环境中先设置 JAVA_HOME 和 ANDROID_HOME。
sdk/scripts/build-android.sh
```

NDK 可用 `ANDROID_NDK_HOME` 覆盖默认版本目录。脚本使用 Gradle 8.14.3、AGP 8.13.0、Kotlin 2.2.21，生成 Kotlin、交叉编译两种 ABI、发布 AAR 与 Maven 元数据、运行 host 测试，并编译 source/Maven 两种引用方式的压缩混淆 consumer。可追加 Gradle 的 `--offline` 或临时网络参数，工程不保存代理配置。

输出：

- `library/build/outputs/aar/library-release.aar`
- `build/maven/com/syncap/syncap-sdk/0.1.0/`：AAR、sources JAR、POM、Gradle metadata 与校验文件。
- `consumer/build/outputs/apk/release/consumer-release-unsigned.apk`：独立接入示例，不是正式 SynCap App，不可当作签名发布版直接安装。

`consumer/` 页面提供只读状态查询和原生预览；`CaptureWorkflow.kt` 演示明确触发的采集/停止/导出，`BluetoothWorkflow.kt` 演示扫描/配置。测试源码有显式 opt-in 的硬件链路，没有自动录制或硬编码的当前设备。完整示例引用三个模块；生产应用可只引用基础控制坐标。使用已发布坐标独立构建示例：

```sh
sdk/android/gradlew -p sdk/android :consumer:assembleDebug -PusePublishedSdk=true
```

宿主 Mac/Linux 的 Kotlin/JVM 测试直接加载同一 Rust 动态库，验证异步类型、错误、设备时钟精度、显式取消与断点导出：

```sh
sdk/android/gradlew -p sdk/android :host-tests:test
```

Android 运行时验证需明确连接模拟器或真机后运行，不属于以上 host 测试：

```sh
sdk/android/gradlew -p sdk/android :consumer:connectedDebugAndroidTest -PusePublishedSdk=true
# 运行经过 R8 的 release 测试宿主（使用调试签名与下述测试专用保留规则）：
sdk/android/gradlew -p sdk/android :consumer:connectedReleaseAndroidTest -PusePublishedSdk=true -PtestReleaseSdk=true
```

发布 ZIP 的独立示例对应命令为 `./example/gradlew -p example :consumer:connectedDebugAndroidTest`；R8 运行测试对应 `./example/gradlew -p example :consumer:connectedReleaseAndroidTest -PtestReleaseSdk=true`。

`testReleaseSdk=true` 额外启用 `consumer/test-host-rules.pro`，保留目标 APK 中 Kotlin 标准库与协程运行库的共享 ABI。AndroidX Test 是另一个单独压缩的 APK；若目标 App 删除只有测试使用的 `LazyKt`/`Lambda`，或改写 `Intrinsics` 参数，测试会在启动前崩溃。这些测试支持规则不是发布 AAR 的 consumer 规则，普通 release 构建也不启用。因此 R8 测试验证的是带明确测试支持配置的混淆宿主，不能宣称普通 release 的全部执行路径或任意生产 App 的完整压缩配置均已验证。`test-rules.pro` 只处理测试依赖的编译期注解引用。

`NativeSdkTest` 目前包含 **7 项 Android 运行时测试**：真实加载原生库与类型化时钟、存储/开始/停止调用链、结构化错误且不重试、输入验证、预取消、断点导出及文件校验、并发锁拒绝与取消后续传。它们通过设备内本地 HTTP fixture 测试，并不自动操作物理相机；当前 Android API 36 ARM64 模拟器 Debug 运行已通过 7/7，带上述测试支持规则的 R8 release 宿主也通过 7/7。另有 `LiveDeviceSmokeTest`，只有显式提供 `syncap.endpoint` 和 `syncap.sessionId` 才会运行，否则跳过；它只读取、导出指定的已有会话，不自动开始或停止采集。

本轮另在 OPPO 真机通过这 7 项，以及显式操作物理 Tina 的采集/导出和蓝牙配网；范围以随包 `RELEASE_2026_09_13.md` 为准。发行 ZIP 还包含可独立构建的 `source/` 库工程及蓝牙/媒体测试，不需要原 App；基础 JNI 二进制与生成绑定已备齐，重新生成核心则使用 Rust 包及仓库构建脚本。实际执行命令与风险见 [测试指南](TESTING.md)。

## 16 KB 页面

NDK 构建显式传入 16 KB linker page 参数；SDK 与 JNA 的两个 64 位 ELF 已检查 `LOAD` 对齐为 `0x4000`，示例 APK 通过 `zipalign -c -P 16`。这不替代在 16 KB 页面设备上的运行时验证。JNA 官方变更记录包含相关 Android 修复：[JNA CHANGES](https://github.com/java-native-access/jna/blob/master/CHANGES.md)；Android AAR 用法与反射保留规则见 [JNA FAQ](https://github.com/java-native-access/jna/blob/master/www/FrequentlyAskedQuestions.md)。
