//! Verified session export. Use a dedicated directory: `*.part` names are reserved
//! for SDK downloads. Existing final files are never replaced. The persistent
//! `.syncap-export.lock` file prevents simultaneous writers across processes.

use crate::{Client, Error, ErrorKind, Result};
use futures_util::StreamExt;
use reqwest::{header, StatusCode};
use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{collections::HashSet, io::Write, path::Path, sync::Arc, time::Duration};
use tokio::{fs, io::AsyncReadExt};
use url::Url;

const MANIFEST_LIMIT: usize = 4 * 1024 * 1024;
const LOCK_FILE: &str = ".syncap-export.lock";

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ExportReport {
    pub session_id: String,
    pub path: String,
    pub bytes: u64,
    pub files: usize,
    pub verified_files: usize,
    pub manifest_saved: bool,
    pub manifest_file: String,
    pub verified: bool,
}

struct ExportFile {
    name: String,
    size: u64,
    sha256: String,
    path: String,
}

// A pending blocking file operation owns the directory lock together with its
// descriptor. Cancelling its async waiter cannot unlock while writes continue.
struct ExportWriter {
    file: std::fs::File,
    _directory_lock: Arc<std::fs::File>,
}

async fn file_io<T>(operation: tokio::task::JoinHandle<Result<T>>) -> Result<T> {
    operation
        .await
        .map_err(|error| Error::new(ErrorKind::Io, error.to_string()))?
}

fn open_partial(
    path: &Path,
    append: bool,
    directory_lock: &Arc<std::fs::File>,
) -> tokio::task::JoinHandle<Result<ExportWriter>> {
    let path = path.to_path_buf();
    let directory_lock = Arc::clone(directory_lock);
    tokio::task::spawn_blocking(move || {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .create(true)
            .append(append)
            .truncate(!append)
            .open(path)?;
        Ok(ExportWriter {
            file,
            _directory_lock: directory_lock,
        })
    })
}

fn write_partial(
    mut writer: ExportWriter,
    bytes: impl AsRef<[u8]> + Send + 'static,
) -> tokio::task::JoinHandle<Result<ExportWriter>> {
    tokio::task::spawn_blocking(move || {
        writer.file.write_all(bytes.as_ref())?;
        Ok(writer)
    })
}

fn sync_partial(writer: ExportWriter) -> tokio::task::JoinHandle<Result<()>> {
    tokio::task::spawn_blocking(move || {
        writer.file.sync_all()?;
        drop(writer);
        Ok(())
    })
}

fn protocol(message: impl Into<String>) -> Error {
    Error::new(ErrorKind::Protocol, message)
}

fn integrity(message: impl Into<String>) -> Error {
    Error::new(ErrorKind::Integrity, message)
}

fn valid_name(name: &str) -> bool {
    !name.is_empty()
        && name.len() <= 160
        && !matches!(name, "." | "..")
        && !name.ends_with('.')
        && name
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || "._-".contains(c))
        && !matches!(
            name.split('.')
                .next()
                .unwrap_or_default()
                .to_ascii_uppercase()
                .as_str(),
            "CON"
                | "PRN"
                | "AUX"
                | "NUL"
                | "COM1"
                | "COM2"
                | "COM3"
                | "COM4"
                | "COM5"
                | "COM6"
                | "COM7"
                | "COM8"
                | "COM9"
                | "LPT1"
                | "LPT2"
                | "LPT3"
                | "LPT4"
                | "LPT5"
                | "LPT6"
                | "LPT7"
                | "LPT8"
                | "LPT9"
        )
}

fn check_identity(manifest: &Value, session_id: &str) -> Result<()> {
    let object = manifest
        .as_object()
        .ok_or_else(|| protocol("Session manifest must be an object"))?;
    if !["id", "sessionId", "session_id"]
        .iter()
        .any(|key| object.contains_key(*key))
    {
        return Err(protocol("Session manifest has no session identity"));
    }
    for key in ["id", "sessionId", "session_id"] {
        if object
            .get(key)
            .is_some_and(|value| value.as_str() != Some(session_id))
        {
            return Err(protocol(format!(
                "Session manifest {key} does not match {session_id}"
            )));
        }
    }
    Ok(())
}

