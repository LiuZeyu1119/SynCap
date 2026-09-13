use serde_json::{json, Value};
use std::{sync::Arc, time::Duration};
use syncap_ffi::{Cancellation, DeviceClient, SdkError, SdkErrorKind, StorageTarget};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    task::JoinHandle,
};

async fn fixture(status: &str, payload: Value) -> (Arc<DeviceClient>, JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let client = DeviceClient::new(format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let status = status.to_owned();
    let payload = payload.to_string();
    let server = tokio::spawn(async move {
        let (mut socket, _) = tokio::time::timeout(Duration::from_secs(3), listener.accept())
            .await
            .unwrap()
            .unwrap();
        let mut bytes = Vec::new();
        let mut buffer = [0u8; 2048];
        loop {
            let size = socket.read(&mut buffer).await.unwrap();
            assert!(size > 0);
            bytes.extend_from_slice(&buffer[..size]);
            if let Some(index) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&bytes[..index]).to_ascii_lowercase();
                let length: usize = headers
                    .lines()
                    .find_map(|line| line.strip_prefix("content-length:"))
                    .map(|length| length.trim().parse().unwrap())
                    .unwrap_or(0);
                if bytes.len() >= index + 4 + length {
                    break;
                }
            }
        }
        if !status.is_empty() {
            let headers = format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", payload.len());
            socket.write_all(headers.as_bytes()).await.unwrap();
            socket.write_all(payload.as_bytes()).await.unwrap();
        }
        socket.shutdown().await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(80), listener.accept())
                .await
                .is_err(),
            "Binding must not retry requests"
        );
        String::from_utf8(bytes).unwrap()
    });
    (client, server)
}

fn kind(error: SdkError) -> SdkErrorKind {
    let SdkError::Failure { kind, .. } = error;
    kind
}

#[test]
fn constructor_rejects_credentials_without_exposing_them() {
    let error = DeviceClient::new("http://user:secret@localhost".into())
        .err()
        .unwrap();
    assert!(!error.to_string().contains("secret"));
    assert_eq!(kind(error), SdkErrorKind::InvalidInput);
}

#[test]
fn export_report_preserves_verified_counts_and_destination() {
    let report = syncap_ffi::ExportReport::from(syncap_core::ExportReport {
        session_id: "ses_test".into(),
        path: "/tmp/export/ses_test".into(),
        bytes: 4_294_967_296,
        files: 3,
        verified_files: 2,
        manifest_saved: true,
        manifest_file: "session.json".into(),
        verified: true,
    });
    assert_eq!(report.session_id, "ses_test");
    assert_eq!(report.path, "/tmp/export/ses_test");
    assert_eq!(report.bytes, 4_294_967_296);
    assert_eq!(report.files, 3);
    assert_eq!(report.verified_files, 2);
    assert!(report.manifest_saved);
    assert_eq!(report.manifest_file, "session.json");
    assert!(report.verified);
}

#[test]
fn every_core_error_kind_has_a_distinct_mobile_mapping() {
    use syncap_core::ErrorKind;
    for (source, expected) in [
        (ErrorKind::InvalidInput, SdkErrorKind::InvalidInput),
        (ErrorKind::Transport, SdkErrorKind::Transport),
        (ErrorKind::Timeout, SdkErrorKind::Timeout),
        (ErrorKind::Device, SdkErrorKind::Device),
        (ErrorKind::Protocol, SdkErrorKind::Protocol),
        (ErrorKind::Integrity, SdkErrorKind::Integrity),
        (ErrorKind::Io, SdkErrorKind::Io),
        (ErrorKind::Cancelled, SdkErrorKind::Cancelled),
    ] {
        assert_eq!(
            kind(syncap_core::Error::new(source, "test").into()),
            expected
        );
    }
}

