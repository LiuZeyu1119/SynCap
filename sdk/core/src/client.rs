use crate::{CaptureStart, CaptureState, CaptureStop, Error, ErrorKind, Result, Session};
use reqwest::{redirect::Policy, Method};
use serde::de::DeserializeOwned;
use serde_json::{json, Value};
use std::time::Duration;
use url::Url;

const READ_TIMEOUT: Duration = Duration::from_millis(2500);
const MAX_JSON_BYTES: usize = 4 * 1024 * 1024;

/// A client for one explicitly selected device endpoint, e.g.
/// `http://192.168.1.12:8080`. No UI, discovery, recorder or automatic retries.
#[derive(Clone)]
pub struct Client {
    pub(crate) http: reqwest::Client,
    pub(crate) base: Url,
}

impl Client {
    pub fn new(endpoint: &str) -> Result<Self> {
        let base = Url::parse(endpoint)
            .map_err(|_| Error::new(ErrorKind::InvalidInput, "Invalid device endpoint"))?;
        if !matches!(base.scheme(), "http" | "https")
            || base.host_str().is_none()
            || !base.username().is_empty()
            || base.password().is_some()
            || base.query().is_some()
            || base.fragment().is_some()
            || base.path() != "/"
            || base.port_or_known_default() == Some(0)
        {
            return Err(Error::new(
                ErrorKind::InvalidInput,
                "Endpoint must be an HTTP(S) origin without credentials, path or query",
            ));
        }
        let http = reqwest::Client::builder()
            .connect_timeout(Duration::from_millis(1500))
            .redirect(Policy::none())
            // Device traffic must not leave the local network via a host proxy.
            .no_proxy()
            .build()?;
        Ok(Self { http, base })
    }

    pub(crate) fn endpoint(&self, path: &str) -> Result<Url> {
        if !path.starts_with("/v1/") || path.contains("..") || path.contains(['?', '#', '%', '\\'])
        {
            return Err(Error::new(
                ErrorKind::InvalidInput,
                "Invalid device API path",
            ));
        }
        self.base
            .join(path)
            .map_err(|_| Error::new(ErrorKind::InvalidInput, "Invalid device API path"))
    }

    async fn request<T: DeserializeOwned>(
        &self,
        method: Method,
        path: &str,
        body: Option<Value>,
        timeout: Duration,
    ) -> Result<T> {
        let mutating = method != Method::GET;
        let mut request = self
            .http
            .request(method, self.endpoint(path)?)
            .timeout(timeout);
        if let Some(body) = body {
            request = request.json(&body);
        }
        let result = async {
            let mut response = request.send().await?;
            let status = response.status();
            let mut bytes = Vec::new();
            while let Some(chunk) = response.chunk().await? {
                if bytes.len().saturating_add(chunk.len()) > MAX_JSON_BYTES {
                    return Err(Error::new(
                        ErrorKind::Protocol,
                        "Device response exceeds the JSON size limit",
                    ));
                }
                bytes.extend_from_slice(&chunk);
            }
            if !status.is_success() {
                let payload: Value = serde_json::from_slice(&bytes).unwrap_or(Value::Null);
                let mut error = Error::new(
                    ErrorKind::Device,
                    payload
                        .get("message")
                        .and_then(Value::as_str)
                        .unwrap_or("Device request failed"),
                );
                error.http_status = Some(status.as_u16());
                error.device_code = payload
                    .get("error")
                    .and_then(Value::as_str)
                    .map(str::to_owned);
                error.outcome_unknown = mutating && status.is_server_error();
                return Err(error);
            }
            let value: Value = serde_json::from_slice(&bytes)?;
            if !value.is_object() {
                return Err(Error::new(
                    ErrorKind::Protocol,
                    "Device response must be a JSON object",
                ));
            }
            serde_json::from_value(value).map_err(Error::from)
        }
        .await;
        result.map_err(|mut error: Error| {
            if mutating
                && matches!(
                    error.kind,
                    ErrorKind::Transport | ErrorKind::Timeout | ErrorKind::Protocol
                )
            {
                error.outcome_unknown = true;
            }
            error
        })
    }

    pub async fn manifest(&self) -> Result<Value> {
        self.request(Method::GET, "/v1/manifest", None, READ_TIMEOUT)
            .await
    }

    pub async fn status(&self) -> Result<Value> {
        self.request(Method::GET, "/v1/status", None, READ_TIMEOUT)
            .await
    }

    pub async fn storage(&self) -> Result<Value> {
        self.request(Method::GET, "/v1/storage", None, READ_TIMEOUT)
            .await
    }

