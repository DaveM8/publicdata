#!/bin/bash
# One-off setup for dgi-explore that needs root. Safe to run again.
#   sudo bash ~/dgi-explore/deploy/sudo-setup.sh
# What it does (see ~/org/dgi-explore.org, "Your part"):
#   1. installs pgvector and enables it in the gis database
#   2. creates the read-only login dgi_reader (for Metabase), asking for a password
#   3. adds the /dgi/, /datasette/ and /metabase/ pages to nginx, home network
#      and Tailscale only (same allow-list as /org/); checks and reloads nginx,
#      and puts the old config back if the check fails
#   4. restarts martin so it serves the new map layers in dgi_geo
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "== 1. pgvector"
if ! dpkg -s postgresql-18-pgvector >/dev/null 2>&1; then
    apt-get install -y postgresql-18-pgvector
fi
sudo -u postgres psql -d gis -qc 'CREATE EXTENSION IF NOT EXISTS vector'
sudo -u postgres psql -d gis -Atc "SELECT 'vector ' || extversion FROM pg_extension WHERE extname = 'vector'"

echo "== 2. read-only login dgi_reader"
if sudo -u postgres psql -d gis -Atc "SELECT 1 FROM pg_roles WHERE rolname = 'dgi_reader'" | grep -q 1; then
    echo "dgi_reader exists (to change its password: sudo -u postgres psql -c '\\password dgi_reader')"
else
    read -r -s -p "Choose a password for dgi_reader (used by Metabase): " PW; echo
    sudo -u postgres psql -d gis -qc "CREATE ROLE dgi_reader LOGIN PASSWORD '${PW//\'/\'\'}'"
fi
sudo -u postgres psql -d gis -q <<'SQL'
GRANT CONNECT ON DATABASE gis TO dgi_reader;
DO $$
DECLARE s text;
BEGIN
  FOREACH s IN ARRAY ARRAY['dgi_cat','dgi_prof','dgi_ref','dgi_link','dgi_data','dgi_geo','dgi_vec','dgi_insight',
                           'census','census_raw','census_map','public']
  LOOP
    EXECUTE format('GRANT USAGE ON SCHEMA %I TO dgi_reader', s);
    EXECUTE format('GRANT SELECT ON ALL TABLES IN SCHEMA %I TO dgi_reader', s);
    EXECUTE format('ALTER DEFAULT PRIVILEGES FOR ROLE dave IN SCHEMA %I GRANT SELECT ON TABLES TO dgi_reader', s);
  END LOOP;
END $$;
ALTER ROLE dgi_reader SET statement_timeout = '120s';
SQL
echo "dgi_reader can read the dgi_* and census schemas (queries stop after 2 minutes)"

echo "== 3. nginx"
# sites-enabled/default is a symlink: edit the real file it points to, and keep
# the backup outside sites-enabled (nginx loads every file in that folder).
SITE="$(readlink -f /etc/nginx/sites-enabled/default)"
BACKUP_DIR=/etc/nginx/backup-dgi-explore
mkdir -p "$BACKUP_DIR"
# Clean up after the first version of this script, which left a backup link here.
rm -f /etc/nginx/sites-enabled/default.before-dgi-explore
install -m 644 "$HERE/nginx-dgi.conf" /etc/nginx/snippets/dgi-explore.conf
if ! grep -q 'snippets/dgi-explore.conf' "$SITE"; then
    cp -a "$SITE" "$BACKUP_DIR/default.$(date +%Y%m%d-%H%M%S)"
    # Put the include just after the first "server_name _;" (the default server block).
    sed -i '0,/server_name _;/s//server_name _;\n\tinclude snippets\/dgi-explore.conf;/' "$SITE"
fi
if nginx -t; then
    systemctl reload nginx
    echo "nginx reloaded: /dgi/, /datasette/, /metabase/"
else
    echo "nginx check failed; putting the old config back"
    LAST="$(ls -t "$BACKUP_DIR"/default.* 2>/dev/null | head -1)"
    [ -n "$LAST" ] && cp -a "$LAST" "$SITE"
    nginx -t && systemctl reload nginx
    exit 1
fi

echo "== 4. martin"
systemctl restart martin
echo "done"
