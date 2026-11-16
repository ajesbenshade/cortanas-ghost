"""Clock, timers and weather for Cortana.

The language model gets the current time, the running timers and a short
forecast in its system prompt (see context()), and starts or cancels timers
by ending its reply with a tag, the same way it controls music:

    <timer action="start" seconds="600" label="pasta"/>
    <timer action="cancel" label="pasta"/>

When a timer ends, its announcement is queued in `alerts`; the speaker link
collects it from the bridge's GET /v1/alerts and speaks it.

Settings (environment variables):
    CORTANA_PLACE     town for the weather and the time zone, e.g. "Springfield, IL"
    CORTANA_UNITS     "fahrenheit" (default) or "celsius"
    CORTANA_TZ        time zone without a place, e.g. "America/Chicago"
Weather comes from Open-Meteo (open-meteo.com), which needs no account; only
the town's coordinates are sent to it.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timedelta
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
        await alerts.put(f"{name} is done.")

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


# ---------- what the model is told, and the tags it can use ----------

MODEL_HELP = (
    "You can set kitchen-style timers. To start one, end your reply with "
    '<timer action="start" seconds="600" label="pasta"/> (label optional); to cancel one, '
    '<timer action="cancel" label="pasta"/> (no label cancels the newest). A timer announces itself '
    "out loud when it ends. You still cannot set alarms for a time of day, reminders or calendar events. "
    "Tags are never read aloud."
)


async def context(http: httpx.AsyncClient) -> str:
    """Facts for the system prompt: the time, timers and the forecast."""
    dt = now()
    where = f" in {_where['name']}" if _where else ""
    parts = [f"It is {dt.strftime('%A, %B')} {dt.day}, {dt.year}, {_clock(dt)}{where}.",
             timers.describe(), MODEL_HELP]
    forecast = await weather(http)
    parts.append(f"Weather{where}: {forecast}" if forecast
                 else "You don't have weather information; say so if asked.")
    return " ".join(parts)


_TAG = re.compile(r"<timer\b([^>]*?)/?>(?:\s*</timer>)?", re.I)


def run_tags(reply: str) -> str:
    """Start or cancel the timers the model asked for; return the reply without tags."""
    for attrs in _TAG.findall(reply):
        a = {k.lower(): v.strip() for k, v in re.findall(r'(\w+)\s*=\s*"([^"]*)"', attrs)}
        if a.get("action", "start").lower() == "cancel":
            timers.cancel(a.get("label", ""))
        elif a.get("seconds", "").isdigit() and 0 < int(a["seconds"]) <= 24 * 3600:
            timers.start(int(a["seconds"]), a.get("label", ""))
    return _TAG.sub("", reply).strip()
