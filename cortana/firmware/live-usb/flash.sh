#!/usr/bin/env bash
# Run from an Ubuntu live session:   bash flash.sh
# Add "stock" to go back to Harman's firmware:   bash flash.sh stock
#
# This script only uploads Harman's bootloader into the speaker's memory and
# opens its console. It never writes to the speaker's storage by itself: that
# happens only when YOU type the tftp2nand command at the >> prompt.
set -euo pipefail

MODE="${1:-encore}"
SRC="$(cd "$(dirname "$0")" && pwd)"
WORK="$HOME/invoke-flash-work"

# The stick's filesystem cannot mark files executable, so work from a copy.
rm -rf "$WORK"
mkdir -p "$WORK"
cp "$SRC"/uboot/* "$WORK"/
cp "$SRC"/console.py "$SRC"/SHA256SUMS "$WORK"/
if [ "$MODE" = "stock" ]; then
    cp "$SRC"/stock/83_IMAGE "$WORK"/83_IMAGE
    EXPECT=$(grep ' stock/83_IMAGE$' "$WORK"/SHA256SUMS | cut -d' ' -f1)
else
    EXPECT=$(grep ' uboot/83_IMAGE$' "$WORK"/SHA256SUMS | cut -d' ' -f1)
fi
cd "$WORK"
chmod +x usb_boot

echo "Checking files..."
ok=1
while read -r sum path; do
    case "$path" in uboot/83_IMAGE|stock/*) continue ;; esac
    f="${path#uboot/}"
    [ "$(sha256sum "$f" | cut -d' ' -f1)" = "$sum" ] || { echo "  DAMAGED: $f"; ok=0; }
done < SHA256SUMS
[ "$(sha256sum 83_IMAGE | cut -d' ' -f1)" = "$EXPECT" ] || { echo "  DAMAGED: 83_IMAGE"; ok=0; }
[ "$(cat 79_IMAGE)" = "#skip" ] || { echo "  79_IMAGE is not the wait-for-commands script"; ok=0; }
[ "$ok" = 1 ] || { echo "Stopping: copy the kit again."; exit 1; }
SIZE=$(stat -c%s 83_IMAGE)
echo "  all files intact. Firmware: $MODE, $SIZE bytes"

for lib in libusb-1.0.so.0 libudev.so.1; do
    ldconfig -p | grep -q "$lib" || { echo "Missing $lib. Connect to the internet and run: sudo apt install libusb-1.0-0"; exit 1; }
done

sudo -v
sudo pkill -x usb_boot 2>/dev/null || true
sudo sh -c "cd '$WORK' && exec stdbuf -oL -eL ./usb_boot 1286 8174 ./ 8141 true" > loader.log 2>&1 &
sleep 1

cat <<EOF

======================================================================
 The loader is waiting. NOW put the speaker in service mode:
   1. Unplug the speaker's power. Leave the USB cable connected.
   2. Hold the reset pinhole and plug power back in.
   3. Still holding reset, press mic-mute 4 times.
   4. Release when the ring is yellow.

 Text will scroll, then stop at a prompt ending in  >>
 Type these by hand, one at a time, pressing Enter after each:

     usbload 0x83 0x7000000
     tftp2nand -d $SIZE 0x7000000
     reset

 - After usbload, wait for the prompt to come back before continuing.
 - tftp2nand is the step that writes. It takes a few minutes.
   Do not unplug anything while it runs.
 - NEVER add -m to tftp2nand: that erases the whole chip.
 - To back out before tftp2nand, just unplug the speaker's power.

 Press Ctrl+]  to close this console when you are done.
======================================================================

EOF
python3 console.py 8141 || true
sudo pkill -x usb_boot 2>/dev/null || true
echo "Loader stopped. Logs: $WORK/console.log and $WORK/loader.log"
