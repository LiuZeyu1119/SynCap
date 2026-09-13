#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
sdk_dir="$(cd "$script_dir/.." && pwd)"
apple_dir="$sdk_dir/apple"
build_root="$sdk_dir/target/apple-package"

if [[ "$(uname -s)" != Darwin ]]; then
  echo "Apple SDK packaging requires macOS and full Xcode." >&2
  exit 1
fi

for command in cargo rustup xcrun xcodebuild lipo; do
  command -v "$command" >/dev/null || { echo "Missing tool: $command" >&2; exit 1; }
done

targets=(aarch64-apple-ios aarch64-apple-ios-sim x86_64-apple-ios aarch64-apple-darwin x86_64-apple-darwin)
installed_targets="$(rustup target list --installed)"
for target in "${targets[@]}"; do
  if ! printf '%s\n' "$installed_targets" | /usr/bin/grep -qx "$target"; then
    echo "Missing Rust target: $target. Install with: rustup target add $target" >&2
    exit 1
  fi
done

xcrun --sdk iphoneos --show-sdk-path >/dev/null
xcrun --sdk iphonesimulator --show-sdk-path >/dev/null
mkdir -p "$build_root"
stage="$(mktemp -d "$build_root/stage.XXXXXX")"
echo "Apple package build directory: $stage"

export CARGO_TARGET_DIR="$sdk_dir/target"
export IPHONEOS_DEPLOYMENT_TARGET=15.0
export MACOSX_DEPLOYMENT_TARGET=12.0

cargo build --locked --manifest-path "$sdk_dir/Cargo.toml" -p syncap-ffi --release --lib
(
  cd "$sdk_dir"
  cargo run --locked --manifest-path "$sdk_dir/Cargo.toml" -p syncap-ffi \
    --features bindgen-cli --bin uniffi-bindgen -- \
    generate --library "$sdk_dir/target/release/libsyncap_ffi.dylib" \
    --language swift --out-dir "$stage/generated"
)

for target in "${targets[@]}"; do
  cargo build --locked --manifest-path "$sdk_dir/Cargo.toml" -p syncap-ffi \
    --release --lib --target "$target"
done

mkdir -p "$stage/ios" "$stage/simulator" "$stage/macos" "$stage/headers"
cp "$sdk_dir/target/aarch64-apple-ios/release/libsyncap_ffi.a" "$stage/ios/libsyncap_ffi.a"
lipo -create "$sdk_dir/target/aarch64-apple-ios-sim/release/libsyncap_ffi.a" \
  "$sdk_dir/target/x86_64-apple-ios/release/libsyncap_ffi.a" \
  -output "$stage/simulator/libsyncap_ffi.a"
lipo -create "$sdk_dir/target/aarch64-apple-darwin/release/libsyncap_ffi.a" \
  "$sdk_dir/target/x86_64-apple-darwin/release/libsyncap_ffi.a" \
  -output "$stage/macos/libsyncap_ffi.a"

cp "$stage/generated/SynCapSDKFFI.h" "$stage/headers/SynCapSDKFFI.h"
cp "$stage/generated/SynCapSDKFFI.modulemap" "$stage/headers/module.modulemap"
xcodebuild -create-xcframework \
  -library "$stage/ios/libsyncap_ffi.a" -headers "$stage/headers" \
  -library "$stage/simulator/libsyncap_ffi.a" -headers "$stage/headers" \
  -library "$stage/macos/libsyncap_ffi.a" -headers "$stage/headers" \
  -output "$stage/SynCapSDKFFI.xcframework"

# Retain a prior generated package until this new package is complete.
if [[ -e "$apple_dir/SynCapSDKFFI.xcframework" ]]; then
  mv "$apple_dir/SynCapSDKFFI.xcframework" "$stage/previous-SynCapSDKFFI.xcframework"
fi
mv "$stage/SynCapSDKFFI.xcframework" "$apple_dir/SynCapSDKFFI.xcframework"
mkdir -p "$apple_dir/Sources/SynCapSDK"
cp "$stage/generated/SynCapSDK.swift" "$apple_dir/Sources/SynCapSDK/SynCapSDK.swift"

echo "Built: $apple_dir (SwiftPM with iOS and macOS XCFramework)"
echo "Verify: bash $apple_dir/test.sh"
