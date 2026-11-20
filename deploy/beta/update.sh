#!/bin/bash
# Put the latest code from GitHub live on the server. Always use this rather
# than git pull alone: the site must be restarted with the code it pulled, or
# it runs old code with new page templates. Run as dave:
#
#   bash ~/dgi-explore/deploy/beta/update.sh
set -euo pipefail
cd ~/dgi-explore
before=$(git rev-parse --short HEAD)
git pull --ff-only
~/.local/bin/uv sync --frozen -q
~/.local/bin/uv run --frozen pytest -q tests/test_web.py
sudo systemctl restart dgi-web dgi-search
sleep 3
systemctl is-active dgi-web dgi-search
code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8003/)
echo "updated $before -> $(git rev-parse --short HEAD); front page answers $code"
[ "$code" = 200 ]