#[tokio::test]
async fn capture_record_retains_nanosecond_precision_and_vendor_fields() {
    let payload = json!({"captureId":"cap_test", "sessionId":"ses_test", "state":"recording",
        "startedAtDeviceTimeNs":"3689123456789012345", "elapsedMs":0,
        "vendor":{"hardwareTrigger":true}});
    let (client, server) = fixture("200 OK", payload.clone()).await;
    let capture = client
        .start_capture("Mobile SDK test".into())
        .await
        .unwrap();
    assert_eq!(capture.capture_id, "cap_test");
    assert_eq!(capture.session_id, "ses_test");
    assert_eq!(capture.elapsed_ms, Some(0));
    assert_eq!(
        capture.started_at_device_time_ns.as_deref(),
        Some("3689123456789012345")
    );
    assert_eq!(
        serde_json::from_str::<Value>(&capture.raw_json).unwrap(),
        payload
    );
    let request = server.await.unwrap();
    assert!(request.starts_with("POST /v1/captures/start HTTP/1.1"));
    let body: Value = serde_json::from_str(request.split_once("\r\n\r\n").unwrap().1).unwrap();
    assert_eq!(body, json!({"name":"Mobile SDK test"}));
}

#[tokio::test]
async fn failed_capture_keeps_recovery_metadata() {
    let payload = json!({"state":"failed", "recoveryRequired":true, "rebootRequired":true,
        "failureReason":"incomplete producer segment"});
    let (client, server) = fixture("200 OK", payload.clone()).await;
    let capture = client.current_capture().await.unwrap();
    assert_eq!(capture.state, "failed");
    assert_eq!(capture.elapsed_ms, None);
    assert_eq!(
        serde_json::from_str::<Value>(&capture.raw_json).unwrap(),
        payload
    );
    server.await.unwrap();
}

#[tokio::test]
async fn manifest_keeps_arbitrary_device_capabilities() {
    let payload = json!({"device":{"model":"Tina"}, "cameras":[{"id":"left"},{"id":"right"}],
        "futureExtension":{"schema":2}});
    let (client, server) = fixture("200 OK", payload.clone()).await;
    assert_eq!(
        serde_json::from_str::<Value>(&client.manifest_json().await.unwrap()).unwrap(),
        payload
    );
    server.await.unwrap();
}

#[tokio::test]
async fn removable_target_maps_to_existing_wire_slot() {
    let payload = json!({"target":"usb", "usb":{"canCapture":true,"mediaType":"sd"}});
    let (client, server) = fixture("200 OK", payload.clone()).await;
    let storage = client
        .configure_storage_json(StorageTarget::Removable)
        .await
        .unwrap();
    assert_eq!(serde_json::from_str::<Value>(&storage).unwrap(), payload);
    let request = server.await.unwrap();
    let body: Value = serde_json::from_str(request.split_once("\r\n\r\n").unwrap().1).unwrap();
    assert_eq!(body, json!({"target":"usb","claimCode":"123456"}));
}

#[tokio::test]
async fn structured_device_error_preserves_all_fields() {
    let (client, server) = fixture(
        "409 Conflict",
        json!({"error":"storage.not_ready", "message":"SD card unavailable"}),
    )
    .await;
    let SdkError::Failure {
        kind,
        detail,
        http_status,
        device_code,
        outcome_unknown,
    } = client.start_capture("test".into()).await.unwrap_err();
    assert_eq!(kind, SdkErrorKind::Device);
    assert_eq!(detail, "SD card unavailable");
    assert_eq!(http_status, Some(409));
    assert_eq!(device_code.as_deref(), Some("storage.not_ready"));
    assert!(!outcome_unknown);
    server.await.unwrap();
}

#[tokio::test]
async fn lost_start_response_keeps_outcome_unknown_and_never_retries() {
    let (client, server) = fixture("", Value::Null).await;
    let SdkError::Failure {
        kind,
        outcome_unknown,
        ..
    } = client.start_capture("test".into()).await.unwrap_err();
    assert_eq!(kind, SdkErrorKind::Transport);
    assert!(outcome_unknown);
    server.await.unwrap();
}

