#!/usr/bin/env bash
# Exercise console.py under a pseudo-terminal against the real loader (no speaker needed).
set -uo pipefail
cd /root/invoke-flash-work
pkill -x usb_boot 2>/dev/null
stdbuf -oL -eL ./usb_boot 1286 8174 ./ 8141 true > loader.log 2>&1 &
sleep 1
rm -f console.log
# type "hi", Enter, then Ctrl+] after a pause
( sleep 2; printf 'hi\n'; sleep 2; printf '\035' ) | script -qec "python3 console.py 8141" /dev/null | cat -v | tail -3
echo "console exit handled"
echo "--- what the loader received ---"
grep -a 'recved' loader.log | grep -av 'recved 255\|recved cmd\|recved 25[0-4]' | tail -5
echo "negotiation lines in loader log: $(grep -ac send_telnet_cmd loader.log)"
pkill -x usb_boot
stty sane 2>/dev/null || true
