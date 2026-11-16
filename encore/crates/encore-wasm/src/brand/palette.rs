//! Brand palette — single source of truth for every gold value.
//!
//! Every other place that needs a gold color references a constant from this
//! module, either directly in Rust (e.g. SVG template strings) or indirectly
//! via CSS custom properties emitted by `brand::css::css()`.

// ── Accent (primary gold) ──
// Center Stage gold. Dark accent #e6b34d; light (matinee) accent #74561b is the
// AA-safe dark gold (5.0:1 even on the darkest light surface), used for any gold
// text/icon/focus/thin-stroke in light mode.
pub const ACCENT: &str = "#4fc8ff";
pub const ACCENT_LIGHT: &str = "#0b5f8a";
pub const ACCENT_HOVER: &str = "#8fdcff";
pub const ACCENT_HOVER_LIGHT: &str = "#0e78ad";

// ── Ring gradient endpoint ──
pub const RING_CREAM: &str = "#c9f1ff";
pub const RING_CREAM_LIGHT: &str = "#7cc7ea";

// ── Speaker dome (LED stand-in) — brightest gold, glow/highlight only ──
pub const DOME: &str = "#7fe3ff";
// Light (matinee) brightest gold — glow-only, used for the light data-theme override.
pub const DOME_LIGHT: &str = "#1592c9";

// ── RGB triplets for rgba() composition via CSS vars ──
pub const ACCENT_R: &str = "79";
pub const ACCENT_G: &str = "200";
pub const ACCENT_B: &str = "255";
pub const ACCENT_LIGHT_R: &str = "11";
pub const ACCENT_LIGHT_G: &str = "95";
pub const ACCENT_LIGHT_B: &str = "138";

// ── LED-reactive dome fallbacks (fixes off-brand GitHub-blue fallbacks) ──
pub const LED_VOLUME: &str = "#4fc8ff"; // same as DOME — volume arc uses dome gold (was "#58a6ff")
pub const LED_BOOT: &str = "#c9f1ff"; // was "#58a6ff"
pub const LED_SAFE: &str = "#f85149"; // unchanged (matches --red in style.rs)
