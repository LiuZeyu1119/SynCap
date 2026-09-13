# SynCap Device Protocol 1.0（草案）

状态：设计草案 0.1  
线缆协议标识：`syncap.device/1.0`  
适用范围：SynCap Studio 与头环、同步相机组、IMU 采集设备之间的发现、配网、控制、预览、采集、诊断、标定和数据导出。

## 1. 设计目标

1. **设备能力优先**：客户端不能假设设备一定有四路相机、固定端口、IMU 或 Wi-Fi。界面和操作必须由设备清单动态生成。
2. **传输与业务分离**：BLE、HTTPS 和 WebSocket 复用相同的请求、响应和事件语义；视频和大文件使用独立数据通道。
3. **向后兼容**：主版本只在发生破坏性修改时升级；次版本只增加字段、方法或枚举值。
4. **稳定标识**：相机、传感器、同步组和数据文件都使用稳定字符串 ID，不使用数组下标表达身份。
5. **可靠采集**：正式采集默认在设备端完成，手机和电脑只负责控制与监看，避免无线网络抖动造成原始数据丢失。
6. **安全配网**：Wi-Fi 密码只允许通过加密 BLE 会话发送；生产设备的局域网控制必须使用 TLS 和设备身份校验。

## 2. 分层架构

| 层 | 推荐技术 | 用途 |
| --- | --- | --- |
| 引导层 | BLE GATT | 发现、认领设备、读取身份、扫描和配置 Wi-Fi、获取局域网地址 |
| 发现层 | BLE 广播、mDNS/DNS-SD | 在近场和同网段查找设备 |
| 控制层 | HTTPS RPC | 查询能力、修改参数、开始/停止采集、管理标定和会话 |
| 事件层 | WebSocket | 设备状态、同步误差、采集进度、告警和操作完成通知 |
| 预览层 | RTSP / WebRTC / SRT | 实时低延迟视频；具体传输方式由设备清单声明 |
| 数据层 | HTTPS Range Download | 会话清单、分段视频、IMU、日志、标定文件和校验值 |

所有业务控制都使用同一套消息封装。BLE 默认编码为 CBOR，HTTPS 和 WebSocket 默认编码为 JSON。CBOR 与 JSON 使用相同字段名和语义。

## 3. 兼容与扩展规则

### 3.1 版本协商

- 设备声明 `supported_versions`，客户端选择双方支持的最高版本。
- `1.0` 到 `1.1` 只能增加可选字段、方法、事件或枚举值。
- `1.x` 客户端必须忽略不认识的对象字段。
- 客户端遇到未知枚举值时，应显示“未知状态”，不能崩溃或擅自映射为错误。
- 业务能力独立版本化，例如 `camera.preview@1.0`、`sync.metrics@1.1`。

### 3.2 命名空间

- 标准方法：`device.getManifest`、`wifi.scan`、`capture.start`。
- 标准事件：`device.state`、`sync.sample`、`capture.state`。
- 厂商扩展：`vendor.<反向域名>.<名称>`，例如 `vendor.com.example.laser.setPower`。
- 扩展字段统一放入 `ext`；客户端必须原样忽略未知扩展。

### 3.3 数据类型约定

- JSON 字符串：UTF-8。
- 时间戳：设备单调时钟的纳秒值，使用十进制字符串，例如 `"3912451527000"`，避免 JavaScript 64 位整数精度损失。
- UTC 时间：RFC 3339，例如 `"2026-08-06T10:12:41.527+08:00"`；设备未校时则为 `null`。
- 物理量：使用 SI 单位，并在属性描述中显式声明 `unit`。
- 位姿：平移单位为米；四元数顺序固定为 `x, y, z, w`。
- 二进制摘要：小写十六进制 SHA-256。

## 4. 通用消息封装

### 4.1 请求

```json
{
  "v": "1.0",
  "type": "request",
  "id": "01K1A9Q8J6Z9M2C8XK7H4V0Y3P",
  "method": "device.getManifest",
  "params": {},
  "meta": {
    "client": "syncap-mobile/0.1.0",
    "locale": "zh-CN",
    "idempotency_key": "5f061777-1ed0-4f8f-8d21-5027d7ad32fb"
  },
  "ext": {}
}
```

`idempotency_key` 对开始采集、停止采集、修改网络等操作是必需的。设备在可配置时间窗内重复收到相同键时，必须返回第一次操作的结果，不能重复执行。

### 4.2 成功响应

