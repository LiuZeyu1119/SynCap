//! Device-side recording control and verified export, without a UI runtime.
//!
//! Run async methods within a Tokio runtime. Dropping an export future stops
//! further network transfer; already scheduled disk operations may finish while
//! retaining their directory lock. Partial or verified final files remain for
//! a later retry. Dropping a capture request never implies recording stopped.

mod client;
mod error;
mod export;
mod export_manifest;
mod models;

pub use client::Client;
pub use error::{Error, ErrorKind, Result};
pub use export::ExportReport;
pub use models::{CaptureStart, CaptureState, CaptureStop, Session};
