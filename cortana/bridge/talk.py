"""Talk to the bridge with this PC's default microphone and speakers.

    python talk.py              press Enter to start, Enter to stop, hear the reply
    python talk.py --text "hi"  skip the microphone and send text
    python talk.py --devices    list audio devices
"""

from __future__ import annotations

import argparse
import io
import os
import wave
from urllib.parse import unquote

import httpx
import numpy as np
import sounddevice as sd

RATE = 16000


def record(device) -> bytes:
    chunks: list[np.ndarray] = []
    input("Press Enter to start talking...")
    with sd.InputStream(
        samplerate=RATE, channels=1, dtype="int16", device=device,
        callback=lambda data, *_: chunks.append(data.copy()),
    ):
        input("Recording. Press Enter when done...")
    pcm = np.concatenate(chunks) if chunks else np.zeros((0, 1), dtype="int16")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def play(wav_bytes: bytes, device) -> None:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="int16")
        sd.play(pcm.reshape(-1, w.getnchannels()), w.getframerate(), device=device)
        sd.wait()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=os.environ.get("BRIDGE_URL", "http://127.0.0.1:8765"))
    p.add_argument("--token", default=os.environ.get("BRIDGE_TOKEN", ""))
    p.add_argument("--text")
    p.add_argument("--devices", action="store_true")
    p.add_argument("--mic", type=int, help="input device index")
    p.add_argument("--out", type=int, help="output device index")
    args = p.parse_args()

    if args.devices:
        print(sd.query_devices())
        return

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    with httpx.Client(base_url=args.url, headers=headers, timeout=180) as http:
        while True:
            if args.text:
                r = http.post("/v1/ask", json={"text": args.text})
                r.raise_for_status()
                reply = r.json()["reply"]
                print("Qwen:", reply)
                audio = http.post("/v1/speak", json={"text": reply})
                audio.raise_for_status()
                play(audio.content, args.out)
                return
            r = http.post(
                "/v1/voice",
                content=record(args.mic),
                headers={"Content-Type": "audio/wav"},
            )
            r.raise_for_status()
            print("You: ", unquote(r.headers["X-Transcript"]))
            print("Qwen:", unquote(r.headers["X-Reply"]))
            print("     ", r.headers["X-Timing-Ms"])
            play(r.content, args.out)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
