#!/usr/bin/env bash
# Setup of BlindSpot's AWS server (Ubuntu 24.04, arm64). Run as root; safe to run again.
# `infra/aws/deploy.py --setup` uploads and runs it. Installs:
#   PostgreSQL 18 + PostGIS 3 (PGDG; Neon ran 18.7 + PostGIS 3.6), tuned for a 1 GB machine
#   Caddy (HTTPS), nginx (the API's cache only), Node 24 + pnpm (website, relay), uv + Python 3.12 (API)
#   a 2 GB swap file, the `blindspot` service user, the nightly backup and the weekly housekeeping.
# The three services themselves (API, website, relay) are installed by deploy.py with their code.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# --- swap: lets the website build and the occasional heavy request ride out a memory peak
if [ ! -f /swapfile ]; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
printf 'vm.swappiness=10\nvm.vfs_cache_pressure=50\n' > /etc/sysctl.d/99-blindspot.conf
sysctl -q --system

# --- packages (a first boot runs unattended-upgrades, which holds the apt lock for a while; an
#     interrupted earlier run can leave dpkg half done)
apt_get() { apt-get -o DPkg::Lock::Timeout=900 "$@"; }
dpkg --configure -a || true
apt_get update -q
apt_get install -y -q curl ca-certificates gnupg postgresql-common debian-keyring debian-archive-keyring apt-transport-https unattended-upgrades
if [ ! -f /etc/apt/sources.list.d/pgdg.list ] && [ ! -f /etc/apt/sources.list.d/pgdg.sources ]; then
  /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y
fi
if [ ! -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]; then
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt > /etc/apt/sources.list.d/caddy-stable.list
fi
if ! command -v node >/dev/null || ! node -v | grep -q '^v24\.'; then
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
fi
apt_get update -q
apt_get install -y -q postgresql-18 postgresql-18-postgis-3 caddy nodejs
# nginx: only the API's cache (nginx-api-cache.conf, installed by deploy.py), never a web server.
# Its package would start the default site on :80, which Caddy holds, so it installs without
# starting, and the default site goes.
if ! dpkg -s nginx >/dev/null 2>&1; then
  printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d && chmod 755 /usr/sbin/policy-rc.d
  apt_get install -y -q nginx || { rm -f /usr/sbin/policy-rc.d; exit 1; }
  rm -f /usr/sbin/policy-rc.d
fi
rm -f /etc/nginx/sites-enabled/default
corepack enable
corepack prepare pnpm@10.28.0 --activate
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
fi

# --- service user and folders
id blindspot >/dev/null 2>&1 || useradd --system --create-home --home-dir /srv/blindspot --shell /usr/sbin/nologin blindspot
install -d -o blindspot -g blindspot /srv/blindspot /srv/blindspot/api /srv/blindspot/web /srv/blindspot/relay /srv/blindspot/cache
install -d -m 750 -o root -g blindspot /etc/blindspot
install -d -m 750 -o postgres -g postgres /var/backups/blindspot

# --- PostgreSQL: small-machine settings, TLS on, reachable by the jobs on GitHub's runners
PGCONF=/etc/postgresql/18/main
cat > $PGCONF/conf.d/blindspot.conf <<'EOF'
listen_addresses = '*'
max_connections = 40
shared_buffers = 96MB
effective_cache_size = 384MB
work_mem = 8MB
maintenance_work_mem = 64MB
random_page_cost = 1.1
effective_io_concurrency = 200
# write-ahead log: on a 16 GB disk, ~250 MB is plenty for this write volume; lz4 shrinks what is kept
max_wal_size = 256MB
min_wal_size = 64MB
wal_compression = lz4
checkpoint_completion_target = 0.9
jit = off
ssl = on
password_encryption = scram-sha-256
log_min_duration_statement = 2000
idle_in_transaction_session_timeout = '10min'
# TCP keepalives every 60 s: a job's long statement sends nothing for minutes, and the network in
# front of GitHub's runners drops a silent connection after ~4 min (the risk job's UPDATE, 2026-11-22)
tcp_keepalives_idle = 60
tcp_keepalives_interval = 15
tcp_keepalives_count = 4
EOF
if ! grep -q 'blindspot-remote' $PGCONF/pg_hba.conf; then
  cat >> $PGCONF/pg_hba.conf <<'EOF'
