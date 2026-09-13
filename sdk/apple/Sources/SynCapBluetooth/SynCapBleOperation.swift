import CoreBluetooth
import Foundation

public struct SynCapBluetoothError: Error {
    public let message: String
    public let outcomeUnknown: Bool
}

public final class SynCapBleOperation: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    enum Kind {
        case scan(durationMs: Int)
        case command(address: UUID, payload: [String: Any])
    }

    public typealias Completion = (Result<[String: Any], SynCapBluetoothError>) -> Void
    public let id = UUID()
    public var onFinish: (() -> Void)?

    private static let serviceUUID = CBUUID(string: "8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1")
    private static let configUUID = CBUUID(string: "8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1")
    private static let statusUUID = CBUUID(string: "8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1")

    private let kind: Kind
    private let completion: Completion
    private var central: CBCentralManager!
    private var selectedPeripheral: CBPeripheral?
    private var configCharacteristic: CBCharacteristic?
    private var statusCharacteristic: CBCharacteristic?
    private var chunks: [Data] = []
    private var chunkIndex = 0
    private var scanResults: [UUID: [String: Any]] = [:]
    private var completed = false
    private var started = false
    private var writeStarted = false
    private var deadline: DispatchWorkItem?

    private init(kind: Kind, completion: @escaping Completion) {
        self.kind = kind
        self.completion = completion
    }

    /// Keep the returned operation alive until completion, and start it explicitly.
    public static func scan(durationMs: Int = 10000, completion: @escaping Completion) throws -> SynCapBleOperation {
        guard (1000...30000).contains(durationMs) else {
            throw SynCapBluetoothError(message: "Invalid scan duration", outcomeUnknown: false)
        }
        return SynCapBleOperation(kind: .scan(durationMs: durationMs), completion: completion)
    }

    /// CoreBluetooth identifiers are UUIDs, not Android Bluetooth MAC addresses.
    public static func command(address: UUID, payload: [String: Any], completion: @escaping Completion) throws -> SynCapBleOperation {
        let allowed = ["wifi.scan", "wifi.configure", "device.status", "storage.configure", "storage.eject", "capture.start", "capture.stop"]
        guard let op = payload["op"] as? String, allowed.contains(op) else {
            throw SynCapBluetoothError(message: "Unsupported Bluetooth operation", outcomeUnknown: false)
        }
        var request = payload
        request["claimCode"] = "123456"
        let encoded = try JSONSerialization.data(withJSONObject: request)
        guard !encoded.isEmpty, encoded.count <= 4096 else {
            throw SynCapBluetoothError(message: "Bluetooth command exceeds packet limit", outcomeUnknown: false)
        }
        return SynCapBleOperation(kind: .command(address: address, payload: request), completion: completion)
    }

    public static func wifiConfiguration(ssid: String, password: String, security: String = "wpa2-psk") throws -> [String: Any] {
        guard (1...32).contains(ssid.utf8.count), !ssid.contains("\0"),
              ["open", "wpa2-psk", "wpa3-sae"].contains(security),
              security == "open" ? password.isEmpty : ((8...63).contains(password.utf8.count) && !password.contains("\0")) else {
            throw SynCapBluetoothError(message: "Invalid Wi-Fi configuration", outcomeUnknown: false)
        }
        return ["op": "wifi.configure", "ssid": ssid, "password": password, "security": security]
    }

    public func cancel() {
        DispatchQueue.main.async { [weak self] in self?.reject("Bluetooth operation cancelled") }
    }

    public func start() {
        DispatchQueue.main.async { [weak self] in
            guard let self, !self.started, !self.completed else { return }
            self.started = true
            self.central = CBCentralManager(delegate: self, queue: .main)
            let timeout = DispatchWorkItem { [weak self] in self?.reject("BLE operation timed out") }
            self.deadline = timeout
            DispatchQueue.main.asyncAfter(deadline: .now() + 120, execute: timeout)
        }
    }

    public func centralManagerDidUpdateState(_ central: CBCentralManager) {
        guard !completed else { return }
        guard central.state == .poweredOn else {
            if central.state == .poweredOff || central.state == .unauthorized || central.state == .unsupported {
                reject("Bluetooth is unavailable or disabled")
            }
            return
        }
        central.scanForPeripherals(withServices: [Self.serviceUUID], options: [CBCentralManagerScanOptionAllowDuplicatesKey: true])
        if case let .scan(durationMs) = kind {
            DispatchQueue.main.asyncAfter(deadline: .now() + .milliseconds(durationMs)) { [weak self] in
                self?.finishScan()
            }
        }
    }

    public func centralManager(
        _ central: CBCentralManager,
        didDiscover peripheral: CBPeripheral,
        advertisementData: [String: Any],
        rssi RSSI: NSNumber
    ) {
        guard !completed else { return }
        let name = (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? peripheral.name ?? ""
        switch kind {
        case .scan:
            scanResults[peripheral.identifier] = [
                "id": peripheral.identifier.uuidString,
                "address": peripheral.identifier.uuidString,
                "name": name,
                "rssi": RSSI.intValue
            ]
        case let .command(address, _):
            guard peripheral.identifier == address, selectedPeripheral == nil else { return }
            selectedPeripheral = peripheral
            peripheral.delegate = self
            central.stopScan()
            central.connect(peripheral)
        }
    }

    public func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        peripheral.discoverServices([Self.serviceUUID])
    }

    public func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        reject("BLE connection failed", error: error)
    }

    public func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        if !completed { reject("BLE device disconnected during operation", error: error) }
    }

    public func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error { reject("BLE service discovery failed", error: error); return }
        guard let service = peripheral.services?.first(where: { $0.uuid == Self.serviceUUID }) else {
            reject("Selected device does not expose SynCap provisioning")
            return
        }
        peripheral.discoverCharacteristics([Self.configUUID, Self.statusUUID], for: service)
    }

    public func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
        if let error { reject("BLE characteristic discovery failed", error: error); return }
        configCharacteristic = service.characteristics?.first(where: { $0.uuid == Self.configUUID })
        statusCharacteristic = service.characteristics?.first(where: { $0.uuid == Self.statusUUID })
        guard configCharacteristic != nil, statusCharacteristic != nil else {
            reject("SynCap provisioning characteristics are incomplete")
            return
        }
        guard case let .command(_, payload) = kind else {
            reject("Invalid BLE operation state")
            return
        }
        do {
            let data = try JSONSerialization.data(withJSONObject: payload)
            let maximum = max(1, peripheral.maximumWriteValueLength(for: .withResponse) - 16)
            chunks = try Self.fragment(data, payloadSize: maximum)
            writeNextChunk()
        } catch {
            reject("Unable to encode BLE command", error: error)
        }
    }

    private func writeNextChunk() {
        guard !completed, let peripheral = selectedPeripheral, let characteristic = configCharacteristic,
              chunkIndex < chunks.count else {
            reject("BLE command payload was not prepared")
            return
        }
        writeStarted = true
        peripheral.writeValue(chunks[chunkIndex], for: characteristic, type: .withResponse)
    }

    public func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
        guard characteristic.uuid == Self.configUUID else { return }
        if let error { reject("Encrypted BLE write failed", error: error); return }
        chunkIndex += 1
        if chunkIndex < chunks.count {
            writeNextChunk()
        } else {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in self?.readStatus() }
        }
    }

    private func readStatus() {
        guard !completed, let peripheral = selectedPeripheral, let characteristic = statusCharacteristic else { return }
        peripheral.readValue(for: characteristic)
    }

    public func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard characteristic.uuid == Self.statusUUID else { return }
        if let error { reject("Encrypted BLE status read failed", error: error); return }
        do {
            guard let data = characteristic.value, (1...4096).contains(data.count),
                  let result = try JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                throw SynCapBluetoothError(message: "Invalid BLE status JSON", outcomeUnknown: writeStarted)
            }
            let state = result["state"] as? String ?? "unknown"
            if state == "working" || state == "connecting" {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in self?.readStatus() }
            } else {
                resolve(result)
            }
        } catch {
            reject("Device returned invalid BLE operation status", error: error)
        }
    }

    private func finishScan() {
        guard case .scan = kind else { return }
        let devices = scanResults.values.sorted {
            ($0["rssi"] as? Int ?? -127) > ($1["rssi"] as? Int ?? -127)
        }
        resolve(["devices": devices])
    }

    private func resolve(_ result: [String: Any]) {
        guard markCompleted() else { return }
        cleanup()
        completion(.success(result))
    }

    private func reject(_ message: String, error: Error? = nil) {
        guard markCompleted() else { return }
        cleanup()
        completion(.failure(SynCapBluetoothError(message: message, outcomeUnknown: writeStarted)))
    }

    private func markCompleted() -> Bool {
        guard !completed else { return false }
        completed = true
        deadline?.cancel()
        return true
    }

    private func cleanup() {
        central?.stopScan()
        if let selectedPeripheral { central?.cancelPeripheralConnection(selectedPeripheral) }
        onFinish?()
    }

    static func fragment(_ data: Data, payloadSize: Int) throws -> [Data] {
        guard (1...4096).contains(data.count), (1...496).contains(payloadSize) else {
            throw SynCapBluetoothError(message: "Invalid BLE payload size", outcomeUnknown: false)
        }
        let count = max(1, Int(ceil(Double(data.count) / Double(payloadSize))))
        guard count <= 128 else {
            throw SynCapBluetoothError(message: "Command exceeds negotiated BLE packet limit", outcomeUnknown: false)
        }
        let messageId = UInt32.random(in: UInt32.min...UInt32.max)
        return (0..<count).map { index in
            let offset = index * payloadSize
            let length = min(payloadSize, data.count - offset)
            var result = Data([0x53, 0x43, 1, 1])
            result.appendBigEndian(messageId)
            result.appendBigEndian(UInt16(index))
            result.appendBigEndian(UInt16(count))
            result.appendBigEndian(UInt16(length))
            result.appendBigEndian(UInt16(0))
            result.append(data.subdata(in: offset..<(offset + length)))
            return result
        }
    }
}

private extension Data {
    mutating func appendBigEndian<T: FixedWidthInteger>(_ value: T) {
        var encoded = value.bigEndian
        Swift.withUnsafeBytes(of: &encoded) { append(contentsOf: $0) }
    }
}
