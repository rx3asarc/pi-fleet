#!/bin/bash
# Invariant C guard: every live tmux server must be reachable at a socket path.
# Reads /proc and asks each socket who it is. Silent when clean.
out=$(/root/.pi/agent/bin/fleet_tmux_orphan_guard.py 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then
  mkdir -p /root/.fleet/logs
  printf '%s  VIOLATION (exit %s)\n%s\n\n' "$(date -u +%FT%TZ)" "$rc" "$out" >> /root/.fleet/logs/tmux-orphan.log
fi