```json
{
  "v": "1.0",
  "type": "response",
  "id": "01K1A9Q8J6Z9M2C8XK7H4V0Y3P",
  "result": {
    "selected_version": "1.0"
  },
  "ext": {}
}
```

### 4.3 错误响应

```json
{
  "v": "1.0",
  "type": "response",
  "id": "01K1A9Q8J6Z9M2C8XK7H4V0Y3P",
  "error": {
    "code": "property.out_of_range",
    "message": "video.fps 超出设备支持范围",
    "retryable": false,
    "details": {
      "path": "camera/cam0/video/fps",
      "received": 60,
      "allowed": [25, 30, 40, 50]
    }
  },
  "ext": {}
}
```

建议的通用错误码：

- `protocol.incompatible`
- `request.invalid`
- `authentication.required`
- `permission.denied`
- `capability.unsupported`
- `property.out_of_range`
- `property.revision_conflict`
- `device.busy`
- `storage.insufficient`
- `operation.timeout`
- `internal.error`

### 4.4 事件

```json
{
  "v": "1.0",
  "type": "event",
  "topic": "capture.state",
  "seq": "1842",
  "device_time_ns": "3912451527000",
  "data": {
    "capture_id": "cap_20260806_101241_01",
    "state": "recording",
    "duration_ns": "8123000000"
  },
  "ext": {}
}
```

`seq` 在一次设备启动周期内严格递增。客户端发现序号跳变时，应重新查询当前状态。

## 5. BLE 引导与离线控制协议

### 5.1 GATT 服务

SynCap 配网服务使用以下冻结 UUID：

| 名称 | UUID | 属性 |
| --- | --- | --- |
| SynCap Provisioning and Control Service | `8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1` | Primary Service |
| Encrypted Command | `8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1` | Encrypted Write With Response |
| Encrypted Status | `8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1` | Encrypted Read |

UUID 与首版 Wi-Fi 配网服务保持不变，旧客户端仍可发送不带 `op` 的 Wi-Fi JSON；新客户端使用 `op` 区分配网和离线控制。

设备通过 128 位 Service Data 广播以下最小信息：

```text
byte 0      protocol_major      0x01
byte 1      flags               bit0=可认领 bit1=已配网 bit2=正在采集
byte 2..7   short_device_id     设备 ID 哈希的前 6 字节
byte 8      product_class       0x01=同步相机组，其他值保留
```

设备完整型号、厂商和功能必须通过 `device.getManifest` 获取，不能从广播中的 `product_class` 推断。

### 5.2 BLE 分片头

GATT 特征值中的每个分片使用 16 字节大端头：

```text
offset  size  field
0       2     magic = 0x53 0x43  // "SC"
2       1     header_version = 1
3       1     flags              // bit0: 0=CBOR, 1=JSON
4       4     transport_message_id
8       2     fragment_index     // 从 0 开始
10      2     fragment_count
12      2     payload_length
14      2     reserved = 0
```

当 ATT MTU 为 247 时，每个分片最多承载 `247 - 3 - 16 = 228` 字节业务数据。客户端写入使用 Write With Response，并轮询加密状态特征获得结果。接收方必须先按 `transport_message_id` 完成重组，再解析 CBOR/JSON 消息。

当前命令特征支持最多 128 个分片和 4096 字节重组后载荷。Android 客户端必须根据协商后的 ATT MTU 动态分片；即使协商停留在 MTU 23，也必须保持可用。为兼容早期调试工具，设备也可以接受单次写入的完整 JSON，但正式客户端必须使用分片头。

### 5.3 设备认领与安全

推荐首次绑定流程：

1. 设备进入限时可认领状态。
2. App 使用 LE Secure Connections 建立加密连接。
3. App 调用 `security.claim.begin`。
4. 根据设备声明的方式完成 `qr_secret`、`physical_confirm` 或 `numeric_compare`。
5. 设备返回拥有者令牌、局域网 TLS 证书指纹和令牌刷新信息。

没有屏幕和确认按键的头环，推荐在机身标签上提供一次性 QR 认领密钥。开发样机可声明 `security_mode: "development"`，但不能作为量产默认配置。

### 5.4 Wi-Fi 扫描示例

请求：

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_wifi_scan_01",
  "method": "wifi.scan",
  "params": {
    "bands": ["2.4ghz", "5ghz"],
    "include_hidden": false
  },
  "meta": {"client": "syncap-mobile/0.1.0"},
  "ext": {}
}
```

响应：

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_wifi_scan_01",
  "result": {
    "networks": [
      {
        "ssid": "Lab-5G",
        "bssid": "12:34:56:78:9a:bc",
        "band": "5ghz",
        "channel": 149,
        "rssi_dbm": -48,
        "security": "wpa2-psk"
      }
    ]
  },
  "ext": {}
}
```

