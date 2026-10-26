#!/usr/bin/env python3
"""pi_index — U2 of the Session Consolidation Protocol. THE CORE FIX.

Four tools each key sessions differently and therefore disagree:

    pi-peers   name            (drifts; 8 names map to 3-4 files each)
    pi-live    sessionId + pid (the durable record)
    pi-tmux    tmux name -> pane pid -> pi name
    fleet      realpath(worktree)

This module is ONE array, keyed on sessionId, that all of them agree on.

THE SEAM
  `build_index` is PURE: it takes four sources in their NATIVE shapes and returns
  the joined rows. It performs no I/O. What talks to the world (tmux, /proc, git,
  the registry) is NOT here, and must not be.

  The loader deliberately does NOT return sessionId-keyed maps. That would push
  identity reconciliation below the seam, where it could only be integration
  tested -- and identity reconciliation is precisely the bug being fixed.

THE CORE INSIGHT
  The join is on PID, not on name. `pane_pid` IS the pi process's pid. Because of
  that, the five drifted sessions resolve correctly on their own: the drifted name
  stops mattering, because it becomes an attribute of the row rather than its
  identity. Every tool that joins on NAME is wrong for exactly this reason.
"""

from __future__ import annotations

__all__ = ["build_index", "bare_tmux_shells", "pending_ask_of"]

# Row shape. sessionId is the identity; everything else is an attribute.
ROW_DEFAULTS = {
    "sessionId": None,
    "name": "",
    "pid": None,
    "tmux": None,
    "cwd": "",
    "phase": None,
    "detail": None,
    "pending_ask": None,
    "ctx_pct": None,
    "transcript": None,
    "lastPrompt": None,
    "registry_topic": None,
    "worktree": None,
    "branch": None,
    "merged": None,
    "unpushed": None,
    "updated_at": None,
}


# `pi-live` has no `pending_ask` field, but its `detail` states the condition.
# ONLY an in-flight `ask_user_question` means a human is actually blocking the
# session. `waiting for input` is emphatically NOT a pending ask: it is the normal
# idle state, because every turn ends by waiting for input. Counting it marked 45
# of 48 live sessions as needing attention -- which is the same as marking none,
# and would have made the top of the view useless.
_PENDING_ASK_HEADS = ("ask_user_question",)


def pending_ask_of(record: dict):
    """The pending ask, or None. Pure.

    NOTE on ctx_pct: context percentage is reported by the in-process extension
    API (`ctx.getContextUsage()`), which no external reader can reach. It is left
    as None on purpose -- a fabricated ctx% would silently disable the
    `exhausted` state, which is the one state that must never be missed.
    """
    detail = str(record.get("detail") or "")
    return detail if any(h in detail.lower() for h in _PENDING_ASK_HEADS) else None


def _by(rows, key):
    """Index a native-shape list by one field, skipping rows missing that field."""
    out = {}
    for row in rows or []:
        value = row.get(key)
        if value is not None and value != "":
            out[value] = row
    return out


def build_index(sources: dict) -> list:
    """Join the four native sources into one sessionId-keyed array.

    Pure: never mutates `sources`, never touches the outside world.
    Output is deterministically ordered so two runs are byte-identical.
    """
    panes = (sources or {}).get("panes") or []
    live = (sources or {}).get("live") or []
    registry = (sources or {}).get("registry") or []
    fleet = (sources or {}).get("fleet") or []

    # A pane is matched by PID -- this is the whole trick.
    pane_by_pid = _by(panes, "pane_pid")
    reg_by_sid = _by(registry, "sessionId")
    reg_by_name = _by(registry, "name")
    fleet_by_path = _by(fleet, "worktree")

    rows = []
    for record in live:
        sid = record.get("sessionId")
        if not sid:
            # No identity => not a session row. Bare shells are counted, not listed.
            continue

        pid = record.get("pid")
        pane = pane_by_pid.get(pid) or {}

        reg = reg_by_sid.get(sid) or reg_by_name.get(record.get("name")) or {}

        cwd = record.get("cwd") or pane.get("cwd") or ""
        wt = record.get("worktree") or cwd
        f = fleet_by_path.get(wt) or {}

        row = dict(ROW_DEFAULTS)
        row.update({
            "sessionId": sid,
            # The pi name is the truth. The tmux name is one attribute among many.
            "name": record.get("name") or reg.get("name") or "",
            "pid": pid,
            "tmux": pane.get("session_name"),
            "cwd": cwd,
            "phase": record.get("phase"),
            "detail": record.get("detail"),
            "pending_ask": pending_ask_of(record),
            "ctx_pct": None,
            # `updatedAt` is epoch ms. Absent, pi_view's recency tiebreak silently
            # falls through to alphabetical, so it is carried even though the
            # view tolerates its absence.
            "updated_at": record.get("updatedAt"),
            "transcript": record.get("transcript"),
            "lastPrompt": record.get("lastPrompt"),
            "registry_topic": reg.get("topic"),
            # The spawn-time declaration, if the spawner made one (U8). It lives
            # in the REGISTRY record (the identity store), not the live record.
            "taxonomy": reg.get("taxonomy"),
            "worktree": f.get("worktree"),
            "branch": f.get("branch"),
            "merged": f.get("merged"),
            # None (unknown) is NOT the same as 0 (nothing left to harvest).
            "unpushed": f.get("unpushed"),
        })
        # U7: measured context, keyed by tmux name (pi_sources.read_ctx_pct).
        # None when no footer is readable -- and None never becomes `exhausted`.
        row["ctx_pct"] = (sources.get("ctx_pct") or {}).get(row.get("tmux"))
        rows.append(row)

    rows.sort(key=lambda r: r["sessionId"])
    return rows


def bare_tmux_shells(sources: dict) -> list:
    """tmux sessions with no pi in them: no identity, so not index rows.

    Surfaced separately, and counted, because they are real tmux sessions that
    would otherwise look like missing sessions. (Observed live: review-main,
    review-falsifier, hermes, pi.)
    """
    live_pids = {r.get("pid") for r in ((sources or {}).get("live") or [])
                 if r.get("sessionId")}
    return sorted(
        p["session_name"]
        for p in ((sources or {}).get("panes") or [])
        if p.get("session_name") and p.get("pane_pid") not in live_pids
    )
