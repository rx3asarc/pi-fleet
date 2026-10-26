#!/usr/bin/env python3
"""Deterministic rollup: assemble evidence, print conflicts, quarantine prose.

WHY (2026-10-26, pi-moa review objections 1/4/5). Two failures were possible in the
obvious design and both produce confident wrong answers:

  1. A model summarising worker narratives. The lead wrote the plan, so it has an
     incentive to report the plan worked; a summary is where that incentive shows up.
     So this module imports nothing that can talk to a model. The rollup is assembled
     from records, byte for byte, and a test asserts the import list.
  2. "Print contradictions instead of resolving them" was unimplementable over prose.
     Contradiction is a property of MEANING, so `diff` cannot find it. It becomes
     computable only if workers emit typed claims: group by `assertion_key`, and any
     key with more than one distinct (value, polarity) is a CONFLICT — a SQL
     `GROUP BY ... HAVING COUNT(DISTINCT ...) > 1`, not a judgement.

Prose is not forbidden, it is quarantined: `WORKER NARRATIVE (uninterpreted)` is a
section nothing downstream reads. A worker that cannot express its result as a typed
claim has not met the plan's precondition, and the rollup says so per unit rather than
smoothing it into the summary.

`UNSURE` passes through verbatim. `COULD NOT DO` is a section, not a footnote.
"""
from __future__ import annotations

import time

POLARITIES = {"is", "is-not", True, False}
REQUIRED = ("claim_id", "assertion_key", "value", "polarity")


def _norm_polarity(p):
    if p is True or p == "is":
        return "is"
    if p is False or p == "is-not":
        return "is-not"
    return None


def validate_claims(claims):
    """Shape, not truth. Returns (clean_list, errors)."""
    errors, clean = [], []
    if not isinstance(claims, list):
        return [], ["claims must be a list of objects"]
    seen = set()
    for i, c in enumerate(claims):
        if not isinstance(c, dict):
            errors.append(f"claim {i}: not an object")
            continue
        missing = [k for k in REQUIRED if k not in c]
        if missing:
            errors.append(f"claim {i}: missing {', '.join(missing)}")
            continue
        key = str(c["assertion_key"]).strip()
        if not key:
            errors.append(f"claim {i}: empty assertion_key")
            continue
        pol = _norm_polarity(c["polarity"])
        if pol is None:
            errors.append(f"claim {i}: polarity must be 'is', 'is-not', true or false")
            continue
        cid = str(c["claim_id"]).strip()
        if cid in seen:
            errors.append(f"claim {i}: duplicate claim_id {cid!r}")
            continue
        seen.add(cid)
        clean.append({"claim_id": cid, "assertion_key": key,
                      "value": c["value"], "polarity": pol,
                      "evidence_ref": str(c.get("evidence_ref") or "").strip()})
    return clean, errors


def _claim_rows(children):
    """(unit_name, claim) for every claim on every child."""
    rows = []
    for name, rec in children:
        for c in (rec.get("claims_v2") or []):
            rows.append((name, c))
    return rows


def conflicts(children):
    """Assertion keys where workers disagree. Deterministic; no model, no prose."""
    by_key = {}
    for unit, c in _claim_rows(children):
        by_key.setdefault(c["assertion_key"], []).append(
            {"unit": unit, "claim_id": c["claim_id"], "value": c["value"],
             "polarity": c["polarity"], "evidence_ref": c.get("evidence_ref")})
    out = []
    for key, entries in sorted(by_key.items()):
        distinct = {(repr(e["value"]), e["polarity"]) for e in entries}
        if len(distinct) > 1:
            out.append({"assertion_key": key, "entries": entries})
    return out


def unit_status(rec):
    """One word per unit, straight from what was recorded.

    Order matters and is not cosmetic: a FAILED check outranks an UNSURE statement,
    because a red command is harder evidence than a hedge. Summarising "unsure" over
    "the test failed" is exactly the smoothing this module exists to prevent.
    """
    if rec.get("reaped_at"):
        return "reaped"
    if rec.get("closed_at"):
        return rec.get("verdict") or "closed"
    v = (rec.get("checks") or {}).get("verify") or {}
    if v.get("cwd_unresolved"):
        # A check that ran in the wrong directory is not a verdict. Calling it FAILED
        # is how a passing unit became a red line in a real report (2026-10-26).
        return f"unverified(rc={v.get('rc')}, no known worktree)"
    if v.get("rc") is not None and v.get("rc") != 0:
        return f"FAILED(rc={v['rc']})"
    if rec.get("claims", {}).get("unsure"):
        return "UNSURE"
    if rec.get("reported_at"):
        if v.get("rc") is not None:
            return "verified"
        return "reported-unchecked"
    return "open"


