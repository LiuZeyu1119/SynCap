import hashlib
import json
import unittest

try:
    from . import test_tina_service as base
except ImportError:
    import test_tina_service as base


class TinaSessionIntegrityTest(unittest.TestCase):
    setUpClass = classmethod(base.TinaServiceTest.setUpClass.__func__)
    tearDownClass = classmethod(base.TinaServiceTest.tearDownClass.__func__)
    setUp = base.TinaServiceTest.setUp
    tearDown = base.TinaServiceTest.tearDown
    service = base.TinaServiceTest.service

    def persisted_session(self, session_id="ses_integrity_fixture", state="complete"):
        directory = self.storage / "SynCap" / session_id
        directory.mkdir(parents=True)
        payloads = {
            "segment-0001.mcap": b"saved-camera-and-imu-data" + base.MCAP_MAGIC,
            "segment-0001.mcap.status.json": b'{"state":"clean"}',
        }
        files = []
        for name, payload in payloads.items():
            (directory / name).write_bytes(payload)
            files.append({
                "name": name,
                "sizeBytes": len(payload),
                "sha256": hashlib.sha256(payload).hexdigest(),
            })
        total = sum(item["sizeBytes"] for item in files)
        manifest = {
            "schema": "syncap.session/1.0",
            "id": session_id,
            "name": "saved capture",
            "createdAt": "2026-09-10T01:02:03Z",
            "state": state,
            "durationMs": 2000,
            "sizeBytes": total,
            "fileCount": len(files),
            "files": files,
        }
        export = {
            "sessionId": session_id,
            "rangeSupported": True,
            "totalBytes": total,
            "files": [dict(item, url=f"/v1/sessions/{session_id}/files/{item['name']}") for item in files],
        }
        (directory / "session.json").write_text(json.dumps(manifest))
        (directory / ".syncap-export.json").write_text(json.dumps(export))
        return directory, manifest, export

    def test_empty_invalid_and_missing_session_metadata_remain_visible_as_failed(self):
        for index, content in enumerate((b"", b"{", b"{}", None)):
            directory, _, _ = self.persisted_session(f"ses_corrupt_{index}")
            path = directory / "session.json"
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
        service = self.service(dry_run=True)
        status, _, body = service.request("GET", "/v1/sessions")
        self.assertEqual(status, 200)
        sessions = json.loads(body)["sessions"]
        self.assertEqual(len(sessions), 4)
        for session in sessions:
            self.assertEqual(session["name"], session["id"])
            self.assertEqual(session["status"], "failed")
            self.assertEqual(session["failureReason"], "session.metadata_corrupt")
            self.assertFalse(session["exportAvailable"])
        for index, content in enumerate((b"", b"{", b"{}")):
            status, _, body = service.request("GET", f"/v1/sessions/ses_corrupt_{index}/manifest")
            self.assertEqual(status, 409)
            self.assertEqual(json.loads(body)["error"], "session.metadata_corrupt")
            self.assertEqual((self.storage / "SynCap" / f"ses_corrupt_{index}" / "session.json").read_bytes(), content)

    def test_empty_and_invalid_export_metadata_are_rejected_not_http_200(self):
        directory, _, _ = self.persisted_session()
        service = self.service(dry_run=True)
        for content in (b"", b"{", b"{}", b'{"sessionId":"wrong","totalBytes":0,"files":[]}'):
            with self.subTest(content=content):
                (directory / ".syncap-export.json").write_bytes(content)
                status, _, body = service.request("POST", "/v1/sessions/ses_integrity_fixture/prepare-export", {})
                self.assertEqual(status, 409)
                self.assertEqual(json.loads(body)["error"], "session.metadata_corrupt")
                self.assertEqual((directory / ".syncap-export.json").read_bytes(), content)

    def test_missing_zero_length_and_wrong_size_data_are_not_exportable(self):
        service = self.service(dry_run=True)
        for index, content in enumerate((None, b"", b"short")):
            directory, _, _ = self.persisted_session(f"ses_data_corrupt_{index}")
            path = directory / "segment-0001.mcap"
            if content is None:
                path.unlink()
            else:
                path.write_bytes(content)
            for method, resource in (("POST", "prepare-export"), ("GET", "manifest")):
                status, _, body = service.request(method, f"/v1/sessions/ses_data_corrupt_{index}/{resource}", {} if method == "POST" else None)
                self.assertEqual(status, 409)
                self.assertEqual(json.loads(body)["error"], "session.data_corrupt")
        status, _, body = service.request("GET", "/v1/sessions")
        self.assertEqual(status, 200)
        for session in json.loads(body)["sessions"]:
            self.assertEqual(session["name"], "saved capture")
            self.assertEqual(session["status"], "failed")
            self.assertEqual(session["failureReason"], "session.data_corrupt")
            self.assertFalse(session["exportAvailable"])

    def test_valid_complete_and_retained_failed_sessions_still_export(self):
        service = self.service(dry_run=True)
        for state in ("complete", "failed"):
            session_id = f"ses_valid_{state}"
            _, manifest, export = self.persisted_session(session_id, state)
            status, _, body = service.request("GET", f"/v1/sessions/{session_id}/manifest")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), manifest)
            status, _, body = service.request("POST", f"/v1/sessions/{session_id}/prepare-export", {})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), export)

    def test_export_total_mismatch_and_unsafe_filename_are_rejected(self):
        directory, _, export = self.persisted_session()
        service = self.service(dry_run=True)
        export["totalBytes"] += 1
        (directory / ".syncap-export.json").write_text(json.dumps(export))
        status, _, body = service.request("POST", "/v1/sessions/ses_integrity_fixture/prepare-export", {})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "session.metadata_corrupt")
        export["totalBytes"] -= 1
        export["files"][0]["name"] = "../outside.mcap"
        (directory / ".syncap-export.json").write_text(json.dumps(export))
        status, _, body = service.request("POST", "/v1/sessions/ses_integrity_fixture/prepare-export", {})
        self.assertEqual(status, 409)
        self.assertEqual(json.loads(body)["error"], "session.metadata_corrupt")


if __name__ == "__main__":
    unittest.main()
