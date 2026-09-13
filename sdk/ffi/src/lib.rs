//! Mobile bindings only: all device protocol and export logic stays in core.
//! Export cancellation is explicit and does not stop a device recording.

mod models;

pub use models::{CaptureStart, CaptureState, CaptureStop, ExportReport, Session, StorageTarget};
use std::{fmt, path::Path, sync::Arc};
use tokio_util::sync::CancellationToken;

uniffi::setup_scaffolding!();

#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Enum)]
pub enum SdkErrorKind {
    InvalidInput,
    Transport,
    Timeout,
    Device,
    Protocol,
    Integrity,
    Io,
    Cancelled,
}

#[derive(Debug, Clone, uniffi::Error)]
pub enum SdkError {
    Failure {
        kind: SdkErrorKind,
        // `message` conflicts with Kotlin Throwable.message in generated errors.
        detail: String,
        http_status: Option<u16>,
        device_code: Option<String>,
        outcome_unknown: bool,
    },
}

impl fmt::Display for SdkError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let Self::Failure {
            detail,
            http_status,
            device_code,
            ..
        } = self;
        if let Some(status) = http_status {
            write!(f, "Device returned HTTP {status}: ")?;
        }
        if let Some(code) = device_code {
            write!(f, "{code}: ")?;
        }
        f.write_str(detail)
    }
}

impl std::error::Error for SdkError {}

impl From<syncap_core::Error> for SdkError {
    fn from(value: syncap_core::Error) -> Self {
        use syncap_core::ErrorKind;
        Self::Failure {
            kind: match value.kind {
                ErrorKind::InvalidInput => SdkErrorKind::InvalidInput,
                ErrorKind::Transport => SdkErrorKind::Transport,
                ErrorKind::Timeout => SdkErrorKind::Timeout,
                ErrorKind::Device => SdkErrorKind::Device,
                ErrorKind::Protocol => SdkErrorKind::Protocol,
                ErrorKind::Integrity => SdkErrorKind::Integrity,
                ErrorKind::Io => SdkErrorKind::Io,
                ErrorKind::Cancelled => SdkErrorKind::Cancelled,
            },
            detail: value.message,
            http_status: value.http_status,
            device_code: value.device_code,
            outcome_unknown: value.outcome_unknown,
        }
    }
}

/// One-shot cancellation token. Create a new token for each export attempt.
#[derive(uniffi::Object)]
pub struct Cancellation {
    token: CancellationToken,
}

#[uniffi::export]
impl Cancellation {
    #[uniffi::constructor]
    pub fn new() -> Arc<Self> {
        Arc::new(Self {
            token: CancellationToken::new(),
        })
    }

    pub fn cancel(&self) {
        self.token.cancel();
    }

    pub fn is_cancelled(&self) -> bool {
        self.token.is_cancelled()
    }
}

#[derive(uniffi::Object)]
pub struct DeviceClient {
    client: syncap_core::Client,
}

#[uniffi::export]
impl DeviceClient {
    #[uniffi::constructor]
    pub fn new(endpoint: String) -> Result<Arc<Self>, SdkError> {
        Ok(Arc::new(Self {
            client: syncap_core::Client::new(&endpoint)?,
        }))
    }
}

#[uniffi::export(async_runtime = "tokio")]
impl DeviceClient {
    pub async fn manifest_json(&self) -> Result<String, SdkError> {
        Ok(self.client.manifest().await?.to_string())
    }

    pub async fn status_json(&self) -> Result<String, SdkError> {
        Ok(self.client.status().await?.to_string())
    }

    pub async fn storage_json(&self) -> Result<String, SdkError> {
        Ok(self.client.storage().await?.to_string())
    }

    pub async fn configure_storage_json(&self, target: StorageTarget) -> Result<String, SdkError> {
        let target = match target {
            StorageTarget::Internal => "internal",
            StorageTarget::Removable => "usb",
        };
        Ok(self.client.configure_storage(target).await?.to_string())
    }

    pub async fn camera_configuration_json(&self) -> Result<String, SdkError> {
        Ok(self.client.camera_configuration().await?.to_string())
    }

    pub async fn configure_camera_orientation_json(
        &self,
        rotation_degrees: u16,
    ) -> Result<String, SdkError> {
        Ok(self
            .client
            .configure_camera_orientation(rotation_degrees)
            .await?
            .to_string())
    }

    /// The caller must separately configure and confirm storage before starting.
    /// This method sends only the start request and never retries.
    pub async fn start_capture(&self, name: String) -> Result<CaptureStart, SdkError> {
        self.client.start_capture(&name).await?.try_into()
    }

    pub async fn current_capture(&self) -> Result<CaptureState, SdkError> {
        self.client.current_capture().await?.try_into()
    }

    pub async fn stop_capture(&self, capture_id: String) -> Result<CaptureStop, SdkError> {
        self.client.stop_capture(&capture_id).await?.try_into()
    }

    pub async fn sessions(&self) -> Result<Vec<Session>, SdkError> {
        self.client
            .sessions()
            .await?
            .into_iter()
            .map(Session::try_from)
            .collect()
    }

    pub async fn prepare_export_json(&self, session_id: String) -> Result<String, SdkError> {
        Ok(self.client.prepare_export(&session_id).await?.to_string())
    }

    /// Destination is an absolute, app-owned local directory path, not a
    /// content:// or file:// URI. Cancel never stops the camera. It retains
    /// partials or final files, and pending disk work may finish while holding
    /// the export lock; cancellation is not a synchronous disk-stop barrier.
    pub async fn export_session(
        &self,
        session_id: String,
        destination: String,
        cancellation: Option<Arc<Cancellation>>,
    ) -> Result<ExportReport, SdkError> {
        if !Path::new(&destination).is_absolute() || destination.contains('\0') {
            return Err(syncap_core::Error::new(
                syncap_core::ErrorKind::InvalidInput,
                "Export destination must be an absolute local directory path, not a URI",
            )
            .into());
        }
        let export = self
            .client
            .export_session(&session_id, Path::new(&destination));
        if let Some(cancellation) = cancellation {
            tokio::select! {
                biased;
                _ = cancellation.token.cancelled() => Err(syncap_core::Error::new(
                    syncap_core::ErrorKind::Cancelled,
                    "Export cancelled; completed files and partials are retained",
                ).into()),
                result = export => Ok(result?.into()),
            }
        } else {
            Ok(export.await?.into())
        }
    }
}
