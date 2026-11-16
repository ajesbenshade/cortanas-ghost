#!/usr/bin/env bash
# Add the Wi-Fi audio input to Encore and rebuild. Usage: 10-build-netaudio.sh <invoke-flash dir>
set -euo pipefail
source ~/.cargo/env
export PATH="$HOME/invoke/pytools/bin:$PATH"
python3 "$1/09-add-netaudio.py" "$1"
cd ~/invoke/Encore
git diff --stat; git status --short | grep '^??' || true
make firmware > /root/invoke/build4.log 2>&1 || { grep -E '^(error|warning: unused)' -A14 /root/invoke/build4.log | head -80; exit 1; }
grep -E 'BUILD COMPLETE|ALL CHECKS PASSED' /root/invoke/build4.log
grep -c 'netaudio' /root/invoke/build4.log || true
strings build/encore | grep -c 'NetAudio: listening' 
cp build/encore /mnt/c/Users/${WIN_USER:?set WIN_USER to your Windows user name}/AppData/Local/invoke-bridge/ota/encore
cp build/firmware/83_IMAGE uboot/83_IMAGE
cp build/firmware/83_IMAGE /mnt/c/Users/${WIN_USER:?set WIN_USER to your Windows user name}/Desktop/invoke-flash/uboot/83_IMAGE
cp build/firmware/rootfs.squashfs /mnt/c/Users/${WIN_USER:?set WIN_USER to your Windows user name}/Desktop/encore-rootfs-wifi-fix.squashfs
(cd /mnt/c/Users/${WIN_USER:?set WIN_USER to your Windows user name}/Desktop/invoke-flash && sha256sum uboot/* stock/* > SHA256SUMS)
sha256sum build/encore
echo BUILD_OK
