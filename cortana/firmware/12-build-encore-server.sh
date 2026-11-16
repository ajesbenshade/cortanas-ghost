#!/usr/bin/env bash
# Build just the `encore` program (for an over-the-air install with POST /api/update)
# on the Ubuntu server, with all of this project's patches. No stock image needed.
# Usage: 12-build-encore-server.sh <invoke-flash dir>
set -euo pipefail
here=$(cd "$1" && pwd)
source ~/.cargo/env
export PATH="$HOME/invoke/pytools/bin:$PATH"
cd ~/invoke/Encore

python3 "$here/07-patch-wyoming.py"
python3 "$here/09-add-netaudio.py" "$here"
python3 "$here/11-patch-listen-while-speaking.py"
git diff --stat

echo "=== tests (wyoming) ==="
(cd encore && cargo test -p encore-firmware wyoming 2>&1 | grep -E '^test |test result|error(\[|:)' | tail -25) || true

echo "=== build ==="
make encore > ~/invoke/build-encore.log 2>&1 || { grep -E '^(error|warning: unused)' -A14 ~/invoke/build-encore.log | head -80; exit 1; }
file build/encore
strings build/encore | grep -c 'NetAudio: listening'
strings build/encore | grep -c 'listen while speaking'
sha256sum build/encore
echo BUILD_OK
