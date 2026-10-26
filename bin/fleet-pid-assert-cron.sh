#!/bin/bash
# Invariant A guard: a finished ledger row must have no live process.
# Reads /proc, never panes. Silent when clean; appends only on violation.
out=$(/root/.pi/agent/bin/fleet_pid_assert.py 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then
  mkdir -p /root/.fleet/logs
  printf '%s  VIOLATION (exit %s)\n%s\n\n' "$(date -u +%FT%TZ)" "$rc" "$out" >> /root/.fleet/logs/pid-assert.log
fi
