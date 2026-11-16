//! Network audio input: plays raw PCM sent over TCP (port 10710).
//!
//! Lets a computer on the same network play music through the speaker over
//! Wi-Fi, the way it would over Bluetooth. Wire format: a continuous stream of
//! 48 kHz, 2-channel, signed 16-bit little-endian samples, interleaved. No
//! header. One sender at a time; a new connection replaces the old one.
//!
//! The audio goes into its own mixer slot, so the speaker's voice replies duck
//! it automatically (the Wyoming slot is the voice slot) and the microphones
//! keep streaming while music plays.
#![cfg(target_os = "linux")]

use crate::audio::mixer::MixerSlot;
use crate::subsystem::{Subsystem, SubsystemContext};
use anyhow::{Context, Result};
use encore_common::protocol::SubsystemState;
use std::sync::Arc;
use std::time::Duration;
use tokio::io::AsyncReadExt;
use tracing::{info, warn};

pub const DEFAULT_PORT: u16 = 10710;
/// Samples (not frames) buffered before playback starts: 300 ms of stereo 48 kHz.
const PREBUFFER: usize = 48_000 * 2 * 3 / 10;
/// With no data for this long the input goes idle, so the amplifier can sleep.
const IDLE_AFTER: Duration = Duration::from_secs(2);

pub struct NetAudioSubsystem {
    port: u16,
    slot: Arc<MixerSlot>,
}

impl NetAudioSubsystem {
    pub fn new(slot: Arc<MixerSlot>, port: Option<u16>) -> Self {
        Self { port: port.unwrap_or(DEFAULT_PORT), slot }
    }

    /// Serve one sender until it disconnects. Returns Ok(true) on shutdown.
    async fn serve(
        &self,
        mut stream: tokio::net::TcpStream,
        ctx: &mut SubsystemContext,
    ) -> Result<bool> {
        stream.set_nodelay(true).ok();
        self.slot.clear();
        let mut buf = vec![0u8; 16 * 1024];
        let mut carry: Vec<u8> = Vec::with_capacity(4);
        let mut playing = false;
        loop {
            let read = tokio::select! {
                r = tokio::time::timeout(IDLE_AFTER, stream.read(&mut buf)) => r,
                _ = ctx.shutdown.recv() => return Ok(true),
            };
            let n = match read {
                Err(_) => {
                    // Sender is quiet (music paused): let the buffer finish, go idle.
                    if playing {
                        self.drain().await;
                        playing = false;
                        info!("NetAudio: idle");
                    }
                    continue;
                }
                Ok(Ok(0)) => break,
                Ok(Ok(n)) => n,
                Ok(Err(e)) => {
                    warn!("NetAudio: read error: {}", e);
                    break;
                }
            };
            carry.extend_from_slice(&buf[..n]);
            let usable = carry.len() / 4 * 4; // whole stereo frames only
            let samples: Vec<i32> = carry[..usable]
                .chunks_exact(2)
                .map(|c| (i16::from_le_bytes([c[0], c[1]]) as i32) << 16)
                .collect();
            carry.drain(..usable);

            let mut off = 0;
            while off < samples.len() {
                off += self.slot.push(&samples[off..]);
                if !playing && self.slot.available() >= PREBUFFER {
                    self.slot.set_active(true);
                    playing = true;
                    info!("NetAudio: playing");
                }
                if off < samples.len() {
                    // Ring full: the sender is ahead of real time. Wait for room
                    // (this is what paces a sender that sends faster than playback).
                    if !playing {
                        self.slot.set_active(true);
                        playing = true;
                    }
                    tokio::time::sleep(Duration::from_millis(10)).await;
                }
            }
        }
        if playing {
            self.drain().await;
        }
        Ok(false)
    }

    /// Let buffered audio play out (up to 3 s), then mark the input inactive.
    async fn drain(&self) {
        for _ in 0..150 {
            if self.slot.available() == 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(20)).await;
        }
        self.slot.set_active(false);
        self.slot.clear();
    }
}

#[async_trait::async_trait]
impl Subsystem for NetAudioSubsystem {
    fn name(&self) -> &'static str {
        "netaudio"
    }

    async fn run(&mut self, mut ctx: SubsystemContext) -> Result<()> {
        ctx.health.set_state(SubsystemState::Running);
        let socket = tokio::net::TcpSocket::new_v4().context("create NetAudio socket")?;
        socket.set_reuseaddr(true).ok();
        socket
            .bind(std::net::SocketAddr::from(([0, 0, 0, 0], self.port)))
            .context("bind NetAudio TCP")?;
        let listener = socket.listen(4).context("listen NetAudio TCP")?;
        info!("NetAudio: listening on port {}", self.port);

        loop {
            let accepted = tokio::select! {
                a = listener.accept() => a,
                _ = ctx.shutdown.recv() => break,
            };
            match accepted {
                Ok((stream, addr)) => {
                    info!("NetAudio: sender connected from {}", addr);
                    ctx.health.inc_msg();
                    if self.serve(stream, &mut ctx).await? {
                        break;
                    }
                    info!("NetAudio: sender disconnected");
                }
                Err(e) => warn!("NetAudio: accept error: {}", e),
            }
        }
        self.slot.set_active(false);
        Ok(())
    }
}
