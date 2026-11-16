#!/usr/bin/env python3
"""Rebrand Encore's web dashboard as Cortana's Ghost, with a Cortana theme.
Run on a clean checkout (it edits the stock text); running it twice is harmless.

  * palette.rs: the gold accent constants become holographic blue
  * brand/cortana.rs + cortana.css: an override stylesheet layered last
    (navy surfaces, hex grid, scan lines, corner-bracket cards, glow)
  * web/fonts: Rajdhani (SIL OFL), subset to Latin, for headings
  * index.html / manifest.json: loading screen colors and the name
  * the name "Cortana's Ghost" in the logo, About page and setup, crediting
    Encore; the update checker looks at this project's releases

Usage: 13-cortana-theme.py <invoke-flash dir> <dir with Rajdhani-{Medium,SemiBold,Bold}.ttf>
"""
import pathlib
import re
import shutil
import subprocess
import sys

here, fonts_in = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
root = pathlib.Path.home() / "invoke/Encore/encore"
wasm = root / "crates/encore-wasm/src"
web = root / "web"


def edit(path: pathlib.Path, pairs, regex=False):
    text = path.read_text()
    for old, new in pairs:
        if regex:
            text, n = re.subn(old, new, text)
        else:
            n = text.count(old)
            text = text.replace(old, new)
        if n == 0 and new not in text:
            sys.exit(f"{path.name}: nothing matched {old!r}")
    path.write_text(text)


# ── Palette: gold -> holographic blue ──
BLUE = {
    "ACCENT": "#4fc8ff", "ACCENT_LIGHT": "#0b5f8a",
    "ACCENT_HOVER": "#8fdcff", "ACCENT_HOVER_LIGHT": "#0e78ad",
    "RING_CREAM": "#c9f1ff", "RING_CREAM_LIGHT": "#7cc7ea",
    "DOME": "#7fe3ff", "DOME_LIGHT": "#1592c9",
    "ACCENT_R": "79", "ACCENT_G": "200", "ACCENT_B": "255",
    "ACCENT_LIGHT_R": "11", "ACCENT_LIGHT_G": "95", "ACCENT_LIGHT_B": "138",
    "LED_VOLUME": "#4fc8ff", "LED_BOOT": "#c9f1ff",
}
edit(wasm / "brand/palette.rs",
     [(rf'(pub const {name}: &str = )"[^"]*";', rf'\g<1>"{value}";') for name, value in BLUE.items()],
     regex=True)

# ── Canvas graphics (ring, rays, volume arc) hard-code their RGB: gold -> blue.
# Their tests compare the same literals, so both change together.
CANVAS = {
    (230, 179, 77): (79, 200, 255),     # accent
    (255, 206, 106): (127, 227, 255),   # glow
    (240, 194, 103): (143, 220, 255),   # accent hover
    (201, 154, 58): (42, 169, 224),     # accent active
    (199, 154, 58): (21, 146, 201),     # light glow
    (116, 86, 27): (11, 95, 138),       # light accent
    (176, 134, 58): (14, 120, 173),
    (146, 108, 40): (8, 80, 111),
    (199, 165, 108): (124, 199, 234),
    (242, 233, 218): (227, 246, 255),   # cream text
    (244, 236, 221): (234, 244, 250),
    (20, 17, 13): (5, 11, 20),          # dark stage
    (36, 29, 16): (10, 22, 38),
    (60, 42, 16): (10, 60, 90),         # light outline
    (154, 142, 114): (127, 179, 209),
    (122, 108, 76): (61, 102, 128),
}
for rs in sorted((wasm / "graphics").glob("*.rs")):
    text = rs.read_text()
    for (r, g, b), (nr, ng, nb) in CANVAS.items():
        text = re.sub(rf"\b{r}(,\s*){g}(,\s*){b}\b", rf"{nr}\g<1>{ng}\g<2>{nb}", text)
    rs.write_text(text)

# ── Override stylesheet, injected after everything else ──
shutil.copy(here / "cortana-theme.css", wasm / "brand/cortana.css")
(wasm / "brand/cortana.rs").write_text(
    "//! Cortana theme (invoke-flash/13-cortana-theme.py): blue holographic overrides\n"
    "//! and effects, injected after every other stylesheet.\n\n"
    'pub const CSS: &str = include_str!("cortana.css");\n')
mod_rs = wasm / "brand/mod.rs"
if "pub mod cortana;" not in mod_rs.read_text():
    edit(mod_rs, [("pub mod css;", "pub mod cortana;\npub mod css;")])
edit(mod_rs, [('dom::set_text(&brand, "Encore");', 'dom::set_text(&brand, "Cortana\'s Ghost");'),
              ('dom::set_text(&text, "ENCORE");', 'dom::set_text(&text, "CORTANA\'S GHOST");')])

