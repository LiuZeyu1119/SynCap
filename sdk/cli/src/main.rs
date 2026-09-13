use std::io::{self, Write};
use std::path::PathBuf;

use serde_json::{json, Value};
use syncap_core::Client;

const HELP: &str = "SynCap device command-line client

Usage:
  syncap --endpoint http://HOST:PORT manifest
  syncap --endpoint http://HOST:PORT status
  syncap --endpoint http://HOST:PORT storage
  syncap --endpoint http://HOST:PORT capture current
  syncap --endpoint http://HOST:PORT capture start --storage usb|internal --name NAME
  syncap --endpoint http://HOST:PORT capture stop ID
  syncap --endpoint http://HOST:PORT sessions list
  syncap --endpoint http://HOST:PORT sessions export ID --output DIR
  syncap --help

This version supports HTTP device control and export only, not Bluetooth
provisioning or video preview. The endpoint must be reachable; no ADB forwarding
or connection setup is performed automatically.

Capture start configures the selected storage before starting. Mutating requests
are not automatically retried. Export resumes verified partial downloads, and DIR
is the session's destination directory (no session ID is appended).
The usb storage slot represents removable media, including the Tina camera's SD
card. Use storage to inspect the actual medium and supported storage targets.

Command results are JSON on stdout; errors are JSON on stderr with a nonzero exit.
";

#[derive(Debug, PartialEq, Eq)]
enum Command {
    Manifest,
    Status,
    Storage,
    CaptureCurrent,
    CaptureStart { storage: String, name: String },
    CaptureStop { id: String },
    SessionsList,
    SessionsExport { id: String, output: PathBuf },
}

#[derive(Debug, PartialEq, Eq)]
struct Options {
    endpoint: String,
    command: Command,
}

#[derive(Debug, PartialEq, Eq)]
enum Invocation {
    Help,
    Run(Options),
}

fn argument_value<'a>(args: &'a [String], index: usize, message: &str) -> Result<&'a str, String> {
    args.get(index)
        .map(String::as_str)
        .filter(|value| !value.trim().is_empty() && !value.starts_with('-'))
        .ok_or_else(|| message.to_owned())
}

fn parse_args(args: &[String]) -> Result<Invocation, String> {
    if args.len() == 1 && matches!(args[0].as_str(), "--help" | "-h" | "help") {
        return Ok(Invocation::Help);
    }
    if args.first().map(String::as_str) != Some("--endpoint") {
        return Err(
            "Supply --endpoint http://HOST:PORT before the command; use --help for usage."
                .to_owned(),
        );
    }
    let endpoint = argument_value(args, 1, "--endpoint requires a URL.")?.to_owned();
    let args = &args[2..];
    let command = match args.first().map(String::as_str) {
        Some("manifest") if args.len() == 1 => Command::Manifest,
        Some("status") if args.len() == 1 => Command::Status,
        Some("storage") if args.len() == 1 => Command::Storage,
        Some("capture") => parse_capture(&args[1..])?,
        Some("sessions") => parse_sessions(&args[1..])?,
        _ => {
            return Err(
                "Missing or invalid command or unexpected arguments; use --help for usage."
                    .to_owned(),
            )
        }
    };
    Ok(Invocation::Run(Options { endpoint, command }))
}

fn parse_capture(args: &[String]) -> Result<Command, String> {
    match args.first().map(String::as_str) {
        Some("current") if args.len() == 1 => Ok(Command::CaptureCurrent),
        Some("stop") if args.len() == 2 => Ok(Command::CaptureStop {
            id: argument_value(args, 1, "capture stop requires a capture ID.")?.to_owned(),
        }),
        Some("start") => {
            let mut storage = None;
            let mut name = None;
            let mut index = 1;
            while index < args.len() {
                match args[index].as_str() {
                    "--storage" if storage.is_none() => {
                        let value =
                            argument_value(args, index + 1, "--storage requires usb or internal.")?;
                        if !matches!(value, "usb" | "internal") {
                            return Err("--storage must be usb or internal.".to_owned());
                        }
                        storage = Some(value.to_owned());
                    }
                    "--name" if name.is_none() => {
                        let value =
                            argument_value(args, index + 1, "--name requires a nonempty name.")?;
                        if value.len() > 96 {
                            return Err("--name must not exceed 96 UTF-8 bytes.".to_owned());
                        }
                        name = Some(value.to_owned());
                    }
                    _ => return Err("Unknown or duplicate capture start option.".to_owned()),
                }
                index += 2;
            }
            Ok(Command::CaptureStart {
                storage: storage.ok_or("capture start requires --storage usb|internal.")?,
                name: name.ok_or("capture start requires --name NAME.")?,
            })
        }
        _ => Err("Invalid capture command or arguments; use --help for usage.".to_owned()),
    }
}

