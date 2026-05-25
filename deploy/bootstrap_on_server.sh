#!/usr/bin/env bash
# Quick-start script to run on a fresh Ubuntu server.
# Pulls the code, sets up .env interactively, then runs install.sh.
#
# Usage on the server:
#   wget -O- https://raw.githubusercontent.com/<YOU>/oanda-bots/main/deploy/bootstrap_on_server.sh | bash
# OR after scp'ing the project to /tmp/oanda-paper-trading:
#   bash /tmp/oanda-paper-trading/deploy/bootstrap_on_server.sh

set -euo pipefail

APP_DIR=/opt/oanda-bots

# If repo URL provided as env var, clone it; otherwise expect files already in /tmp/oanda-paper-trading
if [ -n "${REPO_URL:-}" ]; then
  sudo git clone "$REPO_URL" "$APP_DIR"
elif [ -d /tmp/oanda-paper-trading ]; then
  sudo mkdir -p "$APP_DIR"
  sudo cp -r /tmp/oanda-paper-trading/* "$APP_DIR"/
else
  echo "Neither REPO_URL nor /tmp/oanda-paper-trading found. Aborting." >&2
  exit 1
fi

# Interactive .env setup if missing
if [ ! -f "$APP_DIR/.env" ]; then
  echo
  echo "Need OANDA credentials. Get them from your OANDA practice dashboard."
  read -p "OANDA Account ID (101-003-...):  " ACCID
  read -p "OANDA API Token (paste long string):  " TOKEN
  sudo tee "$APP_DIR/.env" > /dev/null <<EOF
OANDA_ACCOUNT_ID=$ACCID
OANDA_API_TOKEN=$TOKEN
OANDA_ENV=practice
EOF
  sudo chmod 600 "$APP_DIR/.env"
fi

sudo mkdir -p /var/log/oanda-bots
sudo chown -R oandabot:oandabot /var/log/oanda-bots 2>/dev/null || true

bash "$APP_DIR/deploy/install.sh"
