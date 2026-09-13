import CryptoKit
import Foundation
import SynCapSDK
import XCTest

/// Explicit opt-in export of an existing session. Never starts, stops, or reboots a device.
final class LiveDeviceTests: XCTestCase {
    func testExportExistingSession() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let endpoint = environment["SYNCAP_LIVE_ENDPOINT"],
              let sessionId = environment["SYNCAP_LIVE_SESSION_ID"],
              let path = environment["SYNCAP_LIVE_EXPORT_DIRECTORY"] else {
            throw XCTSkip("Set explicit live endpoint, session ID and export directory to run")
        }
        let destination = URL(fileURLWithPath: path, isDirectory: true)
        let client = try DeviceClient(endpoint: endpoint)
        let state = try await client.currentCapture()
        let report = try await client.exportSession(sessionId: sessionId, destination: path, cancellation: nil)
        XCTAssertTrue(report.verified)
        XCTAssertEqual(report.sessionId, sessionId)
        let manifestBytes = try Data(contentsOf: destination.appendingPathComponent("session.json"))
        let manifest = try XCTUnwrap(JSONSerialization.jsonObject(with: manifestBytes) as? [String: Any])
        XCTAssertEqual(manifest["id"] as? String, sessionId)
        let files = try XCTUnwrap(manifest["files"] as? [[String: Any]])
        XCTAssertFalse(files.isEmpty)
        XCTAssertEqual(UInt64(files.count), report.verifiedFiles)
        for item in files {
            let name = try XCTUnwrap(item["name"] as? String)
            let expectedSize = try XCTUnwrap(item["sizeBytes"] as? NSNumber).uint64Value
            let expectedHash = try XCTUnwrap(item["sha256"] as? String)
            let handle = try FileHandle(forReadingFrom: destination.appendingPathComponent(name))
            defer { try? handle.close() }
            var hash = SHA256()
            var size: UInt64 = 0
            while let data = try handle.read(upToCount: 65536), !data.isEmpty {
                size += UInt64(data.count)
                hash.update(data: data)
            }
            XCTAssertGreaterThan(size, 0, name)
            XCTAssertEqual(size, expectedSize, name)
            XCTAssertEqual(hash.finalize().map { String(format: "%02x", $0) }.joined(), expectedHash, name)
        }
        let proof: [String: Any] = [
            "sessionId": sessionId,
            "captureStateBeforeExport": state.state,
            "path": path,
            "bytesIncludingManifest": report.bytes,
            "verifiedFiles": report.verifiedFiles,
            "transportVerified": report.verified,
            "captureStartedByThisTest": false,
        ]
        // This file is outside the export inventory; preserve the device manifest unchanged.
        try JSONSerialization.data(withJSONObject: proof, options: [.prettyPrinted, .sortedKeys])
            .write(to: destination.appendingPathExtension("validation.json"), options: .atomic)
        print("Swift SDK exported \(sessionId): \(report.verifiedFiles) verified files, \(report.bytes) bytes")
    }
}
