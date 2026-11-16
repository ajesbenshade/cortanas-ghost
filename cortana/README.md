# Cortana's Ghost: the server side

Everything that turns an Invoke running this firmware back into a talking
Cortana. It runs on a Linux machine on the same network as the speaker (an
Ubuntu server here) and talks to the speaker over Wi-Fi.

```
 "Hey Cortana ..."
  Invoke speaker (this repo's firmware)
   ├─ TCP 10700  Wyoming: mic audio out, reply audio in
   ├─ TCP 10710  network audio input: music in
   └─ HTTPS 443  dashboard / API, WebSocket (ring color)
        ▲  Wi-Fi
  Server
   ├─ bridge/satellite.py   speaker link: wake word, conversations,
   │                        interrupting, timer alerts, blue listening ring
   ├─ bridge/server.py      http://127.0.0.1:8765
   │    ├─ faster-whisper (speech-to-text; GPU optional)
   │    ├─ LLM: GitHub Copilot CLI (ACP), or any OpenAI-compatible server
   │    ├─ voice: Microsoft Edge neural voice, Piper offline fallback
   │    ├─ skills.py: clock, timers, weather (Open-Meteo)
   │    └─ music.py: Apple Music commands -> player page
   ├─ Chrome on Xvfb: /music, the MusicKit player  ┐
   ├─ shairport-sync: AirPlay receiver "Cortana"   ├─> PipeWire sink "cortana_music"
   └─ bridge/streamer.py: records that sink ───────┘   -> speaker port 10710
```

| Folder | What it is |
|---|---|
| `bridge/` | The voice bridge, speaker link, music and skills (Python) |
| `airplay/airplay.conf` | shairport-sync config for the AirPlay receiver |
| `deploy/` | systemd service templates, PipeWire sink, `install.sh` |
| `firmware/` | Encore patch scripts and build helpers (the patches are already committed in this repo; the scripts re-apply them to a fresh Encore checkout) |

## What Cortana can do

- **Talk.** "Hey Cortana, …" in one breath, or "Hey Cortana", wait for the blue
  pulse, then ask. Follow-up questions work without the wake word for 15 s;
  "that's all" ends the conversation. The ring lights as soon as the name is
  heard, while you are still talking.
- **Be interrupted.** "Cortana, stop" or a new question cuts a reply short
  (needs the `listen-while-speaking` firmware patch).
- **Clock, timers, weather.** "Set a ten minute timer for the pasta",
  "how long is left?", "what's the weather tomorrow?". Timers announce
  themselves on the speaker.
- **Apple Music by voice.** "Play Abbey Road", "play that one", "pause",
  "next", "turn it down", "what's playing".
- **AirPlay.** Pick "Cortana" from the AirPlay menu on an iPhone, iPad or Mac.
  The music is lowered while Cortana listens and talks.
- **Music on the dashboard.** Apple Music and AirPlay show up as a "Music"
  source on the speaker's dashboard, with cover art, play/pause/next/previous
  and their own volume slider (needs this repo's firmware).

About 2.5 to 3.5 s from the end of a question to the start of the answer with
a GPU for speech-to-text and Claude Haiku through Copilot.

## Setup on Ubuntu

1. **Firmware.** Flash Encore to the speaker as its README describes, then
   install this repo's build over Wi-Fi:
   `make encore` and `curl -k --data-binary @build/encore https://<speaker-ip>/api/update`.
   `firmware/12-build-encore-server.sh` builds just that program on Linux
   (toolchain: `firmware/02-tools.sh`).
2. **Packages.**
   ```bash
   sudo apt install python3-venv pulseaudio-utils libportaudio2 nodejs npm xvfb \
       pipewire pipewire-pulse wireplumber avahi-daemon shairport-sync
   ```
   plus `google-chrome-stable` from Google's apt repository (Apple Music needs
   Chrome's Widevine; Chromium will not play it).
3. **Python environment.**
   ```bash
   python3 -m venv ~/invoke-bridge/venv
   ~/invoke-bridge/venv/bin/pip install -r cortana/bridge/requirements.txt
   ~/invoke-bridge/venv/bin/python -m piper.download_voices en_US-amy-medium --data-dir ~/invoke-bridge/voices
   echo "edge:en-US-AvaNeural" > ~/invoke-bridge/voice.txt
   echo <speaker-ip> > ~/invoke-bridge/speaker.txt
   ```
4. **Language model.** `sudo npm install -g @github/copilot`, then
   `copilot login --device-code`. On a headless server, first put
   `{"storeTokenPlaintext": true}` in `~/.copilot/settings.json` or the login
   will not be saved. Or point `BRIDGE_LLM` at a local OpenAI-compatible server.
5. **Apple Music** (optional). Make a MusicKit key at developer.apple.com and
   put `AuthKey_<KEYID>.p8` and `team.txt` (your Team ID) in
   `~/invoke-bridge/apple/`. Never commit them.
6. **Services.** `cortana/deploy/install.sh` (add `--gpu` for an NVIDIA GPU).
   Set `CORTANA_PLACE` or `CORTANA_TZ` in `cortana-bridge.service`.
7. **Sign in to Apple Music once** on the server's virtual screen: run
   `x11vnc -display :99 -localhost`, tunnel it with
   `ssh -L 5901:localhost:5900 <server>`, open `vnc://localhost:5901`, click
   Start on the page and sign in.

## Settings

Environment variables, set in the service files:

| Variable | Default | |
|---|---|---|
| `BRIDGE_LLM` | local llama-server, then Ollama | `url\|model;…`; `copilot\|<model>` uses Copilot CLI |
| `BRIDGE_STT_MODEL` / `_DEVICE` | `base.en` / `cpu` | `medium.en` on `cuda` with a GPU |
| `BRIDGE_EDGE_TIMEOUT` | 2.5 s | wait for the online voice before using Piper |
| `CORTANA_PLACE` / `CORTANA_TZ` | none | "City, ST" for weather and time zone, or a time zone |
| `LISTEN_COLOR` | `0,90,255` | ring color while listening; empty for the stock animation |
| `BARGE_IN` | `1` | interrupting replies |
| `BRIDGE_TOKEN` | none | required before exposing the bridge beyond localhost |

## Privacy

- Your questions go to GitHub Copilot (or your own model), and replies go to
  Microsoft to be spoken unless you use the Piper voice.
- Weather sends only the town's coordinates to Open-Meteo.
- Speech is transcribed locally. The speaker link never logs what it hears
  unless it was said to Cortana.

## Security

- Encore's root password is the same on every speaker. Change it.
- The speaker's dashboard and API have no login. Keep the speaker on a network
  you trust.
- When the speaker loses Wi-Fi it may briefly start an open setup hotspot.

## Credits and trademarks

Built on [Encore](https://github.com/ScottNorton/Encore) by Scott Norton
(GPL-3.0). Cortana and Halo are trademarks of Microsoft; this project is not
affiliated with or endorsed by Microsoft or Harman Kardon. Rajdhani is by the
Indian Type Foundry under the SIL Open Font License.
