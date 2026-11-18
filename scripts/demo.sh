#!/usr/bin/env bash
# End-to-end demo against a running API (default http://127.0.0.1:8000).
# Replays: import -> publish fails (all missing) -> moisture -> publish ->
# late moisture (stale) -> republish -> correction (stale) -> republish ->
# error settings -> error-range report -> settings revision (report stale).
set -euo pipefail

BASE="${1:-http://127.0.0.1:8000}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EX="$HERE/../examples"

step() { printf '\n=== %s ===\n' "$1"; }
latest_id() { curl -s "$BASE/$1/latest" | python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])'; }

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

step "8. install measurement-error settings (shared gain + per-plot errors)"
curl -s -X PUT "$BASE/error-settings" -H 'Content-Type: application/json' -d @"$EX/error_settings.json"

step "9. error-range report: reachable B-A bounds and sign determination"
PID="$(latest_id publications)"
curl -s -X POST "$BASE/publications/$PID/error-reports"

step "10. revise the gain error -> previous report flagged stale"
python3 -c 'import json; d=json.load(open("'$EX'/error_settings.json")); d["gain_error"]=0.03; print(json.dumps(d))' \
  | curl -s -X PUT "$BASE/error-settings" -H 'Content-Type: application/json' -d @-
curl -s "$BASE/error-reports"

step "11. a fresh correction -> reports also carry source_data_changed (old rows untouched)"
curl -s -X POST "$BASE/corrections" -H 'Content-Type: application/json' \
  -d '{"corrections":[{"block_id": 1, "treatment": "A", "fresh_weight": 43.0}]}'
curl -s "$BASE/error-reports"

step "12. publication history (only the newest is current)"
curl -s "$BASE/publications"
echo
