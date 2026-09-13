import CryptoKit
import Foundation
import XCTest
import SynCapSDK

final class DeviceClientTests: XCTestCase {
    func testAsyncCaptureRoundTripPreservesDeviceTimeAndExtensions() async throws {
        let server = try HTTPFixture { request in
            switch request.path {
            case "/v1/storage/configure":
                return HTTPResponse(json: #"{"target":"usb","usb":{"canCapture":true,"mediaType":"sd","displayName":"SD card"}}"#)
            case "/v1/captures/start":
                return HTTPResponse(json: #"{"captureId":"cap_swift","sessionId":"ses_swift","state":"recording","elapsedMs":0,"startedAtDeviceTimeNs":"3689123456789012345","vendor":{"cameraCount":2}}"#)
            case "/v1/captures/current":
                return HTTPResponse(json: #"{"captureId":"cap_swift","sessionId":"ses_swift","state":"recording","elapsedMs":1234}"#)
            case "/v1/captures/cap_swift/stop":
                return HTTPResponse(json: #"{"captureId":"cap_swift","sessionId":"ses_swift","state":"completed","session":{"id":"ses_swift","status":"completed","sizeBytes":42,"exportAvailable":true}}"#)
            case "/v1/sessions":
                return HTTPResponse(json: #"{"sessions":[{"id":"ses_swift","sizeBytes":42,"durationMs":1234,"status":"completed","exportAvailable":true,"cameraCount":2}]}"#)
            default: return HTTPResponse("404 Not Found", json: "{}")
            }
        }
        defer { server.close() }
        let device = try DeviceClient(endpoint: server.endpoint)
        let storage = try await device.configureStorageJson(target: .removable)
        XCTAssertTrue(storage.contains("SD card"))
        let start = try await device.startCapture(name: "Swift SDK test")
        XCTAssertEqual(start.captureId, "cap_swift")
        XCTAssertEqual(start.elapsedMs, 0)
        XCTAssertEqual(start.startedAtDeviceTimeNs, "3689123456789012345")
        XCTAssertTrue(start.rawJson.contains("cameraCount"))
        let state = try await device.currentCapture()
        XCTAssertEqual(state.elapsedMs, 1234)
        let stopped = try await device.stopCapture(captureId: start.captureId)
        XCTAssertEqual(stopped.state, "completed")
        XCTAssertEqual(stopped.session?.sizeBytes, 42)
        let sessions = try await device.sessions()
        XCTAssertEqual(sessions.count, 1)
        XCTAssertEqual(sessions[0].durationMs, 1234)
        XCTAssertTrue(sessions[0].rawJson.contains("cameraCount"))
        XCTAssertEqual(server.requests.map(\.method), ["POST", "POST", "GET", "POST", "GET"])
        let storageBody = try XCTUnwrap(JSONSerialization.jsonObject(with: server.requests[0].body) as? [String: Any])
        XCTAssertEqual(storageBody["target"] as? String, "usb")
    }

    func testDeviceErrorPreservesCodeStatusAndUnknownOutcome() async throws {
        let server = try HTTPFixture { _ in
            HTTPResponse("503 Service Unavailable", json: #"{"error":"capture.unavailable","message":"Recorder unavailable"}"#)
        }
        defer { server.close() }
        let device = try DeviceClient(endpoint: server.endpoint)
        do {
            _ = try await device.startCapture(name: "No blind retry")
            XCTFail("Expected device failure")
        } catch let SdkError.Failure(kind, message, httpStatus, deviceCode, outcomeUnknown) {
            XCTAssertEqual(kind, .device)
            XCTAssertEqual(message, "Recorder unavailable")
            XCTAssertEqual(httpStatus, 503)
            XCTAssertEqual(deviceCode, "capture.unavailable")
            XCTAssertTrue(outcomeUnknown)
        }
        XCTAssertEqual(server.requests.count, 1)
    }

    func testInvalidEndpointIsTypedAndDoesNotExposeCredentials() throws {
        do {
            _ = try DeviceClient(endpoint: "http://user:secret@localhost")
            XCTFail("Expected invalid endpoint")
        } catch let SdkError.Failure(kind, message, httpStatus, _, outcomeUnknown) {
            XCTAssertEqual(kind, .invalidInput)
            XCTAssertFalse(message.contains("secret"))
            XCTAssertNil(httpStatus)
            XCTAssertFalse(outcomeUnknown)
        }
    }

    func testQuarantinedDeviceIsNotReportedAsIdle() async throws {
        let server = try HTTPFixture { _ in
            HTTPResponse(json: #"{"state":"failed","recoveryRequired":true,"rebootRequired":true,"failureReason":"incomplete segment"}"#)
        }
        defer { server.close() }
        let state = try await DeviceClient(endpoint: server.endpoint).currentCapture()
        XCTAssertEqual(state.state, "failed")
        XCTAssertTrue(state.rawJson.contains("rebootRequired"))
        XCTAssertNil(state.elapsedMs)
    }

    func testExplicitCancellationStopsWaitingWithoutStoppingCamera() async throws {
        let received = expectation(description: "Export request reached fixture")
        let server = try HTTPFixture { _ in
            received.fulfill()
            return nil
        }
        defer { server.close() }
        let device = try DeviceClient(endpoint: server.endpoint)
        let cancellation = Cancellation()
        let destination = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let export = Task {
            try await device.exportSession(sessionId: "ses_swift", destination: destination.path, cancellation: cancellation)
        }
        await fulfillment(of: [received], timeout: 5)
        cancellation.cancel()
        XCTAssertTrue(cancellation.isCancelled())
        do {
            _ = try await export.value
            XCTFail("Expected cancellation")
        } catch let SdkError.Failure(kind, _, _, _, outcomeUnknown) {
            XCTAssertEqual(kind, .cancelled)
            XCTAssertFalse(outcomeUnknown)
        }
        XCTAssertEqual(server.requests.count, 1)
        XCTAssertEqual(server.requests[0].path, "/v1/sessions/ses_swift/prepare-export")
    }

    func testVerifiedExportResumesPartialAndPreservesManifest() async throws {
        let data = Data("swift sdk binary payload".utf8)
        let hash = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
        let inventory = #"[{"name":"cam0.mcap","sizeBytes":\#(data.count),"sha256":"\#(hash)","url":"/v1/sessions/ses_swift/files/cam0.mcap"}]"#
        let plan = #"{"sessionId":"ses_swift","files":\#(inventory),"totalBytes":\#(data.count)}"#
        let source = "{\n\"id\":\"ses_swift\",\"state\":\"complete\",\"files\":\(inventory),\"sizeBytes\":\(data.count),\"fileCount\":1\n}"
        let server = try HTTPFixture { request in
            switch request.path {
            case "/v1/sessions/ses_swift/prepare-export":
                return HTTPResponse(json: plan)
            case "/v1/sessions/ses_swift/manifest":
                return HTTPResponse(json: source)
            case "/v1/sessions/ses_swift/files/cam0.mcap":
                return HTTPResponse("206 Partial Content", body: Data(data.dropFirst(5)), headers: ["Content-Range": "bytes 5-\(data.count - 1)/\(data.count)"])
            default: return HTTPResponse("404 Not Found", json: "{}")
            }
        }
        defer { server.close() }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("syncap-swift-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: false)
        defer { try? FileManager.default.removeItem(at: directory) }
        try data.prefix(5).write(to: directory.appendingPathComponent("cam0.mcap.part"))
        let device = try DeviceClient(endpoint: server.endpoint)
        let report = try await device.exportSession(sessionId: "ses_swift", destination: directory.path, cancellation: nil)
        XCTAssertEqual(report.bytes, UInt64(data.count + source.utf8.count))
        XCTAssertEqual(report.files, 2)
        XCTAssertEqual(report.verifiedFiles, 1)
        XCTAssertTrue(report.verified)
        XCTAssertTrue(report.manifestSaved)
        XCTAssertEqual(try Data(contentsOf: directory.appendingPathComponent("cam0.mcap")), data)
        XCTAssertEqual(try Data(contentsOf: directory.appendingPathComponent("session.json")), Data(source.utf8))
        XCTAssertFalse(FileManager.default.fileExists(atPath: directory.appendingPathComponent("cam0.mcap.part").path))
        XCTAssertEqual(server.requests.last?.headers["range"], "bytes=5-")
        let repeated = try await device.exportSession(sessionId: "ses_swift", destination: directory.path, cancellation: nil)
        XCTAssertTrue(repeated.verified)
        XCTAssertEqual(server.requests.filter { $0.path.hasSuffix("/files/cam0.mcap") }.count, 1)
    }
}
