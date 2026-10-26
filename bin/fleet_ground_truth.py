#!/usr/bin/env python3
"""Ground truth, and the store as a cache of it.

WHY (2026-10-26, pi-moa review objection 3, and this box's own measurements).
The lineage store was being read as *the* truth about who is alive. It held 572
liveness records for 31 live sessions, four names mapped to both a dead and a live
session, and 17 records whose "identity" was a process id. A manager that reads such a
store reaps unfinished work and keeps dead work — and does it confidently, because
nothing in the design let the world contradict the store.

The inversion is small and it is the whole fix:

    GROUND TRUTH  = the tmux server, the git worktree list, and /proc.
    THE STORE     = a cache of that, allowed to be wrong, rebuildable at any time.

Two rules fall out, and both are enforced here rather than in prose:

  1. Identity is `sessionId`. A pid is a *hint* that must be confirmed by the record's
     own sessionId. A pid that now belongs to a different session is `STALE_PID` —
     never a silent re-identification. (That silent re-identification is what paired
     a live pid with a dead record's sessionId and produced the 17 phantom records.)
  2. A cache may be corrected, never reaped. Rows the world does not confirm are
     marked `stale: true` with a reason and kept: a cache entry is history, and
     deleting history to make a listing tidy is how the backlog became unexplainable.

Nothing here kills anything or deletes a record.
"""
from __future__ import annotations

import os
import subprocess
import time


def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def truth_from(pisources, panes=None, alive=_alive):
    """The live set, keyed by sessionId, derived from the world.

    `pisources` is passed in so tests can supply a fixture; the CLI passes the real
    module. Nothing below reads the lineage store — that is the point.
    """
    rows = {}
    for r in (pisources.live_records(live_only=True) or []):
        sid = r.get("sessionId")
        if not sid:
            continue
        pid = r.get("pid")
        rows[sid] = {
            "sessionId": sid,
            "name": r.get("name"),
            "pid": pid,
            "alive": bool(alive(pid)),
            "transcript": r.get("transcript"),
            "source": "liveness",
        }
    # A pane proves a tmux session exists; it does not by itself name a sessionId,
    # so it is used to CONFIRM a row and to spot an owner mismatch, never to invent
    # one. Inventing identity from a name is the bug this module exists to end.
    for p in (panes if panes is not None else (pisources.read_panes() or [])):
        sid = p.get("sessionId") or p.get("id")
        tmux = p.get("session") or p.get("tmux")
        if sid and sid in rows:
            rows[sid]["tmux"] = tmux
            rows[sid]["pid_in_pane"] = p.get("pid")
    return rows


def reconcile(store_children, truth, alive=_alive, now=None):
    """Compare a cached store against the world. Pure: no writes, no clock.

    Returns a report with four named sets. Each entry carries the reason, because
    "stale" without a reason is indistinguishable from a bug.
    """
    now = int(now or time.time())
    report = {"checked_at": now, "live": sorted(truth), "stale_rows": [],
              "stale_pid": [], "unrecorded_live": [], "pid_owner":
              {}}
    live_pids = {}
    for sid, row in truth.items():
        if row.get("pid"):
            live_pids[int(row["pid"])] = sid

    for key, rec in (store_children or {}).items():
        if rec.get("reaped_at") or rec.get("closed_at"):
            continue                      # terminal: history, not a claim about now
        sid = rec.get("sessionId")
        pid = rec.get("pid")
        if sid in truth:
            # alive by its own id — the only confirmation that counts
            if pid and int(pid) != int(truth[sid].get("pid") or 0):
                report["pid_owner"][str(sid)] = {
                    "recorded": pid, "now": truth[sid].get("pid"),
                    "reason": "the record's pid differs from the live pid for this "
                              "sessionId; the session is alive, the pid is history"}
            continue
        if pid and int(pid) in live_pids:
            other = live_pids[int(pid)]
            report["stale_pid"].append({
                "key": key, "sessionId": sid, "pid": pid,
                "now_owned_by": other,
                "reason": "the recorded pid is alive but belongs to a DIFFERENT "
                          "sessionId — pid reuse, never a re-identification"})
            continue
        # A record that SAYS it ended is terminal evidence in its own right: an
        # alive pid on an `ended` record is a recycled pid, not a revived session
        # (measured: nordisk-ads' record ended cleanly while its tmux name was
        # reused by a new pi). The record's own statement outranks pid aliveness.
        if str(rec.get("phase") or "") == "ended" or rec.get("exitClean") is True:
            report["stale_rows"].append({
                "key": key, "sessionId": sid, "name": rec.get("name"), "pid": pid,
                "reason": "the record itself says ended (any live pid is a reuse)"})
            continue
        if pid and alive(pid):
            continue                      # alive pid, no resolvable id: not evidence
        # nothing in the world confirms it
        report["stale_rows"].append({
            "key": key, "sessionId": sid, "name": rec.get("name"),
            "pid": pid,
            "reason": ("no live session with this id"
                       if sid else "record has no sessionId at all")})

    known = {(rec.get("sessionId") or k) for k, rec in (store_children or {}).items()}
    for sid in truth:
        if sid not in known:
            report["unrecorded_live"].append({
                "sessionId": sid, "name": truth[sid].get("name"),
                "pid": truth[sid].get("pid"),
                "reason": "live session with no lineage record (informational: "
                          "`fleet scan` enrols these as protected orphans)"})
    return report


def summarise(report):
    """One-line-per-finding, for a human. No model, no interpretation."""
    lines = [f"  live sessions (ground truth): {len(report['live'])}",
             f"  stale rows     : {len(report['stale_rows'])}",
             f"  stale pids     : {len(report['stale_pid'])}",
             f"  pid/owner notes: {len(report['pid_owner'])}",
             f"  unrecorded live: {len(report['unrecorded_live'])}"]
    for row in report["stale_rows"][:20]:
        lines.append(f"    STALE   {str(row['name'] or row['key'])[:24]:<24} "
                     f"{row['reason']}")
    for row in report["stale_pid"][:20]:
        lines.append(f"    STALE-PID {str(row['name'] or row['key'])[:20]:<20} pid "
                     f"{row['pid']} now belongs to {str(row['now_owned_by'])[:8]}")
    return "\n".join(lines)


def apply(store_children, report, now=None):
    """Mark what the world does not confirm. Never deletes, never re-identifies."""
    now = int(now or time.time())
    out = dict(store_children or {})
    touched = 0
    marks = {}
    for row in report["stale_rows"]:
        marks[row["key"]] = ("stale", row["reason"])
    for row in report["stale_pid"]:
        marks[row["key"]] = ("stale_pid", row["reason"])
    for key, (kind, reason) in marks.items():
        rec = dict(out.get(key) or {})
        if not rec:
            continue
        rec["stale"] = True
        rec["stale_kind"] = kind
        rec["stale_reason"] = reason
        rec["stale_at"] = now
        out[key] = rec
        touched += 1
    return out, touched


def worktrees(repos):
    """`git worktree list` for each repo that still exists. Ground truth for disk."""
    out = {}
    for repo in repos:
        if not repo or not os.path.isdir(str(repo)):
            continue
        try:
            p = subprocess.run(["git", "-C", str(repo), "worktree", "list",
                                "--porcelain"], capture_output=True, text=True,
                               timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue
        paths = [ln.split(" ", 1)[1] for ln in (p.stdout or "").splitlines()
                 if ln.startswith("worktree ")]
        if paths:
            out[str(repo)] = paths
    return out
