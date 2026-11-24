#!/usr/bin/env bash
# blindspot-warm.timer, every 10 minutes: keep the API cache (nginx-api-cache.conf) in step with the
# data, then request the website's main pages so that their API answers are cached before a visitor
# needs them.
#
# 1. After a daily job, replace the answers it feeds. Each cached answer is kept until its job clock
#    next ticks (s-maxage, services/api/blindspot/api/cache.py CDN_RULES): up to ~23 h for the DAILY
#    group, whose last point is 14:30 UTC. A daily job that finishes after that point (a late
#    Sentinel-2 run, a long risk run) would leave those answers on the old data until the next day,
#    while the hourly answers move on: two pages, two different counts. So each run asks the
#    database (local psql, as postgres) which of the jobs in ROUTES finished since the last run and
#    at least SETTLE_MIN ago, and for every cached answer those jobs feed:
#      - refreshed: fetched again through 127.0.0.1:8002, a second door to the same cache that always
#        asks the API and stores the new answer (proxy_cache_bypass). Visitors keep getting the old
#        copy until the new one is stored, so nothing goes cold. At most REFRESH_MAX, newest first,
#        within REFRESH_BUDGET_S;
#      - purged: case files, CSVs, any answer over that cap, and copies of a refreshed answer kept for
#        a request with another Origin (the API varies on Origin) are deleted. The next request that
#        needs one builds it again, once (proxy_cache_lock);
#      - kept: once REFRESH_BUDGET_S is spent (a slow API), the answers not reached yet stay as they
#        are until their clock's next tick. Deleting them would leave their next readers waiting on
#        the same slow API, with no stale copy to fall back on.
#    The hourly and six-hourly jobs are left to their clocks, which tick within the hour.
# 2. Request the main pages. An answer still within its cache time comes straight from the cache; an
#    expired one is refreshed in the background while the old copy is served; one not cached yet is
#    built now, by this request rather than by a visitor.
#
# Try it without changing anything (prints what it would refresh and delete, writes no state):
#   sudo WARM_DRY_RUN=1 /usr/local/sbin/blindspot-warm
# Test overrides (infra/aws/warm_test.sh): WARM_PSQL (a command printing "id name" lines for the SQL
# in $WARM_SQL), WARM_CACHE_DIR, WARM_STATE_DIR, WARM_REFRESH_URL, WARM_REFRESH_MAX,
# WARM_REFRESH_BUDGET_S, WARM_SITE_URL, WARM_SKIP_PAGES=1.
set -u

