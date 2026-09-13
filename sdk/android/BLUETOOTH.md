# Android 蓝牙 SDK

独立坐标：`com.syncap:syncap-bluetooth-android:0.1.0`。不依赖原 App、Capacitor、Rust/JNA 或播放器，可与控制 AAR 分开使用。

## 权限与生命周期

Android 12+ 由宿主通过系统对话框请求 `BLUETOOTH_SCAN`、`BLUETOOTH_CONNECT`；旧版本扫描需要定位权限/系统定位设置。库只声明权限，不自行授权。配对必须经过 Android 系统确认；`requestPairing(address)` 只能由明确的用户操作触发。不要自动删除已有配对或注入 PIN。

一个 `ProvisioningClient` 对应一个连接页面/前台服务生命周期，结束时 `close()`。异步操作取消会关闭本次 GATT/socket；不会停止已经在设备端开始的采集，也不等于配置未生效。客户端内部串行操作，但多个实例/其他手机仍可能同时访问设备，宿主应避免这种用法。

```kotlin
import com.syncap.sdk.bluetooth.ProvisioningClient
import org.json.JSONObject

// context 为宿主提供，先取得所需系统权限。
val bluetooth = ProvisioningClient(context)
val devices = bluetooth.discover() // 约 10 秒；包含已配对、BLE 和 Classic 发现结果。
val selected = devices[userSelectedIndex] // 用户选择自己的设备，不默认操作第一台。
if (!selected.bonded) {
    bluetooth.requestPairing(selected.address)
    // 等待系统完成配对，再刷新设备列表。
} else {
    val scan = JSONObject(bluetooth.scanWifi(selected.address).rawJson)
    check(scan.getString("state") == "completed")
    val networks = scan.getJSONArray("networks")
    val chosen = networks.getJSONObject(userSelectedNetworkIndex)
    val configured = JSONObject(bluetooth.configureWifi(
        selected.address, chosen.getString("ssid"), passwordEnteredByUser,
        chosen.optString("security", "wpa2-psk"),
    ).rawJson)
    check(configured.getString("state") == "connected")
    val actualAddress = configured.getString("ipAddress")
    // 手机必须能够访问该网络；手机自己的热点也是有效场景。
    // 用实际地址及设备 API 端口建立 HTTP DeviceClient，再读取能力/相机/采集状态。
}
```

片段假定在协程中运行，交互选择、密码输入和系统配对等待由宿主完成。`rawJson` 可能包含网络名称，不应写入公共日志；不要持久化或记录 Wi-Fi 密码。

## 通道与错误

- `AUTO`：已配对的 Classic/SPP 设备直接使用加密 RFCOMM。其他设备先 GATT，只允许在**尚未写入命令**时回退 RFCOMM；写入后不会换通道重发。
- `BluetoothReply.transport` 返回实际 `GATT` / `RFCOMM`。可显式指定通道用于诊断，不能把 Classic 配网标为 BLE 验收。
- `ProvisioningException.outcomeUnknown=true`：写入已开始但未取得确定结果；先查询 `deviceStatus` / HTTP 采集状态，不盲目重发配置或开始。
- `BluetoothReply.state` 是设备返回的操作状态，不是“相机全部在线”。设备返回 `failed` 时仍保留原始 JSON，调用方必须检查，不能仅以函数返回视为配网成功。
- API 包含 `pairedDevices`、`discover`、`requestPairing`、`scanWifi`、`configureWifi`、`deviceStatus`、`configureStorage`、`ejectStorage`、`startCapture`、`stopCapture`。后四类离线功能需要设备支持；`internal` / `usb` 为协议存储槽位。
- **当前 Tina 蓝牙 helper 仅支持 Wi-Fi 扫描/配置与设备网络状态**。采集、存储、会话导出须在联网后使用控制 SDK，不假装支持蓝牙离线采集。RoboBaton helper 有相应离线命令，但本轮未接入该硬件验收。
- `123456` 只在协议内部作为兼容字段，不要求用户输入，也不构成安全认证。

RFCOMM 使用 4 字节大端长度 + UTF-8 JSON，限制 4096 字节；BLE 使用 16 字节 SC 片段头，最大 128 片，按协商 MTU 发送并确认。截断、无效 UTF-8、超长与尾随数据会拒绝。

本轮通过 Android 独立库测试，并在 OPPO PLP110 + 当前 Tina 经 RFCOMM 扫到 5 个 Wi-Fi，完成实际配网写入。GATT 真机不在此结论内。前台运行测试使用测试宿主的有时限唤醒锁；生产后台任务需要宿主前台服务/系统生命周期管理，SDK 不隐式持锁或承诺进程被挂起后仍然执行。
