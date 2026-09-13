import Foundation
import SynCapSDK

@main
struct InspectDevice {
    static func main() async {
        guard CommandLine.arguments.count == 2 else {
            print("Usage: syncap-swift-example http://DEVICE_IP:8080")
            return
        }
        do {
            let device = try DeviceClient(endpoint: CommandLine.arguments[1])
            let state = try await device.currentCapture()
            print("state=\(state.state) elapsedMs=\(state.elapsedMs.map(String.init) ?? "unknown")")
            for session in try await device.sessions() {
                print("\(session.id) bytes=\(session.sizeBytes.map(String.init) ?? "unknown")")
            }
        } catch {
            FileHandle.standardError.write(Data("\(error)\n".utf8))
            exit(1)
        }
    }
}
