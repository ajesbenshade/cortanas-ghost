#!/usr/bin/env bash
# Apply the Wyoming receiver patch, run Encore's tests for that module, and rebuild.
# Usage: 08-build-patched.sh <dir containing 07-patch-wyoming.py>
set -euo pipefail
source ~/.cargo/env
export PATH="$HOME/invoke/pytools/bin:$PATH"
python3 "$1/07-patch-wyoming.py"
cd ~/invoke/Encore
git diff --stat
echo "=== tests (wyoming) ==="
(cd encore && cargo test -p encore-firmware wyoming 2>&1 | grep -E '^test |test result|error(\[|:)|warning: unused' | tail -25) || true
echo "=== build ==="
make firmware > /root/invoke/build3.log 2>&1 || { grep -E 'error|Error' -A12 /root/invoke/build3.log | head -60; exit 1; }
grep -E 'BUILD COMPLETE|ALL CHECKS PASSED' /root/invoke/build3.log
unsquashfs -ll build/firmware/rootfs.squashfs | grep -E 'sbin/wpa_supplicant_setup.sh$|usr/bin/encore$'
ls -l build/encore build/firmware/
sha256sum build/encore build/firmware/rootfs.squashfs build/firmware/83_IMAGE
echo BUILD_OK
