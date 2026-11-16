"""Send this PC's music to the speaker over Wi-Fi.

Captures everything played on one output device and streams it to the
speaker's network audio input on TCP 10710, as 48 kHz stereo 16-bit PCM.

Windows: the device is the silent virtual "Steam Streaming Speakers". Point the
browser running the music player at it once:
Settings > System > Sound > Volume mixer > Microsoft Edge > Output device.

Linux: the device is a PipeWire/PulseAudio sink (default "cortana_music") and
its monitor is recorded with parec. Play the music player into that sink.

    python streamer.py                 stream until stopped
    python streamer.py --tone 3        send a 3-second test tone and exit
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import time

import numpy as np

PORT = int(os.environ.get("MUSIC_PORT", "10710"))
DEVICE = os.environ.get(
    "MUSIC_DEVICE", "Steam Streaming Speakers" if sys.platform == "win32" else "cortana_music"
)
RATE, CHANNELS = 48000, 2
BLOCK = 960  # 20 ms


def find_loopback(pa, name: str) -> dict:
    for dev in pa.get_loopback_device_info_generator():
        if name.lower() in dev["name"].lower():
            return dev
    raise RuntimeError(f"no output device named like {name!r}")


class Streamer:
    def __init__(self, host_fn) -> None:
        self.host_fn = host_fn   # returns the speaker's current address, or ""
        self.sock: socket.socket | None = None
        self.sent_at = 0.0
        self.status = "starting"

    def _connect(self) -> None:
        host = self.host_fn()
        if not host:
            raise OSError("speaker address not known yet")
        s = socket.create_connection((host, PORT), timeout=3)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        s.settimeout(5)
        self.sock = s
        print(f"Music: streaming to {host}:{PORT}")

    def send(self, pcm: bytes) -> None:
        if self.sock is None:
            self._connect()
        try:
            self.sock.sendall(pcm)
        except OSError:
            self.sock.close()
            self.sock = None
            raise

    def capture_windows(self):
        import pyaudiowpatch as pyaudio

        pa = pyaudio.PyAudio()
        dev = find_loopback(pa, DEVICE)
        if int(dev["defaultSampleRate"]) != RATE:
            raise RuntimeError(f"{dev['name']} runs at {dev['defaultSampleRate']} Hz, expected {RATE}")
        stream = pa.open(format=pyaudio.paInt16, channels=CHANNELS, rate=RATE, input=True,
                         input_device_index=dev["index"], frames_per_buffer=BLOCK)
        print(f"Music: capturing {dev['name']}")
        while True:
            yield stream.read(BLOCK, exception_on_overflow=False)

    def capture_linux(self):
        proc = subprocess.Popen(
            ["parec", f"--device={DEVICE}.monitor", "--format=s16le", f"--rate={RATE}",
             f"--channels={CHANNELS}", "--raw", "--latency-msec=20"],
            stdout=subprocess.PIPE,
        )
        print(f"Music: capturing {DEVICE}.monitor")
        size = BLOCK * CHANNELS * 2
        while True:
            pcm = proc.stdout.read(size)
            if len(pcm) < size:
                raise RuntimeError(f"parec stopped (exit {proc.wait()}); is {DEVICE} a sink?")
            yield pcm

    def run(self) -> None:
        blocks = self.capture_windows() if sys.platform == "win32" else self.capture_linux()
        last_error = ""
        for pcm in blocks:
            if not np.frombuffer(pcm, dtype=np.int16).any():
                continue  # digital silence: nothing playing, send nothing
            try:
                self.send(pcm)
                last_error = ""
            except OSError as e:
                if str(e) != last_error:
                    print(f"Music: speaker not reachable ({e}); will keep trying")
                    last_error = str(e)
                time.sleep(1)


def start_background(host_fn) -> threading.Thread | None:
    """Start streaming in a daemon thread. Returns None if capture isn't possible."""
    def guarded() -> None:
        try:
            Streamer(host_fn).run()
        except Exception as e:
            print(f"Music streaming disabled: {e}")

    t = threading.Thread(target=guarded, name="music-streamer", daemon=True)
    t.start()
    return t


def tone(host: str, seconds: float) -> None:
    """Send a test tone straight to the speaker (no capture involved)."""
    t = np.arange(int(RATE * seconds)) / RATE
    wave = (0.2 * 32767 * np.sin(2 * np.pi * 440 * t) * np.minimum(1, np.minimum(t, seconds - t) * 20))
    pcm = np.repeat(wave.astype(np.int16), 2).tobytes()
    s = socket.create_connection((host, PORT), timeout=3)
    step = BLOCK * 4
    start = time.monotonic()
    for i in range(0, len(pcm), step):
        s.sendall(pcm[i:i + step])
        ahead = (i + step) / (RATE * 4) - (time.monotonic() - start)
        if ahead > 0.2:
            time.sleep(ahead - 0.2)
    time.sleep(0.6)
    s.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="")
    p.add_argument("--tone", type=float)
    args = p.parse_args()

    def host() -> str:
        if args.host:
            return args.host
        try:
            path = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                                "invoke-bridge", "speaker.txt")
            return open(path).read().strip()
        except OSError:
            return ""

    if args.tone:
        tone(host(), args.tone)
    else:
        try:
            Streamer(host).run()
        except KeyboardInterrupt:
            pass
