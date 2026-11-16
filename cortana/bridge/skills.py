"""Clock, timers, alarms, reminders and weather for Cortana.

The language model gets the current time, the running timers, the alarms and
reminders and a short forecast in its system prompt (see context()), and sets
or cancels them by ending its reply with a tag, the same way it controls music:

    <timer action="start" seconds="600" label="pasta"/>
    <timer action="cancel" label="pasta"/>
    <alarm time="06:30" days="weekdays" music="Morning Coffee playlist"/>
    <alarm action="cancel" id="a1"/>
    <reminder at="2026-11-16 17:00" text="call Mom"/>
    <reminder action="cancel" id="r2"/>

Alarms and reminders are kept in ~/invoke-bridge/schedule.json, so they
survive restarts. When anything goes off, its announcement is queued in
`alerts` ({"text", "repeat", "music"}); the speaker link collects it from the
bridge's GET /v1/alerts, says it, and starts the music if there is any.

Settings (environment variables):
    CORTANA_PLACE     town for the weather and the time zone, e.g. "Springfield, IL"
    CORTANA_UNITS     "fahrenheit" (default) or "celsius"
    CORTANA_TZ        time zone without a place, e.g. "America/Chicago"
Weather comes from Open-Meteo (open-meteo.com), which needs no account; only
the town's coordinates are sent to it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx

PLACE = os.environ.get("CORTANA_PLACE", "").strip()
UNITS = os.environ.get("CORTANA_UNITS", "fahrenheit").lower()
TZ = os.environ.get("CORTANA_TZ", "").strip()  # used until CORTANA_PLACE gives one

_where: dict = {}          # name, latitude, longitude, timezone (from geocoding)
_weather: tuple[float, str] | None = None
alerts: asyncio.Queue = asyncio.Queue()


# ---------- place and clock ----------

async def locate(http: httpx.AsyncClient) -> None:
    """Look up CORTANA_PLACE once: coordinates for the weather, and its time zone."""
    if not PLACE or _where:
        return
    name, _, region = PLACE.partition(",")
    r = await http.get("https://geocoding-api.open-meteo.com/v1/search",
                       params={"name": name.strip(), "count": 10, "language": "en"}, timeout=5)
    r.raise_for_status()
    results = r.json().get("results") or []
    region = region.strip().lower()
    for res in results:  # prefer the one whose state or country matches "…, PA"
        names = {str(res.get(k, "")).lower() for k in ("admin1", "country", "country_code", "admin1_code")}
        if not region or region in names or any(region in n for n in names if len(region) > 2):
            _where.update(res)
            break
    if not _where and results:
        _where.update(results[0])
    if _where:
        print(f"Location: {_where.get('name')}, {_where.get('admin1')} ({_where.get('timezone')})")
    else:
        print(f"Location {PLACE!r} not found; weather is off and the clock uses the server's time zone")


def now() -> datetime:
    tz = _where.get("timezone") or TZ
    return datetime.now(ZoneInfo(tz)) if tz else datetime.now().astimezone()


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


# ---------- weather ----------

# WMO weather codes, as words
_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains", 80: "rain showers",
    81: "rain showers", 82: "heavy rain showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "thunderstorms with hail",
}


async def weather(http: httpx.AsyncClient) -> str:
    """A few lines of forecast, refreshed at most every 15 minutes. "" if unavailable."""
    global _weather
    if not _where:
        return ""
    if _weather and time.monotonic() - _weather[0] < 900:
        return _weather[1]
    deg = "F" if UNITS.startswith("f") else "C"
    try:
        r = await http.get("https://api.open-meteo.com/v1/forecast", timeout=4, params={
            "latitude": _where["latitude"], "longitude": _where["longitude"], "timezone": _where["timezone"],
            "temperature_unit": "fahrenheit" if deg == "F" else "celsius", "wind_speed_unit": "mph" if deg == "F" else "kmh",
            "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 3,
        })
        r.raise_for_status()
        w = r.json()
    except Exception as e:
        print(f"weather unavailable ({type(e).__name__}: {e})")
        return _weather[1] if _weather else ""
    c, d = w["current"], w["daily"]
    lines = [f"Now: {round(c['temperature_2m'])}°{deg} (feels like {round(c['apparent_temperature'])}°{deg}), "
             f"{_CODES.get(c['weather_code'], 'unknown')}, wind {round(c['wind_speed_10m'])} {'mph' if deg == 'F' else 'km/h'}."]
    for i, label in enumerate(["Today", "Tomorrow", datetime.fromisoformat(d["time"][2]).strftime("%A")]):
        lines.append(f"{label}: {_CODES.get(d['weather_code'][i], 'unknown')}, high {round(d['temperature_2m_max'][i])}°{deg}, "
                     f"low {round(d['temperature_2m_min'][i])}°{deg}, {d['precipitation_probability_max'][i]}% chance of rain or snow.")
    _weather = (time.monotonic(), " ".join(lines))
    return _weather[1]


# ---------- timers ----------

class Timers:
    def __init__(self) -> None:
        self.running: dict[str, tuple[float, asyncio.Task]] = {}  # label -> (ends at, task)

    def start(self, seconds: int, label: str) -> str:
        label = label.strip().lower() or "timer"
        if label in self.running:   # "another pasta timer": keep both
            n = 2
            while f"{label} {n}" in self.running:
                n += 1
            label = f"{label} {n}"
        ends = time.time() + seconds
        self.running[label] = (ends, asyncio.create_task(self._ring(label, seconds)))
        return label

    async def _ring(self, label: str, seconds: int) -> None:
        await asyncio.sleep(seconds)
        self.running.pop(label, None)
        name = "Your timer" if label == "timer" else f"Your {label} timer"
        await alerts.put({"text": f"{name} is done.", "repeat": True})

    def cancel(self, label: str = "") -> list[str]:
        label = label.strip().lower()
        if label and label not in self.running:   # "the pasta one" -> "pasta"
            label = next((k for k in self.running if label in k or k in label), "")
        names = [label] if label else (list(self.running)[-1:] if self.running else [])
        for name in names:
            _, task = self.running.pop(name)
            task.cancel()
        return names

    def describe(self) -> str:
        if not self.running:
            return "No timers are running."
        parts = []
        for label, (ends, _) in sorted(self.running.items(), key=lambda kv: kv[1][0]):
            left = max(0, round(ends - time.time()))
            m, s = divmod(left, 60)
            h, m = divmod(m, 60)
            span = (f"{h} h " if h else "") + (f"{m} min " if m else "") + f"{s} s"
            # A clock time only for long timers: "ends at 11:25" when it is 11:25
            # reads as already finished.
            at = f" (ends at {_clock(now() + timedelta(seconds=left))})" if left >= 600 else ""
            parts.append(f"'{label}' timer: {span} still left{at}")
        return "Running timers: " + "; ".join(parts) + "."


timers = Timers()


# ---------- alarms and reminders ----------

DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "invoke-bridge"
_DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_REPEATS = {"daily": list(range(7)), "weekdays": list(range(5)), "weekends": [5, 6]}
LATE_OK = 15 * 60  # still go off up to 15 min late (e.g. after a restart); older ones are dropped

# Set by the bridge: writes the spoken wake-up greeting with the language model.
briefing_writer: Callable[[str], Awaitable[str]] | None = None


def _days(spec: str) -> list[int]:
    """'once' -> [], 'daily'/'weekdays'/'weekends', or 'mon,wed,fri'."""
    spec = spec.strip().lower()
    if spec in _REPEATS:
        return _REPEATS[spec]
    return sorted({_DAY_NAMES.index(d[:3]) for d in re.split(r"[\s,]+", spec) if d[:3] in _DAY_NAMES})


def _days_text(days: list[int]) -> str:
    for name, ds in _REPEATS.items():
        if days == ds:
            return name
    return ", ".join(_DAY_NAMES[d].title() for d in days) if days else "once"


def _when(dt: datetime) -> str:
    """'today at 6:30 AM', 'tomorrow at …', 'Friday at …'."""
    today = now().date()
    if dt.date() == today:
        day = "today"
    elif dt.date() == today + timedelta(days=1):
        day = "tomorrow"
    elif dt.date() < today + timedelta(days=7):
        day = dt.strftime("%A")
    else:
        day = f"{dt.strftime('%A, %B')} {dt.day}"
    return f"{day} at {_clock(dt)}"


class Schedule:
    """Alarms (a time of day, once or repeating) and reminders (one moment)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.items: list[dict] = []
        self.counter = 0
        try:
            data = json.loads(path.read_text())
            self.items, self.counter = data.get("items", []), data.get("counter", 0)
        except (OSError, ValueError):
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"items": self.items, "counter": self.counter}, indent=1))
        tmp.replace(self.path)

    def _id(self, prefix: str) -> str:
        self.counter += 1
        return f"{prefix}{self.counter}"

    @staticmethod
    def _next_alarm(hhmm: str, days: list[int], after: datetime) -> datetime | None:
        h, m = (int(x) for x in hhmm.split(":"))
        for i in range(8):
            day = after + timedelta(days=i)
            at = day.replace(hour=h, minute=m, second=0, microsecond=0)
            if at > after and (not days or at.weekday() in days):
                return at
        return None

    def add_alarm(self, hhmm: str, days: str, music: str, label: str) -> dict | None:
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", hhmm.strip()):
            return None
        ds = _days(days or "once")
        at = self._next_alarm(hhmm.strip(), ds, now())
        if at is None:
            return None
        item = {"id": self._id("a"), "kind": "alarm", "time": hhmm.strip(), "days": ds,
                "music": music.strip(), "label": label.strip(), "next": at.timestamp()}
        self.items.append(item)
        self.save()
        return item

    def add_reminder(self, at_text: str, text: str) -> dict | None:
        try:
            at = datetime.strptime(at_text.strip(), "%Y-%m-%d %H:%M").replace(tzinfo=now().tzinfo)
        except ValueError:
            return None
        if not text.strip() or at.timestamp() < time.time() - 60:
            return None
        item = {"id": self._id("r"), "kind": "reminder", "text": text.strip(), "next": at.timestamp()}
        self.items.append(item)
        self.save()
        return item

    def cancel(self, ident: str = "", time_text: str = "", text: str = "", kind: str = "") -> list[str]:
        """Cancel by id, by alarm time ("06:30") or by reminder words."""
        ident, text = ident.strip().lower(), text.strip().lower()
        gone = [i for i in self.items if (not kind or i["kind"] == kind) and (
            (ident and i["id"] == ident)
            or (time_text and i.get("time") == time_text.strip())
            or (text and text in i.get("text", "").lower()))]
        self.items = [i for i in self.items if i not in gone]
        self.save()
        return [i["id"] for i in gone]

    def describe(self) -> str:
        if not self.items:
            return "No alarms or reminders are set."
        parts = []
        for i in sorted(self.items, key=lambda i: i["next"]):
            when = _when(datetime.fromtimestamp(i["next"], now().tzinfo))
            if i["kind"] == "alarm":
                extra = f", then music: {i['music']}" if i["music"] else ""
                label = f" '{i['label']}'" if i["label"] else ""
                parts.append(f"[{i['id']}] alarm{label} {_days_text(i['days'])}, next {when}{extra}")
            else:
                parts.append(f"[{i['id']}] reminder '{i['text']}' {when}")
        return "Alarms and reminders: " + "; ".join(parts) + "."

    async def _go_off(self, item: dict) -> None:
        if item["kind"] == "reminder":
            await alerts.put({"text": f"Reminder: {item['text']}.", "repeat": True})
            return
        text = default_briefing(item)
        if briefing_writer:
            try:
                written = await asyncio.wait_for(briefing_writer(briefing_prompt(item)), 20)
                text = written.strip() or text
            except Exception as e:
                print(f"alarm: using the plain greeting ({type(e).__name__}: {e})")
        await alerts.put({"text": text, "repeat": False, "music": item["music"]})

    async def run(self) -> None:
        """Check every 10 s for anything due; repeating alarms are rescheduled."""
        while True:
            t = time.time()
            due = [i for i in self.items if i["next"] <= t]
            for item in due:
                late = t - item["next"]
                if late <= LATE_OK:
                    asyncio.create_task(self._go_off(item))
                else:
                    print(f"schedule: skipped {item['id']}, {late / 60:.0f} min late")
                if item["kind"] == "alarm" and item["days"]:
                    at = self._next_alarm(item["time"], item["days"], now())
                    item["next"] = at.timestamp() if at else t + 7 * 86400
                else:
                    self.items.remove(item)
            if due:
                self.save()
            await asyncio.sleep(10)