CACHE=${WARM_CACHE_DIR:-/var/cache/nginx/blindspot}   # proxy_cache_path in nginx-api-cache.conf
STATE_DIR=${WARM_STATE_DIR:-/var/lib/blindspot-warm}  # StateDirectory= of blindspot-warm.service
REFRESH_URL=${WARM_REFRESH_URL:-http://127.0.0.1:8002}  # the refresh door (nginx-api-cache.conf)
SITE_URL=${WARM_SITE_URL:-http://127.0.0.1:3000}
DRY=${WARM_DRY_RUN:-0}
ORIGIN=https://www.blindspotin.tech  # the Origin the website and its visitors send: their shared copy
API_HOST=api.blindspotin.tech        # the Host Caddy passes on, so a refresh looks like a visitor's read
# A job run is handled once it finished at least SETTLE_MIN ago: the API keeps a built answer in
# process for up to 600 s after its build (cached_route on /api/brief and /api/risk, the boundaries
# GeoJSON, the slow orbit answers), so a refresh any sooner could fetch an answer built from the data
# before the job and store it as new until the next clock point. The brief's answers are built on a
# district list that is itself kept 600 s (brief.known_districts, which names the newest pass), so one
# built up to 600 s after the job from a list made before it lives until 20 minutes after the job.
# 25 minutes leaves 5 for a slow build. A job's answers are refreshed 25 to 35 minutes after it ends.
SETTLE_MIN=25
LOOKBACK_MIN=115                     # runs that finished this recently are looked at (handled ones skipped)
REFRESH_MAX=${WARM_REFRESH_MAX:-60}  # answers refreshed per run; the rest are purged
REFRESH_BUDGET_S=${WARM_REFRESH_BUDGET_S:-480}  # and no refresh starts after this long (a slow API)

# The daily, weekly and bulletin jobs (data-jobs.yml) and the answers they feed (cache keys are the
# request URI). /api/stats/headline and /api/scars/summary tick hourly but carry the Sentinel-2 totals
# and the TLE passes, so they are refreshed with those jobs: every page then shows one count.
declare -A ROUTES=(
  [sentinel]='^/api/(scars|brief|stats/headline|imagery/scenes)([/?]|$)'
  [risk]='^/api/(risk|brief)([/?]|$)'
  [featured]='^/api/imagery/featured([/?]|$)'
  [featured_investigations]='^/api/agent/examples([/?]|$)'
  [bulletin]='^/api/bulletin/'
  [tle]='^/api/(orbits|stats/headline)([/?]|$)'
  [smoke_validate]='^/api/smoke/validation([/?]|$)'
  [boundaries]='^/api/geo/'
)
# Answers deleted rather than refreshed: many and rarely read (a case file, a CSV download).
PURGE_ONLY='^/api/scars/[0-9]+([/?]|$)|/csv([/?]|$)'
# A key fetched again must be a plain request URI (no spaces, quotes or backslashes) of at most 1,500
# characters; others are purged. (A {0,1500} bound would exceed POSIX regex's RE_DUP_MAX of 255.)
SAFE_KEY='^/api/[][A-Za-z0-9._~%/?&=,:;+@!*()-]*$'

log() { echo "warm: $*"; }

if [ -n "${WARM_PSQL:-}" ]; then
  read -r -a PSQL <<< "$WARM_PSQL"
else
  PSQL=(runuser -u postgres -- psql -d blindspot -X -q -At -v ON_ERROR_STOP=1)
fi

# --- 1. which jobs finished since the last run ----------------------------------------------------
names=$(printf "'%s'," "${!ROUTES[@]}")
export WARM_SQL="SELECT id || ' ' || name FROM job_run WHERE ok AND name IN (${names%,})
  AND finished_at > now() - interval '${LOOKBACK_MIN} minutes'
  AND finished_at <= now() - interval '${SETTLE_MIN} minutes' ORDER BY finished_at"
handled_file="$STATE_DIR/handled-job-runs"
declare -A handled=()
if [ -r "$handled_file" ]; then
  while read -r id; do [ -n "$id" ] && handled[$id]=1; done < "$handled_file"
fi

if ! rows=$("${PSQL[@]}" -c "$WARM_SQL" 2>&1); then
  log "could not read job_run, no refresh this time: ${rows//$'\n'/ }"
  rows=""
  db_ok=0
else
  db_ok=1
fi

declare -A jobs=()
seen_ids=()
while read -r id name; do
  [[ "$id" =~ ^[0-9]+$ ]] && [ -n "$name" ] && [ -n "${ROUTES[$name]+x}" ] || continue
  seen_ids+=("$id")
  [ -n "${handled[$id]+x}" ] && continue
  jobs[$name]=1
  log "job $name finished (job_run $id)"
done <<< "$rows"

# --- 2. the cached answers those jobs feed --------------------------------------------------------
if [ "${#jobs[@]}" -gt 0 ] && [ -d "$CACHE" ]; then
  pattern=""
  for name in "${!jobs[@]}"; do pattern+="${pattern:+|}(${ROUTES[$name]})"; done

  # One line per cache file: "path:KEY: <request URI>" (nginx writes the key near the file's start).
  # Names with a dot are left alone: <md5>.0000000NNN is nginx's temp file of a store in progress
  # (use_temp_path=off puts it beside the cache files), renamed to <md5> when complete.
  declare -A files_of=()   # key -> its cache files, space-separated
  while IFS= read -r line; do
    path=${line%%:*}
    key=${line#*:KEY: }
    [[ "$key" =~ $pattern ]] || continue
    files_of[$key]+="${files_of[$key]:+ }$path"
  done < <(find "$CACHE" -type f ! -name '*.*' -exec grep -a -m1 -H '^KEY: ' {} + 2>/dev/null)

  # nginx names a cache file by the MD5 of its key (levels=1:2: .../<last char>/<2 before it>/<md5>);
  # a file of the same key under another name holds another Origin's copy (Vary).
  primary() { local h; h=$(printf '%s' "$1" | md5sum); h=${h%% *}; echo "$CACHE/${h: -1}/${h: -3:2}/$h"; }
  remove() { if [ "$DRY" = 1 ]; then echo "warm: would delete $1"; else rm -f -- "$1"; fi; }
  # An answer purged: every copy of its key (each Origin's) deleted, counted once.
  purge_key() { local f; for f in ${files_of[$1]}; do remove "$f"; done; purged=$((purged + 1)); }

  declare -A primary_of=()  # key -> its primary file, for each answer to refresh
  purged=0
  for key in "${!files_of[@]}"; do
    if [[ "$key" =~ $PURGE_ONLY ]] || ! [[ "$key" =~ $SAFE_KEY ]] || [ "${#key}" -gt 1500 ]; then
      purge_key "$key"
    else
      primary_of[$key]=$(primary "$key")
    fi
  done

  # Most recently stored first (the primary copy's mtime). That is the last time the answer was built,
  # not the last time it was read: one read all day but stored just after the last clock point sorts
  # below one stored later by a single read, so over the cap it is purged and its next reader builds
  # it again, once (proxy_cache_lock).
  ordered=()
  if [ "${#primary_of[@]}" -gt 0 ]; then
    while IFS=$'\t' read -r _age key; do ordered+=("$key"); done < <(
      for key in "${!primary_of[@]}"; do
        printf '%s\t%s\n' "$(stat -c %Y -- "${primary_of[$key]}" 2>/dev/null || echo 0)" "$key"
      done | sort -t $'\t' -k1,1nr)
  fi

  refreshed=0
  failed=0
  kept=0
  started=$SECONDS
  for key in "${ordered[@]}"; do
    p=${primary_of[$key]}
    if [ "$refreshed" -ge "$REFRESH_MAX" ]; then  # over the cap: purged, built again when next read
      purge_key "$key"
      continue
    fi
    # over the time budget: kept as they are, every copy (the timer's next run and the pages are not
    # held up behind a slow API, and its readers keep a copy while it is slow)
    if [ $((SECONDS - started)) -ge "$REFRESH_BUDGET_S" ]; then
      kept=$((kept + 1))
      continue
    fi
    for f in ${files_of[$key]}; do [ "$f" = "$p" ] || remove "$f"; done
    if [ "$DRY" = 1 ]; then
      echo "warm: would refresh $key"
      refreshed=$((refreshed + 1))
    elif curl -fsS -g -o /dev/null --max-time 180 -H "Origin: $ORIGIN" -H "Host: $API_HOST" \
        -H "X-Forwarded-Proto: https" "$REFRESH_URL$key"; then
      refreshed=$((refreshed + 1))
    else  # the old copy stays (stale-if-error); the clock or the next job tries again
      failed=$((failed + 1))
      log "refresh failed: $key"
    fi
  done
  log "after ${!jobs[*]}: ${refreshed} answers refreshed, ${purged} purged, ${failed} failed"
  if [ "$kept" -gt 0 ]; then
    log "time budget of ${REFRESH_BUDGET_S} s spent: ${kept} answers kept as they are until their clock's next tick"
  fi
fi

# Remember what was handled (the look-back window keeps the file small). A dry run changes nothing.
if [ "$DRY" != 1 ] && [ "$db_ok" = 1 ]; then
  { mkdir -p "$STATE_DIR" && printf '%s\n' "${seen_ids[@]}" > "$handled_file.tmp" \
      && mv -f "$handled_file.tmp" "$handled_file"; } 2>/dev/null \
    || log "could not write $handled_file: the runs above are handled again by the next run"
fi

# --- 3. the pages -------------------------------------------------------------------------------
[ "${WARM_SKIP_PAGES:-0}" = 1 ] && exit 0
for path in / /live /bulletin /brief /method; do
  if [ "$DRY" = 1 ]; then echo "warm: would request $SITE_URL$path"; continue; fi
  curl -fsS -o /dev/null --max-time 120 "${SITE_URL}${path}" || log "${path} did not answer"
done
