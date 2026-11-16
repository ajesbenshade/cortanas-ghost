#!/usr/bin/env bash
# Fetch the stock image (checksum-verified) and build the Encore firmware image.
# Everything stays inside WSL; nothing is sent to the speaker.
set -euo pipefail
source ~/.cargo/env
export PATH="$HOME/invoke/pytools/bin:$PATH"
cd ~/invoke/Encore

# wasm-pack runs from the repo root, where encore/rust-toolchain.toml does not
# apply, so the pinned nightly must also be rustup's default.
PINNED=$(sed -n 's/^channel = "\(.*\)"/\1/p' encore/rust-toolchain.toml)
rustup default "$PINNED"
rustup target add --toolchain "$PINNED" armv7-unknown-linux-musleabihf wasm32-unknown-unknown

make download
make firmware
echo "--- outputs ---"
ls -l build/encore build/firmware/
sha256sum build/firmware/83_IMAGE build/firmware/rootfs.squashfs
echo BUILD_OK
