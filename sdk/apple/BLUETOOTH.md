# Apple BLE 模块

Swift Package 的独立 product `SynCapBluetooth`，支持 iOS 15+ / macOS 12+ 的 CoreBluetooth GATT。没有 Capacitor 依赖，也无需引入 Rust 控制库。

**当前 Tina 只有可用 Classic RFCOMM 配网通道，不能用此模块在 iPhone 上配网。** Android Classic 测试不能替代 Apple GATT 验收；需要设备真正提供 SynCap GATT 服务及 iPhone 实机验证。本轮仅完成协议/取消测试与 iOS 编译。

宿主 Info.plist 需要清楚描述用途的 `NSBluetoothAlwaysUsageDescription`；系统负责权限和加密配对。macOS 沙盒应用还需蓝牙能力。操作对象必须保留到完成；单设备操作由宿主串行化。

```swift
import SynCapBluetooth

// 在宿主连接控制器中持有，不能只使用临时局部变量然后立即释放。
var operation: SynCapBleOperation?
operation = try SynCapBleOperation.scan(durationMs: 10000) { result in
    switch result {
    case .success(let response): print(response["devices"] ?? [])
    case .failure(let failure): print(failure.message)
    }
    operation = nil
}
operation?.start()
// 用户取消时 operation?.cancel()。
```

选择扫描返回的 **Apple UUID** 后发送命令，不能使用 Android MAC 地址：

```swift
let payload = try SynCapBleOperation.wifiConfiguration(
    ssid: selectedNetworkName, password: passwordEnteredByUser
)
operation = try SynCapBleOperation.command(address: selectedDeviceUUID, payload: payload) { result in
    // 成功回调只表示收到回复；检查 response["state"] == "connected" 和真实 IP。
    // outcomeUnknown 为 true 时先查状态，不重发可能已生效的命令。
    operation = nil
}
operation?.start()
```

Wi-Fi 列表用 `payload: ["op": "wifi.scan"]`；网络状态用 `device.status`。设备支持时还可发送 `storage.configure`、`storage.eject`、`capture.start`、`capture.stop`。兼容字段 123456 自动填入，不向用户索要认领码。

命令最多 4096 字节，BLE 片段按最大写入长度分割，最多 128 片。按写入确认顺序发送，轮询 working/connecting 状态；不重复发送已写入的变更命令。`cancel()` 中断客户端操作，不代表远端未执行。

SDK 不持久化密码、不恢复系统后台蓝牙任务。原移动 App 的 Bluetooth 桥接尚未迁移到此 API。控制 SDK 的异步 HTTP、采集、会话、断点导出与此模块独立，见主 README。