schedule = Schedule(DATA_DIR / "schedule.json")


def default_briefing(item: dict) -> str:
    dt = now()
    label = f" Your {item['label']} alarm." if item.get("label") else ""
    forecast = _weather[1].split(" Tomorrow:")[0] if _weather else ""
    return (f"Good morning. It's {_clock(dt)} on {dt.strftime('%A, %B')} {dt.day}.{label} "
            + (f"Weather: {forecast}" if forecast else "")).strip()


def briefing_prompt(item: dict) -> str:
    dt = now()
    label = f" The alarm is labelled '{item['label']}'." if item.get("label") else ""
    music = " Music starts right after you finish." if item.get("music") else ""
    weather_part = (f" Weather: {_weather[1]} Mention today's high, low and chance of rain."
                    if _weather else " Don't mention the weather.")
    return (f"Your alarm is going off. It is {_clock(dt)} on {dt.strftime('%A, %B')} {dt.day}."
            f"{label}{music}{weather_part} Say a short, cheerful greeting for the time of day in two "
            "or three spoken sentences: the time, the weather if given, and one light encouraging "
            "line in your own voice. No tags.")


# ---------- what the model is told, and the tags it can use ----------

MODEL_HELP = (
    "You can set kitchen-style timers. To start one, end your reply with "
    '<timer action="start" seconds="600" label="pasta"/> (label optional); to cancel one, '
    '<timer action="cancel" label="pasta"/> (no label cancels the newest). A timer announces itself '
    "out loud when it ends. "
    "You can set alarms for a time of day: "
    '<alarm time="06:30" days="once" music="what to play" label="gym"/> where time is 24-hour '
    'local time, days is "once", "daily", "weekdays", "weekends" or days like "mon,wed,fri", and '
    "music (optional) is something to play after the wake-up greeting. When an alarm goes off you "
    "greet the household with the time and weather, then the music starts. "
    'You can set reminders: <reminder at="YYYY-MM-DD HH:MM" text="call Mom"/> in local time; work '
    "out the date from today's date. A reminder is spoken at that time. "
    'Cancel with <alarm action="cancel" id="a1"/> or <reminder action="cancel" id="r2"/> using the '
    "ids listed below. Use the listed alarms and reminders to answer questions about them, and "
    "confirm what you set in plain words (\"Alarm set for 6:30 tomorrow\"). You cannot read "
    "calendars. Tags are never read aloud."
)


