"""Link between the Invoke speaker (running Encore) and the voice bridge.

Encore exposes a Wyoming "satellite" on TCP 10700: once a client says
run-satellite it streams the microphone continuously and plays back any audio
it is sent. This script is that client. It

  1. splits the mic stream into spoken phrases (simple energy-based detection),
  2. transcribes each phrase and looks for the wake word "Cortana",
  3. sends the request to Qwen through the bridge, and
  4. streams the spoken reply back to the speaker.

Say "Hey Cortana, what time is it" in one breath, or say "Hey Cortana", wait
for the ring to light, then ask. After each reply the ring stays lit and you
can keep talking without the wake word; the conversation ends after
CONVERSATION_WINDOW seconds of silence or when you say "that's all".

    python satellite.py              run the link
    python satellite.py --levels 8   print mic levels for 8 seconds and exit
    python satellite.py --say "hi"   speak a phrase through the speaker and exit
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import re
import time
import wave
from collections import deque

import httpx
import numpy as np

RATE = 16000
FRAME = 480  # 30 ms

WAKE = re.compile(
    r"\b(?:hey|hi|hello|ok|okay|yo)?[\s,]*"
    r"(?:cortana|kortana|cortona|cortanna|cartana|courtana|quartana|cortina)\b[\s,.!?]*",
    re.I,
)


class Cfg:
    # "auto" finds the speaker on USB or Wi-Fi; set an address to pin it.
    speaker = os.environ.get("SPEAKER_HOST", "auto")
    found = ""
    port = int(os.environ.get("SPEAKER_PORT", "10700"))
    bridge = os.environ.get("BRIDGE_URL", "http://127.0.0.1:8765")
    token = os.environ.get("BRIDGE_TOKEN", "")
    session = os.environ.get("SPEAKER_SESSION", "invoke")
    # Largest event sent to the speaker, in bytes. Stock Encore drops the session
    # unless every event fits one network packet (340 on the USB link); with the
    # receiver patch in invoke-flash/07-patch-wyoming.py any size works.
    max_event = int(os.environ.get("SPEAKER_MAX_EVENT", "1400"))
    # Phrase detection
    start_ratio = float(os.environ.get("VAD_START_RATIO", "3.0"))  # x noise floor
    min_rms = float(os.environ.get("VAD_MIN_RMS", "250"))
    end_silence = float(os.environ.get("VAD_END_SILENCE", "0.5"))  # seconds
    max_phrase = float(os.environ.get("VAD_MAX_PHRASE", "12"))
    followup_window = float(os.environ.get("FOLLOWUP_WINDOW", "7"))
    # After each reply, keep listening this long without needing the wake word.
    conversation_window = float(os.environ.get("CONVERSATION_WINDOW", "15"))
    # Ring while waiting for a request: "r,g,b" pulses that color through the
    # speaker's dashboard WebSocket; empty keeps Encore's stock animation.
    listen_color = os.environ.get("LISTEN_COLOR", "0,90,255")
    listen_period_ms = int(os.environ.get("LISTEN_PERIOD_MS", "1400"))
    # Interrupting a reply ("Cortana, stop"). Needs firmware with
    # invoke-flash/11-patch-listen-while-speaking.py; older firmware ignores it.
    barge_in = os.environ.get("BARGE_IN", "1") != "0"


# Said on its own, ends a conversation.
# Said during a reply, cuts it short (as long as the reply itself doesn't say it).
STOP = re.compile(r"\b(?:stop|cancel|quiet|shut up|enough|never ?mind|hold on|wait)\b", re.I)


def interrupts(heard: str, speaking: str) -> bool:
    """True if `heard` (mic during a reply) is the user cutting in rather than the
    mic picking up Cortana's own voice saying `speaking`."""
    speaking = speaking.lower()
    if WAKE.search(heard) and "cortana" not in speaking:
        return True
    return any(m.group(0).lower() not in speaking for m in STOP.finditer(heard))


