use btleplug::api::{Central, Peripheral as _, ScanFilter};
use btleplug::platform::{Adapter, Peripheral};
use futures_util::StreamExt;
use serde::Serialize;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::Duration;
use tauri::{AppHandle, Emitter};
use tokio::time::{sleep, timeout};
use uuid::Uuid;

use crate::first_adapter;

const TELEMETRY_UUID: &str = "8f7a0004-6c2b-4dd4-9f1a-53f65c9b40d1";
const TELEMETRY_EVENT: &str = "syncap://ble-telemetry";

#[derive(Default)]
pub struct BleTelemetryState {
    active: Arc<AtomicU64>,
    next: AtomicU64,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct Telemetry {
    recording: bool,
    preview: bool,
    battery_percent: Option<u8>,
    voltage_uv: Option<u32>,
    charging: Option<bool>,
    full: bool,
    sd_mounted: bool,
    sd_free_mib: u32,
    capture_state: &'static str,
    elapsed_ms: u32,
    sequence: u32,
}

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct TelemetryEvent {
    state: &'static str,
    address: String,
    telemetry: Option<Telemetry>,
    error: Option<String>,
}

fn emit_state(
    app: &AppHandle,
    state: &'static str,
    address: &str,
    telemetry: Option<Telemetry>,
    error: Option<String>,
) {
    eprintln!(
        "[BLE-TELEMETRY] state={state} address={address} sequence={} error={}",
        telemetry.as_ref().map(|value| value.sequence).unwrap_or(0),
        error.as_deref().unwrap_or("-")
    );
    let _ = app.emit(
        TELEMETRY_EVENT,
        TelemetryEvent {
            state,
            address: address.to_string(),
            telemetry,
            error,
        },
    );
}

fn le_u32(value: &[u8]) -> u32 {
    u32::from_le_bytes([value[0], value[1], value[2], value[3]])
}

fn decode_telemetry(value: &[u8]) -> Result<Telemetry, String> {
    if value.len() != 20 || value[0] != 1 {
        return Err("Unsupported SynCap BLE telemetry packet".into());
    }
    let flags = value[1];
    let capture_state = match value[3] {
        0 => "idle",
        1 => "recording",
        2 => "finalizing",
        _ => "unknown",
    };
    Ok(Telemetry {
        recording: flags & (1 << 0) != 0,
        preview: flags & (1 << 1) != 0,
        battery_percent: (flags & (1 << 2) != 0).then_some(value[2]),
        voltage_uv: (flags & (1 << 3) != 0).then(|| le_u32(&value[8..12])),
        charging: (flags & (1 << 4) != 0).then_some(flags & (1 << 5) != 0),
        full: flags & (1 << 6) != 0,
        sd_mounted: flags & (1 << 7) != 0,
        sd_free_mib: le_u32(&value[12..16]),
        capture_state,
        elapsed_ms: le_u32(&value[4..8]),
        sequence: le_u32(&value[16..20]),
    })
}

async fn find_device(adapter: &Adapter, address: &str) -> Result<Peripheral, String> {
    timeout(
        Duration::from_secs(5),
        adapter.start_scan(ScanFilter::default()),
    )
    .await
    .map_err(|_| "BLE scan start timed out".to_string())?
    .map_err(|error| error.to_string())?;
    for _ in 0..80 {
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
        sleep(Duration::from_millis(250)).await;
    }
    let _ = adapter.stop_scan().await;
    Err("未发现已配对的 SynCap 头环；请长按设备按键开启配对".into())
}

async fn watch(
    app: AppHandle,
    address: String,
    active: Arc<AtomicU64>,
    token: u64,
) -> Result<(), String> {
    emit_state(&app, "connecting", &address, None, None);
    let adapter = first_adapter().await?;
    let peripheral = find_device(&adapter, &address).await?;
    if active.load(Ordering::Acquire) != token {
        return Ok(());
    }
    if !peripheral
        .is_connected()
        .await
        .map_err(|error| error.to_string())?
    {
        peripheral
            .connect()
            .await
            .map_err(|error| format!("BLE connection failed: {error}"))?;
    }
    let result = async {
        peripheral
            .discover_services()
            .await
            .map_err(|error| format!("BLE service discovery failed: {error}"))?;
        let uuid = Uuid::parse_str(TELEMETRY_UUID).map_err(|error| error.to_string())?;
        let characteristic = peripheral
            .characteristics()
            .into_iter()
            .find(|value| value.uuid == uuid)
            .ok_or_else(|| "SynCap telemetry characteristic is missing".to_string())?;
        peripheral
            .subscribe(&characteristic)
            .await
            .map_err(|error| format!("Encrypted telemetry subscription failed: {error}"))?;
        let initial = peripheral
            .read(&characteristic)
            .await
            .map_err(|error| format!("Encrypted telemetry read failed: {error}"))?;
        emit_state(
            &app,
            "connected",
            &address,
            Some(decode_telemetry(&initial)?),
            None,
        );
        let mut notifications = peripheral
            .notifications()
            .await
            .map_err(|error| error.to_string())?;
        while active.load(Ordering::Acquire) == token {
            match timeout(Duration::from_secs(3), notifications.next()).await {
                Ok(Some(notification)) if notification.uuid == uuid => emit_state(
                    &app,
                    "connected",
                    &address,
                    Some(decode_telemetry(&notification.value)?),
                    None,
                ),
                Ok(Some(_)) | Err(_) => {}
                Ok(None) => return Err("BLE notification stream ended".into()),
            }
        }
        let _ = peripheral.unsubscribe(&characteristic).await;
        Ok(())
    }
    .await;
    if result.is_err() || active.load(Ordering::Acquire) != token {
        let _ = peripheral.disconnect().await;
    }
    result
}

#[tauri::command]
pub async fn start_ble_telemetry(
    app: AppHandle,
    state: tauri::State<'_, BleTelemetryState>,
    address: String,
) -> Result<(), String> {
    if address.trim().is_empty() {
        return Err("BLE device address is required".into());
    }
    let token = state.next.fetch_add(1, Ordering::Relaxed).wrapping_add(1);
    let active = Arc::clone(&state.active);
    active.store(token, Ordering::Release);
    tauri::async_runtime::spawn(async move {
        while active.load(Ordering::Acquire) == token {
            let result = watch(app.clone(), address.clone(), Arc::clone(&active), token).await;
            if active.load(Ordering::Acquire) != token {
                break;
            }
            match result {
                Ok(()) => emit_state(&app, "disconnected", &address, None, None),
                Err(error) => emit_state(&app, "error", &address, None, Some(error)),
            }
            sleep(Duration::from_secs(2)).await;
        }
        emit_state(&app, "disconnected", &address, None, None);
    });
    Ok(())
}

#[tauri::command]
pub fn stop_ble_telemetry(state: tauri::State<'_, BleTelemetryState>) {
    state.active.store(0, Ordering::Release);
}

#[cfg(test)]
mod tests {
    use super::decode_telemetry;

    #[test]
    fn decodes_version_one_packet() {
        let mut value = [0u8; 20];
        value[0] = 1;
        value[1] = 0b1011_1101;
        value[2] = 78;
        value[3] = 2;
        value[4..8].copy_from_slice(&12_345u32.to_le_bytes());
        value[8..12].copy_from_slice(&4_012_000u32.to_le_bytes());
        value[12..16].copy_from_slice(&2048u32.to_le_bytes());
        value[16..20].copy_from_slice(&7u32.to_le_bytes());
        let decoded = decode_telemetry(&value).expect("valid packet");
        assert!(decoded.recording);
        assert_eq!(decoded.capture_state, "finalizing");
        assert_eq!(decoded.battery_percent, Some(78));
        assert_eq!(decoded.voltage_uv, Some(4_012_000));
        assert_eq!(decoded.charging, Some(true));
        assert!(decoded.sd_mounted);
        assert_eq!(decoded.elapsed_ms, 12_345);
        assert_eq!(decoded.sequence, 7);
    }

    #[test]
    fn rejects_wrong_size_or_version() {
        assert!(decode_telemetry(&[1u8; 19]).is_err());
        assert!(decode_telemetry(&[2u8; 20]).is_err());
    }
}