### 5.5 Wi-Fi 配置示例

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_wifi_cfg_01",
  "method": "wifi.configure",
  "params": {
    "ssid": "Lab-5G",
    "security": "wpa2-psk",
    "credential": {"passphrase": "<仅通过加密 BLE 传输>"},
    "ip_mode": "dhcp",
    "country_code": "CN",
    "connect_now": true
  },
  "meta": {
    "client": "syncap-mobile/0.1.0",
    "idempotency_key": "b29a3d56-cfdd-4dc8-a0f8-70e597872ed8"
  },
  "ext": {}
}
```

设备先返回 `operation_id`，再通过 BLE 事件报告 `associating`、`obtaining_ip`、`connected` 或 `failed`。成功事件示例：

```json
{
  "v": "1.0",
  "type": "event",
  "topic": "network.state",
  "seq": "31",
  "device_time_ns": "91221000000",
  "data": {
    "operation_id": "op_wifi_01",
    "interface": "wlan0",
    "state": "connected",
    "ssid": "Lab-5G",
    "addresses": ["192.168.1.12"],
    "mdns_name": "syncap-a4df62.local",
    "rpc_url": "https://syncap-a4df62.local:8443/syncap/v1/rpc"
  },
  "ext": {}
}
```

如果设备清单中没有 `network.wifi` 能力，App 不显示 Wi-Fi 配置入口，仍可使用以太网或 USB 网络。

### 5.6 USB 离线采集

没有可用局域网时，App 通过同一加密 GATT 命令特征控制采集，视频和 IMU 数据不经过 BLE。当前设备适配层接受以下 JSON 操作：

| `op` | 参数 | 结果 |
| --- | --- | --- |
| `storage.configure` | `target: "usb"` | 检查并挂载卷标为 `SYNCAP` 的可移动 exFAT 卷 |
| `device.status` | 无 | 返回 U 盘容量、当前采集和最近会话摘要 |
| `capture.start` | `name` | 启动四路 H.264 原码流和 IMU 采集 |
| `capture.stop` | `captureId` | 停止、生成清单与 SHA-256，并同步文件系统 |
| `storage.eject` | 无 | 仅在未采集时同步并卸载 U 盘 |

当前 RoboBaton-4P 兼容层仍要求每条命令携带 `claimCode`，但它是 App 与设备服务内部使用的固定兼容字段 `123456`，不代表所有权，也不得要求用户查看标签或手动输入。系统蓝牙已配对即视为已完成设备选择。示例：

```json
{
  "op": "storage.configure",
  "target": "usb",
  "claimCode": "123456"
}
```

启用后，采集开始请求示例：

```json
{
  "op": "capture.start",
  "name": "offline_session_001",
  "claimCode": "123456"
}
```

U 盘目录固定为 `SynCap/sessions/<sessionId>/`。每个完成会话至少包含 `cam0.mp4` 至 `cam3.mp4`、`imu.log`、`diagnostics.json` 和 `session.json`；`session.json` 记录卷标、卷 UUID、各文件大小和 SHA-256。选择 USB 后如果卷缺失、不是 exFAT、卷标不符或剩余空间低于 512 MiB，设备必须拒绝开始采集，不能静默回退到内部存储。采集中必须拒绝安全弹出；用户调用 `storage.eject` 成功后才能拔盘。

## 6. 局域网发现与连接

### 6.1 mDNS

服务类型：`_syncap._tcp.local`

建议 TXT 记录：

```text
id=dev_rb4p_a4df62
pv=1.0
model=RoboBaton-4P
port=8443
tls=1
state=idle
```

mDNS 只暴露发现所需的非敏感信息。证书指纹从已加密的 BLE 认领流程获得，App 首次通过局域网连接时必须校验证书指纹。

### 6.2 HTTPS RPC

```http
POST /syncap/v1/rpc HTTP/1.1
Host: syncap-a4df62.local:8443
Authorization: Bearer <access-token>
Content-Type: application/vnd.syncap+json
X-SynCap-Version: 1.0
```

响应体仍使用第 4 节的通用消息封装。

### 6.3 WebSocket 事件

地址：`wss://<device>/syncap/v1/events`  
子协议：`syncap.json.v1` 或 `syncap.cbor.v1`

