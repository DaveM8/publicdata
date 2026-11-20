#!/bin/bash
# One-time set-up of a new Ubuntu 24.04 server for the Ask site (beta).
# Run as root on the NEW server, after logging in with your SSH key:
#
#   sudo bash server-setup.sh
#
# What it does, in order (safe to run again):
#   1. updates the system and turns on automatic security updates
#   2. installs Postgres 18 (from the PostgreSQL apt repository) with PostGIS,
#      nginx, certbot, ufw, fail2ban, git, zstd
#   3. firewall: only SSH (22), HTTP (80) and HTTPS (443) in
#   4. SSH: no password logins, no root login (keys only)
#   5. Postgres: login dave (owner of the data), database gis with the
#      extensions the data uses; memory settings for a 64 GB machine
#   6. folders for the site's settings and logs
# It does not touch nginx's site files, certificates or the dgi_ask login:
# those are later steps in 20261118_dgi-ask-server-install.org.
# Log: /root/server-setup-<time>.log
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
USER_NAME=${USER_NAME:-dave}
id "$USER_NAME" >/dev/null 2>&1 || { echo "user $USER_NAME does not exist; create it first" >&2; exit 1; }
LOG=/root/server-setup-$(date +%Y%m%d-%H%M%S).log
exec > >(tee -a "$LOG") 2>&1
say() { echo; echo "$(date '+%H:%M:%S') == $*"; }

say "1. system update and automatic security updates"
apt-get update
apt-get -y upgrade
apt-get install -y unattended-upgrades ca-certificates curl gnupg
dpkg-reconfigure -f noninteractive unattended-upgrades

say "2. packages"
install -d /usr/share/postgresql-common/pgdg
curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
     https://www.postgresql.org/media/keys/ACCC4CF8.asc
. /etc/os-release
echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
     > /etc/apt/sources.list.d/pgdg.list
apt-get update
apt-get install -y postgresql-18 postgresql-18-postgis-3 \
                   nginx certbot python3-certbot-nginx ufw fail2ban git zstd rsync

say "3. firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ufw status verbose

say "4. SSH: keys only"
if [ -s "/home/$USER_NAME/.ssh/authorized_keys" ]; then
    cat > /etc/ssh/sshd_config.d/10-keys-only.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
EOF
    sshd -t && systemctl reload ssh
else
    echo "SKIPPED: /home/$USER_NAME/.ssh/authorized_keys is empty. Add your key first, or you"
    echo "could lock yourself out. Then run this script again."
fi
systemctl enable --now fail2ban

say "5. Postgres"
systemctl enable --now postgresql
sudo -u postgres psql -X -v ON_ERROR_STOP=1 <<EOF
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '$USER_NAME') THEN
    CREATE ROLE $USER_NAME LOGIN CREATEDB;
  END IF;
END \$\$;
SELECT 'CREATE DATABASE gis OWNER $USER_NAME'
 WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'gis') \gexec
EOF
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d gis <<'EOF'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS unaccent;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
EOF
cat > /etc/postgresql/18/main/conf.d/dgi.conf <<'EOF'
# Ask site, 64 GB machine. The CSO tables are about 32 GB.
shared_buffers = 8GB
effective_cache_size = 40GB
maintenance_work_mem = 2GB
work_mem = 64MB
max_connections = 100
random_page_cost = 1.1
jit = off
EOF
systemctl restart postgresql
pg_isready

say "6. folders"
install -d -m 750 -o root -g "$USER_NAME" /etc/dgi-explore
install -d -o "$USER_NAME" -g "$USER_NAME" "/home/$USER_NAME/backups"
loginctl enable-linger "$USER_NAME" || true

say "done. Next: step 4 of 20261118_dgi-ask-server-install.org (the code)"
