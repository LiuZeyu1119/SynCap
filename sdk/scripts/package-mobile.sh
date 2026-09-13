#!/usr/bin/env bash
set -euo pipefail

sdk_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
output_dir="${1:-$sdk_dir/../artifacts/2026-09-13-sdk-preview-fixed}"
android_dir="$sdk_dir/android"
apple_dir="$sdk_dir/apple"
media_dir="$sdk_dir/apple-media"
cli="$sdk_dir/target/release/syncap"
version=0.1.0
android_version=0.1.1
aar="$android_dir/build/maven/com/syncap/syncap-sdk/$version/syncap-sdk-$version.aar"

for required in "$aar" "$cli" "$apple_dir/Package.swift" \
    "$android_dir/build/maven/com/syncap/syncap-bluetooth-android/$version/syncap-bluetooth-android-$version.aar" \
    "$android_dir/build/maven/com/syncap/syncap-media-android/$android_version/syncap-media-android-$android_version.aar" \
    "$media_dir/Vendor/MobileVLCKit.xcframework/Info.plist" \
    "$apple_dir/Sources/SynCapSDK/SynCapSDK.swift" \
    "$apple_dir/SynCapSDKFFI.xcframework/Info.plist"; do
    if [[ ! -s "$required" ]]; then
        echo "Missing generated SDK artifact: $required. Run the mobile build scripts first." >&2
        exit 1
    fi
done
command -v zip >/dev/null || { echo "Missing zip" >&2; exit 1; }
command -v shasum >/dev/null || { echo "Missing shasum" >&2; exit 1; }
if ! file "$cli" | grep -q 'Mach-O 64-bit executable arm64'; then
    echo "Expected the verified macOS arm64 CLI at $cli" >&2
    exit 1
fi
mkdir -p "$output_dir"
output_dir="$(cd "$output_dir" && pwd)"
stage="$(mktemp -d "$output_dir/package.XXXXXX")"
android_name="SynCapSDK-Android-$android_version"
apple_name="SynCapSDK-Apple-$version"
media_name="SynCapSDK-AppleMedia-$version"
rust_name="SynCapSDK-Rust-$version"
android_bundle="$stage/$android_name"
apple_bundle="$stage/$apple_name"
media_bundle="$stage/$media_name"
rust_bundle="$stage/$rust_name"
mkdir -p "$android_bundle/example/consumer" "$apple_bundle" "$media_bundle/Vendor" "$rust_bundle"

# These three crates form an independent workspace; do not ship caches or recordings.
cp "$sdk_dir/Cargo.toml" "$sdk_dir/Cargo.lock" "$rust_bundle/"
cp "$sdk_dir/TERMINAL.md" "$rust_bundle/README.md"
for crate in core cli ffi; do
    mkdir -p "$rust_bundle/$crate"
    cp "$sdk_dir/$crate/Cargo.toml" "$rust_bundle/$crate/"
    cp -R "$sdk_dir/$crate/src" "$rust_bundle/$crate/"
    for source_dir in tests examples; do
        if [[ -d "$sdk_dir/$crate/$source_dir" ]]; then
            cp -R "$sdk_dir/$crate/$source_dir" "$rust_bundle/$crate/"
        fi
    done
done
cp "$sdk_dir/ffi/uniffi.toml" "$rust_bundle/ffi/"

cp -R "$android_dir/build/maven" "$android_bundle/maven"
cp "$android_dir/README.md" "$android_bundle/README.md"
cp "$android_dir/BLUETOOTH.md" "$android_dir/MEDIA.md" "$android_dir/TESTING.md" "$android_bundle/"
cp "$sdk_dir/MOBILE_SDK.md" "$sdk_dir/MOBILE_VALIDATION.md" "$sdk_dir/HARDWARE_VALIDATION.md" "$android_bundle/"
cp "$android_dir/gradlew" "$android_dir/gradlew.bat" "$android_dir/build.gradle.kts" "$android_bundle/example/"
cp -R "$android_dir/gradle" "$android_bundle/example/gradle"
cp "$android_dir/distribution/settings.gradle.kts" "$android_dir/distribution/gradle.properties" "$android_bundle/example/"
cp "$android_dir/consumer/build.gradle.kts" "$android_dir/consumer/test-rules.pro" \
    "$android_dir/consumer/test-host-rules.pro" "$android_bundle/example/consumer/"
