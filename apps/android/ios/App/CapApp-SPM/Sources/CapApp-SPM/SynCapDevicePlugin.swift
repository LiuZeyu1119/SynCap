import Capacitor
import Foundation
import Network
import SynCapMedia

@objc(SynCapDevicePlugin)
public class SynCapDevicePlugin: CAPPlugin, CAPBridgedPlugin {
    private static let defaultDeviceHost = "192.168.1.12"
    public let identifier = "SynCapDevicePlugin"
    public let jsName = "SynCapDevice"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "probe", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getManifest", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getStatus", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getStorage", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "configureStorage", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getCameraConfiguration", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "configureCameraOrientation", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "startCapture", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stopCapture", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "listSessions", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "prepareExport", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "downloadSession", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "scanBle", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "configureWifiBle", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "offlineBleCommand", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "openPreview", returnType: CAPPluginReturnPromise)
    ]

    private let operationLock = NSLock()
    private var bleOperations: [UUID: SynCapBleOperation] = [:]

    @objc func probe(_ call: CAPPluginCall) {
        let host = call.getString("host", Self.defaultDeviceHost)
        let ports = call.getArray("ports", Int.self) ?? [554, 555, 556, 557]
        let paths = call.getArray("paths", String.self) ?? []
        let transports = call.getArray("transports", String.self) ?? []
        guard Self.validHost(host) else {
            call.reject("Invalid device host")
            return
        }

        let group = DispatchGroup()
        let lock = NSLock()
        var results = Array(repeating: false, count: ports.count)
        var probes: [SynCapRTSPProbe] = []

        for (index, value) in ports.enumerated() {
            guard value > 0, value <= 65_535 else { continue }
            guard !transports.indices.contains(index) || transports[index] != "tcp-hevc" else { continue }
            group.enter()
            let probe = SynCapRTSPProbe(host: host, port: UInt16(value)) { online in
                lock.lock()
                results[index] = online
                lock.unlock()
                group.leave()
            }
            probes.append(probe)
            probe.start()
        }

        group.notify(queue: .main) {
            _ = probes
            let streams: [JSObject] = ports.enumerated().map { index, port in
                let transport = transports.indices.contains(index) ? transports[index] : "rtsp"
                let path = paths.indices.contains(index) ? paths[index] : "/PRR"
                return [
                    "port": port,
                    "path": path,
                    "transport": transport,
                    "online": results[index],
                    "url": transport == "tcp-hevc"
                        ? "tcp://\(host):\(port)\(path)"
                        : "rtsp://\(host):\(port)\(path)"
                ]
            }
            let onlineCount = results.filter { $0 }.count
            call.resolve([
                "host": host,
                "reachable": onlineCount > 0,
                "onlineCount": onlineCount,
                "streams": streams
            ])
        }
    }

    @objc func getManifest(_ call: CAPPluginCall) {
        request(call, path: "/v1/manifest", method: "GET", timeout: 2.5)
    }

    @objc func getStatus(_ call: CAPPluginCall) {
        request(call, path: "/v1/status", method: "GET", timeout: 2.5)
    }

    @objc func getStorage(_ call: CAPPluginCall) {
        request(call, path: "/v1/storage", method: "GET", timeout: 2.5)
    }

    @objc func configureStorage(_ call: CAPPluginCall) {
        let target = call.getString("target", "")
        guard Self.validStorageTarget(target) else {
            call.reject("Storage target must be internal or usb")
            return
        }
        request(
            call,
            path: "/v1/storage/configure",
            method: "POST",
            body: ["target": target, "claimCode": "123456"],
            timeout: 15
        )
    }

    @objc func getCameraConfiguration(_ call: CAPPluginCall) {
        request(call, path: "/v1/camera/configuration", method: "GET", timeout: 2.5)
    }

    @objc func configureCameraOrientation(_ call: CAPPluginCall) {
        let rotationDegrees = call.getInt("rotationDegrees", -1)
        guard [0, 90, 180, 270].contains(rotationDegrees) else {
            call.reject("Camera rotation must be 0, 90, 180, or 270")
            return
        }
        request(
            call,
            path: "/v1/camera/configuration",
            method: "POST",
            body: ["rotationDegrees": rotationDegrees, "claimCode": "123456"],
            timeout: 15
        )
    }

    @objc func startCapture(_ call: CAPPluginCall) {
        let name = call.getString("name", "session")
        guard !name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, name.count <= 96 else {
            call.reject("Invalid session name")
            return
        }
        request(call, path: "/v1/captures/start", method: "POST", body: ["name": name], timeout: 60)
    }

    @objc func stopCapture(_ call: CAPPluginCall) {
        let captureId = call.getString("captureId", "")
        guard Self.validIdentifier(captureId, prefix: "cap_") else {
            call.reject("Invalid capture id")
            return
        }
        request(call, path: "/v1/captures/\(captureId)/stop", method: "POST", body: [:], timeout: 60)
    }

    @objc func listSessions(_ call: CAPPluginCall) {
        request(call, path: "/v1/sessions", method: "GET", timeout: 8)
    }

    @objc func prepareExport(_ call: CAPPluginCall) {
        let sessionId = call.getString("sessionId", "")
        guard Self.validIdentifier(sessionId, prefix: "ses_") else {
            call.reject("Invalid session id")
            return
        }
        request(call, path: "/v1/sessions/\(sessionId)/prepare-export", method: "POST", body: [:], timeout: 30)
    }

    @objc func downloadSession(_ call: CAPPluginCall) {
        let host = call.getString("host", Self.defaultDeviceHost)
        let port = call.getInt("port", 8080)
        let sessionId = call.getString("sessionId", "")
        guard Self.validHost(host), Self.validIdentifier(sessionId, prefix: "ses_"), port > 0, port <= 65_535 else {
            call.reject("Invalid export request")
            return
        }
        Task {
            do {
                let result = try await SynCapSessionExporter.export(host: host, port: port, sessionId: sessionId)
                call.resolve(result)
            } catch {
                call.reject("Unable to export session", nil, error)
            }
        }
    }

    @objc func scanBle(_ call: CAPPluginCall) {
        let duration = min(10_000, max(1_500, call.getInt("durationMs", 4_500)))
        retainBleOperation(SynCapBleOperation.scan(durationMs: duration, call: call))
    }

    @objc func configureWifiBle(_ call: CAPPluginCall) {
        let ssid = call.getString("ssid", "")
        let claimCode = call.getString("claimCode", "")
        guard !ssid.isEmpty, Self.validClaimCode(claimCode) else {
            call.reject("SSID and six-digit claim code are required")
            return
        }
        let payload: JSObject = [
            "op": "wifi.configure",
            "ssid": ssid,
            "password": call.getString("password", ""),
            "security": call.getString("security", "wpa2-psk"),
            "claimCode": claimCode
        ]
        startBleCommand(call, payload: payload)
    }

    @objc func offlineBleCommand(_ call: CAPPluginCall) {
        let op = call.getString("op", "")
        let claimCode = call.getString("claimCode", "")
        let allowed = ["storage.configure", "storage.eject", "capture.start", "capture.stop", "device.status"]
        guard allowed.contains(op), Self.validClaimCode(claimCode) else {
            call.reject("Invalid offline BLE command")
            return
        }
        var payload: JSObject = ["op": op, "claimCode": claimCode]
        if let target = call.getString("target") { payload["target"] = target }
        if let name = call.getString("name") { payload["name"] = name }
        if let captureId = call.getString("captureId") { payload["captureId"] = captureId }
        startBleCommand(call, payload: payload)
    }

    @objc func openPreview(_ call: CAPPluginCall) {
        let urls = call.getArray("urls", String.self) ?? []
        let validURLs = urls.compactMap { value -> URL? in
            guard let url = URL(string: value),
                  let scheme = url.scheme?.lowercased(),
                  scheme == "rtsp" || scheme == "tcp" else { return nil }
            return url
        }
        guard !validURLs.isEmpty, validURLs.count == urls.count else {
            call.reject("No valid camera stream was provided")
            return
        }
        let labels = call.getArray("labels", String.self) ?? []
        DispatchQueue.main.async {
            guard let presenter = self.bridge?.viewController else {
                call.reject("Unable to present live preview")
                return
            }
            do {
                let cameraLabels = validURLs.indices.map { labels.indices.contains($0) ? labels[$0] : "CAM \($0)" }
                let preview = try SynCapPreviewViewController(urls: validURLs, labels: cameraLabels)
                presenter.present(preview, animated: true) { call.resolve() }
            } catch {
                call.reject("Unable to open camera preview", nil, error)
            }
        }
    }

    private func startBleCommand(_ call: CAPPluginCall, payload: JSObject) {
        let address = call.getString("address", "")
        guard UUID(uuidString: address) != nil else {
            call.reject("Invalid Bluetooth device identifier")
            return
        }
        retainBleOperation(SynCapBleOperation.command(address: address, payload: payload, call: call))
    }

    private func retainBleOperation(_ operation: SynCapBleOperation) {
        operation.onFinish = { [weak self, weak operation] in
            guard let self, let operation else { return }
            self.operationLock.lock()
            self.bleOperations.removeValue(forKey: operation.id)
            self.operationLock.unlock()
        }
        operationLock.lock()
        bleOperations[operation.id] = operation
        operationLock.unlock()
        operation.start()
    }

    private func request(
        _ call: CAPPluginCall,
        path: String,
        method: String,
        body: JSObject? = nil,
        timeout: TimeInterval
    ) {
        let host = call.getString("host", Self.defaultDeviceHost)
        let port = call.getInt("port", 8080)
        guard Self.validHost(host), port > 0, port <= 65_535, path.hasPrefix("/"), !path.contains(".."),
              let url = URL(string: "http://\(host):\(port)\(path)") else {
            call.reject("Invalid device request")
            return
        }
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            do {
                request.httpBody = try JSONSerialization.data(withJSONObject: body)
                request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
            } catch {
                call.reject("Unable to encode device request", nil, error)
                return
            }
        }
        URLSession.shared.dataTask(with: request) { data, response, error in
            if let error {
                call.reject("Unable to reach device service", nil, error)
                return
            }
            guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode), let data else {
                call.reject("Device service returned an error")
                return
            }
            do {
                guard let object = try JSONSerialization.jsonObject(with: data) as? JSObject else {
                    throw SynCapPluginError.invalidJSON
                }
                call.resolve(object)
            } catch {
                call.reject("Device returned invalid JSON", nil, error)
            }
        }.resume()
    }

    static func validHost(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 253 && value.allSatisfy {
            $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "." || $0 == "-")
        }
    }

    static func validIdentifier(_ value: String, prefix: String) -> Bool {
        value.hasPrefix(prefix) && value.count <= 64 && value.allSatisfy {
            $0.isASCII && ($0.isLetter || $0.isNumber || $0 == "_")
        }
    }

    static func validStorageTarget(_ value: String) -> Bool {
        value == "internal" || value == "usb"
    }

    static func validClaimCode(_ value: String) -> Bool {
        value.count == 6 && value.allSatisfy { $0.isNumber }
    }
}

