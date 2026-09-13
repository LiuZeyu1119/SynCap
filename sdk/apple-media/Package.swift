// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "SynCapMedia",
    platforms: [.iOS(.v15)],
    products: [.library(name: "SynCapMedia", targets: ["SynCapMedia"])],
    targets: [
        .binaryTarget(name: "MobileVLCKit", path: "Vendor/MobileVLCKit.xcframework"),
        .target(name: "SynCapMedia", dependencies: ["MobileVLCKit"]),
    ]
)
