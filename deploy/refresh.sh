#!/bin/bash
# Bring dgi-explore up to date with the mirror. Each step only does what's new
# or changed (link, build-site and export-datasette rebuild in full).
# Run inside tmux:  bash ~/dgi-explore/deploy/refresh.sh
# Stops at the first step that fails. Log: ~/.local/state/dgi-explore/refresh-<date>.log
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=~/.local/state/dgi-explore/refresh-$(date +%Y%m%d-%H%M).log
step() {
    echo "== $(date '+%F %T') $*" | tee -a "$LOG"
    uv run dgi-explore "$@" 2>&1 | tee -a "$LOG"
}
step catalog
step profile
step link
step load-cso
step load-csv -j 6
step load-geo
step embed
step build-site
step export-datasette
systemctl --user restart dgi-datasette dgi-search
step insights
echo "== $(date '+%F %T') refresh finished" | tee -a "$LOG"