fn parse_sessions(args: &[String]) -> Result<Command, String> {
    match args.first().map(String::as_str) {
        Some("list") if args.len() == 1 => Ok(Command::SessionsList),
        Some("export") if args.len() == 4 && args[2] == "--output" => Ok(Command::SessionsExport {
            id: argument_value(args, 1, "sessions export requires a session ID.")?.to_owned(),
            output: PathBuf::from(argument_value(args, 3, "--output requires a directory.")?),
        }),
        _ => Err("Invalid sessions command or arguments; use --help for usage.".to_owned()),
    }
}

async fn run(options: Options) -> Result<Value, syncap_core::Error> {
    let client = Client::new(&options.endpoint)?;
    let result = match options.command {
        Command::Manifest => client.manifest().await?,
        Command::Status => client.status().await?,
        Command::Storage => client.storage().await?,
        Command::CaptureCurrent => json!(client.current_capture().await?),
        Command::CaptureStart { storage, name } => {
            client.configure_storage(&storage).await?;
            json!(client.start_capture(&name).await?)
        }
        Command::CaptureStop { id } => json!(client.stop_capture(&id).await?),
        Command::SessionsList => json!(client.sessions().await?),
        Command::SessionsExport { id, output } => json!(client.export_session(&id, &output).await?),
    };
    Ok(result)
}

fn emit_error(error: Value) {
    let _ = writeln!(io::stderr().lock(), "{}", json!({ "error": error }));
}

#[tokio::main]
async fn main() -> std::process::ExitCode {
    let args = std::env::args_os()
        .skip(1)
        .map(|arg| {
            arg.into_string()
                .map_err(|_| "Arguments must be valid UTF-8.".to_owned())
        })
        .collect::<Result<Vec<_>, _>>();
    let invocation = match args.and_then(|args| parse_args(&args)) {
        Ok(invocation) => invocation,
        Err(message) => {
            emit_error(json!({ "code": "invalid_arguments", "message": message }));
            return std::process::ExitCode::from(2);
        }
    };
    let output = match invocation {
        Invocation::Help => HELP.to_owned(),
        Invocation::Run(options) => match run(options).await {
            Ok(value) => format!("{value}\n"),
            Err(error) => {
                emit_error(json!(error));
                return std::process::ExitCode::FAILURE;
            }
        },
    };
    if io::stdout().lock().write_all(output.as_bytes()).is_err() {
        emit_error(
            json!({ "code": "output_failed", "message": "Could not write command output." }),
        );
        return std::process::ExitCode::FAILURE;
    }
    std::process::ExitCode::SUCCESS
}

#[cfg(test)]
mod tests {
    use super::*;

    fn parse(args: &[&str]) -> Result<Invocation, String> {
        parse_args(&args.iter().map(|arg| (*arg).to_owned()).collect::<Vec<_>>())
    }

    fn command(args: &[&str]) -> Result<Command, String> {
        let mut full_args = vec!["--endpoint", "http://127.0.0.1:8080"];
        full_args.extend_from_slice(args);
        match parse(&full_args)? {
            Invocation::Run(options) => Ok(options.command),
            Invocation::Help => panic!("Expected a command"),
        }
    }

    #[test]
    fn help_does_not_require_an_endpoint() {
        for option in ["--help", "-h", "help"] {
            assert_eq!(parse(&[option]), Ok(Invocation::Help));
        }
    }

    #[test]
    fn endpoint_and_command_are_required() {
        for args in [
            vec![],
            vec!["status"],
            vec!["--endpoint"],
            vec!["--endpoint", "--help"],
            vec!["--endpoint", "http://localhost:8080"],
            vec!["--help", "status"],
        ] {
            assert!(parse(&args).is_err(), "accepted {args:?}");
        }
    }

