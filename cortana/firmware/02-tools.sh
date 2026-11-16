#!/usr/bin/env bash
# Install everything needed to build Encore inside WSL. Safe to run again.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq build-essential make unzip openssl squashfs-tools usbutils \
  pkg-config python3-venv curl ca-certificates llvm >/dev/null
echo "apt packages installed"

if ! command -v rustup >/dev/null && [ ! -x ~/.cargo/bin/rustup ]; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain none --profile minimal
fi
source ~/.cargo/env

# zig + cargo-zigbuild as prebuilt wheels (no compile needed)
if [ ! -x ~/invoke/pytools/bin/cargo-zigbuild ]; then
  python3 -m venv ~/invoke/pytools
  ~/invoke/pytools/bin/pip install --quiet ziglang cargo-zigbuild
fi
export PATH="$HOME/invoke/pytools/bin:$PATH"

# Pinned nightly + targets come from encore/rust-toolchain.toml
cd ~/invoke/Encore/encore
rustup show active-toolchain

command -v wasm-pack >/dev/null || cargo install --quiet --locked wasm-pack

echo "--- versions ---"
rustc --version; cargo --version; cargo-zigbuild --version
python3 -m ziglang version 2>/dev/null || ~/invoke/pytools/bin/python -m ziglang version
wasm-pack --version; mksquashfs -version | head -1; lsusb --version
