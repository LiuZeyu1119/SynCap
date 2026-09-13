//! Read-only example of using the SDK without either application shell.
use syncap_core::Client;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let endpoint = std::env::args()
        .nth(1)
        .ok_or("Pass a device HTTP(S) origin")?;
    let device = Client::new(&endpoint)?;
    let state = device.current_capture().await?;
    let sessions = device.sessions().await?;
    println!(
        "{}",
        serde_json::json!({"capture": state, "sessions": sessions})
    );
    Ok(())
}
