"""Apple Music by voice.

Apple only allows playback inside its own players, so the music plays in a
browser tab on this PC (bridge page /music, built on Apple's MusicKit) and the
PC sends the sound to the speaker over Bluetooth. This module

  * signs the MusicKit developer token from your Apple Developer key,
  * serves the player page and relays commands to it, and
  * recognises spoken music requests ("play Abbey Road", "pause", "next song").

Setup files, in %LOCALAPPDATA%\\invoke-bridge\\apple:
    AuthKey_<KEYID>.p8   the MusicKit private key downloaded from Apple
    team.txt             your 10-character Apple Developer Team ID
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

APPLE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "invoke-bridge" / "apple"
PAGE = Path(__file__).with_name("music.html")
router = APIRouter()


def local_only(request: Request) -> None:
    if not (request.client and request.client.host in ("127.0.0.1", "::1")):
        raise HTTPException(403, "the music player is only available on this PC")


# ---------- developer token ----------

def credentials() -> tuple[str, str, Path] | None:
    """(team id, key id, key file) or None if setup is incomplete."""
    keys = sorted(APPLE_DIR.glob("AuthKey_*.p8"))
    team_file = APPLE_DIR / "team.txt"
    if not keys or not team_file.is_file():
        return None
    team = team_file.read_text().strip()
    key_id = keys[-1].stem.split("_", 1)[1]
    return (team, key_id, keys[-1]) if team and key_id else None


_token: tuple[float, str] | None = None


def developer_token(origin: str) -> str:
    global _token
    if _token and _token[0] - time.time() > 600:
        return _token[1]
    creds = credentials()
    if not creds:
        raise HTTPException(503, "Apple Music is not set up: add the key and team.txt")
    import jwt

    team, key_id, key_file = creds
    now = int(time.time())
    expires = now + 12 * 3600
    token = jwt.encode(
        # "origin" makes Apple reject this token from any other web page.
        {"iss": team, "iat": now, "exp": expires, "origin": [origin]},
        key_file.read_text(),
        algorithm="ES256",
        headers={"kid": key_id},
    )
    _token = (expires, token)
    return token


# ---------- player page link ----------

class Hub:
    """One browser tab is the player; commands go out over server-sent events."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.state: dict = {}
        self.ducked = False

    @property
    def connected(self) -> bool:
        return self.queue is not None

    @property
    def playing(self) -> bool:
        return self.connected and bool(self.state.get("playing"))

    async def events(self):
        queue: asyncio.Queue = asyncio.Queue()
        self.queue = queue
        try:
            yield ": connected\n\n"
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), 15)
                    yield f"data: {json.dumps(msg)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            if self.queue is queue:
                self.queue, self.state = None, {}

    async def command(self, action: str, timeout: float = 15, **args) -> dict:
        if not self.queue:
            return {"ok": False, "text": "The music player isn't open on the computer."}
        cid = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[cid] = future
        await self.queue.put({"id": cid, "action": action, **args})
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "text": "The music player didn't respond."}
        finally:
            self.pending.pop(cid, None)


hub = Hub()


# ---------- understanding spoken requests ----------

_POLITE = r"(?:(?:can|could|would) you |please |hey |ok(?:ay)? )*"
_TAIL = r"(?: (?:on|from|in) apple music)?(?: please)?[.!?]*$"


