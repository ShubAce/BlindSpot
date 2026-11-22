#!/usr/bin/env bash
# blindspot-warm.timer: request the website's main pages on the server itself, so that their API
# answers are fetched through the cache (nginx-api-cache.conf) before a visitor needs them. An
# answer still within its cache time (s-maxage: until the next job clock point in
# services/api/blindspot/api/cache.py) comes straight from the cache; an expired one is refreshed
# in the background while the old copy is served; one not cached yet (new parameters, evicted) is
# built now, by this request rather than by a visitor.
set -u
for path in / /live /bulletin /brief /method; do
  curl -fsS -o /dev/null --max-time 120 "http://127.0.0.1:3000${path}" || echo "warm: ${path} did not answer"
done