订阅示例：

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_sub_01",
  "method": "event.subscribe",
  "params": {
    "topics": [
      "device.state",
      "network.state",
      "capture.state",
      "sync.sample",
      "storage.state",
      "alert.raised"
    ],
    "rates_hz": {"sync.sample": 10}
  },
  "meta": {"client": "syncap-mobile/0.1.0"},
  "ext": {}
}
```

## 7. 能力清单

客户端连接设备后的第一个业务调用必须是 `device.getManifest`。

### 7.1 示例

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_manifest_01",
  "result": {
    "schema": "syncap.device-manifest/1.0",
    "protocol": {
      "supported_versions": ["1.0"],
      "selected_version": "1.0"
    },
    "device": {
      "id": "dev_rb4p_a4df62",
      "display_name": "头环 001",
      "vendor": "Hessian Matrix",
      "model": "RoboBaton-4P",
      "serial": "RB4P-2026-0001",
      "hardware_revision": "1.0",
      "firmware_version": "1.0.0"
    },
    "security": {
      "mode": "claimed",
      "tls_required": true,
      "certificate_sha256": "8e3c4c4c2b94d60f4a91e70b877bb7b2d0d640dbb6215e5f12b679f8cb550001"
    },
    "capabilities": [
      {"id": "network.ethernet", "version": "1.0"},
      {"id": "network.wifi", "version": "1.0"},
      {"id": "camera.preview", "version": "1.0"},
      {"id": "capture.device", "version": "1.0"},
      {"id": "sensor.imu", "version": "1.0"},
      {"id": "sync.metrics", "version": "1.0"},
      {"id": "clock.sync", "version": "1.0"},
      {"id": "exposure.phase", "version": "1.0"},
      {"id": "calibration.camera", "version": "1.0"},
      {"id": "calibration.camera_imu", "version": "1.0"},
      {"id": "session.export", "version": "1.0"}
    ],
    "components": [
      {"id": "cam0", "kind": "camera", "label": "前", "group_id": "head_ring"},
      {"id": "cam1", "kind": "camera", "label": "右", "group_id": "head_ring"},
      {"id": "cam2", "kind": "camera", "label": "后", "group_id": "head_ring"},
      {"id": "cam3", "kind": "camera", "label": "左", "group_id": "head_ring"},
      {"id": "imu0", "kind": "imu", "label": "头环 IMU"}
    ],
    "groups": [
      {
        "id": "head_ring",
        "kind": "synchronized_camera_group",
        "members": ["cam0", "cam1", "cam2", "cam3"],
        "reference_component": "cam0",
        "timestamp_source": "sensor_hardware",
        "sync_method": "shared_trigger"
      }
    ],
    "transports": {
      "rpc": ["https"],
      "events": ["websocket"],
      "preview": ["rtsp"],
      "export": ["https-range"]
    },
    "limits": {
      "max_concurrent_previews": 4,
      "max_event_rate_hz": 20,
      "max_capture_name_bytes": 96
    }
  },
  "ext": {}
}
```

其他头环可以返回 2、6 或更多相机。App 应根据 `components` 和 `groups` 生成预览布局，并只展示 `capabilities` 中存在的功能。

## 8. 属性与参数

### 8.1 描述属性

请求方法：`property.describe`

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_prop_desc_01",
  "result": {
    "revision": "12",
    "properties": [
      {
        "path": "camera/head_ring/video/fps",
        "label": "帧率",
        "type": "integer",
        "unit": "Hz",
        "access": "read_write",
        "allowed_values": [25, 30, 40, 50],
        "default": 30,
        "apply_mode": "stream_restart"
      },
      {
        "path": "camera/head_ring/video/codec",
        "label": "编码",
        "type": "string",
        "access": "read_write",
        "allowed_values": ["h264", "h265"],
        "default": "h264",
        "apply_mode": "stream_restart"
      },
      {
        "path": "sensor/imu0/sample_rate",
        "label": "IMU 采样率",
        "type": "integer",
        "unit": "Hz",
        "access": "read_write",
        "minimum": 25,
        "maximum": 2000,
        "step": 25,
        "apply_mode": "live"
      }
    ]
  },
  "ext": {}
}
```

### 8.2 原子修改属性

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_prop_set_01",
  "method": "property.set",
  "params": {
    "if_revision": "12",
    "atomic": true,
    "dry_run": false,
    "values": {
      "camera/head_ring/video/fps": 30,
      "camera/head_ring/video/codec": "h264",
      "camera/head_ring/video/bitrate_bps": 8000000,
      "sensor/imu0/sample_rate": 1000
    }
  },
  "meta": {
    "client": "syncap-mobile/0.1.0",
    "idempotency_key": "aaecfd88-d844-4343-9226-164e86b47720"
  },
  "ext": {}
}
```