async def context(http: httpx.AsyncClient) -> str:
    """Facts for the system prompt: the time, timers, alarms and the forecast."""
    dt = now()
    where = f" in {_where['name']}" if _where else ""
    parts = [f"It is {dt.strftime('%A, %B')} {dt.day}, {dt.year}, {_clock(dt)}{where}.",
             timers.describe(), schedule.describe(), MODEL_HELP]
    forecast = await weather(http)
    parts.append(f"Weather{where}: {forecast}" if forecast
                 else "You don't have weather information; say so if asked.")
    return " ".join(parts)


_TAG = re.compile(r"<(timer|alarm|reminder)\b([^>]*?)/?>(?:\s*</\1>)?", re.I)


def run_tags(reply: str) -> str:
    """Set or cancel the timers, alarms and reminders the model asked for;
    return the reply without tags."""
    for kind, attrs in _TAG.findall(reply):
        kind = kind.lower()
        a = {k.lower(): v.strip() for k, v in re.findall(r'(\w+)\s*=\s*"([^"]*)"', attrs)}
        cancel = a.get("action", "").lower() == "cancel"
        if kind == "timer":
            if cancel:
                timers.cancel(a.get("label", ""))
            elif a.get("seconds", "").isdigit() and 0 < int(a["seconds"]) <= 24 * 3600:
                timers.start(int(a["seconds"]), a.get("label", ""))
        elif cancel:
            schedule.cancel(a.get("id", ""), a.get("time", ""), a.get("text", ""), kind)
        elif kind == "alarm":
            schedule.add_alarm(a.get("time", ""), a.get("days", "once"), a.get("music", ""), a.get("label", ""))
        else:
            schedule.add_reminder(a.get("at", ""), a.get("text", ""))
    return _TAG.sub("", reply).strip()
