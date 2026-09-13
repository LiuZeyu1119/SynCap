use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::process::{Command, Output};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread::{self, JoinHandle};
use std::time::Duration;

use serde_json::{json, Value};

struct MockDevice {
    endpoint: String,
    stop: Arc<AtomicBool>,
    requests: Arc<Mutex<Vec<(String, Value)>>>,
    worker: Option<JoinHandle<()>>,
}

impl MockDevice {
    fn new(responses: Vec<(u16, Value)>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let endpoint = format!("http://{}", listener.local_addr().unwrap());
        listener.set_nonblocking(true).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let server_stop = stop.clone();
        let server_requests = requests.clone();
        let worker = thread::spawn(move || {
            while !server_stop.load(Ordering::SeqCst) {
                match listener.accept() {
                    Ok((mut stream, _)) => {
                        // Accepted sockets inherit nonblocking mode on macOS.
                        stream.set_nonblocking(false).unwrap();
                        stream
                            .set_read_timeout(Some(Duration::from_secs(2)))
                            .unwrap();
                        stream
                            .set_write_timeout(Some(Duration::from_secs(2)))
                            .unwrap();
                        let request = read_request(&mut stream);
                        let index = {
                            let mut requests = server_requests.lock().unwrap();
                            requests.push(request);
                            requests.len() - 1
                        };
                        let (status, body) = responses.get(index).cloned().unwrap_or((
                            500,
                            json!({ "error": "unexpected_request", "message": "Unexpected request" }),
                        ));
                        let body = body.to_string();
                        let response = format!(
                            "HTTP/1.1 {status} Test\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                            body.len()
                        );
                        stream.write_all(response.as_bytes()).unwrap();
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(5));
                    }
                    Err(error) => panic!("Mock server failed: {error}"),
                }
            }
        });
        Self {
            endpoint,
            stop,
            requests,
            worker: Some(worker),
        }
    }

    fn run(&self, args: &[&str]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_syncap"))
            .args(["--endpoint", self.endpoint.as_str()])
            .args(args)
            .output()
            .unwrap()
    }

    fn finish(mut self) -> Vec<(String, Value)> {
        self.stop.store(true, Ordering::SeqCst);
        self.worker.take().unwrap().join().unwrap();
        self.requests.lock().unwrap().clone()
    }
}

impl Drop for MockDevice {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::SeqCst);
        if let Some(worker) = self.worker.take() {
            let _ = worker.join();
        }
    }
}

fn read_request(stream: &mut TcpStream) -> (String, Value) {
    let mut bytes = Vec::new();
    let header_end = loop {
        let mut chunk = [0; 1024];
        let count = stream.read(&mut chunk).unwrap();
        assert!(count > 0, "Connection closed before HTTP headers");
        bytes.extend_from_slice(&chunk[..count]);
        assert!(bytes.len() < 64 * 1024, "Unexpectedly large request");
        if let Some(index) = bytes.windows(4).position(|part| part == b"\r\n\r\n") {
            break index + 4;
        }
    };
    let headers = String::from_utf8(bytes[..header_end].to_vec()).unwrap();
    let length = headers
        .lines()
        .find_map(|line| {
            let (name, value) = line.split_once(':')?;
            name.eq_ignore_ascii_case("content-length")
                .then(|| value.trim().parse::<usize>().unwrap())
        })
        .unwrap_or(0);
    while bytes.len() < header_end + length {
        let mut chunk = [0; 1024];
        let count = stream.read(&mut chunk).unwrap();
        assert!(count > 0, "Connection closed before request body");
        bytes.extend_from_slice(&chunk[..count]);
    }
    let body = if length == 0 {
        Value::Null
    } else {
        serde_json::from_slice(&bytes[header_end..header_end + length]).unwrap()
    };
    (headers.lines().next().unwrap().to_owned(), body)
}

