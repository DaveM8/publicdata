#!/bin/bash
# Start Metabase (SQL questions, charts, dashboards) in Docker. No sudo needed.
#   bash ~/dgi-explore/deploy/metabase.sh
# It listens on 127.0.0.1:3001 only (3000 is martin) and is reached through
# nginx at http://scierstar/metabase/. Its own settings live in deploy/metabase-data.
# First visit: create an admin account, then add the database:
#   type PostgreSQL, host localhost, port 5432, database gis,
#   user dgi_reader with the password chosen in sudo-setup.sh.
set -euo pipefail
DATA="$(cd "$(dirname "$0")" && pwd)/metabase-data"
mkdir -p "$DATA"
if docker ps -a --format '{{.Names}}' | grep -qx metabase; then
    docker start metabase
else
    docker run -d --name metabase --restart unless-stopped \
        --network host \
        -e MB_JETTY_HOST=127.0.0.1 -e MB_JETTY_PORT=3001 \
        -e MB_SITE_URL=http://scierstar/metabase/ \
        -e MB_DB_FILE=/metabase-data/metabase.db \
        -v "$DATA:/metabase-data" \
        metabase/metabase:latest
fi
echo "Metabase is starting (takes a minute): http://scierstar/metabase/"
