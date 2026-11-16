#!/usr/bin/env python3
"""Patch Encore's Wyoming satellite so an incoming event is never half-read.

The streaming loop used `read_event(reader)` directly as a tokio::select! branch.
Every time another branch won (a mic frame arrives every few milliseconds), the
in-progress read was dropped together with the bytes it had already consumed, so
any event that arrived in more than one network packet desynchronised the stream
and the session died with "read header".

Fix: one long-lived reader future, polled by reference so it is never dropped,
hands complete events to the loop over a channel (channel receive is safe to
cancel).
"""
import pathlib
import sys

path = pathlib.Path.home() / "invoke/Encore/encore/crates/encore-firmware/src/wyoming/mod.rs"
src = path.read_text()

if "read_loop" in src:
    print("already patched")
    sys.exit(0)

OLD_HEAD = """        while self.streaming {
            tokio::select! {
                mic_data = async {"""
NEW_HEAD = """        // `read_event` is not cancellation-safe: dropping it mid-read loses the bytes
        // it has consumed. Run it in one long-lived future that select! polls by
        // reference (never dropped) and receive whole events over a channel instead.
        let (ev_tx, mut ev_rx) = tokio::sync::mpsc::channel(64);
        let read_loop = async {
            loop {
                let ev = read_event(reader).await;
                let done = !matches!(ev, Ok(Some(_)));
                if ev_tx.send(ev).await.is_err() || done {
                    break;
                }
            }
            std::future::pending::<()>().await
        };
        tokio::pin!(read_loop);

        while self.streaming {
            tokio::select! {
                _ = &mut read_loop => {}
                mic_data = async {"""

OLD_BRANCH = """                event = read_event(reader) => {
                    match event {"""
NEW_BRANCH = """                event = ev_rx.recv() => {
                    let event = match event {
                        Some(ev) => ev,
                        None => break,
                    };
                    match event {"""

for old in (OLD_HEAD, OLD_BRANCH):
    if src.count(old) != 1:
        sys.exit(f"expected exactly one match, found {src.count(old)} for:\n{old}")

path.write_text(src.replace(OLD_HEAD, NEW_HEAD).replace(OLD_BRANCH, NEW_BRANCH))
print("patched", path)