def parse(text: str) -> dict | None:
    """Turn a spoken phrase into a player command, or None if it isn't one."""
    t = re.sub(r"\s+", " ", text.strip().lower())
    t = re.sub(rf"^{_POLITE}", "", t)
    t = re.sub(_TAIL, "", t).strip(" ,.!?")

    if re.fullmatch(r"(?:pause|stop)(?: (?:the |this )?(?:music|song|track|playback|it))?", t):
        return {"action": "pause"}
    if re.fullmatch(r"(?:resume|unpause|continue|play|keep playing|carry on)"
                    r"(?: (?:the |this )?(?:music|song|track|playback|it))?", t):
        return {"action": "resume"}
    if re.fullmatch(r"(?:next|skip)(?: (?:this |the |that )?(?:song|track|one))?|"
                    r"(?:play |go to )?(?:the )?next (?:song|track|one)", t):
        return {"action": "next"}
    if re.fullmatch(r"(?:previous|go back|back)(?: (?:a |one )?(?:song|track))?|"
                    r"(?:play |go to )?(?:the )?(?:previous|last) (?:song|track|one)", t):
        return {"action": "previous"}
    if re.fullmatch(r"what(?:'s| is)(?: this| playing| this song| the name of this song)?(?: playing)?(?: right now)?|"
                    r"what song is this|who (?:is|sings) this", t):
        return {"action": "nowplaying"}
    # Looping what is already playing, or stopping the loop.
    if re.fullmatch(r"(?:loop|repeat)(?: (?:this|it|that))?(?: (?:song|track|one|album|playlist))?|"
                    r"(?:put|keep) (?:this|it)(?: song)? on (?:repeat|a loop|loop)", t):
        return {"action": "repeat", "on": True}
    if re.fullmatch(r"(?:stop|quit|no more) (?:looping|repeating)(?: (?:this|it))?|"
                    r"turn off (?:the )?(?:repeat|loop|looping)|(?:repeat|loop) off|don't repeat", t):
        return {"action": "repeat", "on": False}

    m = re.fullmatch(r"(?:set |turn )?(?:the )?(?:music )?volume (?:to |at )?(\d{1,3})(?: ?(?:percent|%))?", t)
    if m:
        return {"action": "volume", "level": max(0, min(100, int(m.group(1)))) / 100}
    if re.fullmatch(r"(?:turn (?:it|the music|the volume) up|(?:music |volume )?(?:louder|up)|volume up|"
                    r"turn up the (?:music|volume))", t):
        return {"action": "volume", "delta": 0.15}
    if re.fullmatch(r"(?:turn (?:it|the music|the volume) down|(?:music |volume )?(?:quieter|down|softer)|"
                    r"volume down|turn down the (?:music|volume))", t):
        return {"action": "volume", "delta": -0.15}

    # "loop rain sounds", "play ocean waves on repeat": play it and keep it going.
    repeat = False
    m = re.fullmatch(r"(?:loop|repeat) (.+)", t)
    if m:
        repeat, t = True, f"play {m.group(1)}"
    m = re.fullmatch(r"(.+?),? (?:on (?:repeat|a loop|loop)|in a loop|looped|over and over)", t)
    if m:
        repeat, t = True, m.group(1)
    m = re.fullmatch(r"(?:play|put on|listen to|i want to hear|i'd like to hear) (.+)", t)
    if not m:
        return None
    want = m.group(1).strip()
    kind = None
    for word, k in (("the song ", "song"), ("the album ", "album"), ("the playlist ", "playlist"),
                    ("my playlist ", "playlist"), ("songs by ", "artist"), ("music by ", "artist"),
                    ("something by ", "artist"), ("some ", None), ("the artist ", "artist")):
        if want.startswith(word):
            want, kind = want[len(word):], k
            break
    for suffix, k in ((" album", "album"), (" playlist", "playlist")):
        if want.endswith(suffix) and kind is None and len(want) > len(suffix):
            want, kind = want[: -len(suffix)], k
    artist = None
    if kind != "artist" and " by " in want:
        want, artist = (s.strip() for s in want.rsplit(" by ", 1))
    if not want or want in ("music", "a song", "something", "anything", "it"):
        return {"action": "repeat", "on": True} if repeat else {"action": "resume"}
    return {"action": "play", "query": want, "artist": artist, "kind": kind, "repeat": repeat}


async def try_handle(text: str) -> str | None:
    """If `text` is a music request and Apple Music is set up, do it and return
    the sentence to speak. Otherwise return None so the language model answers."""
    intent = parse(text)
    if not intent or not credentials():
        return None
    if intent["action"] != "play" and not hub.connected:
        return None  # "stop", "continue" and so on mean something else with no player
    result = await execute(intent)
    return result.get("text") or ("Okay." if result.get("ok") else "That didn't work.")


async def execute(intent: dict) -> dict:
    result = await hub.command(**intent)
    if result.get("ok") and intent["action"] in ("play", "resume", "next", "previous"):
        hub.state["playing"] = True   # the page confirms a moment later
    elif result.get("ok") and intent["action"] == "pause":
        hub.state["playing"] = False
    return result


# ---------- music commands from the language model ----------

# Added to the system prompt when Apple Music is ready, so requests the phrase
# parser above misses ("go ahead and play that one") still work.
MODEL_HELP = (
    "You can control Apple Music on this speaker. To do it, end your reply with one tag: "
    '<music action="play" query="what to search for" artist="artist, if known"/> '
    '(add kind="song", "album", "playlist" or "artist" when it is clear, and repeat="yes" to '
    'loop it, e.g. for rain or white noise to sleep to), '
    'or <music action="pause"/>, <music action="resume"/>, <music action="next"/>, '
    '<music action="previous"/>, <music action="repeat" on="yes|no"/> to loop what is playing, '
    'or <music action="volume" level="0 to 100"/>. '
    "Use a tag only when the user wants the music changed, and keep the spoken part to a few words. "
    "The tag is never read aloud."
)