END = re.compile(
    r"^(?:ok(?:ay)?[\s,]*)?(?:that'?s all|that is all|that'?s it|good ?bye|bye(?: bye)?|good ?night|"
    r"never ?mind|stop listening|we'?re done|i'?m done|go to sleep|"
    r"(?:no[\s,]*)?(?:thanks|thank you)(?:[\s,]*(?:cortana|that'?s all|that is all|that'?s it|bye|good ?bye))?)"
    r"[\s.!]*$",
    re.I,
)
# One-word phrases worth answering mid-conversation; other single words are
# usually noise or a mis-heard cough.
SHORT_OK = {"yes", "no", "yeah", "yep", "nope", "sure", "okay", "ok", "why", "how",
            "what", "when", "where", "who", "really", "continue", "more", "again",
            "correct", "right", "wrong", "maybe", "please", "go", "next", "repeat"}
# Phrases the transcriber invents from silence or background noise.
PHANTOM = {"you", "thank you", "thanks for watching", "thank you for watching",
           "bye", "so", "the", "i", "hmm", "uh", "um", "oh", "ah"}


def is_noise(text: str) -> bool:
    words = re.sub(r"[^a-z0-9' ]", " ", text.lower()).split()
    if not words:
        return True
    joined = " ".join(words)
    if joined in PHANTOM:
        return True
    return len(words) == 1 and words[0] not in SHORT_OK and not words[0].isdigit()


# ---------- Wyoming wire format ----------

async def send(writer: asyncio.StreamWriter, kind: str, data: dict | None = None,
               payload: bytes = b"") -> None:
    body = json.dumps(data or {}).encode()
    header = {"type": kind, "version": "1.0.0",
              "data_length": len(body), "payload_length": len(payload)}
    writer.write(json.dumps(header).encode() + b"\n" + body + payload)
    await writer.drain()


async def recv(reader: asyncio.StreamReader) -> tuple[str, dict, bytes]:
    line = await reader.readline()
    if not line:
        raise ConnectionError("speaker closed the connection")
    header = json.loads(line)
    data = header.get("data") or {}
    if header.get("data_length"):
        data = json.loads(await reader.readexactly(header["data_length"]))
    payload = b""
    if header.get("payload_length"):
        payload = await reader.readexactly(header["payload_length"])
    return header["type"], data, payload


LAST_SEEN = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
                         "invoke-bridge", "speaker.txt")


async def is_speaker(host: str, timeout: float) -> bool:
    """True if something at host answers like Encore's voice satellite."""
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, Cfg.port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    try:
        await send(writer, "describe")
        kind, data, _ = await asyncio.wait_for(recv(reader), 3)
        return kind == "info" and "satellite" in data
    except Exception:
        return False
    finally:
        writer.close()


def local_subnets() -> list[str]:
    """First three octets of each private /24 this PC is on."""
    import socket
    nets = []
    for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
        ip = info[4][0]
        if ip.startswith(("192.168.", "10.")) or re.match(r"172\.(1[6-9]|2\d|3[01])\.", ip):
            net = ip.rsplit(".", 1)[0]
            if net not in nets:
                nets.append(net)
    return nets


_last_sweep = -1000.0


async def find_speaker() -> str:
    """Locate the speaker: the configured host, the last place it was seen, its
    usual names, then a sweep of the networks this PC is on."""
    if Cfg.speaker != "auto":
        return Cfg.speaker
    known = []
    try:
        known.append(open(LAST_SEEN).read().strip())
    except OSError:
        pass
    known += ["encore.local", "10.55.55.1", "192.168.43.1"]
    for host in dict.fromkeys(h for h in known if h):
        if await is_speaker(host, 1.5):
            return host
    # Sweep the local networks at most twice a minute; the quick checks above
    # run on every attempt.
    global _last_sweep
    if time.monotonic() - _last_sweep > 30:
        _last_sweep = time.monotonic()
        hosts = [f"{net}.{n}" for net in local_subnets() for n in range(1, 255)]
        found = await asyncio.gather(*(is_speaker(h, 0.6) for h in hosts))
        for host, ok in zip(hosts, found):
            if ok:
                return host
    raise ConnectionError("speaker not found on USB or any local network")


_ring_warned = False


