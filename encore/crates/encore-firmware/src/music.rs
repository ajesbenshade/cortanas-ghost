//! Music source — Apple Music or AirPlay streamed in over Wi-Fi by a
//! Cortana's Ghost server (see `netaudio.rs` for the audio itself).
//!
//! The server reports what is playing with `POST /api/music/status`, and picks
//! up the dashboard's transport buttons with `GET /api/music/command` (a long
//! poll). The speaker adds what it knows itself — whether audio is arriving and
//! the Music volume — and broadcasts `ServerMsg::MusicStatus` to dashboards.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use axum::http::StatusCode;
use axum::Json;
use encore_common::protocol::{MusicStatus, ServerMsg, SourceId};
use serde_json::{json, Value};
use tokio::sync::{broadcast, Notify};

/// Music source volume, 0-100, applied to every sample by `netaudio`.
pub static VOLUME: AtomicU8 = AtomicU8::new(100);

static STATUS: Mutex<Option<MusicStatus>> = Mutex::new(None);
static COMMANDS: Mutex<VecDeque<String>> = Mutex::new(VecDeque::new());
static COMMAND_READY: OnceLock<Notify> = OnceLock::new();
static WS_TX: OnceLock<broadcast::Sender<String>> = OnceLock::new();

/// Accepted transport actions (anything else from a client is dropped).
const ACTIONS: &[&str] = &["play", "pause", "next", "previous"];

pub fn set_ws_tx(tx: broadcast::Sender<String>) {
    let _ = WS_TX.set(tx);
}

fn notify() -> &'static Notify {
    COMMAND_READY.get_or_init(Notify::new)
}

/// Current status with the speaker's own fields filled in.
pub fn current() -> MusicStatus {
    let mut s = STATUS.lock().map(|g| g.clone()).unwrap_or_default().unwrap_or_default();
    s.volume = VOLUME.load(Ordering::Relaxed);
    s
}

fn broadcast() {
    if let Some(tx) = WS_TX.get() {
        if let Ok(json) = serde_json::to_string(&ServerMsg::MusicStatus(current())) {
            let _ = tx.send(json);
        }
    }
}

fn update(f: impl FnOnce(&mut MusicStatus)) {
    if let Ok(mut g) = STATUS.lock() {
        f(g.get_or_insert_with(MusicStatus::default));
    }
    broadcast();
}

/// Called by `netaudio` when audio starts or stops arriving.
pub fn set_streaming(streaming: bool) {
    update(|s| s.streaming = streaming);
}

/// `ClientMsg::SetVolume { source: Music, .. }`.
pub fn set_volume(level: u8) {
    VOLUME.store(level.min(100), Ordering::Relaxed);
    broadcast();
    if let Some(tx) = WS_TX.get() {
        let msg = ServerMsg::VolumeChanged {
            source: SourceId::Music,
            level: level.min(100),
        };
        if let Ok(json) = serde_json::to_string(&msg) {
            let _ = tx.send(json);
        }
    }
}

/// `ClientMsg::MusicControl`: queue a transport action for the server.
pub fn push_command(action: &str) {
    if !ACTIONS.contains(&action) {
        return;
    }
    if let Ok(mut q) = COMMANDS.lock() {
        q.push_back(action.to_string());
        while q.len() > 8 {
            q.pop_front(); // nobody is collecting them: keep only the latest
        }
    }
    notify().notify_one();
}

fn pop_command() -> Option<String> {
    COMMANDS.lock().ok().and_then(|mut q| q.pop_front())
}

/// POST /api/music/status — the server's view of what is playing.
pub async fn status_handler(Json(mut incoming): Json<MusicStatus>) -> StatusCode {
    update(|s| {
        incoming.streaming = s.streaming; // the speaker knows this itself
        *s = incoming;
    });
    StatusCode::NO_CONTENT
}

/// GET /api/music/status — current Music status (for the server or a script).
pub async fn get_status_handler() -> Json<MusicStatus> {
    Json(current())
}

/// GET /api/music/command — long poll: `{"action": "next"}` when a dashboard
/// button is pressed, or `{"action": null}` after 25 s with nothing to do.
pub async fn command_handler() -> Json<Value> {
    if let Some(action) = pop_command() {
        return Json(json!({ "action": action }));
    }
    let _ = tokio::time::timeout(Duration::from_secs(25), notify().notified()).await;
    Json(json!({ "action": pop_command() }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn only_known_actions_are_queued() {
        push_command("dance");
        push_command("next");
        assert_eq!(pop_command().as_deref(), Some("next"));
        assert_eq!(pop_command(), None);
    }

    #[test]
    fn volume_is_reported_and_clamped() {
        set_volume(250);
        assert_eq!(current().volume, 100);
        set_volume(40);
        assert_eq!(current().volume, 40);
        VOLUME.store(100, Ordering::Relaxed);
    }
}
