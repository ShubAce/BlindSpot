#!/usr/bin/env bash
# Tests of warm.sh's refresh logic, with no root, database or nginx: a fake nginx cache directory, a
# fake psql and a small local HTTP server standing in for the refresh door. Run it anywhere bash and
# Python 3 are:   bash infra/aws/warm_test.sh      (prints "ok" lines, exits 1 on the first failure)
set -u
here=$(cd "$(dirname "$0")" && pwd)
warm="$here/warm.sh"
tmp=$(mktemp -d)
server_pid=""
cleanup() { [ -n "$server_pid" ] && kill "$server_pid" 2>/dev/null; rm -rf "$tmp"; }
trap cleanup EXIT

fail() { echo "FAIL: $*"; exit 1; }
ok() { echo "ok: $*"; }
has() { grep -qF -- "$2" <<< "$1" || fail "$3: missing \"$2\""; }
hasnt() { ! grep -qF -- "$2" <<< "$1" || fail "$3: unexpected \"$2\""; }

bash -n "$warm" || fail "warm.sh does not parse"
ok "bash -n warm.sh"

cache="$tmp/cache"
state="$tmp/state"
# A cache file as nginx writes it: a binary header, then "\nKEY: <uri>\n", then the response.
put() {  # put <key> [file name]: at the key's MD5 path (levels=1:2), or under the name given
  local h name dir
  h=$(printf '%s' "$1" | md5sum); h=${h%% *}
  name=${2:-$h}
  dir="$cache/${name: -1}/${name: -3:2}"
  mkdir -p "$dir"
  printf '\x05\x00\x00\x00\x00\x00\x00\x00\x8f\x1a\nKEY: %s\nHTTP/1.1 200 OK\r\nVary: Origin\r\n\r\n{}' "$1" > "$dir/$name"
  echo "$dir/$name"
}
headline=$(put "/api/stats/headline")
headline_other=$(put "/api/stats/headline" "0123456789abcdef0123456789abcd5e")  # another Origin's copy
put "/api/scars/summary" > /dev/null
put "/api/scars?unseen=true&min_confidence=0.9&limit=120" > /dev/null
put "/api/brief?days=7" > /dev/null
put "/api/brief/Sangrur?days=7&limit=50" > /dev/null
case_file=$(put "/api/scars/4936479")
csv=$(put "/api/brief/Sangrur/csv?days=7&lang=pa")
odd=$(put "/api/scars?district=a b")  # not a plain URI: purged, never fetched
fires=$(put "/api/fires?hours=24")
risk=$(put "/api/risk?limit=1")
render=$(put "/api/imagery/render?item=x&bbox=1,2,3,4")

cat > "$tmp/psql.sh" <<'EOF'
# fake psql: the SQL must ask for finished, successful runs; the rows come from $FAKE_ROWS
grep -q "FROM job_run WHERE ok" <<< "$WARM_SQL" || { echo "unexpected SQL" >&2; exit 2; }
printf '%b' "${FAKE_ROWS:-}"
EOF

run() {  # run <rows> [VAR=value ...]: warm.sh with the fakes, pages skipped
  local rows=$1; shift
  env FAKE_ROWS="$rows" WARM_PSQL="bash $tmp/psql.sh" WARM_CACHE_DIR="$cache" WARM_STATE_DIR="$state" \
    WARM_SKIP_PAGES=1 "$@" bash "$warm" 2>&1
}

# 1. Dry run after the Sentinel-2 job: what it feeds is listed, nothing else, and nothing changes.
out=$(run '101 sentinel\n102 firms_nrt\n' WARM_DRY_RUN=1)
has "$out" "job sentinel finished (job_run 101)" "dry run"
hasnt "$out" "firms_nrt" "dry run (an hourly job is left to its clock)"
for key in "/api/stats/headline" "/api/scars/summary" "/api/scars?unseen=true&min_confidence=0.9&limit=120" \
           "/api/brief?days=7" "/api/brief/Sangrur?days=7&limit=50"; do
  has "$out" "would refresh $key" "dry run"
done
for f in "$case_file" "$csv" "$odd" "$headline_other"; do has "$out" "would delete $f" "dry run"; done
for f in "$headline" "$fires" "$risk" "$render"; do hasnt "$out" "would delete $f" "dry run"; done
hasnt "$out" "would refresh /api/fires" "dry run"
hasnt "$out" "would refresh /api/risk" "dry run"
has "$out" "5 answers refreshed, 3 purged, 0 failed" "dry run"
[ -e "$case_file" ] && [ -e "$headline_other" ] || fail "dry run deleted a file"
[ ! -e "$state/handled-job-runs" ] || fail "dry run wrote the state"
ok "dry run after sentinel: 5 refreshed, case file, CSV, odd key and the other Origin's copy listed for deletion"

