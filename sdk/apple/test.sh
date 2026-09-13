#!/usr/bin/env bash
set -euo pipefail

apple_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -d "$apple_dir/SynCapSDKFFI.xcframework" ]]; then
  echo "Run bash sdk/scripts/build-apple.sh first." >&2
  exit 1
fi

swift test --package-path "$apple_dir"

# Compile the same generated Swift API for each iOS platform. Tests use an
# isolated localhost server on macOS; they do not control a connected camera.
for platform in "generic/platform=iOS" "generic/platform=iOS Simulator"; do
  for scheme in SynCapSDK SynCapBluetooth; do
  (
    cd "$apple_dir"
    xcodebuild -quiet -scheme "$scheme" -destination "$platform" \
      -derivedDataPath "$apple_dir/.build/xcode" \
      CODE_SIGNING_ALLOWED=NO build
  )
  done
done

# Set an existing simulator UUID explicitly to additionally run the HTTP tests
# on iOS. No simulator is created, reset, or deleted by this script.
if [[ -n "${SYNCAP_IOS_SIMULATOR_ID:-}" ]]; then
  (
    cd "$apple_dir"
    xcodebuild -quiet -scheme SynCapSDK-Package \
      -destination "platform=iOS Simulator,id=$SYNCAP_IOS_SIMULATOR_ID" \
      -derivedDataPath "$apple_dir/.build/xcode" \
      CODE_SIGNING_ALLOWED=NO test
  )
fi
