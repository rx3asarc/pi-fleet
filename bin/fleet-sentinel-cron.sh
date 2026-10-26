#!/usr/bin/env bash
# fleet-sentinel-cron — cron-safe wrapper around `fleet sentinel --apply`.
#
# WHY A WRAPPER AND NOT A BARE CRON LINE
#   * cron's PATH does not include the fleet's own bin directory, so `fleet`
#     (and pi-msg, which the sentinel may call to ask a parent to decide) are not
#     resolvable. `$FLEET_BIN_DIR` (default: this script's directory) is prepended
#     to an absolute PATH for exactly that reason.
#   * `fleet sentinel --apply` reads $FLEET_CONTROL_DIR/lineage.json, mutates it, then
#     writes it back (read-modify-write). Two overlapping ticks can lose an
#     update and, in the worst case, act twice on the same child. An atomic
#     mkdir lock makes "never two ticks at once" a guarantee, not a hope.
#   * cron has no stdout; the log is the only evidence a tick happened. It is
#     append-only and never rotated by this script.
#
# SAFETY
#   This wrapper does not decide anything. It runs the sentinel, which proposes
#   a verdict per child and gates every kill through fleet_gate. It cannot reap
#   a child that does not pass that gate (merged/clean/idle/reported).
#
# STALE LOCK: the lock records its pid. It is treated as stale only when that
#   pid is dead, or when it is older than STALE_AFTER_S (default 1800s = 3x the
#   10-minute cadence) — a live-but-hung tick is eventually taken over, and the
#   takeover is logged loudly so it is never silent.
#
# EXIT: the sentinel's own exit code (0 = something was noted/acted, 2 = the
#   lineage store is empty, so there was nothing at all to decide). Both are
#   benign; the log line says which.

set -u

# The package may live anywhere; the environment says where, and these are the
# same variables fleet_paths reads in python.
BIN="${FLEET_BIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
AGENT_HOME="${PI_AGENT_HOME:-${HOME}/.pi/agent}"
PATH=$BIN:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
export HOME="${HOME:-$(getent passwd "$(id -u)" | cut -d: -f6)}"

STATE=${FLEET_STATE:-${FLEET_CONTROL_DIR:-${HOME}/.fleet}}
LOCK=$STATE/locks/sentinel.lock
LOG=$STATE/logs/sentinel-cron.log
IDLE_AFTER_MIN=${IDLE_AFTER_MIN:-45}
STALE_AFTER_S=${STALE_AFTER_S:-1800}
# TWO INDEPENDENT SWITCHES, ADDED 2026-10-26.
#   FLEET_SWEEPS   which pre-tick sweeps run:
#                  "reconcile,records,lineage" (default), any subset, or "off". The sweeps MUTATE two stores
#                  every 10 minutes; a cron that deletes things needs a way to
#                  stop doing that without editing a live script.
#   PAUSE file     `pi-sentinel pause` already stops the U9 pump. The U11 tick
#                  used to ignore it, so "paused" meant "half paused". Honouring
#                  the same file makes the kill switch mean what it says.
SWEEPS=${FLEET_SWEEPS:-reconcile,records,lineage}
PAUSE_FILE=${FLEET_PAUSE_FILE:-${AGENT_HOME}/../advisories/PAUSE}

want() { case ",$SWEEPS," in *,off,*) return 1;; *,$1,*) return 0;; *) return 1;; esac; }

mkdir -p "$STATE/locks" "$STATE/logs"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log() { printf '%s %s\n' "$(ts)" "$*" >>"$LOG"; }

now=$(date +%s)
if ! mkdir "$LOCK" 2>/dev/null; then
  pid=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  lock_mtime=$(stat -c %Y "$LOCK" 2>/dev/null || echo "$now")
  age=$(( now - lock_mtime ))
  # A LIVE PID IS NEVER STALE. Measured 2026-10-26: a tick that took 16+ minutes
  # (it was building liveness facts for 460 closed records) crossed the 1800s
  # threshold, so the next cron slot "reclaimed" the lock and started a SECOND
  # sentinel against a store the first one was still mutating. Age is evidence of
  # a hang only when the holder cannot be found or is not running; otherwise two
  # read-modify-write cascades race and updates are lost silently.
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    log "SKIP  a tick is live (pid $pid, lock age ${age}s) — a live holder is never reclaimed by age"
    exit 0
  fi
  if [ -z "$pid" ] && [ "$age" -lt "$STALE_AFTER_S" ]; then
    log "SKIP  lock exists with no pid recorded (age ${age}s) — too young to judge, not taking over"
    exit 0
  fi
  log "STALE lock reclaimed (pid ${pid:-unknown} is DEAD, age ${age}s) — taking over"
  rm -rf "$LOCK"
  if ! mkdir "$LOCK" 2>/dev/null; then
    log "SKIP  lock still held after stale reclaim — not starting a second"
    exit 0
  fi
fi

printf '%s\n' "$$" >"$LOCK/pid"
cleanup() { rm -rf "$LOCK"; }
trap cleanup EXIT INT TERM

if [ -e "$PAUSE_FILE" ]; then
  log "SKIP  kill switch present ($PAUSE_FILE) — no sweep, no tick"
  exit 0
