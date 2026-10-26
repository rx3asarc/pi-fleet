#!/usr/bin/env python3
"""Plan acknowledgement: the one gate on the interface that has none.

WHY (2026-10-26, pi-moa review objection 2). I had built gates on everything downstream
of the plan and none on the plan itself — the interface I had just called the quality
multiplier. The failure it permits is quiet: the plan mis-frames the task, every worker
produces impeccable evidence for the wrong thing, and the deterministic rollup faithfully
reports that the wrong thing was accomplished. Nothing is UNSURE. Everyone is correct
about a mis-framed problem.

Two constraints pull against each other and both are real:

  * A fleet-wide mis-frame costs far more than one human round-trip, so there must be a
    gate.
  * Blocking an autonomous lead on a human is exactly how `nordisk-main` froze for ~18h
    on 2026-10-15/16 on a UI selector whose conservative answer was going to be taken
    anyway.

The resolution is a BOUNDED window, not a blocking wait:

  awaiting -> a human-attached caller has `minutes` to ack or reject;
              expiry proceeds and RECORDS `plan_unacked`, so the rollup says so.
  acked    -> proceed immediately.
  not-required -> a non-human caller never waits: there is nobody to ask.

The unacked state is not a shrug. It travels into the rollup's header, which re-quotes
the original task next to what was delivered — so a mis-frame is visible at delivery
rather than at autopsy.
"""
from __future__ import annotations

import time

DEFAULT_MINUTES = 15


def arm(rec, now=None, minutes=DEFAULT_MINUTES, required=True):
    """Record that the plan has been sent up, and when the window closes."""
    now = int(now or time.time())
    out = dict(rec)
    out["plan_sent_at"] = now
    if required:
        out["plan_ack_deadline"] = now + int(minutes * 60)
        out["plan_ack_state"] = "awaiting"
    else:
        out["plan_ack_deadline"] = None
        out["plan_ack_state"] = "not-required"
    return out


def ack(rec, now=None, by=None, note=None):
    """The caller confirmed the plan. Clears the flag; keeps the history."""
    now = int(now or time.time())
    out = dict(rec)
    out["plan_ack_state"] = "acked"
    out["plan_acked_at"] = now
    out["plan_acked_by"] = by
    if note:
        out["plan_ack_note"] = str(note)[:500]
    out["plan_unacked"] = False
    return out


def reject(rec, now=None, by=None, note=None):
    """The caller says the plan is wrong. The fleet stops before spawning."""
    now = int(now or time.time())
    out = dict(rec)
    out["plan_ack_state"] = "rejected"
    out["plan_acked_at"] = now
    out["plan_acked_by"] = by
    out["plan_reject_note"] = str(note or "")[:500]
    return out


def state(rec, now=None):
    """One of: acked | rejected | awaiting | expired | not-required | none."""
    now = int(now or time.time())
    rec = rec or {}
    st = rec.get("plan_ack_state")
    if st == "rejected":
        return "rejected"
    if rec.get("plan_acked_at"):
        return "acked"
    if st == "not-required":
        return "not-required"
    dl = rec.get("plan_ack_deadline")
    if st == "awaiting" and dl:
        return "awaiting" if now < int(dl) else "expired"
    if st == "awaiting":
        return "awaiting"
    return "none"


def can_spawn(rec, now=None):
    """May this lead spawn workers right now? Returns (ok, why)."""
    now = int(now or time.time())
    st = state(rec, now)
    if st == "rejected":
        return False, ("the plan was rejected by the caller — do not spawn; re-plan "
                       "against the note and send it up again")
    if st == "awaiting":
        left = max(0, int(rec.get("plan_ack_deadline") or now) - now)
        return False, (f"awaiting plan acknowledgement ({left}s left). Spawning before "
                       f"an ack means acting on a plan nobody has read")
    return True, ""


def expire(rec, now=None):
    """Turn an elapsed window into a recorded fact rather than an absence."""
    now = int(now or time.time())
    if state(rec, now) != "expired":
        return dict(rec)
    out = dict(rec)
    out["plan_unacked"] = True
    out["plan_ack_state"] = "expired"
    out["plan_expired_at"] = now
    return out