成功响应必须返回新 `revision`、实际应用值和 `apply_mode`。如果 `if_revision` 已过期，设备返回 `property.revision_conflict`，防止多端同时配置时互相覆盖。

## 9. 预览

调用 `stream.open`，而不是在 App 中硬编码 RTSP 端口：

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_stream_01",
  "method": "stream.open",
  "params": {
    "sources": ["cam0", "cam1", "cam2", "cam3"],
    "purpose": "preview",
    "preferences": {
      "transports": ["webrtc", "rtsp"],
      "max_width": 1280,
      "max_fps": 30,
      "low_latency": true
    }
  },
  "meta": {"client": "syncap-mobile/0.1.0"},
  "ext": {}
}
```

RoboBaton-4P 适配器的响应示例：

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_stream_01",
  "result": {
    "expires_at": "2026-08-06T10:22:41+08:00",
    "streams": [
      {"source_id": "cam0", "transport": "rtsp", "uri": "rtsp://192.168.1.12:554/PRR", "codec": "h264", "width": 1280, "height": 1088, "fps": 30},
      {"source_id": "cam1", "transport": "rtsp", "uri": "rtsp://192.168.1.12:555/PRR", "codec": "h264", "width": 1280, "height": 1088, "fps": 30},
      {"source_id": "cam2", "transport": "rtsp", "uri": "rtsp://192.168.1.12:556/PRR", "codec": "h264", "width": 1280, "height": 1088, "fps": 30},
      {"source_id": "cam3", "transport": "rtsp", "uri": "rtsp://192.168.1.12:557/PRR", "codec": "h264", "width": 1280, "height": 1088, "fps": 30}
    ]
  },
  "ext": {}
}
```

固定端口只存在于这个设备适配器内部；上位机始终读取 `stream.open` 的结果。

## 10. 采集控制

### 10.1 开始采集

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_capture_start_01",
  "method": "capture.start",
  "params": {
    "name": "session_001",
    "destination": "device",
    "sources": ["group:head_ring", "imu0"],
    "recording": {
      "segment_duration_s": 600,
      "video_container": "matroska",
      "include_diagnostics": true,
      "include_calibration_snapshot": true
    },
    "metadata": {
      "operator": "user_01",
      "subject_id": "subject_007",
      "tags": ["indoor", "walking"]
    }
  },
  "meta": {
    "client": "syncap-mobile/0.1.0",
    "idempotency_key": "86d9d4b8-20f1-4ca2-ae81-415c20b290ec"
  },
  "ext": {}
}
```

响应：

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_capture_start_01",
  "result": {
    "capture_id": "cap_20260806_101241_01",
    "session_id": "ses_20260806_101241_01",
    "state": "starting",
    "started_at_device_time_ns": null
  },
  "ext": {}
}
```

设备进入 `recording` 后发送 `capture.state` 事件。App 只有收到该事件才显示“正在录制”，不能仅根据请求成功推断。

### 10.2 标记事件

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_marker_01",
  "method": "capture.addMarker",
  "params": {
    "capture_id": "cap_20260806_101241_01",
    "label": "开始行走",
    "category": "operator",
    "client_time": "2026-08-06T10:13:02.104+08:00"
  },
  "meta": {
    "client": "syncap-mobile/0.1.0",
    "idempotency_key": "3e7960c4-bf2f-4e96-a258-ad851e363e8f"
  },
  "ext": {}
}
```

设备应把标记转换到设备时钟域，并在结果中同时返回 `device_time_ns` 和估计误差。

### 10.3 停止采集

```json
{
  "v": "1.0",
  "type": "request",
  "id": "req_capture_stop_01",
  "method": "capture.stop",
  "params": {
    "capture_id": "cap_20260806_101241_01",
    "finalize": true
  },
  "meta": {
    "client": "syncap-mobile/0.1.0",
    "idempotency_key": "5c21e581-f92d-43bf-bc1e-d46500ada4db"
  },
  "ext": {}
}
```

状态机：

```text
idle -> starting -> recording -> stopping -> finalizing -> completed
                     |                           |
                     +------> failed <-----------+