async def ring_listening() -> None:
    """Pulse the light ring in LISTEN_COLOR. Sent after the "detection" event,
    so it replaces the stock listening animation; Encore's own thinking,
    speaking and off states still follow as usual."""
    global _ring_warned
    if not Cfg.listen_color or not Cfg.found:
        return
    import ssl

    import websockets

    r, g, b = (int(v) for v in Cfg.listen_color.split(","))
    msg = {"type": "SetLed", "data": {"kind": "Breathe", "r": r, "g": g, "b": b,
                                       "period_ms": Cfg.listen_period_ms}}
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # Encore's cert is self-signed
    try:
        async with websockets.connect(f"wss://{Cfg.found}/ws", ssl=ctx, open_timeout=2) as ws:
            await ws.send(json.dumps(msg))
    except Exception as e:
        if not _ring_warned:  # say it once; the stock animation still shows
            print(f"  (couldn't set the ring color: {e})")
            _ring_warned = True


async def connect() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    import socket
    host = await find_speaker()
    reader, writer = await asyncio.open_connection(host, Cfg.port)
    if Cfg.speaker == "auto":
        try:
            with open(LAST_SEEN, "w") as f:
                f.write(host)
        except OSError:
            pass
    Cfg.found = host
    # Send each event as its own packet instead of letting the OS merge them.
    writer.get_extra_info("socket").setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return reader, writer


# ---------- phrase detection ----------

class Phraser:
    """Feed 16 kHz mono int16 audio; yields complete spoken phrases."""

    def __init__(self) -> None:
        self.buf = np.zeros(0, dtype=np.int16)
        self.floor = 150.0
        self.pre = deque(maxlen=10)  # 300 ms of audio before speech starts
        self.phrase: list[np.ndarray] = []
        self.loud = 0
        self.quiet = 0
        self.active = False

    def reset(self) -> None:
        self.__init__()

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        out = []
        self.buf = np.concatenate([self.buf, samples])
        while len(self.buf) >= FRAME:
            frame, self.buf = self.buf[:FRAME], self.buf[FRAME:]
            rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
            threshold = max(Cfg.min_rms, self.floor * Cfg.start_ratio)
            if not self.active:
                self.pre.append(frame)
                if rms > threshold:
                    self.loud += 1
                else:
                    self.loud = 0
                    self.floor = 0.95 * self.floor + 0.05 * rms
                if self.loud >= 4:  # 120 ms of speech
                    self.active, self.quiet = True, 0
                    self.phrase = list(self.pre)
            else:
                self.phrase.append(frame)
                self.quiet = self.quiet + 1 if rms < threshold * 0.6 else 0
                seconds = len(self.phrase) * FRAME / RATE
                if self.quiet * FRAME / RATE >= Cfg.end_silence or seconds >= Cfg.max_phrase:
                    out.append(np.concatenate(self.phrase))
                    self.active, self.loud, self.phrase = False, 0, []
                    self.pre.clear()
        return out


def to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


# ---------- the link ----------

