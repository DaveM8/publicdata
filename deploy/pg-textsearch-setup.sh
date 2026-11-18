#!/bin/bash
# Install pg_textsearch (BM25 ranked word search, from the PostgreSQL apt repo)
# into Postgres 18 on this machine, for the Oireachtas word search in dgi_oir.
#
#   sudo ~/dgi-explore/deploy/pg-textsearch-setup.sh
#
# What it does:
#   1. apt-get install postgresql-18-pg-textsearch
#   2. adds pg_textsearch to shared_preload_libraries (keeping anything already there)
#   3. restarts Postgres: about 10 seconds in which every app using the
#      database (dgi-search, dgi-web, Datasette's source, Metabase) gets errors
#   4. CREATE EXTENSION pg_textsearch in the database gis
# Safe to run again. Log: ~/pg-textsearch-<time>.log
set -euo pipefail
export PATH=/usr/sbin:/usr/bin:/sbin:/bin

[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
LOG=/home/dave/pg-textsearch-$(date +%Y%m%d-%H%M%S).log
touch "$LOG"; chown dave:dave "$LOG"
exec > >(tee -a "$LOG") 2>&1
say() { echo "$(date '+%H:%M:%S') $*"; }
psql_pg() { sudo -u postgres psql -X -v ON_ERROR_STOP=1 -Atc "$@"; }

say "== installing postgresql-18-pg-textsearch"
apt-get install -y postgresql-18-pg-textsearch

current=$(psql_pg "SHOW shared_preload_libraries")
say "shared_preload_libraries now: '${current}'"
if [[ ",${current// /}," == *",pg_textsearch,"* ]]; then
    say "pg_textsearch is already preloaded"
else
    new=${current:+$current,}pg_textsearch
    say "== setting shared_preload_libraries = '$new' and restarting Postgres"
    psql_pg "ALTER SYSTEM SET shared_preload_libraries = '$new'"
    systemctl restart postgresql@18-main
    for i in $(seq 30); do
        pg_isready -q && break
        sleep 1
    done
    pg_isready || { say "FAIL: Postgres didn't come back; see /var/log/postgresql/postgresql-18-main.log"; exit 1; }
    say "Postgres is back: $(psql_pg 'SHOW shared_preload_libraries')"
fi

say "== CREATE EXTENSION in gis"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d gis -c "CREATE EXTENSION IF NOT EXISTS pg_textsearch"
sudo -u postgres psql -X -d gis -Atc "SELECT 'pg_textsearch ' || extversion FROM pg_extension WHERE extname = 'pg_textsearch'"
say "== done. Tell Claude; it builds the BM25 index next. Log: $LOG"
