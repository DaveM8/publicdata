#!/bin/bash
# Set voyager up for the Ask site. voyager already runs Yes Deputy and
# pdf2phone, so this only ADDS things and restarts nothing:
#
#   sudo bash ~/dgi-explore/deploy/beta/voyager-setup.sh
#
# What it does (safe to run again):
#   1. installs PostGIS for Postgres 18 (only that package; no system upgrade)
#   2. Postgres: a login dave and a new database gis owned by it, with the
#      extensions the data uses; the site's settings are set on gis only
#   3. folders for the site's settings and the answer backups
# What it does NOT do: change postgresql.conf, restart Postgres or nginx,
# touch the firewall, SSH, Docker, Elasticsearch or the other sites.
# For an empty server, server-setup.sh is the fuller version.
# Log: /root/voyager-setup-<time>.log
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
USER_NAME=${USER_NAME:-dave}
LOG=/root/voyager-setup-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
say() { echo; echo "$(date '+%H:%M:%S') == $*"; }

say "0. checks"
for port in 8002 8003; do
    if ss -tlnH "sport = :$port" | grep -q .; then
        echo "port $port is already in use; the Ask site needs it" >&2; exit 1
    fi
done
pg_isready
df -h /

say "1. PostGIS"
apt-get update
apt-get install -y postgresql-18-postgis-3

say "2. Postgres: login $USER_NAME and database gis"
sudo -u postgres psql -X -v ON_ERROR_STOP=1 <<EOF
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$USER_NAME') THEN
    CREATE ROLE $USER_NAME LOGIN;
  END IF;
END \$\$;
SELECT 'CREATE DATABASE gis OWNER $USER_NAME'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'gis') \gexec
EOF
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d gis <<'EOF'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- Settings for this database only; the other databases keep theirs.
ALTER DATABASE gis SET work_mem = '64MB';
ALTER DATABASE gis SET random_page_cost = 1.1;
ALTER DATABASE gis SET jit = off;
SELECT extname, extversion FROM pg_extension ORDER BY 1;
EOF

say "3. folders"
install -d -m 750 -o root -g "$USER_NAME" /etc/dgi-explore
install -d -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/backups"

say "done. Next: step 4 of 20261118_dgi-ask-server-install.org (the code)"
