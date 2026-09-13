use serde_json::{json, Value};
use std::time::Duration;
use syncap_core::{Client, ErrorKind};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    task::JoinHandle,
};

async fn fixture(status: &str, payload: &[u8]) -> (Client, JoinHandle<String>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let client = Client::new(&format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let status = status.to_owned();
    let payload = payload.to_vec();
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
            if let Some(index) = bytes.windows(4).position(|x| x == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&bytes[..index]).to_ascii_lowercase();
                let length: usize = headers
                    .lines()
                    .find_map(|line| line.strip_prefix("content-length:"))
                    .map(|n| n.trim().parse().unwrap())
                    .unwrap_or(0);
                if bytes.len() >= index + 4 + length {
                    break;
                }
            }
        }
        if !status.is_empty() {
            let headers = format!("HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", payload.len());
            socket.write_all(headers.as_bytes()).await.unwrap();
            socket.write_all(&payload).await.unwrap();
        }
        socket.shutdown().await.unwrap();
        // A lost mutating response must never cause a second request.
        assert!(
            tokio::time::timeout(Duration::from_millis(80), listener.accept())
                .await
                .is_err()
        );
        String::from_utf8(bytes).unwrap()
    });
    (client, server)
}

#[test]
fn endpoints_reject_ambiguous_or_credentialed_urls() {
    for endpoint in [
        "",
        "192.168.1.12",
        "file:///tmp/device",
        "http://user:secret@localhost",
        "http://localhost/api",
        "http://localhost/?token=secret",
        "http://localhost/#fragment",
        "http://localhost:0",
    ] {
        let error = Client::new(endpoint).err().expect(endpoint);
        assert_eq!(error.kind, ErrorKind::InvalidInput);
        assert!(!error.to_string().contains("secret"));
    }
    for endpoint in [
        "http://127.0.0.1:8080",
        "https://device.local/",
        "http://[::1]:8080/",
    ] {
        assert!(Client::new(endpoint).is_ok());
    }
}

#[tokio::test]
async fn invalid_inputs_are_rejected_before_io() {
    let client = Client::new("http://127.0.0.1:1").unwrap();
    assert_eq!(
        client.start_capture("  ").await.unwrap_err().kind,
        ErrorKind::InvalidInput
    );
    assert_eq!(
        client
            .start_capture(&"中".repeat(33))
            .await
            .unwrap_err()
            .kind,
        ErrorKind::InvalidInput
    );
    assert_eq!(
        client.configure_storage("computer").await.unwrap_err().kind,
        ErrorKind::InvalidInput
    );
    assert_eq!(
        client
            .configure_camera_orientation(45)
            .await
            .unwrap_err()
            .kind,
        ErrorKind::InvalidInput
    );
    for id in ["cap_", "cap_a/stop", "cap_a?x=1", "cap_../x", "ses_a"] {
        assert_eq!(
            client.stop_capture(id).await.unwrap_err().kind,
            ErrorKind::InvalidInput
        );
    }
    assert_eq!(
        client.prepare_export("ses_../a").await.unwrap_err().kind,
        ErrorKind::InvalidInput
    );
}

#[tokio::test]
async fn capture_preserves_device_clock_and_extension_fields() {
    let payload = json!({"captureId":"cap_a", "sessionId":"ses_a", "state":"recording", "elapsedMs":0,
        "startedAtDeviceTimeNs":"3689123456789012345", "vendor":{"feature":true}});
    let (client, server) = fixture("200 OK", &serde_json::to_vec(&payload).unwrap()).await;
    let result = client.start_capture("SDK test").await.unwrap();
    assert_eq!(result.elapsed_ms, Some(0));
    assert_eq!(
        result.started_at_device_time_ns.as_deref(),
        Some("3689123456789012345")
    );
    assert_eq!(serde_json::to_value(result).unwrap(), payload);
    let request = server.await.unwrap();
    assert!(request.starts_with("POST /v1/captures/start HTTP/1.1"));
    let body: Value = serde_json::from_str(request.split_once("\r\n\r\n").unwrap().1).unwrap();
    assert_eq!(body, json!({"name":"SDK test"}));
}

