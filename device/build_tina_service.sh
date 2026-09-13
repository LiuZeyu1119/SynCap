#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUTPUT=${TINA_OUTPUT:-"$SCRIPT_DIR/build/tina/syncap-tina-service"}

if [ -n "${TINA_CC:-}" ]; then
    COMPILER=$TINA_CC
elif [ -n "${TINA_TOOLCHAIN:-}" ]; then
    for candidate in \
        "$TINA_TOOLCHAIN/bin/riscv32-unknown-linux-musl-gcc" \
        "$TINA_TOOLCHAIN/bin/riscv32-linux-musl-gcc" \
        "$TINA_TOOLCHAIN/bin/riscv64-unknown-linux-musl-gcc" \
        "$TINA_TOOLCHAIN/bin/riscv64-linux-musl-gcc"
    do
        if [ -x "$candidate" ]; then
            COMPILER=$candidate
            break
        fi
    done
fi

if [ -z "${COMPILER:-}" ]; then
    echo "No Tina musl compiler found. Set TINA_CC or TINA_TOOLCHAIN." >&2
    exit 2
fi

mkdir -p "$(dirname -- "$OUTPUT")"
if [ -n "${TINA_SYSROOT:-}" ]; then
    SYSROOT_FLAG="--sysroot=$TINA_SYSROOT"
else
    SYSROOT_FLAG=
fi

# TINA_CFLAGS is intentionally expanded so board-specific -march/-mabi flags
# can be supplied by the SDK environment.
# shellcheck disable=SC2086
"$COMPILER" $SYSROOT_FLAG ${TINA_CFLAGS:-} \
    -std=c11 -Os -ffunction-sections -fdata-sections \
    -Wall -Wextra -Werror -Wl,--gc-sections -static \
    "$SCRIPT_DIR/tina_service.c" -o "$OUTPUT"

if [ -n "${TINA_STRIP:-}" ]; then
    "$TINA_STRIP" "$OUTPUT"
fi

echo "$OUTPUT"
