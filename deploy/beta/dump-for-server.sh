#!/bin/bash
# Dump only what the Ask site needs from scierstar's database gis, for the
# new server. About 32 GB in the database; the dump is compressed.
# The per-table CSO views (dgi_cso) are not dumped: ask-prepare remakes them
# on the server in about 30 seconds. The CSV tables (dgi_data.csv_*, ds_*),
# the map layers (dgi_geo) and the Oireachtas data (dgi_oir) are left out.
#
#   bash ~/dgi-explore/deploy/beta/dump-for-server.sh [out-dir]
#
# Read-only on the database. Log: <out-dir>.log
set -euo pipefail
OUT=${1:-/mnt/pdf_workspace/dgi-beta-dump}
[ -e "$OUT" ] && { echo "$OUT exists; remove it or give another folder" >&2; exit 1; }
exec > >(tee -a "$OUT.log") 2>&1
echo "$(date '+%F %T') dumping to $OUT"
pg_dump -d gis -Fd -j 8 -Z zstd:3 -f "$OUT" --no-owner --no-privileges \
    -n dgi_ask -n dgi_cat -n dgi_ref -n dgi_vec -n dgi_link -n dgi_prof -n dgi_data -n app \
    -T 'dgi_data.csv_*' -T 'dgi_data.ds_*'
du -sh "$OUT"
( cd "$OUT" && sha256sum * > SHA256SUMS )
echo "$(date '+%F %T') done"
