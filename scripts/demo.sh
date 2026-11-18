#!/usr/bin/env bash
# End-to-end demo against a running API (default http://127.0.0.1:8000).
# Replays: import -> publish fails (all missing) -> moisture -> publish ->
# late moisture (stale) -> republish -> correction (stale) -> republish.
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EX="$HERE/../examples"

step() { printf '\n=== %s ===\n' "$1"; }

step "1. import 6 blocks (area + fresh weight)"
curl -s -X POST "$BASE/imports" -H 'Content-Type: application/json' -d @"$EX/import.json"

step "2. publish with no moisture recorded -> 422, nothing published"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/publications"

step "3. record moisture for blocks 1-4"
curl -s -X POST "$BASE/moisture" -H 'Content-Type: application/json' -d @"$EX/moisture.json"

step "4. publish: 4 complete blocks, blocks 5-6 pending"
curl -s -X POST "$BASE/publications"

step "5. late moisture for blocks 5-6 -> previous conclusion now stale"
curl -s -X POST "$BASE/moisture" -H 'Content-Type: application/json' -d @"$EX/moisture_late.json"
curl -s "$BASE/publications"

step "6. republish with all 6 blocks"
curl -s -X POST "$BASE/publications"

step "7. correction: block 3 B moisture 0.64 -> 0.70, then republish"
curl -s -X POST "$BASE/corrections" -H 'Content-Type: application/json' -d @"$EX/correction.json"
curl -s -X POST "$BASE/publications"

step "8. publication history (only the newest is current)"
curl -s "$BASE/publications"
echo
