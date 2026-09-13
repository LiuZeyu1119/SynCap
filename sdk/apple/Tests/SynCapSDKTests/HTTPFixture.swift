import Foundation
import Darwin

struct HTTPRequest {
    let method: String
    let path: String
    let headers: [String: String]
    let body: Data
}

struct HTTPResponse {
    let status: String
    let body: Data
    var headers: [String: String] = [:]

    init(_ status: String = "200 OK", json: String) {
        self.status = status
        self.body = Data(json.utf8)
    }

    init(_ status: String = "200 OK", body: Data, headers: [String: String] = [:]) {
        self.status = status
        self.body = body
        self.headers = headers
    }
}

/// Small isolated HTTP/1.1 fixture: no camera, App, or external service needed.
final class HTTPFixture {
    private let listener: Int32
    private let queue = DispatchQueue(label: "com.syncap.sdk.tests.http")
    private let lock = NSLock()
    private var received: [HTTPRequest] = []
    private var connections: Set<Int32> = []
    private var stopped = false
    private let handler: (HTTPRequest) -> HTTPResponse?
    let endpoint: String

    var requests: [HTTPRequest] {
        lock.lock()
        defer { lock.unlock() }
        return received
    }

    init(handler: @escaping (HTTPRequest) -> HTTPResponse?) throws {
        self.handler = handler
        let descriptor = Darwin.socket(AF_INET, SOCK_STREAM, 0)
        guard descriptor >= 0 else { throw Self.socketError() }
        var address = sockaddr_in()
        address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        address.sin_family = sa_family_t(AF_INET)
        address.sin_addr.s_addr = inet_addr("127.0.0.1")
        let bound = withUnsafePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                Darwin.bind(descriptor, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
            }
        }
        guard bound == 0, Darwin.listen(descriptor, 8) == 0 else {
            Darwin.close(descriptor)
            throw Self.socketError()
        }
        var size = socklen_t(MemoryLayout<sockaddr_in>.size)
        let named = withUnsafeMutablePointer(to: &address) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { getsockname(descriptor, $0, &size) }
        }
        guard named == 0, address.sin_port != 0 else {
            Darwin.close(descriptor)
            throw Self.socketError()
        }
        listener = descriptor
        endpoint = "http://127.0.0.1:\(UInt16(bigEndian: address.sin_port))"
        queue.async { [weak self] in self?.acceptRequests() }
    }

    func close() {
        lock.lock()
        guard !stopped else { lock.unlock(); return }
        stopped = true
        let active = connections
        connections.removeAll()
        lock.unlock()
        Darwin.shutdown(listener, SHUT_RDWR)
        Darwin.close(listener)
        for descriptor in active {
            Darwin.shutdown(descriptor, SHUT_RDWR)
            Darwin.close(descriptor)
        }
    }

    private static func socketError() -> NSError {
        NSError(domain: NSPOSIXErrorDomain, code: Int(errno))
    }

    private func acceptRequests() {
        while true {
            let descriptor = Darwin.accept(listener, nil, nil)
            guard descriptor >= 0 else { return }
            lock.lock()
            if stopped { lock.unlock(); Darwin.close(descriptor); return }
            connections.insert(descriptor)
            lock.unlock()
            var noSignal: Int32 = 1
            setsockopt(descriptor, SOL_SOCKET, SO_NOSIGPIPE, &noSignal, socklen_t(MemoryLayout<Int32>.size))
            receive(descriptor)
        }
    }

    private func finish(_ descriptor: Int32) {
        lock.lock()
        let owned = connections.remove(descriptor) != nil
        lock.unlock()
        if owned { Darwin.close(descriptor) }
    }

    private func receive(_ descriptor: Int32) {
        var bytes = Data()
        var buffer = [UInt8](repeating: 0, count: 4096)
        while bytes.count < 64 * 1024 {
            let count = Darwin.recv(descriptor, &buffer, buffer.count, 0)
            guard count > 0 else { finish(descriptor); return }
            bytes.append(contentsOf: buffer.prefix(count))
            if let boundary = bytes.range(of: Data("\r\n\r\n".utf8)),
               let text = String(data: bytes[..<boundary.lowerBound], encoding: .utf8) {
                let lines = text.components(separatedBy: "\r\n")
                let requestLine = lines[0].split(separator: " ")
                var headers: [String: String] = [:]
                for line in lines.dropFirst() {
                    if let colon = line.firstIndex(of: ":") {
                        headers[String(line[..<colon]).lowercased()] = line[line.index(after: colon)...].trimmingCharacters(in: .whitespaces)
                    }
                }
                let length = Int(headers["content-length"] ?? "0") ?? 0
                if requestLine.count >= 2, bytes.count - boundary.upperBound >= length {
                    let request = HTTPRequest(
                        method: String(requestLine[0]), path: String(requestLine[1]),
                        headers: headers, body: Data(bytes[boundary.upperBound..<(boundary.upperBound + length)])
                    )
                    lock.lock()
                    received.append(request)
                    lock.unlock()
                    if let response = handler(request) {
                        var header = "HTTP/1.1 \(response.status)\r\nContent-Length: \(response.body.count)\r\nConnection: close\r\n"
                        for (name, value) in response.headers { header += "\(name): \(value)\r\n" }
                        var payload = Data((header + "\r\n").utf8)
                        payload.append(response.body)
                        payload.withUnsafeBytes { pointer in
                            var sent = 0
                            while sent < pointer.count {
                                let count = Darwin.send(descriptor, pointer.baseAddress!.advanced(by: sent), pointer.count - sent, 0)
                                if count <= 0 { break }
                                sent += count
                            }
                        }
                        finish(descriptor)
                    }
                    return
                }
            }
        }
        finish(descriptor)
    }
}
