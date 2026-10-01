#!/usr/bin/env bash
# Deploy the latest committed strategy: pull, reinstall deps, restart.
# The bot rebuilds its state from the exchange on startup, so a restart is safe.
#   bash deploy/update.sh
set -euo pipefail
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
git pull --ff-only
.venv/bin/pip install -q -r requirements.txt
sudo systemctl restart roostoo-bot
echo "Deployed commit $(git rev-parse --short HEAD)"
sudo systemctl status roostoo-bot --no-pager | head -5