#[test]
fn mock_device_waits_for_a_delayed_request() {
    let device = MockDevice::new(vec![(200, json!({"state": "idle"}))]);
    let address = device.endpoint.strip_prefix("http://").unwrap();
    let mut stream = TcpStream::connect(address).unwrap();
    stream
        .set_read_timeout(Some(Duration::from_secs(3)))
        .unwrap();
    thread::sleep(Duration::from_millis(50));
    stream
        .write_all(
            b"GET /v1/captures/current HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n",
        )
        .unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    assert!(response.starts_with("HTTP/1.1 200"));
    assert_eq!(device.finish().len(), 1);
}

#[test]
fn help_is_available_without_a_device() {
    let output = Command::new(env!("CARGO_BIN_EXE_syncap"))
        .arg("--help")
        .output()
        .unwrap();
    assert!(output.status.success());
    assert!(output.stderr.is_empty());
    assert!(String::from_utf8(output.stdout)
        .unwrap()
        .contains("HTTP device control"));
}

#[test]
fn invalid_arguments_are_structured_and_do_not_echo_values() {
    let output = Command::new(env!("CARGO_BIN_EXE_syncap"))
        .args([
            "--endpoint",
            "http://localhost:1",
            "status",
            "private-secret",
        ])
        .output()
        .unwrap();
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    let error: Value = serde_json::from_slice(&output.stderr).unwrap();
    assert_eq!(error["error"]["code"], "invalid_arguments");
    assert!(!String::from_utf8(output.stderr)
        .unwrap()
        .contains("private-secret"));
}

#[test]
fn status_prints_only_machine_readable_json() {
    let expected = json!({ "deviceId": "test-tina", "state": "ready" });
    let device = MockDevice::new(vec![(200, expected.clone())]);
    let output = device.run(&["status"]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stderr.is_empty());
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap(),
        expected
    );
    assert_eq!(
        device.finish(),
        vec![("GET /v1/status HTTP/1.1".to_owned(), Value::Null)]
    );
}

#[test]
fn capture_start_configures_storage_before_starting_exactly_once() {
    let started = json!({ "captureId": "cap_test", "sessionId": "ses_test", "state": "recording" });
    let device = MockDevice::new(vec![
        (
            200,
            json!({ "target": "usb", "canCapture": true, "usb": { "canCapture": true } }),
        ),
        (200, started.clone()),
    ]);
    let output = device.run(&[
        "capture",
        "start",
        "--storage",
        "usb",
        "--name",
        "Outdoor run 1",
    ]);
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(output.stderr.is_empty());
    assert_eq!(
        serde_json::from_slice::<Value>(&output.stdout).unwrap(),
        started
    );
    let requests = device.finish();
    assert_eq!(requests.len(), 2);
    assert_eq!(requests[0].0, "POST /v1/storage/configure HTTP/1.1");
    assert_eq!(requests[0].1["target"], "usb");
    assert_eq!(
        requests[1],
        (
            "POST /v1/captures/start HTTP/1.1".to_owned(),
            json!({ "name": "Outdoor run 1" })
        )
    );
}

#[test]
fn failed_storage_configuration_prevents_capture_and_is_not_retried() {
    let device = MockDevice::new(vec![(
        409,
        json!({ "error": "storage_unavailable", "message": "Storage unavailable" }),
    )]);
    let output = device.run(&["capture", "start", "--storage", "usb", "--name", "test"]);
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    let error: Value = serde_json::from_slice(&output.stderr).unwrap();
    assert!(error.get("error").unwrap().is_object());
    let requests = device.finish();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].0, "POST /v1/storage/configure HTTP/1.1");
}

#[test]
fn unavailable_storage_in_successful_http_response_prevents_capture() {
    let device = MockDevice::new(vec![(
        200,
        json!({
            "target": "usb", "canCapture": false,
            "usb": { "canCapture": false, "reason": "not_mounted" }
        }),
    )]);
    let output = device.run(&["capture", "start", "--storage", "usb", "--name", "test"]);
    assert!(!output.status.success());
    assert!(output.stdout.is_empty());
    let error: Value = serde_json::from_slice(&output.stderr).unwrap();
    assert!(error.get("error").unwrap().is_object());
    let requests = device.finish();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0].0, "POST /v1/storage/configure HTTP/1.1");
}