_TAG = re.compile(r"<music\b([^>]*?)/?>(?:\s*</music>)?", re.I)
_KINDS = {"song", "album", "playlist", "artist"}


def available() -> bool:
    return credentials() is not None and hub.connected


async def run_tags(reply: str) -> str:
    """Carry out any <music .../> tags in a model reply and return the reply
    without them. When a command fails, the player's explanation is spoken
    instead; when a play works, the player's "Playing X by Y" is."""
    tags = _TAG.findall(reply)
    if not tags:
        return reply
    spoken = _TAG.sub("", reply).strip()
    for attrs in tags:
        a = {k.lower(): v.strip() for k, v in re.findall(r'(\w+)\s*=\s*"([^"]*)"', attrs)}
        action = a.get("action", "").lower()
        if action == "play" and a.get("query"):
            kind = a.get("kind", "").lower()
            intent = {"action": "play", "query": a["query"], "artist": a.get("artist") or None,
                      "kind": kind if kind in _KINDS else None,
                      "repeat": a.get("repeat", "").lower() in ("yes", "true", "1", "on")}
        elif action in ("pause", "resume", "next", "previous"):
            intent = {"action": action}
        elif action == "repeat":
            intent = {"action": "repeat", "on": a.get("on", "yes").lower() in ("yes", "true", "1", "on")}
        elif action == "volume" and a.get("level", "").isdigit():
            intent = {"action": "volume", "level": max(0, min(100, int(a["level"]))) / 100}
        else:
            continue
        result = await execute(intent)
        if not result.get("ok"):
            return result.get("text") or "That didn't work."
        if action == "play" and result.get("text"):
            spoken = result["text"]
    return spoken


# ---------- routes ----------

class Result(BaseModel):
    id: str
    ok: bool = True
    text: str = ""


class Duck(BaseModel):
    on: bool


@router.get("/music")
async def page(request: Request):
    local_only(request)
    return FileResponse(PAGE, headers={"Cache-Control": "no-store"})


@router.get("/v1/music/token")
async def token(request: Request):
    local_only(request)
    origin = request.headers.get("origin") or f"{request.url.scheme}://{request.url.netloc}"
    return {"token": developer_token(origin)}


