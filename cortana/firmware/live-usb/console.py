#!/usr/bin/env python3
"""Interactive console for Harman's usb_boot loader (replaces PuTTY).

Connects to the loader's port, shows what the speaker prints, and sends what
you type. Everything is also saved to console.log.  Press Ctrl+] to quit.
"""
import os
import select
import socket
import sys
import termios
import time
import tty

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8141
IAC = 255


def strip_telnet(data: bytes) -> bytes:
    # Drop telnet option messages. Never answer them: usb_boot echoes every
    # reply, which loops forever.
    out = bytearray()
    i = 0
    while i < len(data):
        if data[i] == IAC and i + 1 < len(data):
            i += 3 if data[i + 1] in (251, 252, 253, 254) else 2
            continue
        out.append(data[i])
        i += 1
    return bytes(out)


def main() -> int:
    sock = None
    for _ in range(40):
        try:
            sock = socket.create_connection(("127.0.0.1", PORT), timeout=1)
            break
        except OSError:
            time.sleep(0.25)
    if sock is None:
        print(f"Could not reach the loader on port {PORT}.")
        return 1
    sock.settimeout(None)

    interactive = sys.stdin.isatty()
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd) if interactive else None
    log = open("console.log", "ab", buffering=0)
    try:
        if interactive:
            tty.setraw(fd)
        while True:
            ready, _, _ = select.select([sock, fd], [], [])
            if sock in ready:
                data = sock.recv(4096)
                if not data:
                    break
                text = strip_telnet(data)
                log.write(text)
                os.write(sys.stdout.fileno(), text)
            if fd in ready:
                keys = os.read(fd, 64)
                if not keys or b"\x1d" in keys:  # EOF or Ctrl+]
                    break
                for ch in keys.replace(b"\n", b"\r"):
                    sock.sendall(bytes([ch]))
                    time.sleep(0.02)  # the bootloader drops fast input
    finally:
        if saved:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        log.close()
        sock.close()
    print("\r\n[console closed]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