# 2. The refresh cap: over it, answers are purged instead (newest first are refreshed).
out=$(run '101 sentinel\n' WARM_DRY_RUN=1 WARM_REFRESH_MAX=2)
has "$out" "2 answers refreshed, 6 purged, 0 failed" "cap"
ok "refresh cap"

# 3. For real, with a stand-in refresh door that logs what it is asked.
py=""
for c in python3 python; do "$c" -c 'import sys; assert sys.version_info >= (3,)' 2>/dev/null && py=$c && break; done
[ -n "$py" ] || fail "python 3 is needed for the stand-in server"
port=$((20000 + RANDOM % 20000))
cat > "$tmp/door.py" <<'EOF'
import http.server, sys
class Door(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        with open(sys.argv[2], "a", encoding="utf-8") as log:
            log.write(f"{self.path} {self.headers.get('Origin')} {self.headers.get('Host')}\n")
        self.send_response(500 if "fail" in self.path else 200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")
    def log_message(self, *args):
        pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), Door).serve_forever()
EOF
"$py" "$tmp/door.py" "$port" "$tmp/door.log" & server_pid=$!
for _ in $(seq 50); do curl -fs -o /dev/null "http://127.0.0.1:$port/ready" && break; sleep 0.1; done
: > "$tmp/door.log"
out=$(run '101 sentinel\n' WARM_REFRESH_URL="http://127.0.0.1:$port")
has "$out" "5 answers refreshed, 3 purged, 0 failed" "real run"
asked=$(cat "$tmp/door.log")
has "$asked" "/api/scars?unseen=true&min_confidence=0.9&limit=120 https://www.blindspotin.tech api.blindspotin.tech" "real run"
has "$asked" "/api/stats/headline https://www.blindspotin.tech" "real run"
[ "$(wc -l < "$tmp/door.log")" -eq 5 ] || fail "real run: the door was asked $(wc -l < "$tmp/door.log") times, not 5"
for f in "$case_file" "$csv" "$odd" "$headline_other"; do [ ! -e "$f" ] || fail "real run kept $f"; done
for f in "$headline" "$fires" "$risk" "$render"; do [ -e "$f" ] || fail "real run deleted $f"; done
grep -qx "101" "$state/handled-job-runs" || fail "real run: job_run 101 not remembered"
ok "real run: refreshed through the door with the site's Origin and Host, purged the rest, remembered the run"

# 4. The next run does not repeat it; a new job (risk) refreshes only what it feeds.
: > "$tmp/door.log"
out=$(run '101 sentinel\n' WARM_REFRESH_URL="http://127.0.0.1:$port")
hasnt "$out" "finished" "second run"
[ ! -s "$tmp/door.log" ] || fail "second run asked the door again"
out=$(run '101 sentinel\n103 risk\n' WARM_REFRESH_URL="http://127.0.0.1:$port")
has "$out" "job risk finished (job_run 103)" "risk run"
hasnt "$out" "job sentinel" "risk run"
asked=$(cat "$tmp/door.log")
has "$asked" "/api/risk?limit=1" "risk run"
has "$asked" "/api/brief?days=7" "risk run"
hasnt "$asked" "/api/stats/headline" "risk run"
ok "handled runs are skipped; the risk run refreshes the risk and brief answers only"

# 5. A failed refresh keeps the old copy; an unreadable job_run changes nothing and is not fatal.
fail_key=$(put "/api/imagery/featured?fail=1")
out=$(run '104 featured\n' WARM_REFRESH_URL="http://127.0.0.1:$port")
has "$out" "refresh failed: /api/imagery/featured?fail=1" "failed refresh"
[ -e "$fail_key" ] || fail "a failed refresh deleted the old copy"
before=$(cat "$state/handled-job-runs")
out=$(env WARM_PSQL="false" WARM_CACHE_DIR="$cache" WARM_STATE_DIR="$state" WARM_SKIP_PAGES=1 bash "$warm" 2>&1) \
  || fail "warm.sh failed when job_run could not be read"
has "$out" "could not read job_run" "database down"
[ "$(cat "$state/handled-job-runs")" = "$before" ] || fail "the state changed while job_run could not be read"
ok "a failed refresh keeps the old copy; an unreadable job_run is logged and skipped"

echo "all warm.sh tests passed"
