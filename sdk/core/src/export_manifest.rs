use crate::{Error, ErrorKind, Result};
use serde_json::Value;
use std::collections::BTreeMap;

fn invalid(message: &str) -> Error {
    Error::new(ErrorKind::Integrity, message)
}

fn inventory(document: &Value) -> Result<BTreeMap<&str, (u64, String)>> {
    let files = document
        .get("files")
        .and_then(Value::as_array)
        .filter(|items| !items.is_empty())
        .ok_or_else(|| invalid("Session file inventory is missing or empty"))?;
    let mut inventory = BTreeMap::new();
    for file in files {
        let name = file
            .get("name")
            .and_then(Value::as_str)
            .ok_or_else(|| invalid("Session inventory has no file name"))?;
        let size = file
            .get("sizeBytes")
            .and_then(Value::as_u64)
            .filter(|n| *n > 0)
            .ok_or_else(|| invalid("Session inventory has an invalid file size"))?;
        let hash = file
            .get("sha256")
            .and_then(Value::as_str)
            .filter(|s| s.len() == 64 && s.bytes().all(|ch| ch.is_ascii_hexdigit()))
            .ok_or_else(|| invalid("Session inventory has an invalid SHA-256"))?;
        if inventory
            .insert(name, (size, hash.to_ascii_lowercase()))
            .is_some()
        {
            return Err(invalid("Session inventory contains a duplicate file"));
        }
    }
    Ok(inventory)
}

/// A valid transfer must include the entire saved inventory, not just a subset
/// accidentally returned by prepare-export. Failed sessions may contain useful,
/// valid fragments; export does not relabel their capture status as complete.
pub(crate) fn verify_export_manifest(plan: &Value, source: &Value) -> Result<()> {
    if !matches!(
        source.get("state").and_then(Value::as_str),
        Some("complete" | "failed")
    ) {
        return Err(invalid("Session is not in a finalized state"));
    }
    let planned = inventory(plan)?;
    let saved = inventory(source)?;
    if planned != saved {
        return Err(invalid(
            "Export plan differs from the saved session file inventory",
        ));
    }
    let total = planned
        .values()
        .try_fold(0u64, |sum, (size, _)| sum.checked_add(*size))
        .ok_or_else(|| invalid("Session inventory byte count overflow"))?;
    for (document, field, expected) in [
        (plan, "totalBytes", total),
        (source, "sizeBytes", total),
        (source, "fileCount", planned.len() as u64),
    ] {
        if document
            .get(field)
            .is_some_and(|value| value.as_u64() != Some(expected))
        {
            return Err(invalid("Session inventory totals do not match its files"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn documents() -> (Value, Value) {
        let files = json!([{"name":"a.mcap","sizeBytes":10,"sha256":"a".repeat(64)},
            {"name":"a.mcap.status.json","sizeBytes":5,"sha256":"b".repeat(64)}]);
        (
            json!({"files":files,"totalBytes":15}),
            json!({"state":"complete","files":files,"sizeBytes":15,"fileCount":2}),
        )
    }

    #[test]
    fn complete_or_failed_saved_inventory_is_exportable() {
        let (plan, mut source) = documents();
        verify_export_manifest(&plan, &source).unwrap();
        source["state"] = json!("failed");
        source["files"].as_array_mut().unwrap().reverse();
        verify_export_manifest(&plan, &source).unwrap();
    }

    #[test]
    fn refuses_subset_extra_and_changed_files() {
        let (plan, source) = documents();
        let mut subset = plan.clone();
        subset["files"].as_array_mut().unwrap().pop();
        assert!(verify_export_manifest(&subset, &source).is_err());
        let mut extra = plan.clone();
        extra["files"]
            .as_array_mut()
            .unwrap()
            .push(json!({"name":"extra","sizeBytes":1,"sha256":"c".repeat(64)}));
        assert!(verify_export_manifest(&extra, &source).is_err());
        for field in ["name", "sizeBytes", "sha256"] {
            let mut changed = source.clone();
            changed["files"][0][field] = match field {
                "name" => json!("different.mcap"),
                "sizeBytes" => json!(9),
                _ => json!("c".repeat(64)),
            };
            assert!(verify_export_manifest(&plan, &changed).is_err());
        }
    }

    #[test]
    fn refuses_missing_inventory_or_unfinished_session() {
        let (plan, source) = documents();
        for state in ["recording", "starting", "finalizing", "idle", ""] {
            let mut source = source.clone();
            source["state"] = json!(state);
            assert!(verify_export_manifest(&plan, &source).is_err());
        }
        let mut missing = source.clone();
        missing.as_object_mut().unwrap().remove("files");
        assert!(verify_export_manifest(&plan, &missing).is_err());
        missing["files"] = json!([]);
        assert!(verify_export_manifest(&plan, &missing).is_err());
    }

    #[test]
    fn refuses_wrong_totals_or_duplicates() {
        let (mut plan, mut source) = documents();
        plan["totalBytes"] = json!(14);
        assert!(verify_export_manifest(&plan, &source).is_err());
        plan["totalBytes"] = json!(15);
        source["fileCount"] = json!(1);
        assert!(verify_export_manifest(&plan, &source).is_err());
        source["fileCount"] = json!(2);
        source["sizeBytes"] = json!(0);
        assert!(verify_export_manifest(&plan, &source).is_err());
        source["sizeBytes"] = json!(15);
        let duplicate = source["files"][0].clone();
        source["files"].as_array_mut().unwrap().push(duplicate);
        assert!(verify_export_manifest(&plan, &source).is_err());
    }
}
