"""Invoke voice bridge: speech -> text -> local Qwen -> speech.

Endpoints
    GET  /health      status of the STT, LLM and TTS stages
    POST /v1/ask      {"text": "...", "session": "..."} -> {"reply": "..."}
    POST /v1/speak    {"text": "..."} -> WAV audio
    POST /v1/speak/stream  same, as raw PCM streamed while it is made (rate in X-Audio-Rate)
    POST /v1/transcribe  audio in (raw body) -> {"text": "..."}; ?fast=1 uses a quick model
    GET  /v1/alerts   long poll: timer announcements for the speaker to say
    POST /v1/voice    audio in (multipart field "audio" or raw body) -> WAV reply,
                      with X-Transcript / X-Reply headers (URL-encoded)
    POST /v1/reset    {"session": "..."} forgets that conversation

Everything is configured through environment variables; see Settings below.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import re
import shutil
import time
import wave
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "invoke-bridge"


class Settings:
    host = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port = int(os.environ.get("BRIDGE_PORT", "8765"))
    # Required for any caller that is not this PC. Leave unset for local-only use.
    token = os.environ.get("BRIDGE_TOKEN", "")

    # OpenAI-compatible chat servers, tried in order. Format: url|model;url|model
    # The url "copilot" runs GitHub Copilot CLI instead, e.g. copilot|gpt-5-mini
    # (an empty model lets Copilot choose). Run `copilot login` once first.
    llm_backends = os.environ.get(
        "BRIDGE_LLM",
        "http://127.0.0.1:8181/v1|;http://127.0.0.1:11434/v1|qwen3.8:27b",
    )
    copilot_timeout = float(os.environ.get("BRIDGE_COPILOT_TIMEOUT", "45"))
    max_tokens = int(os.environ.get("BRIDGE_MAX_TOKENS", "300"))
    history_turns = int(os.environ.get("BRIDGE_HISTORY_TURNS", "8"))
    system_prompt = os.environ.get(
        "BRIDGE_SYSTEM_PROMPT",
        "Your name is Cortana. You are a voice assistant speaking through a smart speaker. "
        "Answer in one to three short spoken sentences. Plain words only: "
        "no markdown, lists, emoji or code. If you do not know, say so. "
        "Apart from the clock, weather, timers and music described below, you can only "
        "talk: you have no calendar, email, internet or other device control. If asked to "
        "do any of those, say you can't do that yet. Never claim to have done something.",
    )

    stt_model = os.environ.get("BRIDGE_STT_MODEL", "base.en")
    # "cuda" needs the NVIDIA driver and the CUDA libraries in LD_LIBRARY_PATH;
    # the CPU is used if it fails. Keep "cpu" where the GPUs are full of Qwen.
    stt_device = os.environ.get("BRIDGE_STT_DEVICE", "cpu")
    stt_compute = os.environ.get("BRIDGE_STT_COMPUTE", "int8")
    stt_threads = int(os.environ.get("BRIDGE_STT_THREADS", "8"))
    # Give up on a Microsoft online voice after this long and use the local one.
    # For /v1/speak/stream it is the wait for the first audio, so it can be short.
    edge_timeout = float(os.environ.get("BRIDGE_EDGE_TIMEOUT", "2.5"))
    # /v1/speak and /v1/voice wait for the whole reply from Microsoft first.
    edge_total_timeout = float(os.environ.get("BRIDGE_EDGE_TOTAL_TIMEOUT", "8"))

    voice = Path(
        os.environ.get("BRIDGE_VOICE", DATA_DIR / "voices" / "en_US-amy-medium.onnx")
    )


settings = Settings()
state: dict = {}
histories: dict[str, list[dict]] = defaultdict(list)


def parse_backends(spec: str) -> list[tuple[str, str]]:
    out = []
    for part in spec.split(";"):
        if part.strip():
            url, _, model = part.partition("|")
            out.append((url.strip().rstrip("/"), model.strip()))
    return out


@asynccontextmanager
async def lifespan(app: FastAPI):
    from faster_whisper import WhisperModel
    from piper import PiperVoice

    def load_stt(device: str, compute: str) -> WhisperModel:
        return WhisperModel(
            settings.stt_model,
            device=device,
            compute_type=compute,
            cpu_threads=settings.stt_threads,
            download_root=str(DATA_DIR / "whisper"),
        )

    import numpy as np

    try:
        state["stt"] = load_stt(settings.stt_device, settings.stt_compute)
        if settings.stt_device != "cpu":  # fail here, not on the first request
            list(state["stt"].transcribe(np.zeros(16000, np.float32), language="en")[0])
        state["stt_on"] = settings.stt_device
    except Exception as e:  # no GPU driver or CUDA libraries: the CPU still works
        print(f"speech-to-text on {settings.stt_device} failed ({type(e).__name__}: {e}); using the CPU")
        state["stt"] = load_stt("cpu", "int8")
        state["stt_on"] = "cpu"
    # A small, quick model for checking for the wake word while someone is
    # still talking (/v1/transcribe?fast=1). On the CPU the main model is it.
    state["stt_fast"] = state["stt"]
    if state["stt_on"] != "cpu" and settings.stt_model != "base.en":
        state["stt_fast"] = WhisperModel("base.en", device=state["stt_on"], compute_type=settings.stt_compute,
                                         download_root=str(DATA_DIR / "whisper"))
    state["tts"] = PiperVoice.load(settings.voice)
    state["http"] = httpx.AsyncClient(timeout=httpx.Timeout(120, connect=2))
    try:
        await skills.locate(state["http"])
    except Exception as e:
        print(f"couldn't look up CORTANA_PLACE ({type(e).__name__}: {e}); no weather for now")
    for url, model in parse_backends(settings.llm_backends):
        if url == "copilot":  # start it now so the first question is quick
            try:
                await copilot_agent(model).start()
            except Exception as e:
                print(f"Copilot didn't start ({type(e).__name__}: {e}); will retry on first question")
    # Keep the speaker dashboard's Music card current (its certificate is self-signed).
    speaker_http = httpx.AsyncClient(verify=False, timeout=httpx.Timeout(40, connect=3))
    sync = [asyncio.create_task(music.speaker_sync(speaker_http)),
            asyncio.create_task(music.speaker_commands(speaker_http))]
    yield
    for task in sync:
        task.cancel()
    await speaker_http.aclose()
    for agent in state.get("copilot", {}).values():
        await agent.stop()
    await state["http"].aclose()


app = FastAPI(title="Invoke voice bridge", lifespan=lifespan)

import music  # noqa: E402  (Apple Music by voice; see music.py)
import skills  # noqa: E402  (clock, timers and weather; see skills.py)

app.include_router(music.router)


def check_auth(request: Request, authorization: str = Header(default="")):
    local = request.client and request.client.host in ("127.0.0.1", "::1")
    if settings.token:
        if authorization != f"Bearer {settings.token}":
            raise HTTPException(401, "bad or missing bearer token")
    elif not local:
        raise HTTPException(403, "set BRIDGE_TOKEN to allow non-local callers")


# ---------- stages ----------

def transcribe(audio: bytes, hotwords: str | None = None, fast: bool = False) -> str:
    import numpy as np
    from faster_whisper import decode_audio

    # Lead with half a second of silence: on the GPU, Whisper drops the first
    # words ("Hey Cortana") of a clip that starts mid-speech.
    pcm = decode_audio(io.BytesIO(audio), sampling_rate=16000)
    pcm = np.concatenate([np.zeros(8000, np.float32), pcm])
    segments, _ = state["stt_fast" if fast else "stt"].transcribe(
        pcm, language="en", vad_filter=True, beam_size=1,
        hotwords=hotwords or None,
    )
    return " ".join(s.text.strip() for s in segments).strip()


VOICE_FILE = DATA_DIR / "voice.txt"


def default_voice() -> str:
    """The chosen voice: "" for the built-in Piper voice, a Piper voice name,
    or "edge:<name>" for a Microsoft online voice. Stored in voice.txt."""
    try:
        return VOICE_FILE.read_text().strip()
    except OSError:
        return ""


def edge_synthesize(text: str, name: str) -> bytes:
    """Speak with a Microsoft Edge online voice. Sends `text` to Microsoft."""
    import av
    import edge_tts

    async def fetch() -> bytes:
        mp3 = bytearray()
        async for chunk in edge_tts.Communicate(text, name).stream():
            if chunk["type"] == "audio":
                mp3 += chunk["data"]
        return bytes(mp3)

    mp3 = asyncio.run(asyncio.wait_for(fetch(), settings.edge_total_timeout))
    if not mp3:
        raise RuntimeError("no audio returned")
    rate = 24000
    resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
    pcm = bytearray()
    with av.open(io.BytesIO(mp3)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                pcm += out.to_ndarray().tobytes()
    for out in resampler.resample(None):
        pcm += out.to_ndarray().tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(bytes(pcm))
    return buf.getvalue()


def piper_voice(voice: str):
    """The built-in Piper voice ("") or another installed one by name."""
    if not voice:
        return state["tts"]
    path = settings.voice.parent / f"{Path(voice).stem}.onnx"
    if not path.is_file():
        raise HTTPException(404, f"voice not installed: {path.stem}")
    cache = state.setdefault("voices", {})
    if path.stem not in cache:
        from piper import PiperVoice
        cache[path.stem] = PiperVoice.load(path)
    return cache[path.stem]


def synthesize(text: str, voice: str = "") -> bytes:
    voice = voice or default_voice()
    if voice.startswith("edge:"):
        try:
            return edge_synthesize(text, voice[5:])
        except Exception as e:  # offline, blocked or slow: use the local voice
            print(f"edge voice failed ({type(e).__name__}: {e}); using local voice")
            voice = ""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        piper_voice(voice).synthesize_wav(text, wav)
    return buf.getvalue()


EDGE_RATE = 24000


async def edge_stream(text: str, name: str):
    """16-bit mono PCM from a Microsoft online voice, decoded as the MP3 arrives."""
    import av
    import edge_tts

    codec = av.CodecContext.create("mp3", "r")
    resampler = av.AudioResampler(format="s16", layout="mono", rate=EDGE_RATE)

    def decode(data: bytes | None) -> bytes:
        pcm = bytearray()
        for packet in codec.parse(data):
            for frame in codec.decode(packet):
                for out in resampler.resample(frame):
                    pcm += out.to_ndarray().tobytes()
        if data is None:  # end of stream: flush the decoder and resampler
            for frame in codec.decode(None):
                for out in resampler.resample(frame):
                    pcm += out.to_ndarray().tobytes()
            for out in resampler.resample(None):
                pcm += out.to_ndarray().tobytes()
        return bytes(pcm)

    async for chunk in edge_tts.Communicate(text, name).stream():
        if chunk["type"] == "audio" and (pcm := decode(chunk["data"])):
            yield pcm
    if pcm := decode(None):
        yield pcm


async def piper_stream(tts, text: str):
    """16-bit mono PCM from Piper, one sentence at a time."""
    chunks = iter(tts.synthesize(text))
    while (chunk := await asyncio.to_thread(next, chunks, None)) is not None:
        yield chunk.audio_int16_bytes


async def speech_stream(text: str, voice: str = ""):
    """(sample rate, PCM chunks) for `text`, made as playback goes so the first
    words can play before the rest exists. A Microsoft voice that sends nothing
    within BRIDGE_EDGE_TIMEOUT seconds is replaced by the local voice."""
    voice = voice or default_voice()
    if voice.startswith("edge:"):
        chunks = edge_stream(text, voice[5:])
        try:
            first = await asyncio.wait_for(anext(chunks), settings.edge_timeout)
        except Exception as e:  # offline, blocked or slow: use the local voice
            print(f"edge voice failed ({type(e).__name__}: {e}); using local voice")
            voice = ""
        else:
            async def rest():
                yield first
                try:
                    async for pcm in chunks:
                        yield pcm
                except Exception as e:
                    print(f"edge voice stopped mid-reply ({type(e).__name__}: {e})")
            return EDGE_RATE, rest()
    tts = piper_voice(voice)
    return tts.config.sample_rate, piper_stream(tts, text)


_THINK = re.compile(r"<think>.*?</think>", re.S)
_MARKUP = re.compile(r"[*_`#>|]+")


def speakable(text: str) -> str:
    text = _THINK.sub("", text)
    text = _MARKUP.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


COPILOT_DIR = DATA_DIR / "copilot-cwd"  # empty folder: nothing for Copilot to read


class CopilotAgent:
    """GitHub Copilot CLI kept running in ACP mode (JSON-RPC over stdio), so a
    question costs a new session (~0.1 s) instead of a new process (~2.5 s).
    Every tool is switched off: Copilot can only answer in words."""

    RECYCLE_AFTER = 100  # sessions; restart now and then so memory stays flat

    def __init__(self, model: str) -> None:
        self.model = model
        self.proc: asyncio.subprocess.Process | None = None
        self.next_id = 0
        self.pending: dict[int, asyncio.Future] = {}
        self.replies: dict[str, list[str]] = {}
        self.sessions = 0
        self.lock = asyncio.Lock()

    def _send(self, msg: dict) -> None:
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())

    async def _call(self, method: str, params: dict, timeout: float = 15) -> dict:
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = future
        self._send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        msg = await asyncio.wait_for(future, timeout)
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg["result"]

    async def _read(self, proc: asyncio.subprocess.Process) -> None:
        try:
            while line := await proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "method" in msg and "id" in msg:  # Copilot asks for something: refuse
                    self._send({"jsonrpc": "2.0", "id": msg["id"],
                                "error": {"code": -32601, "message": "not supported"}})
                elif "id" in msg:
                    future = self.pending.pop(msg["id"], None)
                    if future and not future.done():
                        future.set_result(msg)
                elif msg.get("method") == "session/update":
                    params, update = msg["params"], msg["params"]["update"]
                    if update.get("sessionUpdate") == "agent_message_chunk":
                        if params.get("sessionId") in self.replies:
                            self.replies[params["sessionId"]].append(update["content"].get("text", ""))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Copilot stopped"))
            self.pending.clear()

    async def start(self) -> None:
        """Start Copilot if it isn't running (or is due a restart)."""
        async with self.lock:
            if self.proc and self.proc.returncode is None and self.sessions < self.RECYCLE_AFTER:
                return
            await self.stop()
            COPILOT_DIR.mkdir(parents=True, exist_ok=True)
            args = ["copilot", "--acp", "--available-tools", "", "--disable-builtin-mcps",
                    "--no-custom-instructions", "--no-ask-user", "--no-auto-update"]
            if self.model:
                args += ["--model", self.model]
            self.proc = await asyncio.create_subprocess_exec(
                *args, cwd=COPILOT_DIR, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=1 << 22,
            )
            asyncio.create_task(self._read(self.proc))
            self.sessions = 0
            await self._call("initialize", {"protocolVersion": 1, "clientCapabilities": {}})

    async def stop(self) -> None:
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            await self.proc.wait()
        self.proc = None

    async def ask(self, prompt: str) -> str:
        await self.start()
        self.sessions += 1
        session = (await self._call("session/new", {"cwd": str(COPILOT_DIR), "mcpServers": []}))["sessionId"]
        self.replies[session] = []
        try:
            await self._call("session/prompt", {"sessionId": session,
                                                "prompt": [{"type": "text", "text": prompt}]},
                             settings.copilot_timeout)
            return "".join(self.replies[session])
        except asyncio.TimeoutError:
            self._send({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}})
            raise RuntimeError(f"no reply within {settings.copilot_timeout:.0f} s")
        finally:
            self.replies.pop(session, None)


