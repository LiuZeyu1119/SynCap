use std::time::Duration;

use serde_json::{json, Value};
use syncap_core::{Client, ErrorKind};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt},
    net::TcpListener,
    task::JoinHandle,
};

async fn fixture(payload: Value) -> (Client, JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let client = Client::new(&format!("http://{}", listener.local_addr().unwrap())).unwrap();
    let server = tokio::spawn(async move {
        let (mut socket, _) = tokio::time::timeout(Duration::from_secs(3), listener.accept())
            .await
            .unwrap()
            .unwrap();
        let mut request = Vec::new();
        loop {
            let mut buffer = [0; 2048];
            let count = socket.read(&mut buffer).await.unwrap();
            assert!(count > 0);
            request.extend_from_slice(&buffer[..count]);
            if let Some(index) = request.windows(4).position(|bytes| bytes == b"\r\n\r\n") {
                let headers = String::from_utf8_lossy(&request[..index]).to_ascii_lowercase();
                let length = headers
                    .lines()
                    .find_map(|line| line.strip_prefix("content-length:"))
                    .map(|value| value.trim().parse::<usize>().unwrap())
                    .unwrap_or(0);
                if request.len() >= index + 4 + length {
                    break;
                }
            }
        }
        let payload = payload.to_string();
        let response = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{payload}",
            payload.len()
        );
        socket.write_all(response.as_bytes()).await.unwrap();
        socket.shutdown().await.unwrap();
        assert!(
            tokio::time::timeout(Duration::from_millis(80), listener.accept())
                .await
                .is_err(),
            "Mutating request was unexpectedly retried"
        );
    });
    (client, server)
}

#[tokio::test]
async fn start_accepts_only_a_confirmed_recording_with_valid_identifiers() {
    let payload = json!({
        "captureId": "cap_test", "sessionId": "ses_test", "state": "recording",
        "startedAtDeviceTimeNs": "18446744073709551615", "elapsedMs": 0,
        "storage": { "target": "usb", "mediaType": "sd" }
    });
    let (client, server) = fixture(payload.clone()).await;
    let capture = client.start_capture("Test").await.unwrap();
    assert_eq!(serde_json::to_value(capture).unwrap(), payload);
    server.await.unwrap();
}

#[tokio::test]
async fn start_rejects_failed_pending_idle_and_unknown_states_without_retrying() {
    for state in [
        "failed",
        "idle",
        "starting",
        "completed",
        "",
        "future_state",
    ] {
        let (client, server) = fixture(json!({
            "captureId": "cap_test", "sessionId": "ses_test", "state": state
        }))
        .await;
        let error = client.start_capture("Test").await.unwrap_err();
        assert_eq!(error.kind, ErrorKind::Protocol, "{state}");
        assert!(error.outcome_unknown, "{state}");
        server.await.unwrap();
    }
}

#[tokio::test]
async fn start_rejects_empty_malformed_and_wrong_kind_identifiers() {
    for (capture_id, session_id) in [
        ("", "ses_test"),
        ("cap_", "ses_test"),
        ("ses_test", "ses_test"),
        ("cap_a/other", "ses_test"),
        ("cap_test", ""),
        ("cap_test", "ses_"),
        ("cap_test", "cap_test"),
        ("cap_test", "ses_a?other"),
    ] {
        let (client, server) = fixture(json!({
            "captureId": capture_id, "sessionId": session_id, "state": "recording"
        }))
        .await;
        let error = client.start_capture("Test").await.unwrap_err();
        assert_eq!(error.kind, ErrorKind::Protocol);
        assert!(error.outcome_unknown);
        server.await.unwrap();
    }
}

#[tokio::test]
async fn stop_accepts_matching_completed_capture_with_or_without_session_summary() {
    for session in [
        None,
        Some(json!({"id": "ses_test", "status": "complete", "sizeBytes": 1234})),
    ] {
        let mut payload = json!({
            "captureId": "cap_test", "sessionId": "ses_test", "state": "completed"
        });
        if let Some(session) = session {
            payload["session"] = session;
        }
        let (client, server) = fixture(payload.clone()).await;
        let capture = client.stop_capture("cap_test").await.unwrap();
        assert_eq!(serde_json::to_value(capture).unwrap(), payload);
        server.await.unwrap();
    }
}

#[tokio::test]
async fn stop_rejects_noncompleted_states_without_retrying() {
    for state in [
        "recording",
        "finalizing",
        "failed",
        "idle",
        "complete",
        "",
        "future_state",
    ] {
        let (client, server) = fixture(json!({
            "captureId": "cap_test", "sessionId": "ses_test", "state": state
        }))
        .await;
        let error = client.stop_capture("cap_test").await.unwrap_err();
        assert_eq!(error.kind, ErrorKind::Protocol, "{state}");
        assert!(error.outcome_unknown, "{state}");
        server.await.unwrap();
    }
}

#[tokio::test]
async fn stop_rejects_mismatched_and_malformed_identifiers() {
    for (capture_id, session_id) in [
        ("cap_other", "ses_test"),
        ("", "ses_test"),
        ("ses_test", "ses_test"),
        ("cap_test", ""),
        ("cap_test", "ses_"),
        ("cap_test", "cap_test"),
        ("cap_test", "ses_a/other"),
    ] {
        let (client, server) = fixture(json!({
            "captureId": capture_id, "sessionId": session_id, "state": "completed"
        }))
        .await;
        let error = client.stop_capture("cap_test").await.unwrap_err();
        assert_eq!(error.kind, ErrorKind::Protocol);
        assert!(error.outcome_unknown);
        server.await.unwrap();
    }
}

#[tokio::test]
async fn stop_rejects_a_summary_from_another_session() {
    for session_id in ["ses_other", "", "ses_"] {
        let (client, server) = fixture(json!({
            "captureId": "cap_test", "sessionId": "ses_test", "state": "completed",
            "session": { "id": session_id, "status": "complete" }
        }))
        .await;
        let error = client.stop_capture("cap_test").await.unwrap_err();
        assert_eq!(error.kind, ErrorKind::Protocol);
        assert!(error.outcome_unknown);
        server.await.unwrap();
    }
}