```

## 11. 相机组与外部时钟同步

同步指标必须明确测量的时钟域和时间点，不能只返回一个没有含义的“误差”。

### 11.1 状态查询

方法：`sync.getStatus`

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_sync_01",
  "result": {
    "group_id": "head_ring",
    "valid": true,
    "quality": "healthy",
    "clock_domain": "camera_sensor_clock",
    "measurement_point": "exposure_start",
    "timestamp_source": "sensor_hardware",
    "sync_method": "shared_trigger",
    "reference_component": "cam0",
    "instant": {
      "frame_set_id": "885123",
      "max_skew_ns": "41000",
      "rms_skew_ns": "15748"
    },
    "window": {
      "duration_ms": 10000,
      "sample_count": 300,
      "p50_max_skew_ns": "39000",
      "p95_max_skew_ns": "47000",
      "p99_max_skew_ns": "49000",
      "dropped_frame_sets": 0
    }
  },
  "ext": {}
}
```

`max_skew_ns` 定义为同一帧组中最大时间戳减最小时间戳，而不是相对参考相机的最大绝对值。

### 11.2 实时同步事件

```json
{
  "v": "1.0",
  "type": "event",
  "topic": "sync.sample",
  "seq": "1845",
  "device_time_ns": "3912451527000",
  "data": {
    "group_id": "head_ring",
    "frame_set_id": "885123",
    "group_time_ns": "3912451527000",
    "valid": true,
    "members": [
      {"component_id": "cam0", "frame_id": "102330", "delta_ns": "-18000", "dropped": false},
      {"component_id": "cam1", "frame_id": "102330", "delta_ns": "0", "dropped": false},
      {"component_id": "cam2", "frame_id": "102330", "delta_ns": "9000", "dropped": false},
      {"component_id": "cam3", "frame_id": "102330", "delta_ns": "23000", "dropped": false}
    ],
    "max_skew_ns": "41000",
    "rms_skew_ns": "15748"
  },
  "ext": {}
}
```

移动端默认订阅 10 Hz 的聚合事件，桌面诊断模式可根据设备 `limits` 提高频率。同步退化时设备发送 `alert.raised`，并说明 `trigger_missing`、`clock_unlocked`、`frame_dropped` 或 `timestamp_invalid`。

### 11.3 外部时钟状态

方法：`clock.getStatus`。相机组内部偏斜、PHC 相对 Grandmaster 的偏差、相机曝光相对 Grandmaster 的相位误差是三个不同指标，客户端不得合并显示。

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_clock_01",
  "result": {
    "schema": "syncap.clock-status/1.0",
    "source": "ptp",
    "state": "locked",
    "domain": 0,
    "grandmaster_identity": "001122.fffe.334455",
    "offset_from_master_ns": "-41",
    "mean_path_delay_ns": "892",
    "servo_sample_age_ms": 120,
    "interface": {
      "name": "eth0",
      "hardware_timestamping": true,
      "phc_device": "/dev/ptp0",
      "ptp4l_running": true,
      "phc2sys_running": true
    },
    "traceability": {
      "phc_locked_to_grandmaster": true,
      "system_clock_disciplined": true,
      "sensor_clock_mapped": true
    }
  },
  "ext": {}
}
```

`state` 可取 `locked`、`locking`、`listening`、`faulty`、`unsupported` 或 `unknown`。只有设备得到新鲜且有效的 PTP 伺服样本时才能返回 `locked`；仅检测到 `ptp4l` 进程正在运行不代表已经锁定。样本超过设备声明的有效窗口后必须降级为 `unknown`，并把偏差和路径延迟返回为 `null`。

`offset_from_master_ns` 只描述本地 PTP 时钟与 Grandmaster 的偏差，不代表相机组内部同步误差。`sensor_clock_mapped=false` 时，即使 PHC 已锁定，相机帧时间戳也不能宣称可追溯到 Grandmaster。

### 11.4 曝光相位

方法：`sync.getExposurePhase`。设备必须说明时间戳对应触发沿、传感器积分开始、曝光中心还是读出时刻。

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_exposure_phase_01",
  "result": {
    "schema": "syncap.exposure-phase/1.0",
    "group_id": "head_ring",
    "phase_locked_to_grandmaster": false,
    "target_period_ns": "16666667",
    "phase_error_ns": null,
    "timestamp_traceable": false,
    "timestamp_domain": "monotonic_raw",
    "timestamp_source": "gpio417_trigger",
    "measurement_point": "trigger_anchor",
    "physical_exposure_delay_calibrated": false,
    "physical_exposure_delay_ns": null
  },
  "ext": {}
}
```

触发锚点不等于光学积分开始。若 `physical_exposure_delay_calibrated=false`，客户端应显示“曝光物理延迟未标定”，不能把 GPIO 触发时间包装成曝光到 Grandmaster 的最终误差。准确标定通常需要 PPS 与传感器 strobe、光电二极管或等价测量链。

