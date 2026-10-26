#!/usr/bin/env python3
"""The independent reader, and the check nobody's unit owns.

WHY (2026-10-26, pi-moa review objection 6). An evidence gate proves an artifact EXISTS,
not that it is correct. And a second reader is theatre if it reads the same text as the
worker: correlated inputs produce correlated errors, so the "independent" review becomes
a confidence amplifier for a shared misunderstanding.

The correlation source that matters is the BRIEF. If both the worker and its reader read
the same distilled brief, they inherit the same framing blind spot. So this module gives
the reader a different vantage rather than a different model:

  * it reads the ORIGINAL task, not the plan slice and not the brief;
  * it is briefed adversarially ("find the input that breaks this"), never shown the
    worker's reasoning or its self-authored tests;
  * it runs in a tree the worker never touched.

A different model family is a supplement, not the mechanism: two capable models trained
on overlapping corpora share blind spots. The mechanism is that the two readers are
asking different questions about different documents.

`integration` is the second denominator: two units can each pass and still not compose.
It is a whole-fleet gate, owned by no unit, and the rollup refuses to look complete
while it is UNRUN.
"""
from __future__ import annotations

import time


def brief(task, child_name, artifact=None, unit_title=None):
    """The adversary's brief. Built from the ORIGINAL task only.

    Deliberately absent: the plan slice, the worker's narrative, the worker's claims,
    and any mention of the worker's own tests. If it were here, agreement with the
    worker would mean nothing.
    """
    L = [f"# Adversarial review — {child_name}", "",
         "You are the second reader. Another agent claims it finished the work below.",
         "Your job is not to confirm it. Your job is to find where it is wrong.", "",
         "## The task the human actually asked for (verbatim)", "",
         str(task or "(not recorded — treat that as a finding in itself)").strip(), ""]
    if unit_title:
        L += [f"## The claim", "",
              f"The worker says this unit is done: {unit_title}", ""]
    if artifact:
        L += ["## Where the artifact is", "",
              f"`{artifact}` — in a clean tree, not the worker's.", ""]
    L += ["## What you were NOT given, on purpose", "",
          "- the worker's reasoning, notes or narrative",
          "- the plan slice the worker was briefed from",
          "- any test the worker wrote itself", "",
          "If you had those, you would inherit the same framing mistake. You are "
          "checking the work against the ORIGINAL task instead.", ""]
    L += ["## How to review", "",
          "1. Run it from scratch in a clean checkout. Does it work when nothing is "
          "warm?",
          "2. Try to break it: the input at the boundary, the empty case, the case the "
          "brief never mentioned.",
          "3. Does it answer what the human ASKED, not what the worker chose to build?",
          "4. Name the single most likely way this is wrong even though it passes its "
          "own test.", ""]
    L += ["## Report", "",
          "Exactly one of:", "",
          "```",
          "PASSES — <what you checked, concretely>",
          "FAILS  — <the input or case that breaks it, and what happens>",
          "```", "",
          "Do not hedge into PASSES to be agreeable. A reader that never fails anything",
          "is not a reader; it is a receipt.", ""]
    return "\n".join(L)


def record_fails(rec, why, now=None, by=None):
    now = int(now or time.time())
    out = dict(rec)
    out["review_verdict"] = "fails"
    out["review_fails"] = str(why or "")[:2000] or "the reader reported FAILS"
    out["reviewed_at"] = now
    out["reviewed_by"] = by
    return out


def record_passes(rec, note, now=None, by=None):
    now = int(now or time.time())
    out = dict(rec)
    out["review_verdict"] = "passes"
    out["review_note"] = str(note or "")[:2000]
    out["reviewed_at"] = now
    out["reviewed_by"] = by
    out.pop("review_fails", None)
    return out


def integration_required(children):
    """Two or more units means the seams are a risk no unit owns."""
    n = len([c for c in children if (c[1].get("reported_at") or c[1].get("reaped_at"))])
    if n >= 2:
        return True, f"{n} units reported: their seams are unverified by any of them"
    return False, ""


def record_integration(rec, rc, cmd, now=None, cached=False):
    now = int(now or time.time())
    out = dict(rec)
    out["integration_ran_at"] = now
    out["integration_rc"] = rc
    out["integration_cmd"] = cmd
    out["integration_cached"] = bool(cached)
    return out


def integration_line(lead):
    """What the rollup prints. UNRUN is a finding, not a blank."""
    if not lead.get("integration_ran_at"):
        return "UNRUN"
    rc = lead.get("integration_rc")
    out = f"ran rc={rc}" + (" (cached)" if lead.get("integration_cached") else "")
    if (lead.get("integration_mode") or "clean") != "clean":
        out += " — repo worktree, NOT a clean tree"
    return out