#[tokio::test]
async fn sessions_keep_unavailable_and_extension_fields() {
    let session = json!({"id":"ses_bad", "name":"Old session", "sizeBytes":0,
        "durationMs":null, "status":"failed", "exportAvailable":false,
        "failureReason":"empty video", "fileCount":2});
    let (client, server) = fixture("200 OK", json!({"sessions":[session]})).await;
    let sessions = client.sessions().await.unwrap();
    assert_eq!(sessions.len(), 1);
    assert_eq!(sessions[0].size_bytes, Some(0));
    assert_eq!(sessions[0].duration_ms, None);
    assert_eq!(sessions[0].export_available, Some(false));
    assert_eq!(sessions[0].failure_reason.as_deref(), Some("empty video"));
    assert_eq!(
        serde_json::from_str::<Value>(&sessions[0].raw_json).unwrap()["fileCount"],
        2
    );
    server.await.unwrap();
}

#[tokio::test]
async fn stop_maps_nested_session() {
    let payload = json!({"captureId":"cap_test", "sessionId":"ses_test", "state":"completed",
        "session":{"id":"ses_test", "sizeBytes":1984, "custom":"original"},
        "validation":"transport"});
    let (client, server) = fixture("200 OK", payload.clone()).await;
    let stopped = client.stop_capture("cap_test".into()).await.unwrap();
    assert_eq!(stopped.state, "completed");
    assert_eq!(stopped.session.as_ref().unwrap().size_bytes, Some(1984));
    assert_eq!(
        serde_json::from_str::<Value>(&stopped.raw_json).unwrap(),
        payload
    );
    server.await.unwrap();
}

#[tokio::test]
async fn pre_cancelled_export_performs_no_io() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let client = DeviceClient::new(format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let cancellation = Cancellation::new();
    assert!(!cancellation.is_cancelled());
    cancellation.cancel();
    cancellation.cancel();
    assert!(cancellation.is_cancelled());
    let dir = tempfile::tempdir().unwrap();
    let destination = dir.path().join("must-not-be-created");
    let error = client
        .export_session(
            "ses_test".into(),
            destination.to_string_lossy().into(),
            Some(cancellation),
        )
        .await
        .unwrap_err();
    assert_eq!(kind(error), SdkErrorKind::Cancelled);
    assert!(!destination.exists());
    assert!(
        tokio::time::timeout(Duration::from_millis(80), listener.accept())
            .await
            .is_err()
    );
}

#[tokio::test]
async fn cancellation_interrupts_pending_export_request() {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let client = DeviceClient::new(format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let cancellation = Cancellation::new();
    let worker_token = Arc::clone(&cancellation);
    let dir = tempfile::tempdir().unwrap();
    let destination = dir.path().to_string_lossy().into_owned();
    let export = tokio::spawn(async move {
        client
            .export_session("ses_test".into(), destination, Some(worker_token))
            .await
    });
    let (mut socket, _) = tokio::time::timeout(Duration::from_secs(3), listener.accept())
        .await
        .unwrap()
        .unwrap();
    let mut buffer = [0u8; 1024];
    let size = socket.read(&mut buffer).await.unwrap();
    assert!(String::from_utf8_lossy(&buffer[..size])
        .starts_with("POST /v1/sessions/ses_test/prepare-export "));
    cancellation.cancel();
    let error = tokio::time::timeout(Duration::from_secs(1), export)
        .await
        .unwrap()
        .unwrap()
        .unwrap_err();
    assert_eq!(kind(error), SdkErrorKind::Cancelled);
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
}

#[tokio::test]
async fn destinations_require_absolute_local_paths_before_io() {
    let client = DeviceClient::new("http://127.0.0.1:1".into()).unwrap();
    for destination in [
        "",
        "   ",
        "relative/export",
        "content://documents/42",
        "file:///tmp/export",
        "/tmp/a\0b",
    ] {
        let error = client
            .export_session("ses_test".into(), destination.into(), None)
            .await
            .unwrap_err();
        assert_eq!(kind(error), SdkErrorKind::InvalidInput, "{destination:?}");
    }
}
