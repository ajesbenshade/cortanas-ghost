#!/usr/bin/env bash
# Show the speaker console, the loader's recent output and USB state.
# Usage: status.sh [seconds-to-wait-first]
sleep "${1:-0}"
echo '--- console (last 60 lines) ---'
tail -c 6000 ~/invoke/console.log 2>/dev/null | cat -v | tail -60
echo
echo '--- loader (last 15 lines) ---'
tail -c 3000 ~/invoke/usb_boot.log | cat -v | tail -15
echo '--- usb ---'
lsusb | grep -v 'root hub' || echo 'no USB device attached'
pgrep -a usb_boot >/dev/null && echo 'loader: running' || echo 'loader: exited'
pgrep -f 'python3 /root/invoke/console.py' >/dev/null && echo 'console: running' || echo 'console: not running'
