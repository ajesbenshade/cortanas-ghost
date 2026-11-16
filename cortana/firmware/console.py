#!/usr/bin/env python3
"""Hold one connection to usb_boot's console port.

Everything the speaker prints is appended to console.log. Lines written to the
fifo console.in are typed to the speaker one character at a time, because the
bootloader drops characters when text arrives too fast.

    python3 console.py            run the holder (keep it in the background)
    echo 'help' > ~/invoke/console.in
"""
import os
import select
import socket
import time

BASE = os.path.expanduser("~/invoke")
LOG = os.path.join(BASE, "console.log")
FIFO = os.path.join(BASE, "console.in")
IAC = 255


def strip_telnet(data: bytes, sock: socket.socket) -> bytes:
    """Drop telnet option messages and return only the printable stream.

    Never answer them: usb_boot echoes every reply, which loops forever.
    """
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == IAC and i + 1 < len(data):
            i += 3 if data[i + 1] in (251, 252, 253, 254) else 2
            continue
        out.append(b)
        i += 1
    return bytes(out)


def main() -> None:
    if not os.path.exists(FIFO):
        os.mkfifo(FIFO)
    sock = socket.create_connection(("127.0.0.1", 8141))
    fifo = os.open(FIFO, os.O_RDWR | os.O_NONBLOCK)
    pending = b""
    with open(LOG, "ab", buffering=0) as log:
        log.write(b"\n[console connected]\n")
        while True:
            ready, _, _ = select.select([sock, fifo], [], [], 1.0)
            if sock in ready:
                data = sock.recv(4096)
                if not data:
                    log.write(b"\n[console closed by loader]\n")
                    return
                log.write(strip_telnet(data, sock))
            if fifo in ready:
                pending += os.read(fifo, 4096)
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    for ch in line + b"\r":
                        sock.sendall(bytes([ch]))
                        time.sleep(0.03)


if __name__ == "__main__":
    main()
