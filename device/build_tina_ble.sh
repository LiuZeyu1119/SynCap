#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
MODE=${1:-cross}

if [ "$MODE" = "host-test" ]; then
    OUTPUT=${TINA_BLE_HOST_OUTPUT:-"$SCRIPT_DIR/build/host/syncap-tina-ble"}
    DBUS_CFLAGS=$(pkg-config --cflags dbus-1)
    DBUS_LIBS=$(pkg-config --libs dbus-1)
    mkdir -p "$(dirname -- "$OUTPUT")"
    # shellcheck disable=SC2086
    ${CC:-cc} -std=c11 -O2 -Wall -Wextra -Werror -pthread $DBUS_CFLAGS \
        "$SCRIPT_DIR/tina_ble.c" $DBUS_LIBS -o "$OUTPUT"
    "$OUTPUT" --self-test
    echo "$OUTPUT"
    exit 0
fi

if [ "$MODE" != "cross" ]; then
    echo "Usage: $0 [cross|host-test]" >&2
    exit 2
fi

TOOLCHAIN=${TINA_TOOLCHAIN:-/private/tmp/syncap-riscv-toolchain}
SYSROOT=${TINA_SYSROOT:-"$TOOLCHAIN/riscv32-linux-musl"}
OUTPUT=${TINA_BLE_OUTPUT:-"$SCRIPT_DIR/build/tina/syncap-tina-ble"}
DBUS_PREFIX=${TINA_DBUS_PREFIX:-}
DBUS_LIBRARY=${TINA_DBUS_LIB:-}

if [ -z "$DBUS_PREFIX" ] && command -v brew >/dev/null 2>&1; then
    DBUS_PREFIX=$(brew --prefix dbus 2>/dev/null || true)
fi
if [ -z "$DBUS_PREFIX" ] || [ ! -r "$DBUS_PREFIX/include/dbus-1.0/dbus/dbus.h" ]; then
    echo "Set TINA_DBUS_PREFIX to a D-Bus development prefix." >&2
    exit 2
fi
if [ -z "$DBUS_LIBRARY" ] || [ ! -r "$DBUS_LIBRARY" ]; then
    echo "Set TINA_DBUS_LIB to the target RV32 libdbus-1.so.3 file." >&2
    exit 2
fi
if [ ! -d "$SYSROOT/include" ]; then
    echo "Tina musl sysroot is missing: $SYSROOT" >&2
    exit 2
fi

if [ -n "${TINA_BLE_CC:-}" ]; then
    COMPILER=$TINA_BLE_CC
elif [ -x /opt/homebrew/opt/llvm/bin/clang ]; then
    COMPILER=/opt/homebrew/opt/llvm/bin/clang
else
    COMPILER=clang
fi

ARCH_INCLUDE="$SCRIPT_DIR/build/tina/dbus-include"
mkdir -p "$(dirname -- "$OUTPUT")" "$ARCH_INCLUDE/dbus"
cp "$SCRIPT_DIR/tina_dbus_arch_deps.h" "$ARCH_INCLUDE/dbus/dbus-arch-deps.h"

"$COMPILER" --target=riscv32-linux-musl --sysroot="$SYSROOT" \
    --gcc-toolchain="$TOOLCHAIN" -fuse-ld=lld -march=rv32imafdc -mabi=ilp32d \
    -std=c11 -Os -ffunction-sections -fdata-sections -Wall -Wextra -Werror -pthread \
    -I"$DBUS_PREFIX/include/dbus-1.0" -I"$ARCH_INCLUDE" \
    -Wl,--gc-sections -Wl,-rpath-link,"$(dirname -- "$DBUS_LIBRARY")" \
    "$SCRIPT_DIR/tina_ble.c" "$DBUS_LIBRARY" -o "$OUTPUT"

if [ -x /opt/homebrew/opt/llvm/bin/llvm-strip ]; then
    /opt/homebrew/opt/llvm/bin/llvm-strip "$OUTPUT"
elif [ -n "${TINA_STRIP:-}" ]; then
    "$TINA_STRIP" "$OUTPUT"
fi

echo "$OUTPUT"
