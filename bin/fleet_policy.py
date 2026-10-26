#!/usr/bin/env python3
"""U6 — three policies that keep a fleet from producing confident wrong output.

WHY (2026-10-26, pi-moa review objections 7, 9, 13).

7.  Worker-to-worker intercom is OFF by default. The variance argument that justifies
    fan-out in the first place inverts under shared visibility: worker 1's answer
    becomes worker 2's input, five workers converge, and the rollup prints no conflict
    *because everyone agreed, wrongly*. Lead-mediated only; a unit may opt in and the
    opt-in is logged.

9.  Reap on the event, not the timer. A 10-minute tick is blind to a worker that
    finished in twenty seconds, and a wrong-and-exited worker burns its whole window
    before anyone notices. `fleet done` triggers the same gate immediately; the tick is
    demoted to a backstop and says so.

13. `protected` is a backdoor unless it is audited: whoever sets it dodges reaping and
    the evidence gates. Only a human-attached session may set it, the setter is
    recorded, and a fleet child attempting it is refused. (The un-audited version is
    how `fleet adopt --parent X` once enrolled a child nothing could ever reap.)
"""
from __future__ import annotations

import time


def may_message(sender_rec, target_rec):
    """Default-deny worker-to-worker. Returns (ok, why).

    Allowed: anyone to a lead (reporting upward is the point), a lead to its own
    worker, anyone to a non-fleet session, and an explicit opt-in per unit.
    """
    t = target_rec or {}
    s = sender_rec or {}
    if not t.get("parent_session_id"):
        return True, ""                      # not a fleet child: ordinary session
    if t.get("intercom_optin"):
        return True, ""                      # explicitly opted in, logged by the caller
    if s.get("sessionId") and t.get("parent_session_id") == s.get("sessionId"):
        return True, ""                      # the parent talking to its own child
    if s.get("sessionId") and t.get("parent_session_id") == s.get("parent_session_id"):
        return False, ("refused: worker-to-worker messaging is off by default. Shared "
                       "visibility collapses fan-out into consensus — if this unit "
                       "needs to talk to a peer, the plan was wrong. Ask the lead.")
    return False, ("refused: this target is a fleet child. Message its lead instead "
                   "(the lead owns the unit and can re-plan).")


def may_protect(sender_rec, human_attached):
    """Only a human-attached session may make a record unsweepable."""
    s = sender_rec or {}
    if s.get("parent_session_id"):
        return False, ("refused: a fleet child may not protect itself (or anything "
                       "else) from the evidence gate")
    if not human_attached:
        return False, ("refused: only a human-attached session may set `protected` — "
                       "it is the one flag that suspends reaping, so it needs a human")
    return True, ""


def stamp_protection(rec, by, why=None, now=None):
    """Record who suspended reaping and why. No silent protection."""
    now = int(now or time.time())
    out = dict(rec)
    out["protected"] = True
    out["protected_set_by"] = by
    out["protected_set_at"] = now
    if why:
        out["protected_reason"] = str(why)[:400]
    return out


def human_attached(rec):
    """Is this session a human conversation?

    Defined narrowly on purpose: a session that some human is typing into, or one
    reached over the Telegram bridge. Never a fleet child (checked first by callers),
    never "a session that has been alive a while".
    """
    rec = rec or {}
    if rec.get("parent_session_id"):
        return False
    if rec.get("human_attached") is True:
        return True
    tmux = str(rec.get("tmux") or rec.get("session") or "")
    return any(tag in tmux for tag in ("pi-root", "tg", "telegram"))
