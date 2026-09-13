use serde::Serialize;
use std::fmt;

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum ErrorKind {
    InvalidInput,
    Transport,
    Timeout,
    Device,
    Protocol,
    Integrity,
    Io,
    Cancelled,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Error {
    pub kind: ErrorKind,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub http_status: Option<u16>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub device_code: Option<String>,
    /// A failed mutating request may already have taken effect. Query status;
    /// do not blindly retry capture start or assume stop succeeded.
    pub outcome_unknown: bool,
}

impl Error {
    pub fn new(kind: ErrorKind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
            http_status: None,
            device_code: None,
            outcome_unknown: false,
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        if let Some(status) = self.http_status {
            write!(f, "Device returned HTTP {status}: ")?;
        }
        if let Some(code) = &self.device_code {
            write!(f, "{code}: ")?;
        }
        write!(f, "{}", self.message)
    }
}

impl std::error::Error for Error {}

impl From<std::io::Error> for Error {
    fn from(error: std::io::Error) -> Self {
        Self::new(ErrorKind::Io, error.to_string())
    }
}

impl From<reqwest::Error> for Error {
    fn from(error: reqwest::Error) -> Self {
        Self::new(
            if error.is_timeout() {
                ErrorKind::Timeout
            } else {
                ErrorKind::Transport
            },
            error.without_url().to_string(),
        )
    }
}

impl From<serde_json::Error> for Error {
    fn from(error: serde_json::Error) -> Self {
        Self::new(ErrorKind::Protocol, error.to_string())
    }
}
