#!/usr/bin/env bash
# One-time setup on the EC2 instance (Amazon Linux 2023).
#   bash deploy/install.sh
# Installs Python 3.11, creates a virtualenv, installs dependencies and
# registers the bot as a systemd service that restarts automatically on
# crash or reboot.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(whoami)}"

sudo dnf install -y python3.11 python3.11-pip git tmux nano
python3.11 -m venv "$REPO_DIR/.venv"
"$REPO_DIR/.venv/bin/pip" install --upgrade pip
"$REPO_DIR/.venv/bin/pip" install -r "$REPO_DIR/requirements.txt"

sudo tee /etc/systemd/system/roostoo-bot.service > /dev/null <<EOF
[Unit]
Description=Roostoo autonomous trading bot
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
WorkingDirectory=$REPO_DIR
ExecStart=$REPO_DIR/.venv/bin/python -m bot.main
Restart=always
RestartSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
echo
echo "Installed. Next:"
echo "  1) create $REPO_DIR/.env (see .env.example)"
echo "  2) .venv/bin/python -m scripts.check_connection"
echo "  3) sudo systemctl enable --now roostoo-bot"
