#!/usr/bin/env bash
# Clone Encore into the WSL home and show what the build expects. Writes nothing to the speaker.
set -euo pipefail
for t in git make gcc cargo rustup zig wasm-pack mksquashfs unsquashfs lsusb usbip python3 curl; do
  if command -v "$t" >/dev/null; then echo "have    $t"; else echo "MISSING $t"; fi
done
mkdir -p ~/invoke && cd ~/invoke
if [ ! -d Encore ]; then git clone --depth 50 https://github.com/ScottNorton/Encore.git; fi
cd Encore
echo "--- head ---"; git log -3 --format='%h %ad %s' --date=short
echo "--- tree ---"; ls -la; echo; ls docs uboot 2>/dev/null || true
echo "--- size ---"; du -sh .
