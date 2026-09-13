#!/usr/bin/env bash
set -euo pipefail
sdk_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
media_dir="$sdk_dir/apple-media"
if [[ ! -f "$media_dir/Vendor/MobileVLCKit.xcframework/Info.plist" ]]; then
    echo "Missing MobileVLCKit. Restore it using the instructions in sdk/apple-media/README.md." >&2
    exit 1
fi
for platform in "generic/platform=iOS" "generic/platform=iOS Simulator"; do
    (
        cd "$media_dir"
        xcodebuild -quiet -scheme SynCapMedia -destination "$platform" \
            -derivedDataPath "$media_dir/.build/xcode" CODE_SIGNING_ALLOWED=NO build
    )
done