class Link:
    def __init__(self, reader, writer, http: httpx.AsyncClient) -> None:
        self.reader, self.writer, self.http = reader, writer, http
        self.phraser = Phraser()
        self.deaf_until = 0.0     # ignore the mic while our own reply plays
        # None: waiting for the wake word. "armed": wake word heard, waiting for
        # the request. "convo": mid-conversation, no wake word needed.
        self.mode: str | None = None
        self.mode_until = 0.0
        self.mic: asyncio.Queue = asyncio.Queue()
        self.busy = False         # fetching or speaking a reply
        self.phrase_end = 0.0     # timing: when the last phrase stopped
        self.stt_s = 0.0
        self.spoke_at: float | None = None
        # Early wake word: the ring lights while the sentence is still going.
        self.lit = False            # ring already lit for the phrase in progress
        self.wake_checked = 0.0     # phrase length (s) at the last check
        self.wake_task: asyncio.Task | None = None
        # Interrupting: what is being said, and what the mic heard over it.
        self.speaking = ""
        self.barge = asyncio.Event()
        self.barge_heard = ""
        self.barge_phraser = Phraser()
        self.barge_checked = 0.0
        self.barge_task: asyncio.Task | None = None
        self.last_heard = time.monotonic()  # last event from the speaker

    async def transcribe(self, pcm: np.ndarray) -> str:
        r = await self.http.post("/v1/transcribe", params={"hotwords": "Cortana"},
                                 content=to_wav(pcm))
        r.raise_for_status()
        return r.json()["text"].strip()

    async def speak(self, text: str, voice: str = "") -> None:
        """Say `text`. Returns early, and leaves self.barge set, if the user
        cuts in (see check_barge)."""
        self.barge.clear()
        self.barge_phraser.reset()
        self.barge_checked = 0.0
        self.speaking = text
        try:
            await self._speak(text, voice)
        finally:
            self.speaking = ""

    async def _speak(self, text: str, voice: str) -> None:
        # The bridge streams the reply as it is made, so the first words play
        # while the rest is still being synthesized.
        async with self.http.stream("POST", "/v1/speak/stream",
                                    json={"text": text, "voice": voice}) as r:
            r.raise_for_status()
            rate, width, ch = int(r.headers["X-Audio-Rate"]), 2, 1
            frame = width * ch
            step = max(frame, (Cfg.max_event - 90) // frame * frame)
            await send(self.writer, "audio-start", {"rate": rate, "width": width, "channels": ch})
            started = time.monotonic()
            self.spoke_at = started + 0.35  # after the lead-in silence
            sent = 0

            async def push(pcm: bytes) -> None:
                nonlocal sent
                for i in range(0, len(pcm), step):
                    if self.barge.is_set():
                        return
                    await send(self.writer, "audio-chunk", None, pcm[i:i + step])
                    sent += len(pcm[i:i + step])
                    # stay just ahead of real time: no overflow on the speaker, and no
                    # backlog for the network to merge into split packets
                    ahead = sent / (rate * frame) - (time.monotonic() - started)
                    if ahead > 0.06:
                        await asyncio.sleep(ahead - 0.06)

            # The amplifier sleeps when idle; lead with silence so it is awake
            # before the first word.
            pending = b"\x00" * (int(rate * 0.35) * frame)
            async for data in r.aiter_bytes():
                pending += data
                whole = len(pending) // step * step
                if whole:
                    await push(pending[:whole])
                    pending = pending[whole:]
                if self.barge.is_set():
                    break
            await push(pending[:len(pending) // frame * frame])
        duration = sent / (rate * frame)
        try:  # wait for the speaker to finish playing, unless interrupted
            await asyncio.wait_for(self.barge.wait(), max(0.0, duration - (time.monotonic() - started)))
        except asyncio.TimeoutError:
            pass
        await send(self.writer, "audio-stop")
        self.deaf_until = time.monotonic() + 0.8
        self.phraser.reset()

    async def answer(self, request: str) -> None:
        self.busy = True
        try:
            await self._answer(request)
        finally:
            # drop anything captured before or during the reply
            while not self.mic.empty():
                item = self.mic.get_nowait()
                if isinstance(item, Exception):
                    self.mic.put_nowait(item)
                    break
            self.phraser.reset()
            self.busy = False

    async def _answer(self, request: str) -> None:
        print(f"  you:     {request}")
        await send(self.writer, "voice-stopped")
        await send(self.writer, "transcript", {"text": request})
        t0 = time.monotonic()
        try:
            r = await self.http.post("/v1/ask", json={"text": request, "session": Cfg.session})
            r.raise_for_status()
            reply = r.json()["reply"]
            music_playing = bool(r.json().get("music_playing"))
        except Exception as e:
            print(f"  bridge error: {e}")
            await send(self.writer, "error", {"text": str(e)})
            self.mode = None
            return
        t1 = time.monotonic()
        print(f"  cortana: {reply}")
        self.spoke_at = None
        await self.speak(reply)
        heard = (self.spoke_at or time.monotonic()) - self.phrase_end
        print(f"  timing:  end of your speech -> first sound {heard:.1f}s "
              f"(silence wait {Cfg.end_silence:.1f}, transcribe {self.stt_s:.1f}, "
              f"llm {t1 - t0:.1f}, voice {(self.spoke_at or t1) - t1:.1f})")
        if self.barge.is_set():
            await self.after_barge()
        elif music_playing:
            # Don't hold a conversation over a song: the mics would hear the
            # lyrics as requests. Go back to waiting for the wake word.
            await self.stand_down("music playing")
        else:
            await self.listen_on()

    # ---------- interrupting a reply ----------

    def hear_while_speaking(self, payload: bytes) -> None:
        """Mic audio during a reply (patched firmware only): every half second of
        speech, check it for the name or a stop word with the quick model."""
        p = self.barge_phraser
        done = p.feed(np.frombuffer(payload, dtype=np.int16))
        if self.barge.is_set() or self.barge_task:
            return
        if done:
            pcm = done[-1]
        elif p.active and len(p.phrase) * FRAME / RATE - self.barge_checked >= 0.5:
            self.barge_checked = len(p.phrase) * FRAME / RATE
            pcm = np.concatenate(p.phrase[-int(4 * RATE / FRAME):])
        else:
            return
        if len(pcm) >= 0.4 * RATE:
            self.barge_task = asyncio.create_task(self.check_barge(pcm, self.speaking))

    async def check_barge(self, pcm: np.ndarray, speaking: str) -> None:
        try:
            r = await self.http.post("/v1/transcribe", params={"hotwords": "Cortana", "fast": "true"},
                                     content=to_wav(pcm))
            heard = r.json()["text"].strip()
            if heard and self.speaking == speaking and interrupts(heard, speaking):
                self.barge_heard = heard
                self.barge.set()
        except Exception:
            pass
        finally:
            self.barge_task = None

    async def after_barge(self) -> None:
        """Cut off mid-reply: answer what was said, or wait for the next request."""
        heard = self.barge_heard
        request = STOP.sub(" ", WAKE.sub(" ", heard))
        request = re.sub(r"\s+", " ", request).strip(" ,.!?")
        print(f"  (interrupted: {heard})")
        if len(request.split()) >= 2:
            self.stt_s = 0.0
            self.phrase_end = time.monotonic()
            await self._answer(request)
        else:
            await self.listen_on()

    async def duck(self, on: bool) -> None:
        """Lower or restore the music while Cortana listens and talks."""
        try:
            await self.http.post("/v1/music/duck", json={"on": on}, timeout=4)
        except Exception:
            pass

    async def listen_on(self) -> None:
        """Stay in the conversation: light the ring and accept the next phrase."""
        if Cfg.conversation_window <= 0:
            self.mode = None
            return
        self.mode = "convo"
        self.mode_until = time.monotonic() + 0.8 + Cfg.conversation_window
        await send(self.writer, "detection", {"name": "cortana"})  # ring: listening
        await ring_listening()

    async def stand_down(self, why: str) -> None:
        """Leave the conversation and turn the ring off."""
        print(f"  ({why}; say Hey Cortana to start again)")
        self.mode = None
        await self.duck(False)
        # An empty reply is the one message that makes Encore turn the ring off.
        fmt = {"rate": 22050, "width": 2, "channels": 1}
        await send(self.writer, "audio-start", fmt)
        await send(self.writer, "audio-stop")

    def check_wake_early(self) -> None:
        """While someone is still talking, look for the name every half second
        (quick model, last 4 s) so the ring lights as soon as it is said."""
        p = self.phraser
        if self.mode or self.lit or self.busy or not p.active or self.wake_task:
            return
        seconds = len(p.phrase) * FRAME / RATE
        if seconds < 0.7 or seconds - self.wake_checked < 0.5:
            return
        self.wake_checked = seconds
        pcm = np.concatenate(p.phrase[-int(4 * RATE / FRAME):])
        self.wake_task = asyncio.create_task(self.wake_early(pcm))

    async def wake_early(self, pcm: np.ndarray) -> None:
        try:
            r = await self.http.post("/v1/transcribe", params={"hotwords": "Cortana", "fast": "true"},
                                     content=to_wav(pcm))
            r.raise_for_status()
            if WAKE.search(r.json()["text"]) and not self.lit and self.mode is None and not self.busy:
                self.lit = True
                await send(self.writer, "detection", {"name": "cortana"})
                await ring_listening()
                await self.duck(True)
        except Exception:
            pass  # the full transcript at the end of the phrase still decides
        finally:
            self.wake_task = None

    async def on_phrase(self, pcm: np.ndarray) -> None:
        if self.wake_task:
            await self.wake_task
        lit, self.lit, self.wake_checked = self.lit, False, 0.0
        seconds = len(pcm) / RATE
        if seconds < 0.4:
            return
        # the phrase ended one silence-wait before we were handed it
        self.phrase_end = time.monotonic() - Cfg.end_silence
        t = time.monotonic()
        text = await self.transcribe(pcm)
        self.stt_s = time.monotonic() - t
        match = WAKE.search(text) if text else None
        if lit and not match and self.mode is None:
            await self.stand_down("false start")  # the quick check misheard: ring off
        if not text:
            return
        if self.mode and END.match(text.strip()):
            await self.stand_down("conversation ended")
            return
        if match:
            await self.duck(True)
            # Keep anything said before the name too ("what do you think, Cortana?")
            request = (text[:match.start()] + " " + text[match.end():]).strip(" ,.")
            if len(request.split()) >= 2:
                if not lit:
                    await send(self.writer, "detection", {"name": "cortana"})
                await self.answer(request)
            else:
                print("  wake word heard, listening..." + (" (early)" if lit else ""))
                if not lit:
                    await send(self.writer, "detection", {"name": "cortana"})
                    await ring_listening()
                self.mode = "armed"
                self.mode_until = time.monotonic() + Cfg.followup_window
        elif self.mode == "armed":
            await self.answer(text)
        elif self.mode == "convo":
            if is_noise(text):
                print(f"  (noise {seconds:.1f}s)")  # never log what wasn't said to Cortana
                return
            await self.answer(text)
        else:
            print(f"  (ignored {seconds:.1f}s)")

    async def pump(self) -> None:
        """Read the speaker's stream without pause.

        If nothing reads while a reply is being fetched and spoken, the speaker's
        mic stream backs up, both sides stall, and the connection drops.
        """
        try:
            while True:
                kind, _data, payload = await recv(self.reader)
                self.last_heard = time.monotonic()
                if kind != "audio-chunk" or not payload:
                    continue
                if self.speaking and Cfg.barge_in:
                    self.hear_while_speaking(payload)  # only for "Cortana, stop"
                    continue
                if self.busy or time.monotonic() < self.deaf_until:
                    continue  # our own reply is in progress: discard
                self.mic.put_nowait(payload)
        except Exception as e:
            self.mic.put_nowait(e)

    async def announce(self) -> None:
        """Say timers, alarms and reminders from the bridge, between conversations.
        An alarm with music starts it once the greeting is done (unless the
        greeting was cut short with "Cortana, stop")."""
        while True:
            try:
                r = await self.http.get("/v1/alerts", timeout=35)
                alert = r.json() if r.status_code == 200 else {}
            except (httpx.HTTPError, ValueError):
                await asyncio.sleep(5)
                continue
            text = alert.get("text", "") if isinstance(alert, dict) else ""
            if not text:
                continue
            while self.busy or self.phraser.active:  # don't talk over anyone
                await asyncio.sleep(0.2)
            print(f"  alert:   {text}")
            self.busy = True
            try:
                await self.duck(True)
                # Short alerts twice, in case the first is missed.
                await self.speak(f"{text} {text}" if alert.get("repeat") else text)
                stopped = self.barge.is_set()
                await self.duck(False)
                if alert.get("music") and not stopped:
                    await self.http.post("/v1/ask", json={"text": f"play {alert['music']}",
                                                          "session": Cfg.session})
            except httpx.HTTPError as e:
                print(f"  alert error: {e}")
            finally:
                self.phraser.reset()
                self.busy = False

    async def run(self) -> None:
        await send(self.writer, "run-satellite")
        # Patched firmware then keeps the mic on during replies; others ignore it.
        await send(self.writer, "listen-while-speaking", {"enabled": Cfg.barge_in})
        print("Listening. Say: Hey Cortana ...")
        tasks = [asyncio.create_task(self.pump()), asyncio.create_task(self.announce())]
        try:
            await self.listen()
        finally:
            for task in tasks:
                task.cancel()

    async def listen(self) -> None:
        while True:
            try:
                payload = await asyncio.wait_for(self.mic.get(), 0.5)
            except asyncio.TimeoutError:
                payload = b""
            if isinstance(payload, Exception):
                raise ConnectionError(str(payload) or type(payload).__name__)
            # The speaker streams its mic nonstop. Silence means it rebooted or
            # dropped off Wi-Fi without closing the connection: reconnect. (Older
            # firmware mutes the mic while it speaks, so not during a reply.)
            if not self.speaking and time.monotonic() - self.last_heard > 15:
                raise ConnectionError("speaker stopped sending audio")
            # Never time out mid-sentence: a phrase in progress is always finished.
            if self.mode and time.monotonic() > self.mode_until and not self.phraser.active:
                await self.stand_down("no reply heard" if self.mode == "convo"
                                      else "no request heard")
            if not payload:
                continue
            for phrase in self.phraser.feed(np.frombuffer(payload, dtype=np.int16)):
                await self.on_phrase(phrase)
            self.check_wake_early()


async def levels(seconds: float) -> None:
    reader, writer = await connect()
    await send(writer, "run-satellite")
    end, chunks = time.monotonic() + seconds, []
    while time.monotonic() < end:
        kind, _d, payload = await asyncio.wait_for(recv(reader), 5)
        if kind == "audio-chunk":
            chunks.append(np.frombuffer(payload, dtype=np.int16))
    writer.close()
    pcm = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.int16)
    print(f"received {len(pcm) / RATE:.1f}s of audio in {len(chunks)} chunks over {seconds:.0f}s")
    if len(pcm):
        frames = pcm[: len(pcm) // FRAME * FRAME].reshape(-1, FRAME).astype(np.float32)
        rms = np.sqrt((frames ** 2).mean(axis=1))
        print(f"level (rms): quietest {np.percentile(rms, 10):.0f}, typical {np.median(rms):.0f}, "
              f"loudest {rms.max():.0f}, peak sample {np.abs(pcm).max()}")


async def say(text: str, voices: list[str]) -> None:
    """Speak text through the speaker, once per voice (default voice if none)."""
    headers = {"Authorization": f"Bearer {Cfg.token}"} if Cfg.token else {}
    reader, writer = await connect()
    async with httpx.AsyncClient(base_url=Cfg.bridge, headers=headers, timeout=120) as http:
        link = Link(reader, writer, http)
        await send(writer, "run-satellite")
        drain = asyncio.create_task(link.pump())
        link.busy = True
        for n, voice in enumerate(voices or [""], 1):
            await link.speak(text.replace("{n}", str(n)), voice)
            await asyncio.sleep(0.6)
        drain.cancel()
    await asyncio.sleep(0.5)
    writer.close()


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--levels", type=float)
    p.add_argument("--say")
    p.add_argument("--voice", action="append", default=[],
                   help="installed voice name; repeat to audition several")
    args = p.parse_args()
    if args.levels:
        return await levels(args.levels)
    if args.say:
        return await say(args.say, args.voice)

    headers = {"Authorization": f"Bearer {Cfg.token}"} if Cfg.token else {}
    if os.environ.get("MUSIC_STREAM", "1") != "0":
        import streamer  # music to the speaker over Wi-Fi
        streamer.start_background(lambda: Cfg.found)
    async with httpx.AsyncClient(base_url=Cfg.bridge, headers=headers, timeout=120) as http:
        waiting = False
        while True:
            try:
                reader, writer = await connect()
                print(f"Connected to speaker at {Cfg.found}:{Cfg.port}")
                waiting = False
                await Link(reader, writer, http).run()
            except (OSError, ConnectionError, asyncio.IncompleteReadError) as e:
                if not waiting:   # say it once, then keep looking quietly
                    print(f"Speaker link lost ({e}); searching for the speaker...")
                    waiting = True
                await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
