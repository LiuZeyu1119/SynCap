use crate::SdkError;

/// The removable slot uses the existing wire value `usb`, including Tina SD.
#[derive(Debug, Clone, Copy, PartialEq, Eq, uniffi::Enum)]
pub enum StorageTarget {
    Internal,
    Removable,
}

/// Device timestamps remain strings; elapsed time is supplied by the device.
#[derive(Debug, Clone, uniffi::Record)]
pub struct CaptureStart {
    pub capture_id: String,
    pub session_id: String,
    pub state: String,
    pub started_at_device_time_ns: Option<String>,
    pub elapsed_ms: Option<u64>,
    pub raw_json: String,
}

impl TryFrom<syncap_core::CaptureStart> for CaptureStart {
    type Error = SdkError;

    fn try_from(value: syncap_core::CaptureStart) -> Result<Self, Self::Error> {
        let raw_json = serde_json::to_string(&value).map_err(syncap_core::Error::from)?;
        Ok(Self {
            capture_id: value.capture_id,
            session_id: value.session_id,
            state: value.state,
            started_at_device_time_ns: value.started_at_device_time_ns,
            elapsed_ms: value.elapsed_ms,
            raw_json,
        })
    }
}

#[derive(Debug, Clone, uniffi::Record)]
pub struct CaptureState {
    pub state: String,
    pub capture_id: Option<String>,
    pub session_id: Option<String>,
    pub started_at_device_time_ns: Option<String>,
    pub elapsed_ms: Option<u64>,
    pub raw_json: String,
}

impl TryFrom<syncap_core::CaptureState> for CaptureState {
    type Error = SdkError;

    fn try_from(value: syncap_core::CaptureState) -> Result<Self, Self::Error> {
        let raw_json = serde_json::to_string(&value).map_err(syncap_core::Error::from)?;
        Ok(Self {
            state: value.state,
            capture_id: value.capture_id,
            session_id: value.session_id,
            started_at_device_time_ns: value.started_at_device_time_ns,
            elapsed_ms: value.elapsed_ms,
            raw_json,
        })
    }
}

#[derive(Debug, Clone, uniffi::Record)]
pub struct Session {
    pub id: String,
    pub name: Option<String>,
    pub duration_ms: Option<u64>,
    pub size_bytes: Option<u64>,
    pub status: Option<String>,
    pub export_available: Option<bool>,
    pub failure_reason: Option<String>,
    pub raw_json: String,
}

impl TryFrom<syncap_core::Session> for Session {
    type Error = SdkError;

    fn try_from(value: syncap_core::Session) -> Result<Self, Self::Error> {
        let raw_json = serde_json::to_string(&value).map_err(syncap_core::Error::from)?;
        Ok(Self {
            id: value.id,
            name: value.name,
            duration_ms: value.duration_ms,
            size_bytes: value.size_bytes,
            status: value.status,
            export_available: value.export_available,
            failure_reason: value.failure_reason,
            raw_json,
        })
    }
}

#[derive(Debug, Clone, uniffi::Record)]
pub struct CaptureStop {
    pub capture_id: String,
    pub session_id: String,
    pub state: String,
    pub session: Option<Session>,
    pub raw_json: String,
}

impl TryFrom<syncap_core::CaptureStop> for CaptureStop {
    type Error = SdkError;

    fn try_from(value: syncap_core::CaptureStop) -> Result<Self, Self::Error> {
        let raw_json = serde_json::to_string(&value).map_err(syncap_core::Error::from)?;
        Ok(Self {
            capture_id: value.capture_id,
            session_id: value.session_id,
            state: value.state,
            session: value.session.map(Session::try_from).transpose()?,
            raw_json,
        })
    }
}

#[derive(Debug, Clone, uniffi::Record)]
pub struct ExportReport {
    pub session_id: String,
    pub path: String,
    pub bytes: u64,
    pub files: u64,
    pub verified_files: u64,
    pub manifest_saved: bool,
    pub manifest_file: String,
    pub verified: bool,
}

impl From<syncap_core::ExportReport> for ExportReport {
    fn from(value: syncap_core::ExportReport) -> Self {
        Self {
            session_id: value.session_id,
            path: value.path,
            bytes: value.bytes,
            files: value.files as u64,
            verified_files: value.verified_files as u64,
            manifest_saved: value.manifest_saved,
            manifest_file: value.manifest_file,
            verified: value.verified,
        }
    }
}