def copilot_agent(model: str) -> CopilotAgent:
    agents = state.setdefault("copilot", {})
    if model not in agents:
        agents[model] = CopilotAgent(model)
    return agents[model]


async def ask_copilot(messages: list[dict], model: str) -> str:
    """One reply from GitHub Copilot CLI."""
    lines = [messages[0]["content"], ""]
    if len(messages) > 2:
        lines.append("Earlier in this conversation:")
        for m in messages[1:-1]:
            lines.append(f"{'User' if m['role'] == 'user' else 'Cortana'}: {m['content']}")
        lines.append("")
    lines += [f"The user now says: {messages[-1]['content']}", "",
              "Reply as Cortana. Output only the spoken reply."]
    return await copilot_agent(model).ask("\n".join(lines))


async def chat(session: str, user_text: str) -> str:
    history = histories[session]
    # Music requests go straight to the player; the model never sees them.
    done = await music.try_handle(user_text)
    if done is not None:
        history += [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": done or "Okay."},
        ]
        return done
    system = settings.system_prompt + " " + await skills.context(state["http"])
    if music.available():
        system += " " + music.MODEL_HELP
    else:
        system += " Music is not available right now."
    messages = (
        [{"role": "system", "content": system}]
        + history[-2 * settings.history_turns :]
        + [{"role": "user", "content": user_text}]
    )
    errors = []
    for url, model in parse_backends(settings.llm_backends):
        body = {"messages": messages, "max_tokens": settings.max_tokens, "stream": False}
        if model:
            body["model"] = model
        try:
            if url == "copilot":
                raw = await ask_copilot(messages, model)
            else:
                r = await state["http"].post(f"{url}/chat/completions", json=body)
                r.raise_for_status()
                raw = r.json()["choices"][0]["message"]["content"] or ""
        except Exception as e:  # try the next backend
            errors.append(f"{url}: {type(e).__name__} {e}")
            continue
        # Timer and music commands the model asked for (tags), then plain spoken text.
        reply = speakable(await music.run_tags(skills.run_tags(_THINK.sub("", raw))))
        if not reply:
            errors.append(f"{url}: empty reply")
            continue
        history += [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ]
        return reply
    raise HTTPException(502, "no LLM backend answered: " + " | ".join(errors))