# ── Name: Cortana's Ghost, built on Encore (credited, as the GPL requires) ──
edit(wasm / "app.rs", [
    ('if name.eq_ignore_ascii_case("encore") || name.is_empty() {',
     'if name.eq_ignore_ascii_case("encore") || name.eq_ignore_ascii_case("cortana\'s ghost") || name.is_empty() {'),
])
edit(wasm / "panels/about.rs", [
    ('"Encore v{}', '"Cortana\'s Ghost · Encore v{}'),
    ('Some("Harman Kardon Invoke Community Firmware")',
     'Some("Cortana\'s Ghost: the Harman Kardon Invoke as Cortana again")'),
    ('Some("Open-source firmware for the Harman Kardon Invoke. Started as a personal project on spare hardware, and built to be shared and learned from.")',
     'Some("Built on Encore, the open-source community firmware for the Harman Kardon Invoke by Scott Norton (github.com/ScottNorton/Encore), with a local voice bridge, Apple Music and AirPlay.")'),
    ('"https://github.com/ScottNorton/Encore"', '"https://github.com/ajesbenshade/cortanas-ghost"'),
    ('"github.com/ScottNorton/Encore"', '"github.com/ajesbenshade/cortanas-ghost"'),
])
# Offer this project's releases, not upstream ones (which lack its patches).
edit(wasm / "panels/update.rs", [
    ("https://api.github.com/repos/ScottNorton/Encore/releases",
     "https://api.github.com/repos/ajesbenshade/cortanas-ghost/releases"),
])
edit(wasm / "pages/setup.rs", [
    ('dom::set_attr(&input, "value", "Encore");', 'dom::set_attr(&input, "value", "Cortana");'),
    ('        "Encore".to_string()', '        "Cortana".to_string()'),
])
edit(wasm / "pages/connect.rs", [
    ('found.push(("Encore".to_string(), "192.168.43.1".to_string()));',
     'found.push(("Cortana\'s Ghost".to_string(), "192.168.43.1".to_string()));'),
])
style = wasm / "style.rs"
if "brand::cortana::CSS" not in style.read_text():
    edit(style, [
        ('        "{}\\n{}\\n{}\\n{}",\n', '        "{}\\n{}\\n{}\\n{}\\n{}",\n'),
        ("        crate::components::async_state::css()\n",
         "        crate::components::async_state::css(),\n        crate::brand::cortana::CSS\n"),
    ])

# ── Font: Rajdhani, Latin only, as woff2 ──
for weight, name in ((500, "Medium"), (600, "SemiBold"), (700, "Bold")):
    out = web / "fonts" / f"Rajdhani-{weight}.woff2"
    subprocess.run(["pyftsubset", str(fonts_in / f"Rajdhani-{name}.ttf"), "--flavor=woff2",
                    "--unicodes=U+0000-00FF,U+2013-2014,U+2018-201D,U+2022,U+2026,U+20AC",
                    f"--output-file={out}"], check=True)
shutil.copy(fonts_in / "OFL-Rajdhani.txt", web / "fonts" / "OFL-Rajdhani.txt")

# ── Loading screen (shown before the app loads) and app name ──
edit(web / "index.html", [
    ("<title>Encore</title>", "<title>Cortana's Ghost</title>"),
    (".brand{font-size:28px;font-weight:300;letter-spacing:6px;",
     ".brand{font-size:22px;font-weight:300;letter-spacing:5px;"),
    ('content="#0a0a0a"', 'content="#050b14"'),
    ('content="#F5F2ED"', 'content="#eaf4fa"'),
    ("body{background:#0a0a0a;color:#e8e6e3;", "body{background:#050b14;color:#e3f6ff;"),
    ("body{background:#F5F2ED;color:#1a1815}", "body{background:#eaf4fa;color:#071a26}"),
    ("rgba(200,165,92,.12)", "rgba(79,200,255,.22)"),
    ("rgba(160,134,74,.08)", "rgba(11,95,138,.10)"),
    ('stop-color="#C8A55C"', 'stop-color="#2a8cff"'),
    ('stop-color="#F0E6D0"', 'stop-color="#c9f1ff"'),
    ('fill="#e8b040"', 'fill="#7fe3ff"'),
    ("color:#4a4540;", "color:#3e6a86;"),
    ("color:#8c8580}", "color:#3d6680}"),
    ('<div class="brand">Encore</div>', '<div class="brand">Cortana\'s Ghost</div>'),
])
edit(web / "manifest.json", [
    ('"name": "Encore"', '"name": "Cortana\'s Ghost"'),
    ('"short_name": "Encore"', '"short_name": "Cortana"'),
    ('"description": "Encore Smart Speaker Dashboard"', '"description": "Cortana\'s Ghost speaker dashboard"'),
    ('"background_color": "#0d1117"', '"background_color": "#050b14"'),
    ('"theme_color": "#0d1117"', '"theme_color": "#050b14"'),
])
print("Cortana theme applied")