# blindspot-remote: the data jobs (GitHub Actions) over TLS with a password, never in the clear
hostssl blindspot blindspot 0.0.0.0/0 scram-sha-256
hostssl blindspot blindspot ::/0      scram-sha-256
EOF
fi
systemctl enable --now postgresql
# Reload picks up most settings without dropping connections (the jobs may be writing); restart only
# for the few that need it (listen_addresses on a fresh server, shared_buffers).
systemctl reload postgresql
sleep 2
if [ "$(sudo -u postgres psql -Atc "SELECT count(*) FROM pg_settings WHERE pending_restart")" != "0" ]; then
  systemctl restart postgresql
fi

# --- database role and password (generated here once; never leaves the server except to the
#     GitHub secret, piped by `deploy.py --github-secret`)
if [ ! -f /etc/blindspot/db.env ]; then
  PW=$(openssl rand -hex 24)
  sudo -u postgres psql -q -v ON_ERROR_STOP=1 -c "CREATE ROLE blindspot LOGIN PASSWORD '$PW'"
  sudo -u postgres createdb -O blindspot blindspot
  sudo -u postgres psql -q -d blindspot -c "CREATE EXTENSION IF NOT EXISTS postgis"
  umask 077
  echo "DATABASE_URL='postgresql+psycopg://blindspot:$PW@127.0.0.1:5432/blindspot'" > /etc/blindspot/db.env
  chgrp blindspot /etc/blindspot/db.env && chmod 640 /etc/blindspot/db.env
fi

# --- nightly backup of the database (03:00 IST); the previous one is replaced only on success
cat > /etc/cron.d/blindspot-backup <<'EOF'
30 21 * * * postgres pg_dump -Fc -d blindspot -f /var/backups/blindspot/nightly.dump.tmp && mv -f /var/backups/blindspot/nightly.dump.tmp /var/backups/blindspot/nightly.dump
EOF

# --- security updates without reboots; downloaded packages cleared weekly
printf 'APT::Periodic::Unattended-Upgrade "1";\nAPT::Periodic::AutocleanInterval "7";\n' > /etc/apt/apt.conf.d/21blindspot
systemctl enable caddy

# --- keep the disk from filling: the journal capped at 100 MB, and a weekly housekeeping run
#     (infra/aws/maintain.sh: the API's file cache, package caches, stale builds, a disk report)
install -d /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\nMaxRetentionSec=14day\n' > /etc/systemd/journald.conf.d/blindspot.conf
systemctl restart systemd-journald
install -m 755 "$(dirname "$0")/maintain.sh" /usr/local/sbin/blindspot-maintain
install -m 644 "$(dirname "$0")/blindspot-maintain.service" "$(dirname "$0")/blindspot-maintain.timer" /etc/systemd/system/
# ...and every 10 minutes, the API cache is refreshed: the answers of a daily job that has just
# finished, then the website's main pages (infra/aws/warm.sh; deploy.py --only api installs it too)
install -m 755 "$(dirname "$0")/warm.sh" /usr/local/sbin/blindspot-warm
install -m 644 "$(dirname "$0")/blindspot-warm.service" "$(dirname "$0")/blindspot-warm.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now --quiet blindspot-maintain.timer blindspot-warm.timer

# --- snapd and the SSM agent snap ship with Ubuntu's AWS image but are unused here (the server is
#     managed over SSH): removing them frees ~120 MB of disk and ~40 MB of the 1 GB of memory.
if dpkg -s snapd >/dev/null 2>&1; then
  apt_get purge -y -q snapd || true
  rm -rf /snap /var/snap /var/lib/snapd /var/cache/snapd
fi
echo "setup done: $(psql --version), $(caddy version | cut -d' ' -f1), node $(node -v), $(uv --version)"