# ---------- routes ----------

class AskIn(BaseModel):
    text: str
    session: str = "default"


class SpeakIn(BaseModel):
    text: str
    voice: str = ""


class ResetIn(BaseModel):
    session: str = "default"


@app.get("/health")
async def health():
    backends = {}
    for url, _ in parse_backends(settings.llm_backends):
        if url == "copilot":
            backends[url] = shutil.which("copilot") is not None
            continue
        try:
            r = await state["http"].get(f"{url}/models", timeout=2)
            backends[url] = r.status_code == 200
        except Exception:
            backends[url] = False
    return {
        "stt": f"{settings.stt_model} on {state.get('stt_on', '?')}",
        "tts": settings.voice.stem,
        "llm": backends,
        "ok": any(backends.values()),
    }


@app.post("/v1/ask", dependencies=[Depends(check_auth)])
async def ask(body: AskIn):
    reply = await chat(body.session, body.text)
    # music_playing tells the speaker link not to keep listening over a song
    playing = music.hub.playing or await music.airplay_playing()
    return {"reply": reply, "music_playing": playing}


@app.post("/v1/speak", dependencies=[Depends(check_auth)])
async def speak(body: SpeakIn):
    audio = await asyncio.to_thread(synthesize, speakable(body.text), body.voice)
    return Response(audio, media_type="audio/wav")