@router.get("/v1/music/events")
async def events(request: Request):
    local_only(request)
    return StreamingResponse(hub.events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store"})


@router.post("/v1/music/result")
async def result(body: Result, request: Request):
    local_only(request)
    future = hub.pending.get(body.id)
    if future and not future.done():
        future.set_result(body.model_dump())
    return {"ok": True}


@router.post("/v1/music/state")
async def state(request: Request):
    local_only(request)
    hub.state = await request.json()
    return {"ok": True}


@router.post("/v1/music/duck")
async def duck(body: Duck, request: Request):
    """Lower the music while Cortana listens and talks, then bring it back."""
    local_only(request)
    if hub.connected and body.on != hub.ducked and (hub.playing or hub.ducked):
        hub.ducked = body.on
        await hub.command("duck", timeout=3, on=body.on)
    await duck_airplay(body.on)
    return {"ok": True}


# ---------- AirPlay ----------
# shairport-sync (cortana-airplay.service) plays into the same PipeWire sink as
# the player page, under the application name "AirPlay". Its volume follows the
# phone, so ducking turns down its stream in PipeWire instead. Needs pactl and
# XDG_RUNTIME_DIR pointing at the PipeWire session.

async def _run(*cmd: str) -> str:
    """Run a short command; its output, or "" on failure.

    Uses subprocess in a worker thread rather than asyncio's subprocess support:
    under uvloop that forks this busy, multi-threaded process, and the child can
    deadlock before exec, freezing the bridge. subprocess launches with vfork."""
    def run() -> str:
        try:
            r = subprocess.run(cmd, capture_output=True, timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            return ""
        return r.stdout.decode() if r.returncode == 0 else ""
    return await asyncio.to_thread(run)


async def _pactl(*args: str) -> str:
    return await _run("pactl", *args)


async def airplay_streams() -> list[dict]:
    try:
        inputs = json.loads(await _pactl("-f", "json", "list", "sink-inputs") or "[]")
    except ValueError:
        return []
    return [i for i in inputs if i.get("properties", {}).get("application.name") == "AirPlay"]


async def airplay_playing() -> bool:
    return any(not s.get("corked") for s in await airplay_streams())


async def duck_airplay(on: bool) -> None:
    for s in await airplay_streams():
        await _pactl("set-sink-input-volume", str(s["index"]), "15%" if on else "100%")


@router.get("/v1/music/status")
async def status(request: Request):
    local_only(request)
    return {"configured": credentials() is not None, "player_open": hub.connected, **hub.state}


async def _busctl(*args: str) -> str:
    return await _run("busctl", "--user", *args)


MPRIS = ("org.mpris.MediaPlayer2.ShairportSync", "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player")


async def airplay_now() -> dict:
    """AirPlay now-playing from shairport-sync's MPRIS interface ({} if idle)."""
    out = await _busctl("--json=short", "get-property", *MPRIS, "PlaybackStatus", "Metadata")
    try:
        status, meta = (json.loads(line)["data"] for line in out.splitlines()[:2])
    except (ValueError, KeyError):
        return {}
    if status == "Stopped" and not meta:
        return {}
    value = lambda k: (meta.get(k) or {}).get("data")
    artist = value("xesam:artist") or []
    return {
        "playing": status == "Playing",
        "title": value("xesam:title") or "",
        "artist": ", ".join(artist) if isinstance(artist, list) else str(artist),
        "album": value("xesam:album") or "",
        "duration_ms": int(value("mpris:length") or 0) // 1000,
    }


# ---------- the speaker's Music card ----------
# Patched Encore firmware shows a "Music" source on its dashboard. This keeps
# it up to date (POST /api/music/status) and carries out its transport buttons
# (GET /api/music/command, a long poll). Firmware without it just answers 404.

def _speaker() -> str:
    host = os.environ.get("SPEAKER_HOST", "")
    if host and host != "auto":
        return host
    try:
        return (Path.home() / "invoke-bridge" / "speaker.txt").read_text().strip()
    except OSError:
        return ""


async def music_now() -> dict:
    """What the speaker should show: AirPlay while a device is sending, else Apple Music."""
    air = await airplay_now()
    if air and (air["playing"] or await airplay_playing()):
        return {"service": "AirPlay", **air}
    s = hub.state if hub.connected else {}
    if s.get("title"):
        return {"service": "Apple Music", "playing": bool(s.get("playing")),
                "title": s.get("title") or "", "artist": s.get("artist") or "",
                "album": s.get("album") or "", "cover_url": s.get("cover") or "",
                "duration_ms": int(s.get("duration_ms") or 0), "position_ms": int(s.get("position_ms") or 0)}
    if air:
        return {"service": "AirPlay", **air}
    return {}


async def speaker_sync(http) -> None:
    """Report now-playing to the speaker while it changes, at least every 15 s."""
    last, sent_at = None, 0.0
    while True:
        await asyncio.sleep(2)
        host = _speaker()
        if not host:
            continue
        try:
            now = await music_now()
        except Exception:
            continue
        key = {k: v for k, v in now.items() if k != "position_ms"}
        if key == last and time.monotonic() - sent_at < 15:
            continue
        try:
            r = await http.post(f"https://{host}/api/music/status", json=now, timeout=4)
            if r.status_code == 404:
                await asyncio.sleep(60)  # firmware without the Music source
                continue
            last, sent_at = key, time.monotonic()
        except Exception:
            await asyncio.sleep(5)


async def speaker_commands(http) -> None:
    """Carry out the Music card's play/pause/next/previous buttons."""
    while True:
        host = _speaker()
        try:
            r = await http.get(f"https://{host}/api/music/command", timeout=35) if host else None
            # Older firmware answers unknown paths with the dashboard page, not JSON.
            action = r.json().get("action") if r is not None and r.status_code == 200 else None
        except Exception:
            r, action = None, None
        if not action:
            if r is None or r.status_code != 200:
                await asyncio.sleep(10)
            continue
        try:
            now = await music_now()
            if now.get("service") == "AirPlay":
                method = {"play": "Play", "pause": "Pause", "next": "Next", "previous": "Previous"}.get(action)
                if method:
                    await _busctl("call", *MPRIS, method)
            elif hub.connected:
                await execute({"action": "resume" if action == "play" else action})
        except Exception as e:
            print(f"music: couldn't {action} ({type(e).__name__}: {e})")
