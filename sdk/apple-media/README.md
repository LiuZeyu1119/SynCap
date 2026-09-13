# SynCap iOS 预览 SDK

独立 Swift Package，product 为 `SynCapMedia`，iOS 15+。从原 App 移出的 UIKit + MobileVLCKit 3.7.3 预览页面，App 现在引用同一份源码，不依赖 Capacitor、React 或 Rust。

```swift
import SynCapMedia

// 在主线程，URL 和实际相机位置标签来自设备 manifest。
let preview = try SynCapPreviewViewController(urls: cameraURLs, labels: cameraLabels)
present(preview, animated: true)
```

支持 1–8 路 RTSP / TCP-HEVC，内置关闭与安全区域布局。当前 Tina raw HEVC 以 29.4118 fps 解释时间基准。宿主配置局域网权限 `NSLocalNetworkUsageDescription`；仅在用户请求时打开相机预览。

本包提供预览，不提供录像编码或标定算法。录像/导出使用设备控制 SDK，不能从此预览页面采集标定数据。没有 iPhone 真机运行结果；播放器 playing 状态也不等于物理同步/持续出帧验证。

## 依赖与体积

`Vendor/MobileVLCKit.xcframework` 是可选媒体运行库，与基础控制包分开发行。包含设备、模拟器和调试符号的发行框架远大于最终应用单一架构的体积。不使用预览的应用不需要此包。

从项目源码恢复运行库，可在 `apps/android/` 执行 `node scripts/prepare-ios-vlckit.mjs`；该脚本只在缺失时下载项目原先固定版本的 VideoLAN 发行物。发布 ZIP 已包含运行库，不需要原 App 源码或该脚本才能接入。

保留 `Vendor/MobileVLCKit-COPYING.txt`。上游发行源：VideoLAN MobileVLCKit 3.7.3，`https://artifacts.videolan.org/VLCKit/MobileVLCKit/MobileVLCKit-3.7.3-319ed2c0-79128878.tar.xz`。宿主发行需要遵守对应 LGPL 许可及可重链接要求；不要把预编译 SDK 当作免除依赖许可义务。