fn export_files(manifest: &Value, session_id: &str) -> Result<Vec<ExportFile>> {
    check_identity(manifest, session_id)?;
    let items = manifest
        .get("files")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .ok_or_else(|| protocol("Export manifest has no files"))?;
    let mut names = HashSet::from([
        "session.json".to_string(),
        "session.json.part".to_string(),
        LOCK_FILE.to_string(),
        format!("{LOCK_FILE}.part"),
    ]);
    let mut total = 0u64;
    items
        .iter()
        .map(|item| {
            let name = item
                .get("name")
                .and_then(Value::as_str)
                .filter(|name| valid_name(name))
                .ok_or_else(|| protocol("Invalid export file name"))?;
            let key = name.to_ascii_lowercase();
            let partial_key = format!("{key}.part");
            if names.contains(&key) || names.contains(&partial_key) {
                return Err(protocol("Duplicate or reserved export file name"));
            }
            names.insert(key);
            names.insert(partial_key);
            let size = item
                .get("sizeBytes")
                .and_then(Value::as_u64)
                .ok_or_else(|| protocol("Export file size is missing"))?;
            if size == 0 {
                return Err(integrity(format!("Export file {name} is empty")));
            }
            total = total
                .checked_add(size)
                .ok_or_else(|| protocol("Export byte count overflow"))?;
            let sha256 = item
                .get("sha256")
                .and_then(Value::as_str)
                .filter(|s| s.len() == 64 && s.chars().all(|c| c.is_ascii_hexdigit()))
                .ok_or_else(|| protocol("Invalid export SHA-256"))?;
            let path = item
                .get("url")
                .and_then(Value::as_str)
                .ok_or_else(|| protocol("Export URL is missing"))?;
            if path != format!("/v1/sessions/{session_id}/files/{name}") {
                return Err(protocol(
                    "Export URL is outside the requested device/session",
                ));
            }
            Ok(ExportFile {
                name: name.into(),
                size,
                sha256: sha256.into(),
                path: path.into(),
            })
        })
        .collect()
}

fn manifest_url(client: &Client, manifest: &Value, session_id: &str) -> Result<Url> {
    let expected = client.endpoint(&format!("/v1/sessions/{session_id}/manifest"))?;
    let Some(value) = manifest.get("manifestUrl") else {
        return Ok(expected);
    };
    let value = value
        .as_str()
        .ok_or_else(|| protocol("Invalid session manifest URL"))?;
    if value == expected.path() || value == expected.as_str() {
        return Ok(expected);
    }
    Err(protocol(
        "Session manifest URL must use the requested device and session",
    ))
}