    #[test]
    fn parses_read_only_commands() {
        assert_eq!(command(&["manifest"]), Ok(Command::Manifest));
        assert_eq!(command(&["status"]), Ok(Command::Status));
        assert_eq!(command(&["storage"]), Ok(Command::Storage));
        assert_eq!(
            command(&["capture", "current"]),
            Ok(Command::CaptureCurrent)
        );
        assert_eq!(command(&["sessions", "list"]), Ok(Command::SessionsList));
    }

    #[test]
    fn capture_start_requires_explicit_storage_and_name_in_either_order() {
        for args in [
            vec![
                "capture",
                "start",
                "--storage",
                "usb",
                "--name",
                "Outdoor run 1",
            ],
            vec![
                "capture",
                "start",
                "--name",
                "Outdoor run 1",
                "--storage",
                "usb",
            ],
        ] {
            assert_eq!(
                command(&args),
                Ok(Command::CaptureStart {
                    storage: "usb".to_owned(),
                    name: "Outdoor run 1".to_owned(),
                })
            );
        }
        assert!(command(&[
            "capture",
            "start",
            "--storage",
            "internal",
            "--name",
            "采集 1"
        ])
        .is_ok());
    }

    #[test]
    fn capture_start_rejects_missing_invalid_duplicate_and_unknown_options() {
        for args in [
            vec!["capture", "start"],
            vec!["capture", "start", "--storage", "usb"],
            vec!["capture", "start", "--name", "example"],
            vec!["capture", "start", "--storage", "sd", "--name", "example"],
            vec!["capture", "start", "--storage", "usb", "--name"],
            vec!["capture", "start", "--storage", "usb", "--name", " "],
            vec!["capture", "start", "--storage", "--name", "example"],
            vec![
                "capture",
                "start",
                "--storage",
                "usb",
                "--name",
                "example",
                "--name",
                "other",
            ],
            vec![
                "capture",
                "start",
                "--storage",
                "usb",
                "--storage",
                "internal",
                "--name",
                "example",
            ],
            vec![
                "capture",
                "start",
                "--storage",
                "usb",
                "--name",
                "example",
                "--force",
            ],
        ] {
            assert!(command(&args).is_err(), "accepted {args:?}");
        }
    }

    #[test]
    fn capture_stop_requires_exactly_one_id() {
        assert_eq!(
            command(&["capture", "stop", "capture-1"]),
            Ok(Command::CaptureStop {
                id: "capture-1".to_owned()
            })
        );
        for args in [
            vec!["capture", "stop"],
            vec!["capture", "stop", ""],
            vec!["capture", "stop", "--all"],
            vec!["capture", "stop", "one", "two"],
        ] {
            assert!(command(&args).is_err(), "accepted {args:?}");
        }
    }

    #[test]
    fn overly_long_names_are_rejected_before_storage_is_changed() {
        let name = "x".repeat(97);
        assert!(command(&["capture", "start", "--storage", "usb", "--name", &name]).is_err());
    }

    #[test]
    fn export_uses_the_exact_destination_directory() {
        assert_eq!(
            command(&[
                "sessions",
                "export",
                "session-1",
                "--output",
                "/tmp/my session"
            ]),
            Ok(Command::SessionsExport {
                id: "session-1".to_owned(),
                output: PathBuf::from("/tmp/my session")
            })
        );
    }

    #[test]
    fn rejects_incomplete_exports_and_extra_arguments() {
        for args in [
            vec!["sessions", "export"],
            vec!["sessions", "export", "one"],
            vec!["sessions", "export", "one", "--output"],
            vec!["sessions", "export", "one", "--output", ""],
            vec!["sessions", "export", "one", "--output", "--help"],
            vec!["sessions", "export", "one", "--out", "directory"],
            vec![
                "sessions",
                "export",
                "one",
                "--output",
                "directory",
                "extra",
            ],
            vec!["manifest", "extra"],
            vec!["status", "--force"],
            vec!["storage", "usb"],
            vec!["capture", "current", "extra"],
            vec!["sessions", "list", "extra"],
            vec!["unknown"],
        ] {
            assert!(command(&args).is_err(), "accepted {args:?}");
        }
    }
}