    /// `usb` is the existing wire slot for removable media, including Tina SD.
    /// The storage response's mediaType/displayName identify the real medium.
    pub async fn configure_storage(&self, target: &str) -> Result<Value> {
        if !matches!(target, "internal" | "usb") {
            return Err(Error::new(
                ErrorKind::InvalidInput,
                "Invalid storage target",
            ));
        }
        let storage: Value = self
            .request(
                Method::POST,
                "/v1/storage/configure",
                Some(json!({"target": target, "claimCode": "123456"})),
                Duration::from_secs(15),
            )
            .await?;
        if storage.get("target").and_then(Value::as_str) != Some(target) {
            let mut error = Error::new(
                ErrorKind::Protocol,
                "Device did not confirm the selected storage target",
            );
            error.outcome_unknown = true;
            return Err(error);
        }
        let can_capture = storage
            .get(target)
            .and_then(|slot| slot.get("canCapture"))
            .or_else(|| storage.get("canCapture"))
            .and_then(Value::as_bool);
        if can_capture != Some(true) {
            let reason = storage
                .get(target)
                .and_then(|slot| slot.get("reason"))
                .and_then(Value::as_str)
                .unwrap_or("Selected storage is not ready for capture");
            let mut error = Error::new(ErrorKind::Device, reason);
            error.device_code = Some("storage.not_ready".into());
            return Err(error);
        }
        Ok(storage)
    }

    pub async fn camera_configuration(&self) -> Result<Value> {
        self.request(Method::GET, "/v1/camera/configuration", None, READ_TIMEOUT)
            .await
    }

    pub async fn configure_camera_orientation(&self, degrees: u16) -> Result<Value> {
        if !matches!(degrees, 0 | 90 | 180 | 270) {
            return Err(Error::new(
                ErrorKind::InvalidInput,
                "Invalid camera rotation",
            ));
        }
        self.request(
            Method::POST,
            "/v1/camera/configuration",
            Some(json!({"rotationDegrees": degrees, "claimCode": "123456"})),
            Duration::from_secs(15),
        )
        .await
    }

    /// Never retries. Query current_capture if an error has outcome_unknown.
    pub async fn start_capture(&self, name: &str) -> Result<CaptureStart> {
        if name.trim().is_empty() || name.len() > 96 {
            return Err(Error::new(ErrorKind::InvalidInput, "Invalid session name"));
        }
        let capture: CaptureStart = self
            .request(
                Method::POST,
                "/v1/captures/start",
                Some(json!({"name": name})),
                Duration::from_secs(60),
            )
            .await?;
        if capture.state != "recording"
            || validate_identifier(&capture.capture_id, "cap_").is_err()
            || validate_identifier(&capture.session_id, "ses_").is_err()
        {
            let mut error = Error::new(
                ErrorKind::Protocol,
                "Device did not confirm a valid recording capture",
            );
            error.outcome_unknown = true;
            return Err(error);
        }
        Ok(capture)
    }

    pub async fn current_capture(&self) -> Result<CaptureState> {
        self.request(Method::GET, "/v1/captures/current", None, READ_TIMEOUT)
            .await
    }

    pub async fn stop_capture(&self, capture_id: &str) -> Result<CaptureStop> {
        validate_identifier(capture_id, "cap_")?;
        let capture: CaptureStop = self
            .request(
                Method::POST,
                &format!("/v1/captures/{capture_id}/stop"),
                Some(json!({})),
                Duration::from_secs(60),
            )
            .await?;
        if capture.state != "completed"
            || capture.capture_id != capture_id
            || validate_identifier(&capture.session_id, "ses_").is_err()
            || capture
                .session
                .as_ref()
                .is_some_and(|session| session.id != capture.session_id)
        {
            let mut error = Error::new(
                ErrorKind::Protocol,
                "Device did not confirm completion of the requested capture",
            );
            error.outcome_unknown = true;
            return Err(error);
        }
        Ok(capture)
    }

    pub async fn sessions(&self) -> Result<Vec<Session>> {
        #[derive(serde::Deserialize)]
        struct Sessions {
            sessions: Vec<Session>,
        }
        let result: Sessions = self
            .request(Method::GET, "/v1/sessions", None, Duration::from_secs(8))
            .await?;
        Ok(result.sessions)
    }

    pub async fn prepare_export(&self, session_id: &str) -> Result<Value> {
        validate_identifier(session_id, "ses_")?;
        self.request(
            Method::POST,
            &format!("/v1/sessions/{session_id}/prepare-export"),
            Some(json!({})),
            Duration::from_secs(30),
        )
        .await
    }
}

pub(crate) fn validate_identifier(value: &str, prefix: &str) -> Result<()> {
    if value.starts_with(prefix)
        && value.len() > prefix.len()
        && value.len() <= 95
        && value
            .bytes()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == b'_')
    {
        Ok(())
    } else {
        Err(Error::new(
            ErrorKind::InvalidInput,
            "Invalid capture or session identifier",
        ))
    }
}
