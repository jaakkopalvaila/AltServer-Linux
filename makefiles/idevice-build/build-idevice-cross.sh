#!/bin/sh
# Cross-compiles idevice's static library for a musl Rust target with cargo-zigbuild, for
# architectures whose builder image has no musl-hosted Rust toolchain (i686, armv7).
# zig provides the C compiler and musl headers for ring's C and assembly code.
# Pass the printed path to make as IDEVICE_FFI_LIB. The output goes under libraries/idevice/target,
# which idevice's .gitignore already covers, so the submodule stays clean.
#
# Usage: makefiles/idevice-build/build-idevice-cross.sh <rust-target>
set -eu
target="$1"
root="$(cd "$(dirname "$0")/../.." && pwd)"
features="$(cat "$root/makefiles/idevice-build/idevice-features.txt")"

docker run --rm -v "$root/libraries/idevice:/src" -w /src/ffi ghcr.io/rust-cross/cargo-zigbuild:0.23.4 sh -c "
	rustup target add $target >/dev/null &&
	cargo zigbuild --release --locked --target $target --target-dir /src/target/cross --no-default-features --features $features" >&2

echo "$root/libraries/idevice/target/cross/$target/release/libidevice_ffi.a"