@app.post("/v1/speak/stream", dependencies=[Depends(check_auth)])
async def speak_stream(body: SpeakIn):
    """Like /v1/speak, but raw 16-bit mono PCM sent while it is being made.
    The sample rate is in the X-Audio-Rate header."""
    rate, pcm = await speech_stream(speakable(body.text), body.voice)
    return StreamingResponse(pcm, media_type="application/octet-stream",
                             headers={"X-Audio-Rate": str(rate)})


@app.post("/v1/transcribe", dependencies=[Depends(check_auth)])
async def transcribe_route(request: Request, hotwords: str = "", fast: bool = False):
    audio = await request.body()
    if not audio:
        raise HTTPException(400, "no audio received")
    try:
        text = await asyncio.to_thread(transcribe, audio, hotwords, fast)
    except Exception as e:
        raise HTTPException(400, f"could not decode audio: {e}")
    return {"text": text}


@app.get("/v1/alerts", dependencies=[Depends(check_auth)])
async def next_alert():
    """Long poll for the speaker link: {"text": "Your pasta timer is done."} when
    a timer ends, or {"text": ""} after 25 s with nothing to say."""
    try:
        return {"text": await asyncio.wait_for(skills.alerts.get(), 25)}
    except asyncio.TimeoutError:
        return {"text": ""}