### 11.5 相机—IMU 时间偏移

相机与 IMU 的“最近样本时间差”只能作为运行诊断，不能替代物理时间偏移标定。设备可在 `sync.getStatus` 的 `camera_imu` 对象中返回：

```json
{
  "physical_time_offset_calibrated": false,
  "time_offset_ns": null,
  "clock_domain_mapped": false,
  "calibration_id": null
}
```

只有 `physical_time_offset_calibrated=true` 且 `clock_domain_mapped=true` 时，客户端才可把 `time_offset_ns` 标记为已标定的相机—IMU 时间偏移。

## 12. 标定参数

### 12.1 标定集合

方法：`calibration.list`、`calibration.get`、`calibration.activate`。上传新标定使用 `calibration.put`，并要求拥有者权限和 revision 校验。

### 12.2 标定文档示例

```json
{
  "schema": "syncap.calibration/1.0",
  "id": "cal_factory_20260718",
  "revision": "7",
  "status": "valid",
  "created_at": "2026-07-18T09:30:00+08:00",
  "source": "factory",
  "device_id": "dev_rb4p_a4df62",
  "valid_for": {
    "model": "RoboBaton-4P",
    "serial": "RB4P-2026-0001",
    "components": ["cam0", "cam1", "cam2", "cam3", "imu0"]
  },
  "frames": {
    "device_base": {"convention": "right_handed_x_forward_y_left_z_up"},
    "cam0_optical": {"convention": "right_handed_x_right_y_down_z_forward"}
  },
  "camera_intrinsics": {
    "cam0": {
      "model": "kannala_brandt4",
      "resolution": [1280, 1088],
      "k": [412.11, 0.0, 638.92, 0.0, 411.87, 542.76, 0.0, 0.0, 1.0],
      "d": [-0.0211, 0.0038, -0.0007, 0.0001]
    }
  },
  "transforms": [
    {
      "from": "device_base",
      "to": "cam0_optical",
      "translation_m": [0.0832, 0.0, 0.0125],
      "rotation_xyzw": [0.5, -0.5, 0.5, -0.5],
      "covariance": null
    },
    {
      "from": "device_base",
      "to": "imu0",
      "translation_m": [0.0, 0.0, 0.018],
      "rotation_xyzw": [0.0, 0.0, 0.0, 1.0],
      "covariance": null
    }
  ],
  "checksum": {
    "algorithm": "sha256",
    "value": "d6ac25a6478b750b43ef1a1a4fdb5db318a13dbaf8890818ecf1590f34460001"
  },
  "ext": {}
}
```

`camera_intrinsics` 对其他相机使用相同结构继续添加。设备不支持相机-IMU 外参时，不声明 `calibration.camera_imu`，也不返回相应变换。

## 13. 会话与导出

### 13.1 会话清单

