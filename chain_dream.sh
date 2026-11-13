#!/bin/bash
# Chained history dream: one gated dream per batch of <chain dir>/plan.json, each built on the last passing run's staged
# memory (start --after). Resumable: for each batch the newest attempt (<id>, <id>r2, <id>r3 ...) whose gate passed and
# whose predecessor matches is reused. One driver per chain dir. Never swaps.
# Usage: chain_dream.sh <chain dir>   (a <chain dir>/STOP file stops it before the next batch)
set -u
CH="$1"
HERE="$(dirname "$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$0")")"
O="${ONEIROS_PY:-$HERE/scripts/oneiros.py}"
RUNS="${ONEIROS_RUNS:-$HERE/runs}"
LOG="$CH/chain.log"
RES="$CH/results.jsonl"
PLAN="$CH/plan.json"
LOCKD="$CH/.driver.lock"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG"; }
gate_of() { python3 -c "import json,sys
try: print(json.load(open(sys.argv[1])).get('code', ''))
except Exception: print('')" "$1"; }
after_of() { python3 -c "import json,sys
try:
    r = json.load(open(sys.argv[1])); want = set(json.load(open(sys.argv[2]))[int(sys.argv[3])]['sids'])
    print((r.get('after') or '-') + ' ' + ('same' if set(r.get('sessions') or []) == want else 'differ'))
except Exception: print('? differ')" "$1" "$PLAN" "$2"; }
field() { python3 -c "import json,sys
try: print(json.load(sys.stdin).get(sys.argv[1], ''))
except Exception: print('')" "$1"; }

take_lock() {
  if mkdir "$LOCKD" 2>/dev/null; then echo $$ > "$LOCKD/pid"; return 0; fi
  old=$(cat "$LOCKD/pid" 2>/dev/null)
  if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then return 1; fi
  if [ -z "$old" ]; then  # another driver may sit between its mkdir and writing its pid
    age=$(( $(date +%s) - $(stat -c %Y "$LOCKD" 2>/dev/null || stat -f %m "$LOCKD" 2>/dev/null || date +%s) ))  # GNU first: GNU stat -f means file system
    [ "$age" -lt 10 ] && return 1
  fi
  mv "$LOCKD" "$LOCKD.stale.$$" 2>/dev/null || return 1     # one mover wins
  if [ "$(cat "$LOCKD.stale.$$/pid" 2>/dev/null)" != "$old" ]; then  # moved a lock someone just took: give it back
    mv "$LOCKD.stale.$$" "$LOCKD" 2>/dev/null; return 1
  fi
  rm -rf "$LOCKD.stale.$$"
  mkdir "$LOCKD" 2>/dev/null || return 1
  echo $$ > "$LOCKD/pid"
  log "stale driver lock (pid ${old:-none}) taken over"
}
take_lock || { echo "another driver runs this chain" >&2; exit 4; }
[ "$(cat "$LOCKD/pid" 2>/dev/null)" = "$$" ] || { echo "lost the driver lock" >&2; exit 4; }
trap 'rm -rf "$LOCKD"' EXIT

n=$(python3 -c "import json,sys; print(len(json.load(open(sys.argv[1]))))" "$PLAN") || { log "plan.json unreadable"; exit 2; }
[ -n "$n" ] && [ "$n" -gt 0 ] || { log "plan.json has no batches"; exit 2; }
prev=""
covered=" "
summary() {
  python3 - "$PLAN" "$covered" >> "$LOG" 2>&1 <<'EOF'
import json, sys
plan, covered = json.load(open(sys.argv[1])), set(sys.argv[2].split())
miss = [p for p in plan if p['id'] not in covered]
print('chain summary: %d of %d batches covered by this chain' % (len(plan) - len(miss), len(plan)))
for p in miss:
    print('  uncovered %s: %s to %s, %d sessions: %s' % (p['id'], p['since'], p['until'], p['n'], ' '.join(s[:8] for s in p['sids'])))
sys.exit(1 if miss else 0)
EOF
}
log "chain start (driver v3, pid $$): $n batches"
for i in $(seq 0 $((n - 1))); do
  read -r id since until want <<< "$(python3 -c "import json,sys; p=json.load(open(sys.argv[1]))[int(sys.argv[2])]; print(p['id'], p['since'], p['until'], p['n'])" "$PLAN" "$i")"
  if [ -z "${want:-}" ]; then log "batch $i unreadable in plan.json; stopping"; summary; exit 2; fi
  if ! [[ "$id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$ ]]; then log "batch $i has a bad id; stopping"; summary; exit 2; fi
  attempts="$id"; k=2
  while [ -d "$RUNS/${id}r$k" ]; do attempts="${id}r$k $attempts"; k=$((k + 1)); done
  reused=""
  for c in $attempts; do
    [ -f "$RUNS/$c/gate.json" ] || continue
    gc=$(gate_of "$RUNS/$c/gate.json")
    read -r base same <<< "$(after_of "$RUNS/$c/run.json" "$i")"
    [ "$base" = "-" ] && base=""
    if { [ "$gc" = "0" ] || [ "$gc" = "1" ]; } && [ "$base" = "$prev" ] && [ "$same" = "same" ]; then reused="$c"; break; fi
  done
  if [ -n "$reused" ]; then prev="$reused"; covered="$covered$id "; log "$reused already done (gate $gc, after ${base:-live}), reused"; continue; fi
  if [ -f "$CH/STOP" ]; then log "STOP file: chain stopped before $id"; summary; exit 3; fi
  rid="$id"; k=1
  while [ -d "$RUNS/$rid" ]; do k=$((k + 1)); rid="${id}r$k"; done
  [ "$rid" != "$id" ] && log "$id: no reusable attempt after ${prev:-live}; this attempt is $rid"
  after=()
  [ -n "$prev" ] && after=(--after "$prev")
  out=$(python3 "$O" start --id "$rid" --since "$since" --until "$until" "${after[@]+"${after[@]}"}" 2>>"$LOG"); code=$?
  echo "$out" > "$CH/$rid.start.json"
  log "$rid start code=$code sessions=$(echo "$out" | field sessions) (plan $want) after=${prev:-live}"
  if [ "$code" -ne 0 ]; then echo "{\"id\": \"$rid\", \"batch\": \"$id\", \"step\": \"start\", \"code\": $code}" >> "$RES"; continue; fi
  python3 - "$PLAN" "$i" "$RUNS/$rid/run.json" >> "$LOG" 2>&1 <<'EOF'
import json, sys
plan = set(json.load(open(sys.argv[1]))[int(sys.argv[2])]['sids'])
got = set(json.load(open(sys.argv[3]))['sessions'])
if plan != got:
    print('  WARNING session mismatch: missing %s, extra %s' % (sorted(plan - got), sorted(got - plan)))
EOF
  failed=""
  for step in analyze consolidate; do
    python3 "$O" "$step" "$rid" > "$CH/$rid.$step.json" 2>>"$LOG"; code=$?
    if [ "$code" -ne 0 ]; then
      log "$rid $step code=$code, retry with --workers 3"
      python3 "$O" "$step" "$rid" --workers 3 > "$CH/$rid.$step.json" 2>>"$LOG"; code=$?
    fi
    log "$rid $step code=$code"
    if [ "$code" -ne 0 ]; then failed="$step"; break; fi
  done
  if [ -z "$failed" ]; then
    python3 "$O" apply "$rid" > "$CH/$rid.apply.json" 2>>"$LOG" || failed="apply"
  fi
  if [ -z "$failed" ]; then
    python3 "$O" gate "$rid" > "$CH/$rid.gate.json" 2>>"$LOG"
    gc=$(gate_of "$RUNS/$rid/gate.json")
    [ -z "$gc" ] && failed="gate"
  fi
  if [ -n "$failed" ]; then
    log "$rid FAILED at $failed; batch $id stays uncovered; the next batch chains after ${prev:-live}"
    echo "{\"id\": \"$rid\", \"batch\": \"$id\", \"step\": \"$failed\", \"code\": 2}" >> "$RES"; continue
  fi
  python3 "$O" report "$rid" > "$CH/$rid.report.json" 2>>"$LOG"
  applied=$(field applied < "$CH/$rid.apply.json")
  log "$rid applied=$applied gate=$gc"
  echo "{\"id\": \"$rid\", \"batch\": \"$id\", \"step\": \"done\", \"gate\": $gc, \"applied\": ${applied:-0}, \"after\": \"${prev:-live}\"}" >> "$RES"
  if [ "$gc" = "0" ] || [ "$gc" = "1" ]; then prev="$rid"; covered="$covered$id "
  else log "$rid withheld by its gate; batch $id stays uncovered; the next batch chains after ${prev:-live}"; fi
done
summary; uncovered=$?
log "chain done; last run in the chain ${prev:-none}$( [ "$uncovered" -ne 0 ] && echo '; some batches are uncovered (see the summary)')"
exit "$uncovered"
