#!/usr/bin/env bash
# Weekly housekeeping on BlindSpot's server (blindspot-maintain.timer, installed by setup_server.sh).
# Keeps the 16 GB disk from filling with things nothing else trims; the database's own retention is
# the daily `prune` job (services/api/blindspot/jobs/prune.py). Safe to run any time:
#   sudo systemctl start blindspot-maintain    # then: journalctl -u blindspot-maintain
set -uo pipefail

CACHE=/srv/blindspot/cache                 # the API's downloads and rendered pictures (CACHE_DIR)
CACHE_DAYS=30                              # files untouched this long go
CACHE_MAX_MB=500                           # and the oldest go while the folder is larger than this
WARN_PCT=85                                # disk use that gets a warning in the journal

used() { df --output=pcent / | tail -1 | tr -dc '0-9'; }
echo "disk before: $(used)% used, $(df -h --output=avail / | tail -1 | tr -d ' ') free"

# 1. The API's file cache: old files, then the oldest while it is over its cap.
if [ -d "$CACHE" ]; then
  find "$CACHE" -type f -mtime +"$CACHE_DAYS" -delete
  total=$(du -sb "$CACHE" | cut -f1)
  cap=$((CACHE_MAX_MB * 1024 * 1024))
  if [ "$total" -gt "$cap" ]; then  # oldest first, until under the cap (sizes summed once, not re-measured)
    find "$CACHE" -type f -printf '%T@ %s %p\n' | sort -n | while read -r _mtime bytes path; do
      [ "$total" -le "$cap" ] && break
      rm -f -- "$path"
      total=$((total - bytes))
    done
  fi
  find "$CACHE" -mindepth 1 -type d -empty -delete
  echo "api cache: $(du -sh "$CACHE" | cut -f1)"
fi

# 2. Package caches. (The journal keeps itself under 100 MB / 14 days: journald.conf.d, setup_server.sh.)
apt-get -q clean || true
sudo -u blindspot -H bash -c 'cd /srv/blindspot && COREPACK_ENABLE_DOWNLOAD_PROMPT=0 pnpm store prune >/dev/null 2>&1' || true
sudo -u blindspot -H uv cache prune -q 2>/dev/null || true

# 3. A website build left behind by an interrupted deploy (blue/green: only the live one is kept).
live=$(readlink -f /srv/blindspot/web 2>/dev/null || true)
for d in /srv/blindspot/web-a /srv/blindspot/web-b; do
  if [ -d "$d" ] && [ "$d" != "$live" ] && [ -n "$(find "$d" -maxdepth 0 -mmin +360 2>/dev/null)" ]; then
    rm -rf "$d" && echo "removed stale build $d"
  fi
done

# 4. Report, and warn when the disk is getting full.
pct=$(used)
echo "disk after: ${pct}% used; database $(sudo -u postgres psql -Atc "SELECT pg_size_pretty(pg_database_size('blindspot'))" 2>/dev/null)"
if [ "$pct" -ge "$WARN_PCT" ]; then
  echo "WARNING: disk ${pct}% used: check the largest tables (prune job) and /srv/blindspot" >&2
fi
exit 0
