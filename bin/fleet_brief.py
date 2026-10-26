#!/usr/bin/env python3
"""The brief is the artifact workers actually read, so it is gated like one.

WHY (2026-10-26, pi-moa review objection 4 — the sharpest correction of the review).
I had been treating the PLAN as the thing every worker shares. It isn't: no worker
reads `plan.md`. Each worker reads a brief the lead distilled from the plan slice, and
that distillation is lossy and per-worker. A perfect plan with lossy briefs produces
bad workers, and nothing measured the loss.

So this module does three things and nothing else:

  parse_plan()   -- read a plan into units, each with a frozen done-test. A unit with
                    no done-test is not a unit; the parser reports it so the lead can
                    fix the plan BEFORE spawning anything.
  render()       -- render a brief FROM one plan slice, embedding the done-test
                    verbatim. The brief cannot contain more than its slice, so a
                    worker cannot be briefed on another unit's business.
  drift()        -- compare the brief on disk with the one that was issued. A
                    hand-edited brief is a worker writing its own contract, and the
                    gate must be able to see that.

The brief is written once. A worker may read it as many times as it likes.
"""
from __future__ import annotations

import hashlib
import re

_HEAD = re.compile(r"^#{2,4}\s*(?:U|Unit\s*)(\d+)\b[.:\u2014\u2013-]?\s*(.*)$", re.I)
_DONE = re.compile(r"^\s*(?:[-*]\s*)?done[-_ ]test\s*:\s*(.+?)\s*$", re.I)


class PlanError(ValueError):
    """A plan that cannot be spawned from. Says which unit and why."""


def parse_plan(text):
    """[(n, title, body, done_test)] in plan order.

    A unit without a `done-test:` line is returned with done_test None so the caller
    can refuse it BY NAME rather than silently spawning an unverifiable worker.
    """
    units, cur = [], None
    for line in str(text or "").splitlines():
        head = _HEAD.match(line)
        if head:
            if cur:
                units.append(cur)
            cur = {"n": int(head.group(1)), "title": head.group(2).strip(),
                   "body": [], "done_test": None}
            continue
        if cur is None:
            continue
        dm = _DONE.match(line)
        if dm:
            cur["done_test"] = dm.group(1).strip().strip("`")
            continue
        cur["body"].append(line)
    if cur:
        units.append(cur)
    return [(u["n"], u["title"], "\n".join(u["body"]).strip(), u["done_test"])
            for u in units]


def slice_plan(text, n):
    """One unit, or PlanError naming what is wrong with it."""
    units = parse_plan(text)
    if not units:
        raise PlanError("the plan has no units (expected headings like `## U1 — title`)")
    match = [u for u in units if u[0] == int(n)]
    if not match:
        have = ", ".join(f"U{u[0]}" for u in units)
        raise PlanError(f"no unit U{n} in the plan (it has: {have})")
    num, title, body, dt = match[0]
    if not dt:
        raise PlanError(f"U{num} has no `done-test:` line — a unit without a "
                        f"checkable test cannot be spawned (the plan is wrong, not "
                        f"the worker)")
    return {"n": num, "title": title, "body": body, "done_test": dt}


def done_test_kind(value):
    v = str(value or "").strip()
    if v.lower().startswith("file:"):
        return "file", v.split(":", 1)[1].strip()
    return "cmd", v