async fn file_length(path: &Path) -> Result<Option<u64>> {
    match fs::symlink_metadata(path).await {
        Ok(metadata) if metadata.is_file() => Ok(Some(metadata.len())),
        Ok(_) => Err(Error::new(
            ErrorKind::Io,
            format!("Not a regular file: {}", path.display()),
        )),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

async fn hash_file(path: &Path) -> Result<String> {
    let mut file = fs::File::open(path).await?;
    let mut buffer = vec![0u8; 64 * 1024];
    let mut hash = Sha256::new();
    loop {
        let count = file.read(&mut buffer).await?;
        if count == 0 {
            break;
        }
        hash.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hash.finalize()))
}

fn try_lock_file(file: &std::fs::File) -> std::result::Result<(), std::fs::TryLockError> {
    // Rust's std File locking is not implemented for Android. Use the kernel's
    // nonblocking flock through rustix, with the same descriptor-owned lifetime.
    #[cfg(target_os = "android")]
    {
        use rustix::fs::{flock, FlockOperation};
        match flock(file, FlockOperation::NonBlockingLockExclusive) {
            Ok(()) => Ok(()),
            Err(rustix::io::Errno::WOULDBLOCK) => Err(std::fs::TryLockError::WouldBlock),
            Err(error) => Err(std::fs::TryLockError::Error(error.into())),
        }
    }
    #[cfg(not(target_os = "android"))]
    {
        file.try_lock()
    }
}

async fn lock_directory(destination: &Path) -> Result<Arc<std::fs::File>> {
    let path = destination.join(LOCK_FILE);
    tokio::task::spawn_blocking(move || {
        match std::fs::symlink_metadata(&path) {
            Ok(metadata) if metadata.is_file() && metadata.len() == 0 => {}
            Ok(_) => {
                return Err(Error::new(
                    ErrorKind::Io,
                    "Export lock must be an empty regular file",
                ))
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(error.into()),
        }
        // Never unlink this file: another process may already hold its inode.
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(false)
            .open(&path)?;
        match try_lock_file(&file) {
            Ok(()) => Ok(Arc::new(file)),
            Err(std::fs::TryLockError::WouldBlock) => Err(Error::new(
                ErrorKind::Io,
                "Another export is already using this destination directory",
            )),
            Err(std::fs::TryLockError::Error(error)) => Err(error.into()),
        }
    })
    .await
    .map_err(|error| Error::new(ErrorKind::Io, error.to_string()))?
}

async fn final_exists(path: &Path, size: u64, sha256: &str) -> Result<bool> {
    let Some(length) = file_length(path).await? else {
        return Ok(false);
    };
    if length == size && hash_file(path).await?.eq_ignore_ascii_case(sha256) {
        return Ok(true);
    }
    Err(Error::new(
        ErrorKind::Io,
        format!(
            "Existing file differs; refusing to overwrite: {}",
            path.display()
        ),
    ))
}

// Atomic no-clobber persistence keeps the verified .part recoverable on errors.
// Build/own TempPath only inside the blocking operation so cancellation cannot
// drop it before persistence or accidentally remove a resumable download.
async fn commit_partial(
    partial: &Path,
    output: &Path,
    directory_lock: &Arc<std::fs::File>,
) -> Result<()> {
    let partial = partial.to_path_buf();
    let output = output.to_path_buf();
    let directory_lock = Arc::clone(directory_lock);
    tokio::task::spawn_blocking(move || {
        // A cancelled async caller cannot release the lock while this queued or
        // running operation can still publish the verified file.
        let _directory_lock = directory_lock;
        let mut path = tempfile::TempPath::try_from_path(partial)?;
        path.disable_cleanup(true);
        path.persist_noclobber(output).map_err(|failure| {
            // Cleanup is also disabled if keep itself fails (e.g. a vanished
            // mount), so no error path deletes the resumable partial file.
            let error = failure.error;
            let _ = failure.path.keep();
            Error::from(error)
        })
    })
    .await
    .map_err(|error| Error::new(ErrorKind::Io, error.to_string()))?
}

fn validate_range(response: &reqwest::Response, offset: u64, total: u64) -> Result<()> {
    let value = response
        .headers()
        .get(header::CONTENT_RANGE)
        .and_then(|h| h.to_str().ok())
        .and_then(|h| h.strip_prefix("bytes "))
        .ok_or_else(|| protocol("Partial response has no valid Content-Range"))?;
    let (range, declared_total) = value
        .split_once('/')
        .ok_or_else(|| protocol("Invalid Content-Range"))?;
    let (start, end) = range
        .split_once('-')
        .ok_or_else(|| protocol("Invalid Content-Range"))?;
    if start.parse::<u64>().ok() != Some(offset)
        || end.parse::<u64>().ok() != total.checked_sub(1)
        || declared_total.parse::<u64>().ok() != Some(total)
    {
        return Err(protocol(
            "Content-Range does not match the requested file range",
        ));
    }
    Ok(())
}

async fn download_file(
    client: &Client,
    item: &ExportFile,
    destination: &Path,
    directory_lock: &Arc<std::fs::File>,
) -> Result<()> {
    let output = destination.join(&item.name);
    if final_exists(&output, item.size, &item.sha256).await? {
        return Ok(());
    }
    let partial = destination.join(format!("{}.part", item.name));
    let existing = file_length(&partial).await?.unwrap_or(0);
    if existing > item.size {
        return Err(integrity(format!(
            "Partial file exceeds expected size: {}",
            partial.display()
        )));
    }
    if existing == item.size {
        if !hash_file(&partial)
            .await?
            .eq_ignore_ascii_case(&item.sha256)
        {
            return Err(integrity(format!(
                "Partial file SHA-256 mismatch: {}",
                partial.display()
            )));
        }
        return commit_partial(&partial, &output, directory_lock).await;
    }
    let url = client.endpoint(&item.path)?;
    let mut request = client
        .http
        .get(url.clone())
        .timeout(Duration::from_secs(3600));
    if existing > 0 {
        request = request.header(header::RANGE, format!("bytes={existing}-"));
    }
    let response = request.send().await?;
    if response.url() != &url {
        return Err(protocol("Redirected export download is not permitted"));
    }
    let offset = match response.status() {
        StatusCode::OK => 0, // Range is optional on older devices; safely restart the .part.
        StatusCode::PARTIAL_CONTENT => {
            validate_range(&response, existing, item.size)?;
            existing
        }
        status => {
            return Err(Error::new(
                ErrorKind::Device,
                format!("Download returned HTTP {status}"),
            ))
        }
    };
    if response
        .content_length()
        .is_some_and(|length| length != item.size - offset)
    {
        return Err(integrity(
            "Download Content-Length does not match the manifest",
        ));
    }
    let mut file = file_io(open_partial(&partial, offset > 0, directory_lock)).await?;
    let mut written = offset;
    let mut stream = response.bytes_stream();
    while let Some(block) = stream.next().await {
        let block = block?;
        written = written
            .checked_add(block.len() as u64)
            .ok_or_else(|| integrity("Download size overflow"))?;
        if written > item.size {
            return Err(integrity("Download exceeds manifest size"));
        }
        file = file_io(write_partial(file, block)).await?;
    }
    file_io(sync_partial(file)).await?;
    if written != item.size {
        return Err(integrity("Downloaded size does not match the manifest"));
    }
    if !hash_file(&partial)
        .await?
        .eq_ignore_ascii_case(&item.sha256)
    {
        return Err(integrity(format!(
            "SHA-256 verification failed: {}",
            item.name
        )));
    }
    commit_partial(&partial, &output, directory_lock).await
}

async fn fetch_manifest(client: &Client, url: Url, session_id: &str) -> Result<Vec<u8>> {
    let response = client
        .http
        .get(url.clone())
        .header(header::ACCEPT, "application/json")
        .timeout(Duration::from_secs(30))
        .send()
        .await?;
    if response.url() != &url || response.status() != StatusCode::OK {
        return Err(protocol(format!(
            "Session manifest download returned HTTP {} or redirected",
            response.status()
        )));
    }
    if response
        .content_length()
        .is_some_and(|size| size == 0 || size > MANIFEST_LIMIT as u64)
    {
        return Err(protocol("Invalid session manifest size"));
    }
    let mut payload = Vec::new();
    let mut stream = response.bytes_stream();
    while let Some(block) = stream.next().await {
        let block = block?;
        if payload.len().saturating_add(block.len()) > MANIFEST_LIMIT {
            return Err(protocol("Session manifest is too large"));
        }
        payload.extend_from_slice(&block);
    }
    let manifest: Value = serde_json::from_slice(&payload)?;
    check_identity(&manifest, session_id)?;
    Ok(payload)
}

impl Client {
    /// Download into the exact `destination` directory. Verified files are reused;
    /// interrupted `*.part` downloads resume. Conflicting final files are preserved.
    pub async fn export_session(
        &self,
        session_id: &str,
        destination: &Path,
    ) -> Result<ExportReport> {
        if !valid_name(session_id) {
            return Err(Error::new(ErrorKind::InvalidInput, "Invalid session ID"));
        }
        let export = self.prepare_export(session_id).await?;
        let files = export_files(&export, session_id)?;
        let payload =
            fetch_manifest(self, manifest_url(self, &export, session_id)?, session_id).await?;
        crate::export_manifest::verify_export_manifest(
            &export,
            &serde_json::from_slice(&payload)?,
        )?;
        fs::create_dir_all(destination).await?;
        if !fs::symlink_metadata(destination).await?.is_dir() {
            return Err(Error::new(
                ErrorKind::Io,
                "Export destination must be a real directory",
            ));
        }
        let directory_lock = lock_directory(destination).await?;
        // Validate all existing final paths before modifying any partial files.
        for item in &files {
            final_exists(&destination.join(&item.name), item.size, &item.sha256).await?;
            file_length(&destination.join(format!("{}.part", item.name))).await?;
        }
        let output = destination.join("session.json");
        let manifest_hash = format!("{:x}", Sha256::digest(&payload));
        let manifest_exists = final_exists(&output, payload.len() as u64, &manifest_hash).await?;
        let manifest_partial = destination.join("session.json.part");
        file_length(&manifest_partial).await?;
        let mut bytes = payload.len() as u64;
        for item in &files {
            download_file(self, item, destination, &directory_lock).await?;
            bytes = bytes
                .checked_add(item.size)
                .ok_or_else(|| protocol("Export byte count overflow"))?;
        }
        if !manifest_exists {
            let file = file_io(open_partial(&manifest_partial, false, &directory_lock)).await?;
            let file = file_io(write_partial(file, payload)).await?;
            file_io(sync_partial(file)).await?;
            commit_partial(&manifest_partial, &output, &directory_lock).await?;
        }
        Ok(ExportReport {
            session_id: session_id.into(),
            path: destination.to_string_lossy().into(),
            bytes,
            files: files.len() + 1,
            verified_files: files.len(),
            manifest_saved: true,
            manifest_file: "session.json".into(),
            verified: true,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use tokio::{io::AsyncWriteExt, net::TcpListener, task::JoinHandle};

    const DATA: &[u8] = b"camera-video-payload";
    const SESSION: &str = "ses_test";
    const RAW: &[u8] = b"{\n  \"id\": \"ses_test\", \"state\": \"complete\", \"calibration\": {\"original\": true},\n  \"files\": [{\"name\": \"cam0.h265\", \"sizeBytes\": 20, \"sha256\": \"d972132c068fdf138735310bd31d7c3c87c48c0c3280695aa050f35d2cdb0c0a\"}]\n}";

    fn manifest() -> Value {
        json!({"sessionId": SESSION, "files": [{"name": "cam0.h265", "sizeBytes": DATA.len(),
            "sha256": format!("{:x}", Sha256::digest(DATA)),
            "url": "/v1/sessions/ses_test/files/cam0.h265"}]})
    }

    struct Reply {
        status: &'static str,
        headers: String,
        body: Vec<u8>,
        request: &'static str,
    }

    fn reply(request: &'static str, body: &[u8]) -> Reply {
        Reply {
            request,
            status: "200 OK",
            headers: format!("Content-Length: {}\r\n", body.len()),
            body: body.into(),
        }
    }

    async fn server(replies: Vec<Reply>) -> (Client, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        let task = tokio::spawn(async move {
            for reply in replies {
                let (mut socket, _) =
                    tokio::time::timeout(Duration::from_secs(3), listener.accept())
                        .await
                        .unwrap()
                        .unwrap();
                let mut request = Vec::new();
                let mut buffer = [0u8; 1024];
                loop {
                    let count = socket.read(&mut buffer).await.unwrap();
                    assert_ne!(count, 0);
                    request.extend_from_slice(&buffer[..count]);
                    if request.windows(4).any(|s| s == b"\r\n\r\n") {
                        break;
                    }
                }
                let request = String::from_utf8_lossy(&request).to_ascii_lowercase();
                assert!(
                    request.contains(&reply.request.to_ascii_lowercase()),
                    "Unexpected request: {request}"
                );
                let header = format!(
                    "HTTP/1.1 {}\r\n{}Connection: close\r\n\r\n",
                    reply.status, reply.headers
                );
                socket.write_all(header.as_bytes()).await.unwrap();
                socket.write_all(&reply.body).await.unwrap();
                socket.shutdown().await.unwrap();
            }
        });
        let client = Client {
            http: reqwest::Client::builder()
                .redirect(reqwest::redirect::Policy::none())
                .no_proxy()
                .build()
                .unwrap(),
            base: Url::parse(&format!("http://{address}/")).unwrap(),
        };
        (client, task)
    }

    fn prelude() -> Vec<Reply> {
        vec![
            reply(
                "POST /v1/sessions/ses_test/prepare-export",
                &serde_json::to_vec(&manifest()).unwrap(),
            ),
            reply("GET /v1/sessions/ses_test/manifest", RAW),
        ]
    }

    #[tokio::test]
    async fn exports_and_preserves_original_manifest_bytes() {
        let mut replies = prelude();
        replies.push(reply("GET /v1/sessions/ses_test/files/cam0.h265", DATA));
        let (client, server) = server(replies).await;
        let dir = tempfile::tempdir().unwrap();
        let report = client.export_session(SESSION, dir.path()).await.unwrap();
        assert!(report.verified);
        assert_eq!(report.verified_files, 1);
        assert_eq!(report.bytes, (DATA.len() + RAW.len()) as u64);
        assert_eq!(
            fs::read(dir.path().join("session.json")).await.unwrap(),
            RAW
        );
        assert_eq!(fs::read(dir.path().join("cam0.h265")).await.unwrap(), DATA);
        assert!(!dir.path().join("cam0.h265.part").exists());
        server.await.unwrap();
    }

    #[tokio::test]
    async fn resumes_partial_with_validated_range() {
        let mut replies = prelude();
        replies.push(Reply {
            status: "206 Partial Content",
            headers: format!(
                "Content-Length: {}\r\nContent-Range: bytes 5-{}/{}\r\n",
                DATA.len() - 5,
                DATA.len() - 1,
                DATA.len()
            ),
            body: DATA[5..].into(),
            request: "range: bytes=5-",
        });
        let (client, server) = server(replies).await;
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("cam0.h265.part"), &DATA[..5])
            .await
            .unwrap();
        client.export_session(SESSION, dir.path()).await.unwrap();
        assert_eq!(fs::read(dir.path().join("cam0.h265")).await.unwrap(), DATA);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn complete_partial_is_verified_without_range_416() {
        let (client, server) = server(prelude()).await;
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("cam0.h265.part"), DATA)
            .await
            .unwrap();
        client.export_session(SESSION, dir.path()).await.unwrap();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn reuses_only_hash_verified_final_files() {
        let (client, server) = server(prelude()).await;
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("cam0.h265"), DATA).await.unwrap();
        fs::write(dir.path().join("session.json"), RAW)
            .await
            .unwrap();
        client.export_session(SESSION, dir.path()).await.unwrap();
        server.await.unwrap();
    }

    #[tokio::test]
    async fn preserves_conflicting_final_file() {
        let (client, server) = server(prelude()).await;
        let dir = tempfile::tempdir().unwrap();
        let bad = vec![b'x'; DATA.len()];
        fs::write(dir.path().join("cam0.h265"), &bad).await.unwrap();
        assert!(client.export_session(SESSION, dir.path()).await.is_err());
        assert_eq!(fs::read(dir.path().join("cam0.h265")).await.unwrap(), bad);
        server.await.unwrap();
    }

    #[tokio::test]
    async fn bad_hash_never_becomes_a_final_file() {
        let mut replies = prelude();
        replies.push(reply(
            "GET /v1/sessions/ses_test/files/cam0.h265",
            &vec![b'x'; DATA.len()],
        ));
        let (client, server) = server(replies).await;
        let dir = tempfile::tempdir().unwrap();
        assert!(client.export_session(SESSION, dir.path()).await.is_err());
        assert!(!dir.path().join("cam0.h265").exists());
        assert!(dir.path().join("cam0.h265.part").exists());
        server.await.unwrap();
    }

    #[tokio::test]
    async fn interrupted_stream_keeps_partial_for_resume() {
        let mut replies = prelude();
        replies.push(Reply {
            status: "200 OK",
            headers: format!("Content-Length: {}\r\n", DATA.len()),
            body: DATA[..5].into(),
            request: "GET /v1/sessions/ses_test/files/cam0.h265",
        });
        let (client, server) = server(replies).await;
        let dir = tempfile::tempdir().unwrap();
        assert!(client.export_session(SESSION, dir.path()).await.is_err());
        assert_eq!(
            fs::read(dir.path().join("cam0.h265.part")).await.unwrap(),
            DATA[..5]
        );
        assert!(!dir.path().join("cam0.h265").exists());
        server.await.unwrap();
    }

    #[tokio::test]
    async fn invalid_range_does_not_append() {
        let mut replies = prelude();
        replies.push(Reply {
            status: "206 Partial Content",
            headers: format!(
                "Content-Length: {}\r\nContent-Range: bytes 0-{}/{}\r\n",
                DATA.len() - 5,
                DATA.len() - 1,
                DATA.len()
            ),
            body: DATA[5..].into(),
            request: "range: bytes=5-",
        });
        let (client, server) = server(replies).await;
        let dir = tempfile::tempdir().unwrap();
        fs::write(dir.path().join("cam0.h265.part"), &DATA[..5])
            .await
            .unwrap();
        assert!(client.export_session(SESSION, dir.path()).await.is_err());
        assert_eq!(
            fs::read(dir.path().join("cam0.h265.part")).await.unwrap(),
            DATA[..5]
        );
        server.await.unwrap();
    }

    #[test]
    fn rejects_paths_duplicates_partial_collisions_and_empty_files() {
        for name in [
            "../escape",
            "dir/file",
            "session.json",
            "SESSION.JSON.PART",
            LOCK_FILE,
            ".SYNCAP-EXPORT.LOCK.PART",
            "CON",
            "x.",
        ] {
            let mut input = manifest();
            input["files"][0]["name"] = json!(name);
            assert!(export_files(&input, SESSION).is_err());
        }
        let mut input = manifest();
        let mut second = input["files"][0].clone();
        second["name"] = json!("cam0.h265.part");
        second["url"] = json!("/v1/sessions/ses_test/files/cam0.h265.part");
        input["files"].as_array_mut().unwrap().push(second);
        assert!(export_files(&input, SESSION).is_err());
        input = manifest();
        input["files"][0]["url"] = json!("http://evil.example/video");
        assert!(export_files(&input, SESSION).is_err());
        input = manifest();
        input["files"][0]["sizeBytes"] = json!(0);
        assert!(export_files(&input, SESSION).is_err());
        assert!(export_files(&json!({"files": []}), SESSION).is_err());
    }

    #[test]
    fn rejects_mismatched_session_identity() {
        assert!(check_identity(&json!({"sessionId": "other"}), SESSION).is_err());
        assert!(check_identity(&json!({"id": 12}), SESSION).is_err());
        assert!(check_identity(&json!([]), SESSION).is_err());
        assert!(check_identity(
            &json!({"id": SESSION, "sessionId": SESSION, "session_id": SESSION}),
            SESSION
        )
        .is_ok());
        assert!(check_identity(&json!({"id": SESSION, "session_id": "other"}), SESSION).is_err());
        assert!(check_identity(&json!({"legacy": true}), SESSION).is_err());
    }

    #[tokio::test]
    async fn manifest_url_is_restricted_to_exact_device_and_session() {
        let (client, server) = server(vec![]).await;
        let expected = client.endpoint("/v1/sessions/ses_test/manifest").unwrap();
        assert_eq!(
            manifest_url(&client, &json!({}), SESSION).unwrap(),
            expected
        );
        assert_eq!(
            manifest_url(&client, &json!({"manifestUrl": expected.as_str()}), SESSION).unwrap(),
            expected
        );
        for invalid in [
            "http://evil.example/v1/sessions/ses_test/manifest",
            "/v1/sessions/other/manifest",
            "/v1/sessions/ses_test/manifest?token=x",
            "//evil.example/v1/sessions/ses_test/manifest",
        ] {
            assert!(manifest_url(&client, &json!({"manifestUrl": invalid}), SESSION).is_err());
        }
        server.await.unwrap();
    }

    #[tokio::test]
    async fn conflicting_original_manifest_is_preserved() {
        let (client, server) = server(prelude()).await;
        let dir = tempfile::tempdir().unwrap();
        let original = b"{\"id\":\"ses_test\",\"userNotes\":\"keep\"}";
        fs::write(dir.path().join("session.json"), original)
            .await
            .unwrap();
        assert!(client.export_session(SESSION, dir.path()).await.is_err());
        assert_eq!(
            fs::read(dir.path().join("session.json")).await.unwrap(),
            original
        );
        assert!(!dir.path().join("cam0.h265.part").exists());
        server.await.unwrap();
    }

    #[tokio::test]
    async fn atomic_commit_keeps_partial_and_existing_final_on_conflict() {
        let dir = tempfile::tempdir().unwrap();
        let partial = dir.path().join("cam0.h265.part");
        let output = dir.path().join("cam0.h265");
        fs::write(&partial, DATA).await.unwrap();
        fs::write(&output, b"user-file").await.unwrap();
        let lock = lock_directory(dir.path()).await.unwrap();
        assert!(commit_partial(&partial, &output, &lock).await.is_err());
        assert_eq!(fs::read(&partial).await.unwrap(), DATA);
        assert_eq!(fs::read(&output).await.unwrap(), b"user-file");
    }

    #[tokio::test]
    async fn original_manifest_with_wrong_identity_stops_before_download() {
        let mut replies = prelude();
        replies[1] = reply(
            "GET /v1/sessions/ses_test/manifest",
            b"{\"id\":\"ses_other\"}",
        );
        let (client, server) = server(replies).await;
        let dir = tempfile::tempdir().unwrap();
        assert!(client.export_session(SESSION, dir.path()).await.is_err());
        assert!(!dir.path().join("cam0.h265.part").exists());
        server.await.unwrap();
    }

    #[cfg(unix)]
    #[tokio::test]
    async fn partial_symlink_is_not_followed() {
        let (client, server) = server(prelude()).await;
        let dir = tempfile::tempdir().unwrap();
        let external = tempfile::NamedTempFile::new().unwrap();
        fs::write(external.path(), b"keep").await.unwrap();
        std::os::unix::fs::symlink(external.path(), dir.path().join("cam0.h265.part")).unwrap();
        assert!(client.export_session(SESSION, dir.path()).await.is_err());
        assert_eq!(fs::read(external.path()).await.unwrap(), b"keep");
        server.await.unwrap();
    }

    #[tokio::test]
    async fn concurrent_export_is_rejected_without_touching_partial() {
        let (client, server) = server(prelude()).await;
        let dir = tempfile::tempdir().unwrap();
        let lock = lock_directory(dir.path()).await.unwrap();
        let partial = dir.path().join("cam0.h265.part");
        fs::write(&partial, &DATA[..5]).await.unwrap();
        let error = client
            .export_session(SESSION, dir.path())
            .await
            .unwrap_err();
        assert_eq!(error.kind, ErrorKind::Io);
        assert!(error.message.contains("Another export"));
        assert_eq!(fs::read(&partial).await.unwrap(), DATA[..5]);
        drop(lock);
        let next_lock = lock_directory(dir.path()).await.unwrap();
        assert_eq!(
            fs::metadata(dir.path().join(LOCK_FILE))
                .await
                .unwrap()
                .len(),
            0
        );
        drop(next_lock);
        server.await.unwrap();
    }

    #[test]
    fn directory_lock_excludes_another_process() {
        const CHILD_LOCK: &str = "SYNCAP_SDK_TEST_EXPORT_LOCK";
        if let Some(path) = std::env::var_os(CHILD_LOCK) {
            let file = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(path)
                .unwrap();
            assert!(matches!(
                try_lock_file(&file),
                Err(std::fs::TryLockError::WouldBlock)
            ));
            return;
        }
        let dir = tempfile::tempdir().unwrap();
        let runtime = tokio::runtime::Runtime::new().unwrap();
        let lock = runtime.block_on(lock_directory(dir.path())).unwrap();
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "export::tests::directory_lock_excludes_another_process",
                "--nocapture",
            ])
            .env(CHILD_LOCK, dir.path().join(LOCK_FILE))
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        drop(lock);
    }

    #[test]
    fn cancelled_commit_keeps_directory_lock_until_persistence_finishes() {
        // Keep the only blocking worker occupied so cancellation always occurs
        // after commit owns its lock but before it can run the filesystem rename.
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .max_blocking_threads(1)
            .build()
            .unwrap();
        runtime.block_on(async {
            let dir = tempfile::tempdir().unwrap();
            let partial = dir.path().join("cam0.h265.part");
            let output = dir.path().join("cam0.h265");
            std::fs::write(&partial, DATA).unwrap();
            let lock = lock_directory(dir.path()).await.unwrap();
            let (entered_sender, entered) = tokio::sync::oneshot::channel();
            let (release, released) = std::sync::mpsc::channel();
            let blocker = tokio::task::spawn_blocking(move || {
                entered_sender.send(()).unwrap();
                released.recv().unwrap();
            });
            entered.await.unwrap();
            let task_lock = Arc::clone(&lock);
            let task_output = output.clone();
            let task_partial = partial.clone();
            let commit = tokio::spawn(async move {
                commit_partial(&task_partial, &task_output, &task_lock).await
            });
            tokio::time::timeout(Duration::from_secs(3), async {
                while Arc::strong_count(&lock) < 3 {
                    tokio::task::yield_now().await;
                }
            })
            .await
            .unwrap();
            commit.abort();
            assert!(commit.await.unwrap_err().is_cancelled());
            assert_eq!(Arc::strong_count(&lock), 2);
            drop(lock);
            let probe = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(dir.path().join(LOCK_FILE))
                .unwrap();
            assert!(matches!(
                try_lock_file(&probe),
                Err(std::fs::TryLockError::WouldBlock)
            ));
            release.send(()).unwrap();
            blocker.await.unwrap();
            tokio::time::timeout(Duration::from_secs(3), async {
                loop {
                    match try_lock_file(&probe) {
                        Ok(()) => break,
                        Err(std::fs::TryLockError::WouldBlock) => tokio::task::yield_now().await,
                        Err(error) => panic!("Unexpected lock error: {error}"),
                    }
                }
            })
            .await
            .unwrap();
            assert_eq!(std::fs::read(output).unwrap(), DATA);
            assert!(!partial.exists());
        });
    }

    #[test]
    fn cancelled_pending_write_keeps_directory_lock_until_descriptor_is_closed() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .max_blocking_threads(1)
            .build()
            .unwrap();
        runtime.block_on(async {
            let dir = tempfile::tempdir().unwrap();
            let partial = dir.path().join("cam0.h265.part");
            let lock = lock_directory(dir.path()).await.unwrap();
            let writer = file_io(open_partial(&partial, false, &lock)).await.unwrap();
            let (entered_sender, entered) = tokio::sync::oneshot::channel();
            let (release, released) = std::sync::mpsc::channel();
            let blocker = tokio::task::spawn_blocking(move || {
                entered_sender.send(()).unwrap();
                released.recv().unwrap();
            });
            entered.await.unwrap();
            // write_partial queues the actual I/O synchronously and transfers
            // ownership of both the descriptor and the lock to that operation.
            let operation = write_partial(writer, DATA);
            let waiter = tokio::spawn(file_io(operation));
            waiter.abort();
            assert!(matches!(waiter.await, Err(error) if error.is_cancelled()));
            drop(lock);
            let probe = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .open(dir.path().join(LOCK_FILE))
                .unwrap();
            assert!(matches!(
                try_lock_file(&probe),
                Err(std::fs::TryLockError::WouldBlock)
            ));
            release.send(()).unwrap();
            blocker.await.unwrap();
            tokio::time::timeout(Duration::from_secs(3), async {
                loop {
                    match try_lock_file(&probe) {
                        Ok(()) => break,
                        Err(std::fs::TryLockError::WouldBlock) => tokio::task::yield_now().await,
                        Err(error) => panic!("Unexpected lock error: {error}"),
                    }
                }
            })
            .await
            .unwrap();
            assert_eq!(std::fs::read(partial).unwrap(), DATA);
            assert!(!dir.path().join("cam0.h265").exists());
        });
    }
}