enum SynCapPluginError: Error {
    case invalidJSON
}

private final class SynCapRTSPProbe {
    private let host: String
    private let port: UInt16
    private let completion: (Bool) -> Void
    private let lock = NSLock()
    private var connection: NWConnection?
    private var finished = false

    init(host: String, port: UInt16, completion: @escaping (Bool) -> Void) {
        self.host = host
        self.port = port
        self.completion = completion
    }

    func start() {
        let connection = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(rawValue: port)!, using: .tcp)
        self.connection = connection
        connection.stateUpdateHandler = { [weak self] state in
            guard let self else { return }
            switch state {
            case .ready:
                let request = "DESCRIBE rtsp://\(self.host):\(self.port)/PRR RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\nUser-Agent: SynCap-Studio\r\n\r\n"
                connection.send(content: request.data(using: .ascii), completion: .contentProcessed { error in
                    if error != nil { self.complete(false); return }
                    connection.receive(minimumIncompleteLength: 1, maximumLength: 256) { data, _, _, _ in
                        let status = data.flatMap { String(data: $0, encoding: .ascii) } ?? ""
                        self.complete(status.hasPrefix("RTSP/1.0 200"))
                    }
                })
            case .failed, .cancelled:
                self.complete(false)
            default:
                break
            }
        }
        connection.start(queue: DispatchQueue.global(qos: .userInitiated))
        DispatchQueue.global().asyncAfter(deadline: .now() + 0.9) { [weak self] in self?.complete(false) }
    }

    private func complete(_ online: Bool) {
        lock.lock()
        guard !finished else { lock.unlock(); return }
        finished = true
        lock.unlock()
        connection?.cancel()
        completion(online)
    }
}
