// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SynCapSDK",
    platforms: [.iOS(.v15), .macOS(.v12)],
    products: [
        .library(name: "SynCapSDK", targets: ["SynCapSDK"]),
        .library(name: "SynCapBluetooth", targets: ["SynCapBluetooth"]),
        .executable(name: "syncap-swift-example", targets: ["SynCapExample"]),
    ],
    targets: [
        .binaryTarget(name: "SynCapSDKFFI", path: "SynCapSDKFFI.xcframework"),
        .target(name: "SynCapSDK", dependencies: ["SynCapSDKFFI"]),
        .target(name: "SynCapBluetooth"),
        .testTarget(name: "SynCapBluetoothTests", dependencies: ["SynCapBluetooth"]),
        .executableTarget(name: "SynCapExample", dependencies: ["SynCapSDK"]),
        .testTarget(name: "SynCapSDKTests", dependencies: ["SynCapSDK"]),
    ]
)