def render(unit, task, domain=None, parent=None, workers_left=None, scratch=None):
    """The worker brief, rendered from ONE slice. Deterministic: same input, same bytes."""
    kind, value = done_test_kind(unit["done_test"])
    name = f"{domain}-worker" if domain else "worker"
    L = [f"# Brief — U{unit['n']}: {unit['title']}", ""]
    if task:
        L += ["## The task this unit belongs to (verbatim)", "", task.strip(), ""]
    L += ["## Your unit", "", unit["body"] or "(no detail recorded in the plan)", ""]
    L += ["## The test that decides whether you are done", "",
          "This is FROZEN. It was authored with the plan, before you started, and you",
          "do not get to change it. Report it verbatim with `fleet done`:", ""]
    if kind == "cmd":
        L += ["```", value, "```", "",
              f"Report with: `fleet done <your-name> --verify {value!r}`"]
    else:
        L += [f"the file `{value}` (must exist and be non-empty)", "",
              f"Report with: `fleet done <your-name> --file {value}`"]
    L += ["", "If you believe the test is wrong, do NOT invent your own. Say so in",
          "`--unsure` and explain what the right test would be; the lead re-plans.", ""]
    L += ["## How to report", "",
          "Four parts, and the fourth one is not optional:", "",
          "1. **RESULT** — what is now true that was not true before.",
          "2. **ARTIFACTS** — paths (they are hashed and checked at reap).",
          "3. **COULD NOT DO** — with the reason, not an apology.",
          "4. **UNSURE** — passed upstream VERBATIM. Never smooth it. A fleet that",
          "   hides a doubt is worse than a fleet that reports one.", "",
          "Add typed claims when you can (`--claim-file`):",
          "`[{claim_id, assertion_key, value, polarity, evidence_ref}]`. Two workers",
          "disagreeing on an assertion_key is printed as a CONFLICT — that is the",
          "mechanism, not a judgement call.", ""]
    L += ["## Rules", "",
          f"- You are `{name}-<role>-<n>`. The domain is fixed by your parent; do not",
          "  invent one.",
          "- Do not spawn a fleet of your own. If you think this unit needs one, the",
          "  plan is wrong — report it in COULD NOT DO.",
          "- Do not message other workers. Report to your lead.",
          "- Do not edit this file. It is the contract; edits are detected as drift.",
          ""]
    if parent:
        L += [f"- Your lead is `{parent}`. When you are done: `fleet done <your-name>`.",
              ""]
    if workers_left is not None:
        L += [f"- Workers allowed in this domain right now: {workers_left}.", ""]
    if scratch:
        L += [f"- Work in `{scratch}`. Nothing outside it is yours.", ""]
    return "\n".join(L)


def sha(text):
    return hashlib.sha256(str(text).encode()).hexdigest()


def write(path, text):
    from pathlib import Path
    p = Path(path)
    p.write_text(text)
    return str(p)


def issue(path):
    """The sha of a hand-written brief, read at spawn time.

    WHY THIS EXISTS (MEASURED 2026-10-26). A brief that `--plan` RENDERS was hashed
    at issue; a brief handed to `--brief` was not hashed at all, so `brief_sha` was
    "" on every such row and `drift()` therefore answered "nothing was issued" for
    the commonest way a worker is briefed. The drift check was not wrong, it was
    never armed: three units ran from a plan instead of a brief, and a later edit to
    any hand-written brief would have been undetectable under a running worker.

    Raises ValueError for a missing or empty brief -- an empty contract is not a
    contract, and a spawn that proceeds with one records a worker nobody briefed.
    """
    from pathlib import Path
    p = Path(str(path))
    if not p.is_file():
        raise ValueError(f"brief not found: {p}")
    text = p.read_text()
    if not text.strip():
        raise ValueError(f"brief is empty: {p}")
    return sha(text)


def drift(brief_path, expected_sha):
    """None when the brief on disk is the issued one; otherwise a reason string."""
    from pathlib import Path
    if not expected_sha:
        return None                       # legacy child: nothing was issued
    p = Path(brief_path or "")
    if not brief_path or not p.exists():
        return "brief missing: the issued brief is not on disk"
    try:
        got = sha(p.read_text())
    except OSError as ex:
        return f"brief unreadable ({ex})"
    if got != expected_sha:
        return ("brief edited after issue: the worker rewrote its own contract "
                "(the frozen done-test and rules are only trustworthy as issued)")
    return None
