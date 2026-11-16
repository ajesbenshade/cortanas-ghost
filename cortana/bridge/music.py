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


_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "ten": 10,
            "fifteen": 15, "twenty": 20, "thirty": 30, "forty": 40, "forty-five": 45,
            "forty five": 45, "fifty": 50, "sixty": 60, "ninety": 90}


def _minutes(text: str) -> int | None:
    """'30 minutes', 'an hour', 'half an hour', '1 and a half hours' -> minutes."""
    s = text.strip().lower().rstrip(".!?")
    if re.fullmatch(r"(?:half an? hour|a half hour|half hour)", s):
        return 30
    m = re.fullmatch(r"(\d+|[a-z]+(?:[ -]five)?)( and a half)? (minutes?|mins?|hours?|hrs?)"
                     r"( and a half)?", s)
    if not m:
        return None
    n = int(m.group(1)) if m.group(1).isdigit() else _NUMBERS.get(m.group(1))
    if not n:
        return None
    hours = m.group(3).startswith("h")
    half = bool(m.group(2) or m.group(4))
    total = n * 60 if hours else n
    return total + (30 if hours and half else 0) if 0 < total <= 12 * 60 else None


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

    # Sleep timer: "stop the music in 30 minutes", "sleep timer for an hour".
    m = re.fullmatch(r"(?:(?:set )?(?:a |the |my )?sleep timer(?: for)?|"
                     r"(?:stop|turn off|pause|end) (?:the )?(?:music|it|this|playing) (?:in|after)) (.+)", t)
    if m and (minutes := _minutes(m.group(1))):
        return {"action": "sleep", "minutes": minutes}
    if re.fullmatch(r"(?:cancel|stop|turn off|clear) (?:the |my )?sleep timer", t):
        return {"action": "sleep", "minutes": 0}

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
    if intent and intent["action"] == "sleep":  # works for AirPlay too
        return set_sleep(intent["minutes"])
    if not intent:
        return None
    if intent["action"] == "play":
        if not credentials():
            return None
    elif await current_service() is None:
        return None  # "stop", "continue" and so on mean something else with no music
    result = await control(intent)
    return result.get("text") or ("Okay." if result.get("ok") else "That didn't work.")


async def current_service() -> str | None:
    """Which player commands like "pause" or "next" should go to: AirPlay while a
    device plays (or is paused mid-session), else the Apple Music player."""
    air = await airplay_now()
    if air.get("playing"):
        return "airplay"
    if hub.playing:
        return "apple"
    if air:
        return "airplay"
    return "apple" if hub.connected else None


_AIRPLAY_METHODS = {"pause": "Pause", "resume": "Play", "next": "Next", "previous": "Previous"}
_airplay_level = 100  # AirPlay volume set by voice (the phone's slider is separate)
_airplay_muted = False  # muted because the phone ignored a pause


async def _mute_airplay(on: bool) -> None:
    global _airplay_muted
    _airplay_muted = on
    for s in await airplay_streams():
        await _pactl("set-sink-input-mute", str(s["index"]), "1" if on else "0")


async def _pause_airplay() -> dict:
    """Ask the phone to pause. Some apps ignore remote commands from the
    receiver, so if it is still playing a moment later, mute the stream instead."""
    await _busctl("call", *REMOTE, "Pause")
    for _ in range(12):
        await asyncio.sleep(0.25)
        if not await airplay_playing():
            return {"ok": True, "text": "Paused."}
    await _mute_airplay(True)
    return {"ok": True, "text": "The app on your phone won't take a pause from me, so I've muted it. Say resume to bring it back."}


async def control(intent: dict) -> dict:
    """Carry out a music command on whichever player is in use."""
    action = intent["action"]
    if action == "play":
        if (await airplay_now()).get("playing"):  # don't play two things at once
            await _busctl("call", *REMOTE, "Pause")
        return await execute(intent)
    if await current_service() != "airplay":
        return await execute(intent)
    if action == "pause":
        return await _pause_airplay()
    if action == "resume":
        await _mute_airplay(False)
    if action in _AIRPLAY_METHODS:
        await _busctl("call", *REMOTE, _AIRPLAY_METHODS[action])
        return {"ok": True, "text": ""}
    if action == "nowplaying":
        air = await airplay_now()
        if air.get("title"):
            by = f" by {air['artist']}" if air.get("artist") else ""
            return {"ok": True, "text": f"This is {air['title']}{by}, over AirPlay."}
        return {"ok": True, "text": "Something is playing over AirPlay, but it didn't say what."}
    if action == "volume":
        global _airplay_level
        level = intent.get("level")
        new = round(level * 100) if level is not None else _airplay_level + round(intent["delta"] * 100)
        _airplay_level = max(0, min(100, new))
        for s in await airplay_streams():
            await _pactl("set-sink-input-volume", str(s["index"]), f"{_airplay_level}%")
        return {"ok": True, "text": f"AirPlay volume {_airplay_level} percent."}
    if action == "repeat":
        return {"ok": False, "text": "I can't loop AirPlay from here; use repeat in the app that's playing."}
    return {"ok": False, "text": "I can't do that with AirPlay."}


# ---------- sleep timer ----------
# Fades out everything on the "Cortana Music" output (Apple Music and AirPlay
# alike) over about half a minute, pauses, then restores the volume.

_sleep_task: asyncio.Task | None = None
_sleep_ends = 0.0


def _span(minutes: float) -> str:
    minutes = max(1, round(minutes))
    h, m = divmod(minutes, 60)
    parts = ([f"{h} hour" + ("s" if h > 1 else "")] if h else []) + \
            ([f"{m} minute" + ("s" if m > 1 else "")] if m else [])
    return " and ".join(parts)


