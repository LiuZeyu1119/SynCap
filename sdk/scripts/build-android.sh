#!/usr/bin/env bash
set -euo pipefail

sdk_root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
sdk_android_home=${ANDROID_HOME:-${ANDROID_SDK_ROOT:-}}
if [[ -z "$sdk_android_home" || ! -d "$sdk_android_home/platforms/android-36" ]]; then
    echo "Set ANDROID_HOME to an Android SDK containing platforms;android-36." >&2
    exit 1
fi
sdk_ndk=${ANDROID_NDK_HOME:-"$sdk_android_home/ndk/29.0.14206865"}
case "$(uname -s)" in
    Darwin) sdk_ndk_host=darwin-x86_64; sdk_host_library=libsyncap_ffi.dylib ;;
    Linux) sdk_ndk_host=linux-x86_64; sdk_host_library=libsyncap_ffi.so ;;
    *) echo "Build Android native libraries on macOS or Linux." >&2; exit 1 ;;
esac
sdk_toolchain="$sdk_ndk/toolchains/llvm/prebuilt/$sdk_ndk_host/bin"
if [[ ! -x "$sdk_toolchain/llvm-ar" ]]; then
    echo "Missing Android NDK at $sdk_ndk. Set ANDROID_NDK_HOME or install ndk;29.0.14206865." >&2
    exit 1
fi

# Use the same release build for generated checksums and host binding tests.
cargo build --locked --manifest-path "$sdk_root/Cargo.toml" -p syncap-ffi --lib --release
(
    cd "$sdk_root"
    cargo run --locked -p syncap-ffi --features bindgen-cli --bin uniffi-bindgen -- generate \
        --library "$sdk_root/target/release/$sdk_host_library" --language kotlin \
        --out-dir "$sdk_root/android/library/src/main/java" --no-format
)

for sdk_abi in arm64-v8a x86_64; do
    case "$sdk_abi" in
        arm64-v8a) sdk_target=aarch64-linux-android; sdk_cc=aarch64-linux-android24-clang ;;
        x86_64) sdk_target=x86_64-linux-android; sdk_cc=x86_64-linux-android24-clang ;;
    esac
    sdk_target_key=${sdk_target//-/_}
    sdk_cargo_key=$(printf '%s' "$sdk_target_key" | tr '[:lower:]' '[:upper:]')
    env "CARGO_TARGET_${sdk_cargo_key}_LINKER=$sdk_toolchain/$sdk_cc" \
        "CARGO_TARGET_${sdk_cargo_key}_RUSTFLAGS=-C link-arg=-Wl,-z,max-page-size=16384 -C link-arg=-Wl,-z,common-page-size=16384" \
        "CC_${sdk_target_key}=$sdk_toolchain/$sdk_cc" \
        "AR_${sdk_target_key}=$sdk_toolchain/llvm-ar" \
        cargo build --locked --manifest-path "$sdk_root/Cargo.toml" \
        -p syncap-ffi --lib --release --target "$sdk_target"
    sdk_jni_dir="$sdk_root/android/library/src/main/jniLibs/$sdk_abi"
    mkdir -p "$sdk_jni_dir"
    cp "$sdk_root/target/$sdk_target/release/libsyncap_ffi.so" "$sdk_jni_dir/libsyncap_ffi.so"
    "$sdk_toolchain/llvm-strip" --strip-unneeded "$sdk_jni_dir/libsyncap_ffi.so"
done

# Extra arguments are Gradle options, e.g. --offline or temporary proxy properties.
ANDROID_HOME="$sdk_android_home" "$sdk_root/android/gradlew" -p "$sdk_root/android" \
    :library:assembleRelease :library:publishReleasePublicationToSdkLocalRepository \
    :bluetooth:assembleRelease :bluetooth:publishReleasePublicationToSdkLocalRepository \
    :media:assembleRelease :media:publishReleasePublicationToSdkLocalRepository \
    :consumer:assembleRelease :host-tests:test "$@"
ANDROID_HOME="$sdk_android_home" "$sdk_root/android/gradlew" -p "$sdk_root/android" \
    :consumer:assembleRelease -PusePublishedSdk=true "$@"
printf 'AAR: %s\nMaven repository: %s\n' \
    "$sdk_root/android/library/build/outputs/aar/library-release.aar" \
    "$sdk_root/android/build/maven"