def render(lead_name, task, children, now=None, aborted=False, unacked=False,
           integration="UNRUN"):
    """The rollup. Every section below is assembled from records; none is written by
    a model, and the narrative a worker wrote appears in exactly one place."""
    now = int(now or time.time())
    stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(now))
    L = []
    if aborted:
        L.append("# ABORTED (partial) — " + lead_name)
    else:
        L.append("# " + lead_name)
    L.append("")
    # The original task is re-quoted next to what was delivered: a fleet that
    # mis-framed the task must be visible at delivery, not at autopsy.
    L.append(f"**task as given** (verbatim): {task or '(not recorded)'}")
    L.append(f"**assembled**: {stamp} · units: {len(children)}"
             + ("  · PLAN UNACKED (proceeded after the ack window)" if unacked else ""))
    L.append(f"**integration check**: {integration}")
    L.append("")

    L.append("## Units")
    L.append("")
    L.append("| unit | status | done-test | evidence |")
    L.append("|---|---|---|---|")
    for name, rec in children:
        dt = rec.get("done_test") or {}
        if dt:
            test = "`" + (dt.get("cmd") or dt.get("path") or "—") + "`"
        else:
            test = "**legacy (no frozen test)**"
        ev = []
        v = (rec.get("checks") or {}).get("verify") or {}
        if v.get("rc") is not None:
            ev.append(f"rc={v['rc']}")
        vc = (rec.get("checks") or {}).get("verify_clean") or {}
        if vc:
            ev.append("clean:" + ("cached" if vc.get("cached") else
                                  f"rc={vc.get('rc')}"))
        if (rec.get("checks") or {}).get("verify_drift"):
            ev.append("**DRIFT**")
        L.append(f"| {name} | {unit_status(rec)} | {test} | {' '.join(ev) or '—'} |")
    L.append("")

    cons = conflicts(children)
    L.append("## CONFLICTS")
    L.append("")
    if not cons:
        L.append("none reported (no assertion_key has two different values)")
    else:
        for c in cons:
            L.append(f"### {c['assertion_key']}")
            for e in c["entries"]:
                L.append(f"- {e['unit']}: **{e['value']}** ({e['polarity']})"
                         + (f"  — evidence `{e['evidence_ref']}`"
                            if e["evidence_ref"] else ""))
            L.append("")
    L.append("")

    unsure = [(n, r.get("claims", {}).get("unsure")) for n, r in children
              if r.get("claims", {}).get("unsure")]
    L.append("## UNSURE (verbatim)")
    L.append("")
    if not unsure:
        L.append("none")
    for n, u in unsure:
        L.append(f"- **{n}**: {u}")
    L.append("")

    failed = [(n, r) for n, r in children if unit_status(r).startswith("FAILED")]
    L.append("## FAILED CHECKS")
    L.append("")
    if not failed:
        L.append("none")
    for n, r in failed:
        v = (r.get("checks") or {}).get("verify") or {}
        L.append(f"- **{n}**: `{v.get('cmd')}` exited {v.get('rc')}"
                 + (f" — {v.get('tail')}" if v.get("tail") else ""))
    L.append("")

    L.append("## WORKER NARRATIVE (uninterpreted)")
    L.append("")
    L.append("*Nothing above was derived from this section. It is here so a human can "
             "read what workers said, not so the fleet can agree with itself.*")
    L.append("")
    any_prose = False
    for name, rec in children:
        rep = rec.get("report")
        if rep:
            any_prose = True
            L.append(f"### {name}")
            L.append("")
            L.append(rep)
            L.append("")
    if not any_prose:
        L.append("(no narrative recorded)")
    return "\n".join(L) + "\n"


def slug(text, n=48):
    keep = "".join(ch if (ch.isalnum() or ch in "-_") else "-"
                   for ch in str(text or "").lower())
    while "--" in keep:
        keep = keep.replace("--", "-")
    return keep.strip("-")[:n] or "rollup"