@app.post("/v1/voice", dependencies=[Depends(check_auth)])
async def voice(request: Request, session: str = "default"):
    if "multipart/form-data" in request.headers.get("content-type", ""):
        form = await request.form()
        upload = form.get("audio")
        if upload is None:
            raise HTTPException(400, 'multipart field "audio" is missing')
        audio = await upload.read()
    else:
        audio = await request.body()
    if not audio:
        raise HTTPException(400, "no audio received")

    t0 = time.perf_counter()
    try:
        transcript = await asyncio.to_thread(transcribe, audio)
    except Exception as e:
        raise HTTPException(400, f"could not decode audio: {e}")
    t1 = time.perf_counter()
    if not transcript:
        reply = "Sorry, I didn't catch that."
    else:
        reply = await chat(session, transcript)
    t2 = time.perf_counter()
    out = await asyncio.to_thread(synthesize, reply)
    t3 = time.perf_counter()


    return Response(
        out,
        media_type="audio/wav",
        headers={
            "X-Transcript": quote(transcript),
            "X-Reply": quote(reply),
            "X-Timing-Ms": f"stt={1000*(t1-t0):.0f},llm={1000*(t2-t1):.0f},tts={1000*(t3-t2):.0f}",
        },
    )


@app.post("/v1/reset", dependencies=[Depends(check_auth)])
async def reset(body: ResetIn):
    histories.pop(body.session, None)
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn

    # Standard asyncio, not uvloop: uvloop starts subprocesses (Copilot) with fork,
    # which can deadlock in this multi-threaded process.
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", loop="asyncio")
