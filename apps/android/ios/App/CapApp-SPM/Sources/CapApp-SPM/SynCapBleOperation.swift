import Capacitor
import CoreBluetooth
import Foundation

final class SynCapBleOperation: NSObject, CBCentralManagerDelegate, CBPeripheralDelegate {
    enum Kind {
        case scan(durationMs: Int)
        case command(address: UUID, payload: JSObject)
    }

    let id = UUID()
    var onFinish: (() -> Void)?

    private static let serviceUUID = CBUUID(string: "8f7a0001-6c2b-4dd4-9f1a-53f65c9b40d1")
    private static let configUUID = CBUUID(string: "8f7a0002-6c2b-4dd4-9f1a-53f65c9b40d1")
    private static let statusUUID = CBUUID(string: "8f7a0003-6c2b-4dd4-9f1a-53f65c9b40d1")

    private let kind: Kind
    private let call: CAPPluginCall
    private var central: CBCentralManager!
    private var selectedPeripheral: CBPeripheral?
    private var configCharacteristic: CBCharacteristic?
    private var statusCharacteristic: CBCharacteristic?
    private var chunks: [Data] = []
    private var chunkIndex = 0
    private var scanResults: [UUID: JSObject] = [:]
    private var completed = false
    private var deadline: DispatchWorkItem?

    private init(kind: Kind, call: CAPPluginCall) {
        self.kind = kind
        self.call = call
    }

    static func scan(durationMs: Int, call: CAPPluginCall) -> SynCapBleOperation {
        SynCapBleOperation(kind: .scan(durationMs: durationMs), call: call)
    }

    static func command(address: String, payload: JSObject, call: CAPPluginCall) -> SynCapBleOperation {
        SynCapBleOperation(kind: .command(address: UUID(uuidString: address)!, payload: payload), call: call)
    }

    func start() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.central = CBCentralManager(delegate: self, queue: .main)
            let timeout = DispatchWorkItem { [weak self] in self?.reject("BLE operation timed out") }
            self.deadline = timeout
            DispatchQueue.main.asyncAfter(deadline: .now() + 120, execute: timeout)
        }
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
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

    func centralManager(
        _ central: CBCentralManager,
        didDiscover peripheral: CBPeripheral,
        advertisementData: [String: Any],
        rssi RSSI: NSNumber
    ) {
        guard !completed else { return }
        let name = (advertisementData[CBAdvertisementDataLocalNameKey] as? String) ?? peripheral.name ?? ""
        guard name.hasPrefix("SynCap-") else { return }
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

    func centralManager(_ central: CBCentralManager, didConnect peripheral: CBPeripheral) {
        peripheral.discoverServices([Self.serviceUUID])
    }

    func centralManager(_ central: CBCentralManager, didFailToConnect peripheral: CBPeripheral, error: Error?) {
        reject("BLE connection failed", error: error)
    }

    func centralManager(_ central: CBCentralManager, didDisconnectPeripheral peripheral: CBPeripheral, error: Error?) {
        if !completed { reject("BLE device disconnected during operation", error: error) }
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverServices error: Error?) {
        if let error { reject("BLE service discovery failed", error: error); return }
        guard let service = peripheral.services?.first(where: { $0.uuid == Self.serviceUUID }) else {
            reject("Selected device does not expose SynCap provisioning")
            return
        }
        peripheral.discoverCharacteristics([Self.configUUID, Self.statusUUID], for: service)
    }

    func peripheral(_ peripheral: CBPeripheral, didDiscoverCharacteristicsFor service: CBService, error: Error?) {
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
            chunks = Self.fragment(data, payloadSize: maximum)
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
        peripheral.writeValue(chunks[chunkIndex], for: characteristic, type: .withResponse)
    }

    func peripheral(_ peripheral: CBPeripheral, didWriteValueFor characteristic: CBCharacteristic, error: Error?) {
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

    func peripheral(_ peripheral: CBPeripheral, didUpdateValueFor characteristic: CBCharacteristic, error: Error?) {
        guard characteristic.uuid == Self.statusUUID else { return }
        if let error { reject("Encrypted BLE status read failed", error: error); return }
        do {
            guard let data = characteristic.value,
                  let result = try JSONSerialization.jsonObject(with: data) as? JSObject else {
                throw SynCapPluginError.invalidJSON
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

    private func resolve(_ result: JSObject) {
        guard markCompleted() else { return }
        call.resolve(result)
        cleanup()
    }

    private func reject(_ message: String, error: Error? = nil) {
        guard markCompleted() else { return }
        call.reject(message, nil, error)
        cleanup()
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

    private static func fragment(_ data: Data, payloadSize: Int) -> [Data] {
        let count = max(1, Int(ceil(Double(data.count) / Double(payloadSize))))
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