cp -R "$android_dir/consumer/src" "$android_bundle/example/consumer/src"

# Optional editable library sources and their instrumentation tests, independently buildable.
mkdir -p "$android_bundle/source"
cp "$android_dir/gradlew" "$android_dir/gradlew.bat" "$android_dir/build.gradle.kts" \
    "$android_dir/gradle.properties" "$android_bundle/source/"
cp -R "$android_dir/gradle" "$android_bundle/source/gradle"
cp "$android_dir/distribution/source-settings.gradle.kts" "$android_bundle/source/settings.gradle.kts"
for module in library bluetooth media; do
    mkdir -p "$android_bundle/source/$module"
    cp -R "$android_dir/$module/src" "$android_bundle/source/$module/"
    for build_file in build.gradle build.gradle.kts consumer-rules.pro; do
        if [[ -f "$android_dir/$module/$build_file" ]]; then
            cp "$android_dir/$module/$build_file" "$android_bundle/source/$module/"
        fi
    done
done

cp "$apple_dir/Package.swift" "$apple_dir/README.md" "$apple_dir/test.sh" "$apple_dir/VALIDATION.md" "$apple_bundle/"
cp "$apple_dir/BLUETOOTH.md" "$apple_bundle/"
cp "$sdk_dir/MOBILE_SDK.md" "$sdk_dir/MOBILE_VALIDATION.md" "$sdk_dir/HARDWARE_VALIDATION.md" "$apple_bundle/"
cp -R "$apple_dir/Sources" "$apple_dir/Tests" "$apple_dir/SynCapSDKFFI.xcframework" "$apple_bundle/"
cp "$media_dir/Package.swift" "$media_dir/README.md" "$media_bundle/"
cp -R "$media_dir/Sources" "$media_bundle/"
cp -R "$media_dir/Vendor/MobileVLCKit.xcframework" "$media_bundle/Vendor/"
cp "$media_dir/Vendor/MobileVLCKit-COPYING.txt" "$media_bundle/Vendor/"
for bundle in "$android_bundle" "$apple_bundle" "$media_bundle" "$rust_bundle"; do
    cp "$sdk_dir/RELEASE_2026_09_13.md" "$bundle/"
done

# Build new archives in an isolated staging directory, then publish complete files.
(
    cd "$stage"
    zip -q -r "$android_name.zip" "$android_name" -x '*/.DS_Store'
    zip -q -r "$apple_name.zip" "$apple_name" -x '*/.DS_Store'
    zip -q -r "$media_name.zip" "$media_name" -x '*/.DS_Store'
    zip -q -r "$rust_name.zip" "$rust_name" -x '*/.DS_Store'
)
mv "$stage/$android_name.zip" "$output_dir/$android_name.zip"
mv "$stage/$apple_name.zip" "$output_dir/$apple_name.zip"
mv "$stage/$media_name.zip" "$output_dir/$media_name.zip"
mv "$stage/$rust_name.zip" "$output_dir/$rust_name.zip"
cp "$aar" "$output_dir/syncap-sdk-$version.aar"
cp "$cli" "$output_dir/syncap-macos-arm64"
cp "$sdk_dir/DISTRIBUTION.md" "$output_dir/README.md"
cp "$sdk_dir/RELEASE_2026_09_13.md" "$output_dir/"
(
    cd "$output_dir"
    shasum -a 256 "$android_name.zip" "$apple_name.zip" "$media_name.zip" "$rust_name.zip" \
        "syncap-sdk-$version.aar" syncap-macos-arm64 README.md RELEASE_2026_09_13.md > "$stage/SHA256SUMS"
)
mv "$stage/SHA256SUMS" "$output_dir/SHA256SUMS"
echo "Android: $output_dir/$android_name.zip"
echo "Apple:   $output_dir/$apple_name.zip"
echo "iOS media (optional): $output_dir/$media_name.zip"
echo "Rust source: $output_dir/$rust_name.zip"
echo "macOS arm64 CLI: $output_dir/syncap-macos-arm64"
echo "SHA-256: $output_dir/SHA256SUMS"
echo "Unpacked packages retained for verification: $stage"
