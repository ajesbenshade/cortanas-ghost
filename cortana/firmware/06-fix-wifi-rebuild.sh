#!/usr/bin/env bash
# Encore's build marks every boot script executable except wpa_supplicant_setup.sh,
# which mount_partition.sh runs as ./wpa_supplicant_setup.sh. On a clean Linux clone
# (git mode 644) the speaker then never loads its Wi-Fi driver. Fix the build and rebuild.
set -euo pipefail
source ~/.cargo/env
export PATH="$HOME/invoke/pytools/bin:$PATH"
cd ~/invoke/Encore

if ! grep -q 'sbin/wpa_supplicant_setup.sh" # local fix' scripts/build/build_firmware.sh; then
  sed -i '/chmod 755 "\$HYBRID\/sbin\/usb_gadget.sh"/a\    chmod 755 "$HYBRID/sbin/wpa_supplicant_setup.sh" # local fix: Wi-Fi driver loader must be executable' \
    scripts/build/build_firmware.sh
fi
git diff --stat
git diff scripts/build/build_firmware.sh | grep '^[+-]' | grep -v '^[+-][+-]'

cp build/firmware/rootfs.squashfs /root/invoke/rootfs.squashfs.first-build
make firmware > /root/invoke/build2.log 2>&1 || { tail -30 /root/invoke/build2.log; exit 1; }
grep -E 'BUILD COMPLETE|ALL CHECKS PASSED|Flash:' /root/invoke/build2.log

echo "--- permissions in the new image ---"
unsquashfs -ll build/firmware/rootfs.squashfs | grep -E 'squashfs-root/sbin/[a-z_]+\.sh$|init.d/S0|usr/bin/(encore|i2c_mute|run-podium.sh)$'
echo "--- any overlay shell script still not executable? ---"
unsquashfs -ll build/firmware/rootfs.squashfs | grep -E '^-rw-.*squashfs-root/(sbin|etc/init.d|usr/bin)/' | grep -E '\.sh$|/S[0-9]' || echo none
ls -l build/firmware/
sha256sum build/firmware/83_IMAGE build/firmware/rootfs.squashfs