fi

log "START pid $$ idle-after-min $IDLE_AFTER_MIN sweeps=$SWEEPS"

# TEST SEAM, not a production switch. Lock semantics (take over a DEAD holder,
# never a live one) are the one part of this script that has caused a real
# incident, and they can only be tested by running the script. Exiting here lets
# the test prove the lock without also running a tick against a live store.
if [ "${FLEET_CRON_EXIT_AFTER_LOCK:-0}" = "1" ]; then
  log "TEST  exit-after-lock (FLEET_CRON_EXIT_AFTER_LOCK=1)"
  exit 0
fi

# GROUND-TRUTH RECONCILE (added 2026-10-26). Runs FIRST, because the other two
# sweeps judge the store and the store is only a cache: tmux + /proc are the truth.
# Marks rows the world does not confirm as stale (no deletes, backs up first), so a
# later sweep reasons about a cache that has at least been checked against reality.
# The identity rule lives here: a pid is a hint confirmed by sessionId, never a
# re-identification.
if want reconcile; then
  rrec=$(fleet reconcile --apply 2>&1)
  printf '%s\n' "$rrec" | grep -E 'live sessions|stale rows|stale pids|unrecorded live|marked [0-9]+ row|backup:' \
    | while IFS= read -r line; do log "  reconcile: $line"; done
fi
# DEAD-RECORD SWEEP (added 2026-10-26). The liveness store had 572 records for
# 31 live sessions because nothing ran the reaper on a schedule. Every watcher
# then scanned 540 corpses on every tick, and the record of a session that DID
# end sat next to its corpse-mates, so "who is live" was a manual judgement.
#
# Safe to automate: the reaper deletes only records whose pid is dead (zombies
# count as dead), never touches a transcript, backs up to a verified tarball
# first, and refuses to delete at all if any target is missing from the archive.
# It runs BEFORE the scan so the scan judges a clean store.
REAPER="${AGENT_HOME}/session-watch/tools/reap-dead-records.py"
if [ -x "$REAPER" ] && want records; then
  sweep=$("$REAPER" --apply 2>&1)
  printf '%s\n' "$sweep" | grep -E 'records total|LIVE \(kept\)|DEAD \(targets\)|backup:|removed :|after   :|ABORT|note:' \
    | while IFS= read -r line; do log "  sweep: $line"; done
fi
# LINEAGE SWEEP (added 2026-10-26). Same argument one store over: a record whose
# session is gone can only ever return HOLD, so it is bookkeeping debt that
# accumulates every time a session ends. Running the close here is what makes
# the 16-minute tick impossible rather than merely slow -- the tick's cost is
# proportional to OPEN records only if the closed ones are actually closed.
# `sweep-lineage` never kills: closed_at + a disposition, reopenable with
# `fleet revive`. Dry-run first is the default and stays that way for humans.
if want lineage; then
  lsweep=$(fleet sweep-lineage --apply 2>&1)
  printf '%s\n' "$lsweep" | grep -E 'records total|duplicates to merge|dead to close|adopted to unprotect|applied:|records after' \
    | while IFS= read -r line; do log "  lsweep: $line"; done
fi
# Enrol anything the lineage store has never seen BEFORE judging the fleet, so a
# session spawned by a door we do not control is at least visible. Adopted
# orphans are `protected`: they can be seen and reported on, never swept.
scan=$(fleet scan --apply 2>&1)
while IFS= read -r line; do
  [ -n "$line" ] && log "  scan: $line"
done <<EOF
$scan
EOF
# PRE-TICK SNAPSHOT (added 2026-10-17, after fork-2's fault suite proved that a
# truncated lineage.json loads as an EMPTY store, and the next write then erases
# the fleet). The code fix belongs in load_lineage; this is the belt that holds
# until it lands: one copy per distinct store, before anything mutates it.
#
# Cheap by design -- a sha compare, and a copy only when the store actually
# changed since the last tick. Keeps 40, deletes older.
STORE=$STATE/lineage.json
SNAPDIR=$STATE/snapshots
mkdir -p "$SNAPDIR"
if [ -f "$STORE" ]; then
  cur=$(sha256sum "$STORE" 2>/dev/null | cut -d' ' -f1)
  last=$(cat "$SNAPDIR/.last-sha" 2>/dev/null || true)
  if [ -n "$cur" ] && [ "$cur" != "$last" ]; then
    cp -p "$STORE" "$SNAPDIR/lineage-$(date -u +%Y%m%dT%H%M%SZ)-${cur:0:8}.json" 2>/dev/null \
      && printf '%s' "$cur" > "$SNAPDIR/.last-sha"
    ls -1t "$SNAPDIR"/lineage-*.json 2>/dev/null | tail -n +41 | xargs -r rm -f
  fi
fi

out=$(fleet sentinel --apply --idle-after-min "$IDLE_AFTER_MIN" 2>&1)
rc=$?
while IFS= read -r line; do
  [ -n "$line" ] && log "  $line"
done <<EOF
$out
EOF
if [ "$rc" -eq 2 ]; then
  log "END   pid $$ rc 2 (nothing in the lineage store to decide — benign)"
else
  log "END   pid $$ rc $rc"
fi
exit "$rc"