#[tokio::test]
async fn quarantined_device_remains_failed_not_idle() {
    let payload = json!({"state":"failed", "recoveryRequired":true,"rebootRequired":true,"failureReason":"incomplete segment"});
    let (client, server) = fixture("200 OK", &serde_json::to_vec(&payload).unwrap()).await;
    let result = client.current_capture().await.unwrap();
    assert_eq!(result.state, "failed");
    assert_eq!(serde_json::to_value(result).unwrap(), payload);
    assert!(server
        .await
        .unwrap()
        .starts_with("GET /v1/captures/current "));
}

#[tokio::test]
async fn device_errors_preserve_code_status_and_message() {
    let (client, server) = fixture(
        "409 Conflict",
        br#"{"error":"storage.low_space","message":"Not enough space"}"#,
    )
    .await;
    let error = client.start_capture("Test").await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::Device);
    assert_eq!(error.http_status, Some(409));
    assert_eq!(error.device_code.as_deref(), Some("storage.low_space"));
    assert_eq!(error.message, "Not enough space");
    assert!(!error.outcome_unknown);
    server.await.unwrap();
}

#[tokio::test]
async fn lost_start_response_is_unknown_and_never_retried() {
    let (client, server) = fixture("", b"").await;
    let error = client.start_capture("Test").await.unwrap_err();
    assert!(error.outcome_unknown);
    assert_eq!(error.kind, ErrorKind::Transport);
    server.await.unwrap();
}

#[tokio::test]
async fn malformed_success_is_not_a_successful_capture() {
    for payload in [b"null".as_slice(), b"[]", b"{}", b"bad json"] {
        let (client, server) = fixture("200 OK", payload).await;
        let error = client.start_capture("Test").await.unwrap_err();
        assert_eq!(error.kind, ErrorKind::Protocol);
        assert!(error.outcome_unknown);
        server.await.unwrap();
    }
}

#[tokio::test]
async fn storage_requires_matching_ready_target() {
    for payload in [
        json!({"target":"usb","canCapture":true}),
        json!({"target":"usb","usb":{"canCapture":true}}),
    ] {
        let (client, server) = fixture("200 OK", &serde_json::to_vec(&payload).unwrap()).await;
        assert_eq!(client.configure_storage("usb").await.unwrap(), payload);
        let request = server.await.unwrap();
        assert!(request.starts_with("POST /v1/storage/configure "));
        let body: Value = serde_json::from_str(request.split_once("\r\n\r\n").unwrap().1).unwrap();
        assert_eq!(body, json!({"target":"usb","claimCode":"123456"}));
    }
    for payload in [
        json!({"target":"usb","canCapture":false}),
        json!({"target":"usb"}),
    ] {
        let (client, server) = fixture("200 OK", &serde_json::to_vec(&payload).unwrap()).await;
        let error = client.configure_storage("usb").await.unwrap_err();
        assert_eq!(error.device_code.as_deref(), Some("storage.not_ready"));
        server.await.unwrap();
    }
    let (client, server) = fixture("200 OK", br#"{"target":"internal","canCapture":true}"#).await;
    let error = client.configure_storage("usb").await.unwrap_err();
    assert_eq!(error.kind, ErrorKind::Protocol);
    assert!(error.outcome_unknown);
    server.await.unwrap();
}

#[tokio::test]
async fn sessions_keep_incomplete_data_unexportable() {
    let payload = json!({"sessions":[{"id":"ses_failed","status":"failed","sizeBytes":0,"durationMs":null,"exportAvailable":false,"failureReason":"zero byte segment","vendor":9}]});
    let (client, server) = fixture("200 OK", &serde_json::to_vec(&payload).unwrap()).await;
    let result = client.sessions().await.unwrap();
    assert_eq!(result[0].size_bytes, Some(0));
    assert_eq!(result[0].export_available, Some(false));
    assert_eq!(result[0].extra.get("vendor"), Some(&json!(9)));
    server.await.unwrap();
}

#[tokio::test]
async fn redirects_are_not_followed() {
    // Even an endpoint that tries to redirect never supplies a valid manifest.
    let (client, server) = fixture(
        "302 Found\r\nLocation: http://127.0.0.1:1/redirect",
        br#"{"message":"Moved"}"#,
    )
    .await;
    let error = client.manifest().await.unwrap_err();
    assert_eq!(error.http_status, Some(302));
    server.await.unwrap();
}
