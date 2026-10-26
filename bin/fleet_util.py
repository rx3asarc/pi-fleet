"""fleet_util — the three primitives every fleet module needs.

`sh`, `die`, `clip`. They were defined inside `fleet`, which meant any module
that wanted to run a command had to import the CLI (a cycle) or reimplement it
(two behaviours). They are small, pure-ish and have no fleet knowledge at all,
so they live here and both halves import this rather than each other.
"""
from __future__ import annotations

import contextlib
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

import fleet_paths


def sh(args, cwd=None, check=False):
    """Run a command, return (rc, stdout, stderr). Never raises on rc!=0."""
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=120,
            errors="replace",
        )
        if check and p.returncode != 0:
            raise RuntimeError(f"{' '.join(map(str, args))}: {p.stderr.strip()}")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except FileNotFoundError:
        return 127, "", f"not found: {args[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"timeout: {' '.join(map(str, args))}"


# ── reality sources (never a state file) ─────────────────────────────────────
def tmux_panes():
    """[(session, cwd, command)] — the live truth about what exists."""
    rc, out, _ = sh(["tmux", "list-panes", "-a", "-F",
                     "#{session_name}\t#{pane_current_path}\t#{pane_current_command}"])
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            rows.append(tuple(parts[:3]))
    return rows




def clip(s, n: int) -> str:
    """Truncate to width n so columns cannot run together."""
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def die(msg):
    print(f"  error: {msg}", file=sys.stderr)
    sys.exit(1)


# ── the store lock ───────────────────────────────────────────────────────────
# Every verb that mutates the control dir does an unlocked read-modify-write of a
# whole JSON file. Cron, a human tick and any number of forks can overlap, and
# the loser's ledger row vanishes while its pane is already dead. One advisory
# lock around the mutate path, taken on a file that is never the store itself.
LOCK_PATH = Path(os.environ.get("FLEET_LOCK") or fleet_paths.control_dir() / ".lineage.lock")


_depth = 0


@contextlib.contextmanager
def store_lock(timeout=180.0):
    """Exclusive advisory lock around a store mutation.

    Re-entrant in-process, and inherited by children through the environment:
    a subprocess we start while holding the lock must NOT try to take it again
    (that is a deadlock, not a queue), because its parent is inside the critical
    section and will release it when the child is done.
    """
    global _depth
    inherited = os.environ.get("FLEET_LOCK_HELD") == "1"
    if _depth or inherited:
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
        return
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_PATH, "a+")
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.time() >= deadline:
                    # Do NOT close here: the finally would then unlock a closed
                    # fd and mask the real error with "I/O operation on closed
                    # file". Let finally do both.
                    raise SystemExit(
                        "another fleet process holds the store lock for more than "
                        f"{int(timeout)}s — refusing to mutate rather than guessing")
                time.sleep(0.2)
        fh.seek(0); fh.truncate()
        fh.write(f"{os.getpid()} {int(time.time())}\n"); fh.flush()
        os.environ["FLEET_LOCK_HELD"] = "1"
        _depth += 1
        try:
            yield
        finally:
            _depth -= 1
            os.environ.pop("FLEET_LOCK_HELD", None)
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()
