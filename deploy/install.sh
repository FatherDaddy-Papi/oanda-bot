#!/usr/bin/env bash
# One-shot install script for OANDA RSI bots on a fresh Ubuntu 22.04 server.
# Usage:  curl -fsSL <raw-url-of-this-script> | bash
#   OR:   bash install.sh
#
# Assumes you've already SCP'd or git-cloned the project to /opt/oanda-bots
# and put .env in that folder.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/oanda-bots}"
APP_USER="${APP_USER:-oandabot}"

echo "== Updating apt =="
sudo apt-get update -qq
sudo apt-get install -y python3 python3-pip python3-venv git tmux logrotate

echo "== Creating service user '$APP_USER' (if missing) =="
if ! id "$APP_USER" >/dev/null 2>&1; then
  sudo useradd -r -m -d /home/"$APP_USER" -s /bin/bash "$APP_USER"
fi

echo "== Ensuring $APP_DIR exists =="
sudo mkdir -p "$APP_DIR"
sudo chown -R "$APP_USER:$APP_USER" "$APP_DIR"

if [ ! -f "$APP_DIR/.env" ]; then
  echo "!! $APP_DIR/.env not found. Create it first with your OANDA creds, then rerun." >&2
  exit 1
fi

echo "== Setting up Python venv + deps =="
sudo -u "$APP_USER" bash -c "
  cd '$APP_DIR'
  python3 -m venv .venv
  .venv/bin/pip install --quiet --upgrade pip
  .venv/bin/pip install --quiet -r requirements.txt
"

echo "== Installing systemd unit =="
sudo cp "$APP_DIR/deploy/rsi-bot@.service" /etc/systemd/system/rsi-bot@.service
sudo systemctl daemon-reload

echo "== Installing logrotate config =="
sudo cp "$APP_DIR/deploy/logrotate.conf" /etc/logrotate.d/oanda-bots

echo "== Done =="
echo
echo "Now enable + start your bots, one per instrument:"
echo "  sudo systemctl enable --now rsi-bot@EUR_USD"
echo "  sudo systemctl enable --now rsi-bot@XAU_USD"
echo
echo "Watch them:"
echo "  sudo journalctl -u rsi-bot@EUR_USD -f"
echo "  tail -f $APP_DIR/bot_EUR_USD.log"
echo
echo "Status:"
echo "  systemctl status rsi-bot@EUR_USD"
