import Capacitor
import CryptoKit
import Foundation

enum SynCapSessionExporter {
    static func export(host: String, port: Int, sessionId: String) async throws -> JSObject {
        let manifest = try await requestJSON(
            url: apiURL(host: host, port: port, path: "/v1/sessions/\(sessionId)/prepare-export"),
            method: "POST",
            body: [:],
            timeout: 30
        )
        guard let files = manifest["files"] as? [JSObject] else {
            throw SynCapExportError.invalidManifest
        }
        let sessionManifestURL = try Self.sessionManifestURL(
            manifest["manifestUrl"],
            host: host,
            port: port,
            sessionId: sessionId
        )
        let documents = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first!
        let destination = documents.appendingPathComponent("SynCap", isDirectory: true)
            .appendingPathComponent(sessionId, isDirectory: true)
        try FileManager.default.createDirectory(at: destination, withIntermediateDirectories: true)

        let manifestBytes = try await downloadSessionManifest(
            url: sessionManifestURL,
            output: destination.appendingPathComponent("session.json"),
            sessionId: sessionId
        )
        var totalBytes = manifestBytes
        var verifiedFiles = 0
        var savedFileNames = Set<String>()
        for item in files {
            guard let name = item["name"] as? String,
                  let expectedHash = item["sha256"] as? String,
                  let relativeURL = item["url"] as? String,
                  let expectedSize = number(item["sizeBytes"]),
                  validFileName(name), name != "session.json",
                  savedFileNames.insert(name).inserted,
                  relativeURL.hasPrefix("/v1/sessions/") else {
                throw SynCapExportError.invalidManifest
            }
            try await download(
                url: apiURL(host: host, port: port, path: relativeURL),
                output: destination.appendingPathComponent(name),
                expectedSize: expectedSize,
                expectedHash: expectedHash
            )
            totalBytes += expectedSize
            verifiedFiles += 1
        }
        let manifestSaved = true
        return [
            "sessionId": sessionId,
            "path": destination.path,
            "bytes": NSNumber(value: totalBytes),
            "files": files.count + 1,
            "verifiedFiles": verifiedFiles,
            "manifestSaved": manifestSaved,
            "manifestFile": "session.json",
            "verified": manifestSaved && verifiedFiles == files.count
        ]
    }

    private static func downloadSessionManifest(
        url: URL,
        output: URL,
        sessionId: String
    ) async throws -> Int64 {
        var request = URLRequest(url: url, timeoutInterval: 30)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              !data.isEmpty, data.count <= 4 * 1024 * 1024,
              let object = try JSONSerialization.jsonObject(with: data) as? JSObject,
              sessionManifestMatches(object, sessionId: sessionId) else {
            throw SynCapExportError.invalidManifest
        }
        try data.write(to: output, options: .atomic)
        return Int64(data.count)
    }

    private static func download(
        url: URL,
        output: URL,
        expectedSize: Int64,
        expectedHash: String
    ) async throws {
        if fileSize(output) == expectedSize, try sha256(output).caseInsensitiveCompare(expectedHash) == .orderedSame {
            return
        }
        let partial = output.appendingPathExtension("part")
        if fileSize(partial) > expectedSize {
            try? FileManager.default.removeItem(at: partial)
        }
        let existing = max(0, fileSize(partial))
        var request = URLRequest(url: url, timeoutInterval: 3_600)
        if existing > 0 {
            request.setValue("bytes=\(existing)-", forHTTPHeaderField: "Range")
        }
        let (temporary, response) = try await URLSession.shared.download(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode) else {
            throw SynCapExportError.downloadFailed
        }
        if existing > 0, http.statusCode == 206 {
            if !FileManager.default.fileExists(atPath: partial.path) {
                FileManager.default.createFile(atPath: partial.path, contents: nil)
            }
            let destination = try FileHandle(forWritingTo: partial)
            try destination.seekToEnd()
            let source = try FileHandle(forReadingFrom: temporary)
            while let data = try source.read(upToCount: 1024 * 1024), !data.isEmpty {
                try destination.write(contentsOf: data)
            }
            try source.close()
            try destination.close()
        } else {
            try? FileManager.default.removeItem(at: partial)
            try FileManager.default.moveItem(at: temporary, to: partial)
        }
        guard fileSize(partial) == expectedSize else {
            throw SynCapExportError.sizeMismatch
        }
        guard try sha256(partial).caseInsensitiveCompare(expectedHash) == .orderedSame else {
            throw SynCapExportError.hashMismatch
        }
        try? FileManager.default.removeItem(at: output)
        try FileManager.default.moveItem(at: partial, to: output)
    }

