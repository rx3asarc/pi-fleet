#!/bin/bash
# Every path is resolved from the environment the fleet itself uses, so the cron
# line works wherever the package was installed:
#   FLEET_BIN_DIR      the directory these CLIs live in (default: this script's own)
#   FLEET_CONTROL_DIR  the store (default: $HOME/.fleet)
BIN="${FLEET_BIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
STATE="${FLEET_CONTROL_DIR:-${HOME}/.fleet}"
# Invariant C guard: every live tmux server must be reachable at a socket path.
# Reads /proc and asks each socket who it is. Silent when clean.
out=$("$BIN/fleet_tmux_orphan_guard.py" 2>&1); rc=$?
if [ "$rc" -ne 0 ]; then
  mkdir -p "$STATE/logs"
  printf '%s  VIOLATION (exit %s)\n%s\n\n' "$(date -u +%FT%TZ)" "$rc" "$out" >> "$STATE/logs/tmux-orphan.log"
fi
