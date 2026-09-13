mod ble_telemetry;

use ble_telemetry::{start_ble_telemetry, stop_ble_telemetry, BleTelemetryState};
use btleplug::api::{Central, CentralState, Manager as _, Peripheral as _, ScanFilter, WriteType};
use btleplug::platform::{Adapter, Manager, Peripheral};
use rand::random;
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::path::PathBuf;
use std::sync::Mutex;
use std::time::Duration;
use tauri_plugin_shell::process::CommandChild;
use tauri_plugin_shell::ShellExt;
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::OnceCell;
use tokio::time::{sleep, timeout};
use uuid::Uuid;

const BLE_CONFIG_UUID: &str = "8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1";
const BLE_STATUS_UUID: &str = "8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1";
static BLE_ADAPTER: OnceCell<Adapter> = OnceCell::const_new();

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct ProbeStream {
    port: u16,
    online: bool,
    url: String,
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct DeviceProbe {
    host: String,
    reachable: bool,
    online_count: usize,
    streams: Vec<ProbeStream>,
}

#[derive(Serialize)]
struct BleDevice {
    id: String,
    address: String,
    name: String,
    rssi: i16,
}

#[derive(Serialize)]
struct DesktopPreview {
    urls: Vec<String>,
    labels: Vec<String>,
}

struct PreviewProcess {
    child: CommandChild,
    config: PathBuf,
}

#[derive(Default)]
struct PreviewState(Mutex<Option<PreviewProcess>>);

fn validate_host(host: &str) -> Result<(), String> {
    if host.is_empty()
        || host.len() > 253
        || !host
            .chars()
            .all(|value| value.is_ascii_alphanumeric() || value == '.' || value == '-')
    {
        return Err("Invalid device host".into());
    }
    Ok(())
}

fn device_client(host: &str, port: u16) -> Result<syncap_core::Client, String> {
    validate_host(host)?;
    syncap_core::Client::new(&format!("http://{host}:{port}")).map_err(|error| error.to_string())
}

async fn rtsp_responds(host: &str, port: u16) -> bool {
    let connect = timeout(Duration::from_millis(800), TcpStream::connect((host, port))).await;
    let Ok(Ok(mut stream)) = connect else {
        return false;
    };
    let request = format!(
        "DESCRIBE rtsp://{host}:{port}/PRR RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\nUser-Agent: SynCap-Studio\r\n\r\n"
    );
    if timeout(
        Duration::from_millis(800),
        stream.write_all(request.as_bytes()),
    )
    .await
    .is_err()
    {
        return false;
    }
    let mut response = [0u8; 256];
    matches!(
        timeout(Duration::from_millis(800), stream.read(&mut response)).await,
        Ok(Ok(count)) if count > 0 && String::from_utf8_lossy(&response[..count]).starts_with("RTSP/1.0 200")
    )
}

#[tauri::command]
async fn probe_device(host: String, ports: Vec<u16>) -> Result<DeviceProbe, String> {
    validate_host(&host)?;
    let mut streams = Vec::with_capacity(ports.len());
    for port in ports {
        let online = rtsp_responds(&host, port).await;
        streams.push(ProbeStream {
            port,
            online,
            url: format!("rtsp://{host}:{port}/PRR"),
        });
    }
    let online_count = streams.iter().filter(|stream| stream.online).count();
    Ok(DeviceProbe {
        host,
        reachable: online_count > 0,
        online_count,
        streams,
    })
}

#[tauri::command]
async fn get_device_manifest(host: String, port: u16) -> Result<Value, String> {
    device_client(&host, port)?
        .manifest()
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn get_device_status(host: String, port: u16) -> Result<Value, String> {
    device_client(&host, port)?
        .status()
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn get_device_storage(host: String, port: u16) -> Result<Value, String> {
    device_client(&host, port)?
        .storage()
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn configure_device_storage(
    host: String,
    port: u16,
    target: String,
) -> Result<Value, String> {
    device_client(&host, port)?
        .configure_storage(&target)
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn get_camera_configuration(host: String, port: u16) -> Result<Value, String> {
    device_client(&host, port)?
        .camera_configuration()
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn configure_camera_orientation(
    host: String,
    port: u16,
    rotation_degrees: u16,
) -> Result<Value, String> {
    device_client(&host, port)?
        .configure_camera_orientation(rotation_degrees)
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn start_device_capture(host: String, port: u16, name: String) -> Result<Value, String> {
    let result = device_client(&host, port)?
        .start_capture(&name)
        .await
        .map_err(|error| error.to_string())?;
    serde_json::to_value(result).map_err(|error| error.to_string())
}

#[tauri::command]
async fn stop_device_capture(host: String, port: u16, capture_id: String) -> Result<Value, String> {
    let result = device_client(&host, port)?
        .stop_capture(&capture_id)
        .await
        .map_err(|error| error.to_string())?;
    serde_json::to_value(result).map_err(|error| error.to_string())
}

#[tauri::command]
async fn list_device_sessions(host: String, port: u16) -> Result<Vec<Value>, String> {
    let result = device_client(&host, port)?
        .sessions()
        .await
        .map_err(|error| error.to_string())?;
    result
        .into_iter()
        .map(|session| serde_json::to_value(session).map_err(|error| error.to_string()))
        .collect()
}

#[tauri::command]
async fn prepare_device_export(
    host: String,
    port: u16,
    session_id: String,
) -> Result<Value, String> {
    device_client(&host, port)?
        .prepare_export(&session_id)
        .await
        .map_err(|error| error.to_string())
}

#[tauri::command]
async fn download_device_session(
    host: String,
    port: u16,
    session_id: String,
) -> Result<Value, String> {
    let destination = dirs::download_dir()
        .unwrap_or_else(std::env::temp_dir)
        .join("SynCap")
        .join(&session_id);
    let report = device_client(&host, port)?
        .export_session(&session_id, &destination)
        .await
        .map_err(|error| error.to_string())?;
    serde_json::to_value(report).map_err(|error| error.to_string())
}

async fn first_adapter() -> Result<Adapter, String> {
    let adapter = BLE_ADAPTER
        .get_or_try_init(|| async {
            let manager = Manager::new().await.map_err(|error| error.to_string())?;
            manager
                .adapters()
                .await
                .map_err(|error| error.to_string())?
                .into_iter()
                .next()
                .ok_or_else(|| "Bluetooth adapter is unavailable".to_string())
        })
        .await?
        .clone();
    for _ in 0..600 {
        match adapter
            .adapter_state()
            .await
            .map_err(|error| error.to_string())?
        {
            CentralState::PoweredOn => return Ok(adapter),
            CentralState::PoweredOff => return Err("Bluetooth is turned off".into()),
            CentralState::Unknown => sleep(Duration::from_millis(100)).await,
        }
    }
    Err("Bluetooth adapter did not become ready".into())
}

async fn find_ble_device(adapter: &Adapter, address: &str) -> Result<Peripheral, String> {
    timeout(
        Duration::from_secs(5),
        adapter.start_scan(ScanFilter::default()),
    )
    .await
    .map_err(|_| "BLE scan start timed out".to_string())?
    .map_err(|error| error.to_string())?;
    for _ in 0..16 {
        for peripheral in adapter
            .peripherals()
            .await
            .map_err(|error| error.to_string())?
        {
            let id = peripheral.id().to_string();
            let properties = peripheral
                .properties()
                .await
                .map_err(|error| error.to_string())?;
            if id == address
                || properties
                    .as_ref()
                    .map(|value| value.address.to_string() == address)
                    .unwrap_or(false)
            {
                let _ = adapter.stop_scan().await;
                return Ok(peripheral);
            }
        }
        sleep(Duration::from_millis(400)).await;
    }
    let _ = adapter.stop_scan().await;
    Err("Selected SynCap BLE device was not found".into())
}

#[tauri::command]
async fn scan_ble_devices(duration_ms: u64) -> Result<Vec<BleDevice>, String> {
    eprintln!("[BLE-SCAN] start duration_ms={duration_ms}");
    let adapter = first_adapter().await?;
    let syncap_service = Uuid::parse_str("8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1")
        .map_err(|error| error.to_string())?;
    timeout(
        Duration::from_secs(5),
        adapter.start_scan(ScanFilter::default()),
    )
    .await
    .map_err(|_| "BLE scan start timed out".to_string())?
    .map_err(|error| error.to_string())?;
    sleep(Duration::from_millis(duration_ms.clamp(1500, 10_000))).await;
    let peripherals = adapter
        .peripherals()
        .await
        .map_err(|error| error.to_string());
    let _ = adapter.stop_scan().await;
    let peripherals = peripherals?;
    let mut devices = Vec::new();
    eprintln!("[BLE-SCAN] observed peripherals={}", peripherals.len());
    for peripheral in peripherals {
        let Some(properties) = peripheral
            .properties()
            .await
            .map_err(|error| error.to_string())?
        else {
            continue;
        };
        eprintln!(
            "[BLE-SCAN] peripheral={} name={:?} services={:?} rssi={:?}",
            peripheral.id(),
            properties.local_name,
            properties.services,
            properties.rssi
        );
        let is_syncap_service = properties.services.contains(&syncap_service);
        let name = properties.local_name.unwrap_or_else(|| {
            if is_syncap_service {
                "SynCap-HISI".to_string()
            } else {
                String::new()
            }
        });
        if !name.starts_with("SynCap-") && !is_syncap_service {
            continue;
        }
        let id = peripheral.id().to_string();
        devices.push(BleDevice {
            id: id.clone(),
            address: id,
            name,
            rssi: properties.rssi.unwrap_or(-127),
        });
    }
    eprintln!("[BLE-SCAN] complete devices={}", devices.len());
    Ok(devices)
}

fn fragment_ble_payload(payload: &[u8]) -> Vec<Vec<u8>> {
    let fragment_size = 180usize;
    let count = payload.len().div_ceil(fragment_size).max(1);
    let message_id: u32 = random();
    payload
        .chunks(fragment_size)
        .enumerate()
        .map(|(index, fragment)| {
            let mut value = Vec::with_capacity(16 + fragment.len());
            value.extend_from_slice(b"SC");
            value.push(1);
            value.push(1);
            value.extend_from_slice(&message_id.to_be_bytes());
            value.extend_from_slice(&(index as u16).to_be_bytes());
            value.extend_from_slice(&(count as u16).to_be_bytes());
            value.extend_from_slice(&(fragment.len() as u16).to_be_bytes());
            value.extend_from_slice(&0u16.to_be_bytes());
            value.extend_from_slice(fragment);
            value
        })
        .collect()
}

async fn ble_command(address: String, payload: Value) -> Result<Value, String> {
    let adapter = first_adapter().await?;
    let peripheral = find_ble_device(&adapter, &address).await?;
    peripheral
        .connect()
        .await
        .map_err(|error| error.to_string())?;
    peripheral
        .discover_services()
        .await
        .map_err(|error| error.to_string())?;
    let config_uuid = Uuid::parse_str(BLE_CONFIG_UUID).map_err(|error| error.to_string())?;
    let status_uuid = Uuid::parse_str(BLE_STATUS_UUID).map_err(|error| error.to_string())?;
    let config = peripheral
        .characteristics()
        .into_iter()
        .find(|value| value.uuid == config_uuid)
        .ok_or_else(|| "SynCap BLE command characteristic is missing".to_string())?;
    let status = peripheral
        .characteristics()
        .into_iter()
        .find(|value| value.uuid == status_uuid)
        .ok_or_else(|| "SynCap BLE status characteristic is missing".to_string())?;
    let encoded = serde_json::to_vec(&payload).map_err(|error| error.to_string())?;
    for fragment in fragment_ble_payload(&encoded) {
        peripheral
            .write(&config, &fragment, WriteType::WithResponse)
            .await
            .map_err(|error| format!("Encrypted BLE write failed: {error}"))?;
    }
    for _ in 0..100 {
        sleep(Duration::from_millis(800)).await;
        let bytes = peripheral
            .read(&status)
            .await
            .map_err(|error| format!("Encrypted BLE status read failed: {error}"))?;
        let response: Value = serde_json::from_slice(&bytes)
            .map_err(|error| format!("Device returned invalid BLE JSON: {error}"))?;
        let state = response
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("unknown");
        if state != "working" && state != "connecting" {
            let _ = peripheral.disconnect().await;
            return Ok(response);
        }
    }
    let _ = peripheral.disconnect().await;
    Err("BLE operation timed out".into())
}

#[tauri::command]
async fn configure_wifi_ble(
    address: String,
    ssid: String,
    password: String,
    security: String,
    claim_code: String,
) -> Result<Value, String> {
    if ssid.is_empty()
        || !claim_code.chars().all(|value| value.is_ascii_digit())
        || claim_code.len() != 6
    {
        return Err("SSID and six-digit claim code are required".into());
    }
    ble_command(
        address,
        json!({
            "op": "wifi.configure",
            "ssid": ssid,
            "password": password,
            "security": security,
            "claimCode": claim_code
        }),
    )
    .await
}

#[tauri::command]
async fn offline_ble_command(
    address: String,
    claim_code: String,
    op: String,
    target: Option<String>,
    name: Option<String>,
    capture_id: Option<String>,
) -> Result<Value, String> {
    let allowed = [
        "storage.configure",
        "storage.eject",
        "capture.start",
        "capture.stop",
        "device.status",
    ];
    if !allowed.contains(&op.as_str())
        || claim_code.len() != 6
        || !claim_code.chars().all(|value| value.is_ascii_digit())
    {
        return Err("Invalid offline BLE command".into());
    }
    let mut payload = Map::new();
    payload.insert("op".into(), Value::String(op));
    payload.insert("claimCode".into(), Value::String(claim_code));
    if let Some(value) = target {
        payload.insert("target".into(), Value::String(value));
    }
    if let Some(value) = name {
        payload.insert("name".into(), Value::String(value));
    }
    if let Some(value) = capture_id {
        payload.insert("captureId".into(), Value::String(value));
    }
    ble_command(address, Value::Object(payload)).await
}

fn stop_preview_process(state: &PreviewState) -> Result<(), String> {
    let process = state
        .0
        .lock()
        .map_err(|_| "Preview process state is unavailable".to_string())?
        .take();
    if let Some(process) = process {
        let _ = process.child.kill();
        let _ = std::fs::remove_file(process.config);
    }
    Ok(())
}

fn validate_desktop_preview_url(value: &str) -> Result<(), String> {
    let parsed = url::Url::parse(value).map_err(|_| "Invalid preview stream URL".to_string())?;
    match parsed.scheme() {
        "tcp" | "tcp-hevc" => Err(
            "Raw TCP HEVC preview is not supported by this desktop build; use an RTSP stream or the mobile app"
                .into(),
        ),
        "rtsp" if parsed.host_str().is_some() => Ok(()),
        _ => Err("Only RTSP streams are supported by desktop preview".into()),
    }
}

#[tauri::command]
async fn open_preview(
    app: tauri::AppHandle,
    state: tauri::State<'_, PreviewState>,
    urls: Vec<String>,
    labels: Vec<String>,
) -> Result<DesktopPreview, String> {
    if urls.is_empty() || urls.len() > 4 {
        return Err("No valid RTSP stream was provided".into());
    }
    for value in &urls {
        validate_desktop_preview_url(value)?;
    }

    stop_preview_process(&state)?;
    let mut config = String::from(
        "logLevel: warn\nlogDestinations: [stdout]\nrtsp: yes\nrtspAddress: 127.0.0.1:18554\nrtspTransports: [tcp]\nrtmp: no\nhls: yes\nhlsAddress: 127.0.0.1:18888\nhlsAllowOrigins: ['*']\nwebrtc: no\nsrt: no\nmoq: no\nplayback: no\napi: no\nmetrics: no\npprof: no\npaths:\n",
    );
    for (index, value) in urls.iter().enumerate() {
        config.push_str(&format!(
            "  cam{index}:\n    source: {}\n    sourceOnDemand: yes\n    rtspTransport: tcp\n",
            serde_json::to_string(value).map_err(|error| error.to_string())?
        ));
    }
    let config_path =
        std::env::temp_dir().join(format!("syncap-mediamtx-{}.yml", std::process::id()));
    std::fs::write(&config_path, config).map_err(|error| error.to_string())?;
    let command = app
        .shell()
        .sidecar("mediamtx")
        .map_err(|error| error.to_string())?
        .arg(&config_path);
    let (mut events, child) = command
        .spawn()
        .map_err(|error| format!("Unable to start the bundled preview engine: {error}"))?;
    tauri::async_runtime::spawn(async move { while events.recv().await.is_some() {} });
    state
        .0
        .lock()
        .map_err(|_| "Preview process state is unavailable".to_string())?
        .replace(PreviewProcess {
            child,
            config: config_path,
        });
    for _ in 0..15 {
        if TcpStream::connect("127.0.0.1:18888").await.is_ok() {
            let labels = urls
                .iter()
                .enumerate()
                .map(|(index, _)| {
                    labels
                        .get(index)
                        .cloned()
                        .unwrap_or_else(|| format!("CAM {index}"))
                })
                .collect();
            return Ok(DesktopPreview {
                urls: (0..urls.len())
                    .map(|index| format!("http://127.0.0.1:18888/cam{index}/index.m3u8"))
                    .collect(),
                labels,
            });
        }
        sleep(Duration::from_millis(200)).await;
    }
    stop_preview_process(&state)?;
    Err("The bundled preview engine did not become ready".into())
}

#[tauri::command]
fn close_preview(state: tauri::State<'_, PreviewState>) -> Result<(), String> {
    stop_preview_process(&state)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .manage(PreviewState::default())
        .manage(BleTelemetryState::default())
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            probe_device,
            get_device_manifest,
            get_device_status,
            get_device_storage,
            configure_device_storage,
            get_camera_configuration,
            configure_camera_orientation,
            start_device_capture,
            stop_device_capture,
            list_device_sessions,
            prepare_device_export,
            download_device_session,
            scan_ble_devices,
            start_ble_telemetry,
            stop_ble_telemetry,
            configure_wifi_ble,
            offline_ble_command,
            open_preview,
            close_preview
        ])
        .run(tauri::generate_context!())
        .expect("error while running SynCap Studio");
}

#[cfg(test)]
mod tests {
    use super::*;

    async fn mock_device(
        replies: Vec<(&'static str, Option<Value>, &'static str, Value)>,
    ) -> (u16, tokio::task::JoinHandle<()>) {
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let port = listener.local_addr().unwrap().port();
        let task = tokio::spawn(async move {
            for (expected_request, expected_body, status, body) in replies {
                let (mut stream, _) = timeout(Duration::from_secs(5), listener.accept())
                    .await
                    .unwrap()
                    .unwrap();
                let mut request = Vec::new();
                let mut buffer = [0; 1024];
                loop {
                    let count = timeout(Duration::from_secs(5), stream.read(&mut buffer))
                        .await
                        .unwrap()
                        .unwrap();
                    assert!(count > 0);
                    request.extend_from_slice(&buffer[..count]);
                    assert!(request.len() < 64 * 1024);
                    let Some(end) = request.windows(4).position(|part| part == b"\r\n\r\n") else {
                        continue;
                    };
                    let headers = String::from_utf8_lossy(&request[..end]);
                    let length = headers
                        .lines()
                        .find_map(|line| {
                            let (name, value) = line.split_once(':')?;
                            name.eq_ignore_ascii_case("content-length")
                                .then(|| value.trim().parse::<usize>().unwrap())
                        })
                        .unwrap_or(0);
                    if request.len() < end + 4 + length {
                        continue;
                    }
                    assert_eq!(headers.lines().next().unwrap(), expected_request);
                    if let Some(expected) = expected_body {
                        let actual: Value =
                            serde_json::from_slice(&request[end + 4..end + 4 + length]).unwrap();
                        assert_eq!(actual, expected);
                    }
                    break;
                }
                let body = body.to_string();
                let response = format!(
                    "HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                    body.len()
                );
                stream.write_all(response.as_bytes()).await.unwrap();
            }
        });
        (port, task)
    }

    #[tokio::test]
    async fn device_read_commands_preserve_dynamic_payloads() {
        let payload =
            json!({"deviceId": "tina_2p", "cameras": ["left", "right"], "vendor": {"revision": 3}});
        let (port, server) = mock_device(vec![
            ("GET /v1/manifest HTTP/1.1", None, "200 OK", payload.clone()),
            ("GET /v1/status HTTP/1.1", None, "200 OK", payload.clone()),
            ("GET /v1/storage HTTP/1.1", None, "200 OK", payload.clone()),
            (
                "GET /v1/camera/configuration HTTP/1.1",
                None,
                "200 OK",
                payload.clone(),
            ),
        ])
        .await;
        assert_eq!(
            get_device_manifest("127.0.0.1".into(), port).await.unwrap(),
            payload
        );
        assert_eq!(
            get_device_status("127.0.0.1".into(), port).await.unwrap(),
            payload
        );
        assert_eq!(
            get_device_storage("127.0.0.1".into(), port).await.unwrap(),
            payload
        );
        assert_eq!(
            get_camera_configuration("127.0.0.1".into(), port)
                .await
                .unwrap(),
            payload
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn capture_commands_keep_the_existing_json_contract() {
        let started = json!({
            "captureId": "cap_123", "sessionId": "ses_123", "state": "recording",
            "startedAtDeviceTimeNs": "1234567890123456789", "elapsedMs": 0,
            "vendor": {"syncMode": "hardware"}
        });
        let stopped = json!({
            "captureId": "cap_123", "sessionId": "ses_123", "state": "completed",
            "session": {"id": "ses_123", "sizeBytes": 120, "cameras": ["left", "right"]}
        });
        let (port, server) = mock_device(vec![
            (
                "POST /v1/captures/start HTTP/1.1",
                Some(json!({"name": "SDK 测试"})),
                "200 OK",
                started.clone(),
            ),
            (
                "POST /v1/captures/cap_123/stop HTTP/1.1",
                Some(json!({})),
                "200 OK",
                stopped.clone(),
            ),
        ])
        .await;
        assert_eq!(
            start_device_capture("127.0.0.1".into(), port, "SDK 测试".into())
                .await
                .unwrap(),
            started
        );
        assert_eq!(
            stop_device_capture("127.0.0.1".into(), port, "cap_123".into())
                .await
                .unwrap(),
            stopped
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn session_command_still_returns_an_array_without_dropping_vendor_fields() {
        let session = json!({"id": "ses_123", "durationMs": 0, "sizeBytes": 0, "exportAvailable": false, "vendor": {"reason": "incomplete"}});
        let (port, server) = mock_device(vec![(
            "GET /v1/sessions HTTP/1.1",
            None,
            "200 OK",
            json!({"sessions": [session.clone()]}),
        )])
        .await;
        assert_eq!(
            list_device_sessions("127.0.0.1".into(), port)
                .await
                .unwrap(),
            vec![session]
        );
        server.await.unwrap();
    }

    #[tokio::test]
    async fn configuration_commands_keep_wire_arguments_and_device_errors() {
        let payload = json!({"state": "ready"});
        let storage_payload = json!({"target": "usb", "canCapture": true});
        let (port, server) = mock_device(vec![
            (
                "POST /v1/storage/configure HTTP/1.1",
                Some(json!({"target": "usb", "claimCode": "123456"})),
                "200 OK",
                storage_payload.clone(),
            ),
            (
                "POST /v1/camera/configuration HTTP/1.1",
                Some(json!({"rotationDegrees": 90, "claimCode": "123456"})),
                "200 OK",
                payload.clone(),
            ),
            (
                "POST /v1/sessions/ses_123/prepare-export HTTP/1.1",
                Some(json!({})),
                "409 Conflict",
                json!({"error": "session_incomplete", "message": "Capture data is incomplete"}),
            ),
        ])
        .await;
        assert_eq!(
            configure_device_storage("127.0.0.1".into(), port, "usb".into())
                .await
                .unwrap(),
            storage_payload
        );
        assert_eq!(
            configure_camera_orientation("127.0.0.1".into(), port, 90)
                .await
                .unwrap(),
            payload
        );
        let error = prepare_device_export("127.0.0.1".into(), port, "ses_123".into())
            .await
            .unwrap_err();
        assert!(error.contains("Device returned HTTP 409"));
        assert!(error.contains("session_incomplete"));
        assert!(error.contains("Capture data is incomplete"));
        server.await.unwrap();
    }

    #[tokio::test]
    async fn bridge_rejects_invalid_arguments_before_connecting() {
        assert!(get_device_manifest("127.0.0.1/path".into(), 1)
            .await
            .unwrap_err()
            .contains("Invalid device host"));
        assert!(
            configure_device_storage("127.0.0.1".into(), 1, "other".into())
                .await
                .unwrap_err()
                .contains("Invalid storage target")
        );
        assert!(configure_camera_orientation("127.0.0.1".into(), 1, 45)
            .await
            .unwrap_err()
            .contains("Invalid camera rotation"));
        assert!(start_device_capture("127.0.0.1".into(), 1, " ".into())
            .await
            .unwrap_err()
            .contains("Invalid session name"));
        assert!(
            stop_device_capture("127.0.0.1".into(), 1, "../cap_123".into())
                .await
                .is_err()
        );
    }

    #[test]
    fn desktop_preview_rejects_raw_tcp_hevc_but_keeps_rtsp() {
        assert!(validate_desktop_preview_url("rtsp://192.168.1.12:554/PRR").is_ok());
        let error = validate_desktop_preview_url("tcp://192.168.1.13:9100").unwrap_err();
        assert!(error.contains("TCP HEVC"));
        assert!(validate_desktop_preview_url("tcp-hevc://192.168.1.13:9100").is_err());
    }
}