    private static func requestJSON(
        url: URL,
        method: String,
        body: JSObject?,
        timeout: TimeInterval
    ) async throws -> JSObject {
        var request = URLRequest(url: url, timeoutInterval: timeout)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let body {
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
            request.setValue("application/json; charset=utf-8", forHTTPHeaderField: "Content-Type")
        }
        let (data, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, (200..<300).contains(http.statusCode),
              let result = try JSONSerialization.jsonObject(with: data) as? JSObject else {
            throw SynCapExportError.invalidManifest
        }
        return result
    }

    private static func apiURL(host: String, port: Int, path: String) throws -> URL {
        guard SynCapDevicePlugin.validHost(host), port > 0, port <= 65_535,
              path.hasPrefix("/"), !path.contains(".."),
              let url = URL(string: "http://\(host):\(port)\(path)") else {
            throw SynCapExportError.invalidURL
        }
        return url
    }

    static func sessionManifestURL(
        _ value: Any?,
        host: String,
        port: Int,
        sessionId: String
    ) throws -> URL {
        guard let value = value as? String, !value.isEmpty else {
            throw SynCapExportError.invalidManifest
        }
        let expectedPath = "/v1/sessions/\(sessionId)/manifest"
        let url: URL
        if value.hasPrefix("/") {
            url = try apiURL(host: host, port: port, path: value)
        } else {
            guard let absoluteURL = URL(string: value),
                  let scheme = absoluteURL.scheme?.lowercased(),
                  scheme == "http" || scheme == "https" else {
                throw SynCapExportError.invalidURL
            }
            url = absoluteURL
        }
        let defaultPort = url.scheme?.lowercased() == "https" ? 443 : 80
        guard url.host?.caseInsensitiveCompare(host) == .orderedSame,
              (url.port ?? defaultPort) == port,
              url.path == expectedPath,
              url.user == nil, url.password == nil, url.fragment == nil else {
            throw SynCapExportError.invalidURL
        }
        return url
    }

    static func sessionManifestMatches(_ object: JSObject, sessionId: String) -> Bool {
        for key in ["id", "sessionId", "session_id"] where object[key] != nil {
            guard let value = object[key] as? String, value == sessionId else { return false }
        }
        return true
    }

    private static func sha256(_ url: URL) throws -> String {
        let file = try FileHandle(forReadingFrom: url)
        var digest = SHA256()
        while let data = try file.read(upToCount: 1024 * 1024), !data.isEmpty {
            digest.update(data: data)
        }
        try file.close()
        return digest.finalize().map { String(format: "%02x", $0) }.joined()
    }

    private static func fileSize(_ url: URL) -> Int64 {
        let value = try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize
        return Int64(value ?? -1)
    }

    private static func number(_ value: Any?) -> Int64? {
        if let value = value as? NSNumber { return value.int64Value }
        if let value = value as? Int64 { return value }
        if let value = value as? Int { return Int64(value) }
        return nil
    }

    private static func validFileName(_ value: String) -> Bool {
        !value.isEmpty && value.count <= 160 && value.allSatisfy {
            $0.isASCII && ($0.isLetter || $0.isNumber || ".-_".contains($0))
        }
    }
}

enum SynCapExportError: Error {
    case invalidURL
    case invalidManifest
    case downloadFailed
    case sizeMismatch
    case hashMismatch
}
