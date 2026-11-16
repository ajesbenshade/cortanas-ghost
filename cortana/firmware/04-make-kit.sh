#!/usr/bin/env bash
# Assemble the live-USB flash kit in WSL at ~/invoke/kit. Usage: 04-make-kit.sh <dir with flash.sh and console.py>
set -euo pipefail
SCRIPTS="$1"
KIT=~/invoke/kit
rm -rf "$KIT"
mkdir -p "$KIT/uboot" "$KIT/stock"
cd ~/invoke/Encore/uboot
cp usb_boot bootloader.img sysinit.img bcm_erom.bin.usb drm_erom.img \
   06_IMAGE 07_IMAGE 08_IMAGE 09_IMAGE 79_IMAGE 81_IMAGE 82_IMAGE 83_IMAGE 99_IMAGE "$KIT/uboot/"
cp ~/invoke/ota2-stock/OTA2/83_IMAGE "$KIT/stock/83_IMAGE"
cp "$SCRIPTS/flash.sh" "$SCRIPTS/console.py" "$KIT/"
sed -i 's/\r$//' "$KIT/flash.sh" "$KIT/console.py"
cd "$KIT"
sha256sum uboot/* stock/* > SHA256SUMS
cat SHA256SUMS
du -sh "$KIT"