def sleep_status() -> str:
    if _sleep_task and not _sleep_task.done():
        import skills  # the household's clock; no import cycle (skills doesn't import music)
        left = max(0, _sleep_ends - time.time())
        when = f"{round(left)} seconds" if left < 120 else f"about {_span(left / 60)}"
        ends = skills.now() + skills.timedelta(seconds=left)
        return (f"Sleep timer (live, measured now): {when} left; the music fades out at "
                f"{ends.strftime('%I:%M %p').lstrip('0')}. Use this to answer how long is left.")
    return "No sleep timer is set."


def set_sleep(minutes: int) -> str:
    """Start (or with 0, cancel) the sleep timer; returns what to say."""
    global _sleep_task, _sleep_ends
    had = _sleep_task is not None and not _sleep_task.done()
    if had:
        _sleep_task.cancel()
    if minutes <= 0:
        return "Sleep timer cancelled." if had else "There's no sleep timer running."
    _sleep_ends = time.time() + minutes * 60
    _sleep_task = asyncio.create_task(_sleep_then_stop(minutes * 60))
    return f"Okay, the music will fade out in {_span(minutes)}."


async def _sleep_then_stop(seconds: float) -> None:
    await asyncio.sleep(seconds)
    await fade_out_and_stop()


async def fade_out_and_stop() -> None:
    try:
        for level in range(95, 0, -5):  # about 30 s down to silence
            await _pactl("set-sink-volume", "cortana_music", f"{level}%")
            await asyncio.sleep(1.5)
        if hub.connected:
            await execute({"action": "pause"})
        if (await airplay_now()).get("playing"):
            await _busctl("call", *REMOTE, "Pause")
        await asyncio.sleep(1)
    finally:
        await _pactl("set-sink-volume", "cortana_music", "100%")


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
    "You can control the music on this speaker: Apple Music, and pause/resume/next/previous/"
    "volume also reach a phone or Mac playing over AirPlay. To do it, end your reply with one tag: "
    '<music action="play" query="what to search for" artist="artist, if known"/> '
    '(add kind="song", "album", "playlist" or "artist" when it is clear, and repeat="yes" to '
    'loop it, e.g. for rain or white noise to sleep to), '
    'or <music action="pause"/>, <music action="resume"/>, <music action="next"/>, '
    '<music action="previous"/>, <music action="repeat" on="yes|no"/> to loop what is playing, '
    '<music action="volume" level="0 to 100"/>, or a sleep timer that fades the music out and '
    'stops it: <music action="sleep" minutes="30"/> (minutes="0" cancels it). '
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
        elif action == "sleep" and a.get("minutes", "").isdigit():
            set_sleep(int(a["minutes"]))  # the model words the confirmation itself
            continue
        elif action == "volume" and a.get("level", "").isdigit():
            intent = {"action": "volume", "level": max(0, min(100, int(a["level"]))) / 100}
        else:
            continue
        result = await control(intent)
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
    level = min(15, _airplay_level) if on else _airplay_level  # back to the voice-set level
    for s in await airplay_streams():
        await _pactl("set-sink-input-volume", str(s["index"]), f"{level}%")


@router.get("/v1/music/status")
async def status(request: Request):
    local_only(request)
    return {"configured": credentials() is not None, "player_open": hub.connected, **hub.state}


async def _busctl(*args: str) -> str:
    return await _run("busctl", "--user", *args)


MPRIS = ("org.mpris.MediaPlayer2.ShairportSync", "/org/mpris/MediaPlayer2", "org.mpris.MediaPlayer2.Player")
# Transport commands go through shairport-sync's own interface (its MPRIS Pause
# is ignored). Whether it is playing comes from PipeWire: the stream is corked
# while paused, and both D-Bus play states lag or flip.
REMOTE = ("org.gnome.ShairportSync", "/org/gnome/ShairportSync", "org.gnome.ShairportSync.RemoteControl")


def _short(title: str) -> str:
    """First line of a title, cut to a speakable length (video apps send whole posts)."""
    line = title.strip().splitlines()[0] if title.strip() else ""
    return line if len(line) <= 80 else line[:80].rsplit(" ", 1)[0] + "…"


async def airplay_now() -> dict:
    """AirPlay now-playing from shairport-sync's MPRIS interface ({} if idle)."""
    # shairport-sync's own Active flag says whether a device is connected at all;
    # MPRIS alone keeps a stale track id after the session ends.
    active = await _busctl("--json=short", "get-property", "org.gnome.ShairportSync",
                           "/org/gnome/ShairportSync", "org.gnome.ShairportSync", "Active")
    # PipeWire remembers a mute for the next AirPlay stream (or one left over
    # from a bridge restart); only a mute for an ignored pause should stay
    if not _airplay_muted and any(st.get("mute") for st in await airplay_streams()):
        await _mute_airplay(False)
    if '"data":true' not in active.replace(" ", ""):
        return {}
    out = await _busctl("--json=short", "get-property", *MPRIS, "Metadata")
    try:
        meta = json.loads(out)["data"]
    except (ValueError, KeyError):
        meta = {}
    value = lambda k: (meta.get(k) or {}).get("data")
    artist = value("xesam:artist") or []
    return {
        "playing": await airplay_playing(),
        "title": _short(value("xesam:title") or ""),
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
            if await current_service():
                await control({"action": "resume" if action == "play" else action})
        except Exception as e:
            print(f"music: couldn't {action} ({type(e).__name__}: {e})")