方法：`session.getManifest`

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_session_manifest_01",
  "result": {
    "schema": "syncap.session-manifest/1.0",
    "session_id": "ses_20260806_101241_01",
    "name": "session_001",
    "state": "complete",
    "started_at": "2026-08-06T10:12:41.527+08:00",
    "duration_ns": "622450000000",
    "sources": ["cam0", "cam1", "cam2", "cam3", "imu0"],
    "calibration_id": "cal_factory_20260718",
    "files": [
      {"id": "f_cam0_000", "path": "media/cam0/000000.mkv", "role": "video", "source_id": "cam0", "size_bytes": "812345678", "sha256": "4a7d1ed414474e4033ac29ccb8653d9b00000000000000000000000000000001"},
      {"id": "f_imu", "path": "sensors/imu0.cbor", "role": "imu", "source_id": "imu0", "size_bytes": "4819231", "sha256": "4a7d1ed414474e4033ac29ccb8653d9b00000000000000000000000000000002"},
      {"id": "f_diag", "path": "diagnostics/sync.cbor", "role": "sync_diagnostics", "source_id": "head_ring", "size_bytes": "938221", "sha256": "4a7d1ed414474e4033ac29ccb8653d9b00000000000000000000000000000003"},
      {"id": "f_cal", "path": "calibration/calibration.json", "role": "calibration_snapshot", "source_id": null, "size_bytes": "8192", "sha256": "4a7d1ed414474e4033ac29ccb8653d9b00000000000000000000000000000004"}
    ]
  },
  "ext": {}
}
```

### 13.2 准备导出

调用 `session.prepareExport`，可选择 `original`、`proxy` 或 `metadata_only`：

```json
{
  "v": "1.0",
  "type": "response",
  "id": "req_export_01",
  "result": {
    "export_id": "exp_01",
    "expires_at": "2026-08-06T11:20:00+08:00",
    "range_supported": true,
    "items": [
      {
        "file_id": "f_cam0_000",
        "url": "https://syncap-a4df62.local:8443/syncap/v1/download/exp_01/f_cam0_000",
        "size_bytes": "812345678",
        "sha256": "4a7d1ed414474e4033ac29ccb8653d9b00000000000000000000000000000001"
      }
    ]
  },
  "ext": {}
}
```

下载必须支持 HTTP Range、断点续传和 SHA-256 校验。临时 URL 不应包含长期访问令牌。

### 13.3 RoboBaton-4P 局域网适配接口

当前正式板端服务在端口 `8080` 暴露以下 REST 映射。字段使用 camelCase 是适配层约定，语义与上面的通用 RPC 一致：

| 操作 | 请求 |
| --- | --- |
| 开始采集 | `POST /v1/captures/start`，正文 `{"name":"session_001"}` |
| 当前采集 | `GET /v1/captures/current` |
| 停止采集 | `POST /v1/captures/{captureId}/stop` |
| 会话列表 | `GET /v1/sessions` |
| 会话清单 | `GET /v1/sessions/{sessionId}/manifest` |
| 准备导出 | `POST /v1/sessions/{sessionId}/prepare-export` |
| 文件下载 | `GET /v1/sessions/{sessionId}/files/{name}` |
| 存储状态 | `GET /v1/storage` |
| 选择存储 | `POST /v1/storage/configure`，正文 `{"target":"usb"}`，兼容字段由客户端内部发送 |
| 安全弹出 | `POST /v1/storage/eject`，兼容字段由客户端内部发送 |
| BLE 离线摘要 | `GET /v1/offline/status` |

文件下载支持 `Range: bytes=start-end`，成功续传返回 `206 Partial Content`、`Content-Range` 和 `Accept-Ranges: bytes`。Android 先写入 `.part` 文件，重新进入导出时从已有长度继续请求；文件大小和 SHA-256 同时符合会话清单后才原子改名为最终文件。

## 14. 权限模型

建议角色：

| 角色 | 权限 |
| --- | --- |
| `viewer` | 查看设备状态、预览和非敏感诊断 |
| `operator` | viewer + 开始/停止采集、添加标记、导出数据 |
| `maintainer` | operator + 修改设备参数、时间和标定 |
| `owner` | maintainer + 配网、令牌、固件和恢复出厂设置 |

所有写操作应记录审计字段：调用方、方法、设备时间、结果和变更前后 revision。Wi-Fi 密码、访问令牌和认领密钥不得写入日志。

## 15. RoboBaton-4P 首版适配建议

在开发板上增加一个轻量 `syncap-agent`，不直接改动现有相机算法：

1. 包装 `/root/demo/cam_demo` 的启动、停止和状态。
2. 把当前 554–557 四路 RTSP 地址映射到 `stream.open`。
3. 读取现有诊断输出，转换为 `sync.getStatus` 和 `sync.sample`。
4. 读取 IMU，并按会话时间轴落盘。
5. 通过 BlueZ GATT 服务实现认领和网络引导。
6. 在局域网提供 HTTPS RPC、WebSocket 事件与断点下载。

当前设备已经确认具备 AIC SDIO Wi-Fi、UART 蓝牙 5.4 控制器和有线网络。板端以 `SynCap-DF62` 广播加密 GATT 配网服务；Wi-Fi 配置原子写入 `/userdata/wpa_supplicant.conf`，关联成功后通过 DHCP 获取地址。其他型号如果缺少 Wi-Fi，清单只声明 `network.ethernet`，协议和 App 的采集、预览及导出功能不受影响。

## 16. v1 验收条件

- 同一个 App 无硬编码即可识别 2、4、6 路相机模拟设备。
- 缺少 Wi-Fi、IMU 或标定能力时，相关入口自动隐藏且其他功能正常。
- BLE 配网报文在 ATT MTU 23 和 247 下都可完整重组。
- 重复发送相同 `idempotency_key` 不会启动两次采集。
- 四路同步事件能表达瞬时偏差、10 秒窗口统计、丢帧和时间戳有效性。
- 修改参数时可检测 revision 冲突。
- 导出支持断点续传，并能用 SHA-256 验证完整性。
- 客户端收到未知字段、未知能力和未知枚举值时保持可用。
