#!/usr/bin/env bash
# Install Cortana's Ghost's services on an Ubuntu server, for the user running
# this script, from this checkout. Run setup steps in ../README.md first.
#   ./install.sh            bridge, speaker link, Apple Music player, AirPlay
#   ./install.sh --gpu      also run speech-to-text on an NVIDIA GPU
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(cd "$here/../.." && pwd)
user=$(id -un); uid=$(id -u); home=$HOME
site=$(ls -d "$home"/invoke-bridge/venv/lib/python3*/site-packages | head -1)

fill() { sed -e "s|@USER@|$user|g" -e "s|@UID@|$uid|g" -e "s|@HOME@|$home|g" \
             -e "s|@REPO@|$repo|g" -e "s|@SITE@|$site|g" "$1"; }

mkdir -p ~/.config/pipewire/pipewire.conf.d
cp "$here/cortana-music-sink.conf" ~/.config/pipewire/pipewire.conf.d/cortana-music.conf
sudo loginctl enable-linger "$user"            # PipeWire runs without a login
systemctl --user enable --now pipewire pipewire-pulse wireplumber
systemctl --user restart pipewire pipewire-pulse wireplumber

sudo systemctl disable --now shairport-sync 2>/dev/null || true   # replaced by cortana-airplay
for unit in cortana-bridge cortana-speaker cortana-display cortana-music cortana-airplay; do
    fill "$here/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
if [ "${1:-}" = "--gpu" ]; then
    sudo mkdir -p /etc/systemd/system/cortana-bridge.service.d
    fill "$here/gpu.conf" | sudo tee /etc/systemd/system/cortana-bridge.service.d/gpu.conf >/dev/null
fi
sudo systemctl daemon-reload
sudo systemctl enable --now cortana-bridge cortana-speaker cortana-display cortana-music cortana-airplay
systemctl --no-pager --lines=0 status cortana-bridge cortana-speaker cortana-music cortana-airplay | grep -E "●|Active"
