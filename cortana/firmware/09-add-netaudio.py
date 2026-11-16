#!/usr/bin/env python3
"""Add the network audio input (netaudio.rs) to Encore and wire it into main.rs."""
import pathlib
import shutil
import sys

here = pathlib.Path(sys.argv[1])
src = pathlib.Path.home() / "invoke/Encore/encore/crates/encore-firmware/src"
shutil.copy(here / "netaudio.rs", src / "netaudio.rs")

main = src / "main.rs"
text = main.read_text()
if "netaudio::NetAudioSubsystem" in text:
    print("main.rs already wired")
    sys.exit(0)

edits = [
    ("mod network;\n", "mod netaudio;\nmod network;\n"),
    (
        "        let network_slot = audio.add_source().slot; // group follower writes here\n",
        "        let network_slot = audio.add_source().slot; // group follower writes here\n"
        "        let netaudio_slot = audio.add_source().slot; // music sent over Wi-Fi (port 10710)\n",
    ),
    (
        "        // Voice session task — wake word detection + session state machine\n",
        "        // ── Network audio input (music over Wi-Fi) ──\n"
        "        mgr.start(Box::new(netaudio::NetAudioSubsystem::new(netaudio_slot, None)));\n\n"
        "        // Voice session task — wake word detection + session state machine\n",
    ),
]
for old, new in edits:
    if text.count(old) != 1:
        sys.exit(f"expected one match in main.rs, found {text.count(old)}:\n{old}")
    text = text.replace(old, new)
main.write_text(text)
print("added netaudio and wired it into main.rs")
