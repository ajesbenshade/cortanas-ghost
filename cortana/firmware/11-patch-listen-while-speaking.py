#!/usr/bin/env python3
"""Patch Encore's Wyoming satellite so the mic can keep streaming while it speaks.

Stock Encore discards every mic frame between audio-start and audio-stop, so a
client can never hear "Cortana, stop" during a reply. This adds a per-session
setting, off by default (other clients such as Home Assistant see no change):

    {"type": "listen-while-speaking", "data": {"enabled": true}}

While it is on, mic audio is sent during playback too. The client must then tell
its own voice apart from the user's (the speaker has no echo cancelling).
Run after 07-patch-wyoming.py; safe to run twice.
"""
import pathlib
import sys

path = pathlib.Path.home() / "invoke/Encore/encore/crates/encore-firmware/src/wyoming/mod.rs"
src = path.read_text()

if "listen_while_speaking" in src:
    print("already patched")
    sys.exit(0)

EDITS = [
    ("    muted: bool,\n",
     "    muted: bool,\n"
     "    /// Keep streaming the mic during TTS (client event `listen-while-speaking`),\n"
     "    /// so the client can hear the user interrupt a reply.\n"
     "    listen_while_speaking: bool,\n"),
    ("            muted: false,\n",
     "            muted: false,\n"
     "            listen_while_speaking: false,\n"),
    ("Some(samples) if !self.muted => {",
     "Some(samples) if !self.muted || self.listen_while_speaking => {"),
    ('            "describe" => {\n                self.send_info(writer).await?;',
     '            "listen-while-speaking" => {\n'
     '                self.listen_while_speaking = event\n'
     '                    .data\n'
     '                    .get("enabled")\n'
     '                    .and_then(|v| v.as_bool())\n'
     '                    .unwrap_or(false);\n'
     '                info!(\n'
     '                    "Wyoming: listen while speaking {}",\n'
     '                    if self.listen_while_speaking { "on" } else { "off" }\n'
     '                );\n'
     '            }\n'
     '\n'
     '            "describe" => {\n                self.send_info(writer).await?;'),
]

for old, new in EDITS:
    if src.count(old) != 1:
        sys.exit(f"patch anchor not found exactly once: {old.strip()[:60]!r}")
    src = src.replace(old, new)

path.write_text(src)
print("patched", path)
