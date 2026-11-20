#!/bin/bash
# Load the dump made by dump-for-server.sh into the new server's database gis,
# then build the per-table CSO views. Run as dave on the NEW server:
#
#   bash ~/dgi-explore/deploy/beta/restore-on-server.sh ~/dgi-beta-dump
#
# Refuses to run if the database already holds the CSO figures, so it can
# never wipe a live site's answers. Takes about 25 minutes (tested on
# scierstar on 18 Nov: 19 minutes to restore, 3 to build the views).
# Log: ~/restore-<time>.log
set -euo pipefail
DUMP=${1:?give the dump folder}
DB=${DB:-gis}            # DB=gis_beta for a test restore
JOBS=${JOBS:-8}          # JOBS=4 on a server that also runs other sites
export DGI_DSN="dbname=$DB"
LOG=~/restore-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
say() { echo; echo "$(date '+%H:%M:%S') == $*"; }

say "checking the dump against its checksums"
( cd "$DUMP" && sha256sum -c --quiet SHA256SUMS )

if psql -d "$DB" -XAtc "SELECT 1 FROM dgi_data.cso_value LIMIT 1" 2>/dev/null | grep -q 1; then
    echo "$DB already holds dgi_data.cso_value: not restoring over it" >&2
    exit 1
fi

say "restoring ($JOBS at a time)"
pg_restore -d "$DB" -j "$JOBS" --no-owner --no-privileges --exit-on-error "$DUMP"

say "starting with no answers: the trial questions asked on scierstar are not carried over"
psql -d "$DB" -X -v ON_ERROR_STOP=1 -c "TRUNCATE app.answer"

say "statistics for the planner"
# (dave is not a superuser, so the system tables are skipped; that is harmless)
vacuumdb -q -d "$DB" -j "$JOBS" --analyze-only 2>&1 | grep -v "permission denied to analyze" || true

say "the per-table CSO views (dgi_cso) and the ask index"
cd ~/dgi-explore
~/.local/bin/uv run --frozen dgi-explore ask-prepare

say "counts (compare with scierstar: 13,060 tables, about 290 million values)"
psql -d "$DB" -XAt -c "SELECT count(*) || ' CSO tables' FROM dgi_data.cso_table" \
                 -c "SELECT count(*) || ' views in dgi_cso' FROM pg_views WHERE schemaname = 'dgi_cso'" \
                 -c "SELECT count(*) || ' dataset descriptions with vectors' FROM dgi_vec.item WHERE emb IS NOT NULL"
psql -d "$DB" -XAt -c "SELECT reltuples::bigint || ' CSO values (estimate)' FROM pg_class WHERE oid = 'dgi_data.cso_value'::regclass"
say "done"
