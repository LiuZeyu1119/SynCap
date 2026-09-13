import Foundation
import XCTest
@testable import SynCapBluetooth

final class BluetoothProtocolTests: XCTestCase {
    func testFragmentRoundTripAndLimits() throws {
        let payload = try JSONSerialization.data(withJSONObject: ["op": "wifi.scan", "claimCode": "123456"])
        for size in [4, 80, 496] {
            let packets = try SynCapBleOperation.fragment(payload, payloadSize: size)
            var restored = Data()
            for (index, packet) in packets.enumerated() {
                XCTAssertEqual(Array(packet.prefix(4)), [0x53, 0x43, 1, 1])
                XCTAssertEqual(Int(packet[8]) * 256 + Int(packet[9]), index)
                XCTAssertEqual(Int(packet[10]) * 256 + Int(packet[11]), packets.count)
                XCTAssertEqual(Int(packet[12]) * 256 + Int(packet[13]), packet.count - 16)
                XCTAssertEqual(packet[14], 0)
                XCTAssertEqual(packet[15], 0)
                restored.append(packet.dropFirst(16))
            }
            XCTAssertEqual(restored, payload)
        }
        XCTAssertThrowsError(try SynCapBleOperation.fragment(Data(), payloadSize: 4))
        XCTAssertThrowsError(try SynCapBleOperation.fragment(Data(repeating: 1, count: 4097), payloadSize: 496))
        XCTAssertThrowsError(try SynCapBleOperation.fragment(Data(repeating: 1, count: 1024), payloadSize: 4))
    }

    func testWifiValidationUsesUtf8Bytes() throws {
        XCTAssertThrowsError(try SynCapBleOperation.wifiConfiguration(ssid: String(repeating: "中", count: 11), password: "test-only"))
        XCTAssertThrowsError(try SynCapBleOperation.wifiConfiguration(ssid: "Wi-Fi", password: "short"))
        XCTAssertThrowsError(try SynCapBleOperation.wifiConfiguration(ssid: "Wi-Fi\0", password: "test-only"))
        XCTAssertThrowsError(try SynCapBleOperation.wifiConfiguration(ssid: "Wi-Fi", password: "test-only", security: "wep"))
        let open = try SynCapBleOperation.wifiConfiguration(ssid: "Open", password: "", security: "open")
        XCTAssertEqual(open["security"] as? String, "open")
    }

    func testRejectsInvalidScanAndUnsupportedCommandsWithoutStartingBluetooth() {
        XCTAssertThrowsError(try SynCapBleOperation.scan(durationMs: 0) { _ in })
        XCTAssertThrowsError(try SynCapBleOperation.command(address: UUID(), payload: ["op": "device.reboot"]) { _ in })
        XCTAssertThrowsError(try SynCapBleOperation.command(address: UUID(), payload: ["op": "capture.start", "name": String(repeating: "x", count: 4096)]) { _ in })
    }

    func testCancelBeforeStartCompletesExactlyOnceWithoutUsingBluetooth() throws {
        let completion = expectation(description: "cancelled")
        completion.assertForOverFulfill = true
        let operation = try SynCapBleOperation.scan { result in
            guard case let .failure(error) = result else { XCTFail("Unexpected success"); return }
            XCTAssertFalse(error.outcomeUnknown)
            completion.fulfill()
        }
        operation.cancel()
        operation.cancel()
        operation.start()
        wait(for: [completion], timeout: 2)
        withExtendedLifetime(operation) {}
    }
}
