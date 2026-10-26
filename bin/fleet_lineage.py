"""U11 — spawn lineage: heartbeat, durable digest, cascade reap.

PURE. Fixtures only: no tmux, no git, no /proc, no filesystem, no clock.
Every fact arrives as an argument. That is what makes the *decision* testable,
and the decision is the thing that kills a session.

WHY THIS EXISTS
A session that spawns helpers has no memory of having done it. The helpers keep
paying rent (a pane, a pi process, a name, a record a human must triage) after
the unit that justified them is finished, and nothing in the loop is allowed to
notice: `fleet reap` requires the worker to have *reported*, and a worker that
finished quietly never does. Consolidation therefore stays a manual sweep — the
thing that had to be repeated by hand on 2026-10-08 and again on 2026-10-17.

WHAT THIS ADDS, AND WHAT IT DELIBERATELY DOES NOT
  1. LINEAGE   — a child record naming its parent session id, its unit, and when
                 it was spawned. Without this, "which of these 13 panes did I
                 start, and for what?" is unanswerable.
  2. HEARTBEAT — derived, never written. A child's heartbeat is the `updatedAt`
                 of its existing liveness record (pi already writes it every
                 turn). A heartbeat that needs its own writer is a heartbeat that
                 can disagree with reality, and a per-child daemon is exactly the
                 CPU/GPU load this must not create. Cost of a tick: a few stat()
                 calls.
  3. VERDICT   — one pure function: reap / hold / notify, with a reason a human
                 can read. The parent decides; the sentinel only proposes.
  4. DIGEST    — a durable, compact record written ONCE at reap, so a successor
                 can see what was already done without replaying a transcript.
                 Deterministic: same inputs, same bytes (that is how idempotency
                 is proved rather than asserted).

THE RULE THAT MUST NOT WEAKEN
A disposition of `reap` is a PROPOSAL. Kill is still gated by fleet_gate
(busy, pending ask, reported-or-accept-silent, ownership agreement). This module
never sees a pid and never sends a signal. `accept_silent` silences *silence*
only — it does not turn `hold` into `reap` for a busy or asking child.
"""
from __future__ import annotations
import re

__all__ = [
    "REAP", "HOLD", "NOTIFY",
    "new_child", "mark_reported", "keep_until_epoch", "heartbeat_age",
    "verdict", "digest_md", "ledger_line", "DEFAULT_IDLE_AFTER_S",
]

REAP, HOLD, NOTIFY = "reap", "hold", "notify"

DEFAULT_IDLE_AFTER_S = 30 * 60          # "silent for half an hour"
DEFAULT_ORPHAN_AFTER_S = 15 * 60
DEFAULT_GRACE_S = 10 * 60        # parent gone, child silent this long


# ── the child record ─────────────────────────────────────────────────────────
def new_child(session_id, name, parent_session_id, parent_name, unit,
              now, done_criteria=None, transcript=None, enrolled_source=None):
    """A lineage record. `now` is epoch seconds; no clock is read here."""
    if not session_id:
        raise ValueError("session_id is the primary key")
    return {
        "sessionId": session_id,
        "name": name,
        "parent_session_id": parent_session_id,
        "parent_name": parent_name,
        "unit": unit or "",
        "done_criteria": done_criteria or "",
        "transcript": transcript,
        # WHICH DOOR created this record. Without it an auditor can only say
        # UNKNOWN, and "how did this session get into the store" is the first
        # question anyone asks about an orphan.
        "enrolled_source": enrolled_source,
        "spawned_at": int(now),
        "reported_at": None,
        "report": None,
        "keep_until": None,
        "reaped_at": None,
        "verdict": None,
        "why": None,
        "digest": None,
    }


def mark_reported(child, note, now):
    """The child's own account of what it did. Idempotent: last report wins."""
    c = dict(child)
    c["reported_at"] = int(now)
    c["report"] = (note or "").strip() or None
    return c


def record_claims(child, files=(), verify=None, unsure=None):
    """The checkable half of a report: which files, which command proves it.

    A note is prose and prose is cheap; a claim names something the reaper can
    inspect without believing anybody. Kept free of the checking itself so the
    decision stays testable with fixtures.
    """
    c = dict(child)
    c["claims"] = {
        "files": sorted({str(f) for f in (files or ())}),
        "verify": (verify or "").strip() or None,
        "unsure": (unsure or "").strip() or None,
    }
    return c


_TRIVIAL_VERIFY = re.compile(
    r"^(true|:|exit\s+0|/bin/true|sleep\s+0|echo(\s.*)?|printf\s.*)$")

def verify_can_fail(cmd):
    """False when a "verification" cannot produce a failure.

    `--verify "true"` exits 0 always, so the gate would record `verified` with zero
    proof -- a claim that survives inspection while proving nothing (same shape as
    "we signed it, so we can tell lanes apart"). A command that cannot fail is not
    evidence, so it is not allowed to produce the word `verified`.
    """
    c = " ".join(str(cmd or "").split())
    if not c:
        return False
    if any(tok in c for tok in ("&&", "||", ";", "|", "test ", "[ ")):
        return True          # composed: it can fail on one of its parts
    return not _TRIVIAL_VERIFY.match(c)

def claim_status(claims, checks):
    """Reduce the worker's claims and the mechanical checks to one word.

    verified  -- something was claimed and every claim held
    failed    -- a claim was made and something disproved it (the reaper's cue
                 to stop and ask, never to sweep quietly)
    partial   -- a verify command was declared but never produced an exit code
                 (it timed out); a missing file is `failed`, checked first
    unclaimed -- the worker made no checkable claim; the note is all there is
    """
    claims = claims or {}
    checks = checks or {}
    files = claims.get("files") or []
    verify = claims.get("verify")
    if not files and not verify:
        return "unclaimed"
    fc = checks.get("files") or []
    if any(not f.get("exists") for f in fc):
        return "failed"
    if verify:
        v = checks.get("verify") or {}
        if v.get("rc") is None:
            return "partial"
        if v.get("rc") != 0:
            return "failed"
        if not verify_can_fail(v.get("cmd")):
            # The command always exits 0: it re-runs cleanly and proves nothing.
            # Fall through to the file evidence rather than granting `verified`.
            return "verified" if fc and any(f.get("after_spawn") for f in fc) else "unclaimed"
        return "verified"
    # No command to re-run, so authorship rests on mtime. Evidence that predates
    # the unit is the cheapest lie there is ("I wrote this" -> an old file), and
    # a re-run proof overrides it because that is real evidence and mtime is only
    # circumstantial.
    if fc and not any(f.get("after_spawn") for f in fc):
        return "stale"
    return "verified" if fc else "unclaimed"


def keep_until_epoch(now, hours):
    return int(now) + int(float(hours) * 3600)


def heartbeat_age(last_seen, now):
    """Seconds since the child was last active, or None when it never was.

    `last_seen` comes from the child's EXISTING liveness record (`updatedAt`),
    which pi writes on every turn. There is no second writer and no daemon.
    """
    if last_seen is None:
        return None
    return max(0, int(now) - int(last_seen))


# ── the decision ─────────────────────────────────────────────────────────────
def verdict(child, facts, now, idle_after_s=DEFAULT_IDLE_AFTER_S,
            orphan_after_s=DEFAULT_ORPHAN_AFTER_S, accept_silent=False):
    """(action, why) for one child. First matching rule wins.

    `facts` — the impure half, supplied by the caller:
        alive           child's pi process exists
        busy            child is inside a tool right now
        pending_ask     child is waiting on a human (string label or None)
        parent_alive    parent's pi process exists
        parent_finished parent reported done, or its unit is closed
        last_seen       child's liveness `updatedAt` (epoch seconds) or None
    """
    if child.get("reaped_at"):
        return HOLD, "already reaped"

    if child.get("protected"):
        # An orphan enrolled by `fleet scan` has no known parent, so nothing here
        # is entitled to decide it is finished. Visible, never swept.
        return HOLD, "orphan: enrol-only, never swept automatically"

    ku = child.get("keep_until")
    if ku and int(ku) > int(now):
        return HOLD, f"kept by parent until {int(ku)}"

    reported = child.get("reported_at") is not None
    age = heartbeat_age(facts.get("last_seen"), now)

    # Terminal means terminal. A record closed by a human (or reaped with a
    # receipt) is not re-judged: the whole point of `dismiss` is that the answer
    # stops changing. HOLD, not DECIDE -- a closed record must not ask again.
    if child.get("closed_at") or child.get("reaped_at"):
        return HOLD, ("closed " + ("by " + str(child.get("closed_by")) if child.get("closed_by")
                                   else "with a reap receipt")
                      + " — terminal, not re-judged")

    # UNKNOWN resolves towards NOT REAPING, deliberately. "I could not read the
    # process table" and "the process is gone" are different answers, and only
    # the second one is a licence to close a session. An unknown child is
    # visible and noisy; a wrongly killed session is not recoverable.
    if facts.get("alive_state") == "unknown":
        return HOLD, ("identity unknown (no recorded pid start-time, or /proc was "
                      "unreadable) — not reaping")
    if not facts.get("alive"):
        if reported:
            return REAP, "process gone, and it reported its unit"
        if child.get("role") == "reviewer" or str(child.get("name") or "").startswith(
                "fleet-review-"):
            # Reviewers are single-use readers. If one dies without writing, the
            # answer is never "review the reviewer" -- that is the regress.
            return REAP, "reviewer ended without reporting"
        if accept_silent:
            return REAP, "process gone and never reported (accept_silent)"
        # A dead parent cannot decide anything, so "needs a human" means "waits
        # until morning" -- this morning's 18-hour stall, by design. Silence gets
        # one announced grace, and is then swept as silent (the digest keeps the
        # transcript and the tail text, so nothing evaporates).
        if not facts.get("parent_alive", True):
            left = facts.get("grace_left")
            if left is None:
                return NOTIFY, ("process gone, never reported, and its parent is "
                                "gone too — reaping after one grace unless kept")
            if left <= 0:
                return REAP, "silent: parent gone and it never reported"
            return HOLD, f"grace running, {int(left)}s left (fleet keep to stop it)"
        return NOTIFY, ("process gone and never reported — nothing to harvest "
                        "on the record; needs a human decision")

    if facts.get("pending_ask"):
        return HOLD, f"waiting on a human: {facts['pending_ask']}"
    if facts.get("busy"):
        return HOLD, "inside a tool right now"

    if reported:
        return REAP, "reported its unit"

    if facts.get("parent_finished"):
        if age is not None and age > idle_after_s:
            return REAP, f"parent finished, child silent {age}s"
        return HOLD, "parent finished, child still warm — give it a tick"

    if not facts.get("parent_alive"):
        if age is not None and age > orphan_after_s:
            return REAP, f"parent gone and child silent {age}s"
        return HOLD, "parent gone but child still warm"

    if age is not None and age > idle_after_s:
        return NOTIFY, (f"idle {age}s while the parent is still running — "
                        f"reuse it or reap it")
    return HOLD, ("fresh" if age is None else f"active {age}s ago")


# ── the durable half of reaping ──────────────────────────────────────────────
def digest_md(child, facts, now, iso, entry=None, touched=(), commits=(),
              tail=None, checks=None):
    """The compact record that replaces re-reading a transcript.

    Deterministic on purpose: no wall clock, no dict iteration, no random ids.
    Same arguments => same bytes, which is how "re-running the sentinel changes
    nothing" is tested instead of claimed.
    """
    entry = entry or {}
    claims = child.get("claims") or {}
    status = claim_status(claims, checks)
    lines = [
        # Line 1 identifies the SESSION, not just the name: a digest outlives the
        # name registry, and "which session was this" is the question a receipt
        # exists to answer. Receipts written before 2026-10-17 name the session on
        # line 6 instead; they are not rewritten (history stays as written).
        f"# {child.get('name') or '(unnamed)'} {child['sessionId']} — reaped {iso}",
        "",
        f"- unit: {child.get('unit') or '(undeclared)'}",
        f"- done criteria: {child.get('done_criteria') or '(none recorded)'}",
        f"- parent: {child.get('parent_name') or '?'} "
        f"({_sid8(child.get('parent_session_id'))})",
        f"- session: {child['sessionId']}",
        f"- enrolled via: {child.get('enrolled_source') or '(not recorded — a record predating enrolled_source)'}",
        "- template: line 1 names the session (receipts before 2026-10-17 named it "
        "on the session line instead)",
        f"- spawned: {_iso(child.get('spawned_at'))}",
        f"- verdict: {child.get('verdict') or '?'} — {child.get('why') or '?'}",
        "",
        "## What it said it did (its own words)",
        "",
        child.get("report") or "_never reported_",
        "",
        "## Facts (mechanical, not model text)",
        "",
        f"- transcript: {child.get('transcript') or '(none)'}",
        f"- resume: `pi --session {child['sessionId']}`",
    ]
    if entry:
        lines.append(f"- tmux: {entry.get('session') or '(none)'}")
        lines.append(f"- context at reap: {entry.get('context') or 'unknown'}")
        if entry.get("branch"):
            lines.append(f"- branch: {entry['branch']} @ {entry.get('head')}")
            lines.append(f"- merged into base: {bool(entry.get('merged'))}")
    for c in commits:
        lines.append(f"- commit: {c}")
    for f in touched:
        lines.append(f"- touched: {f}")
    if claims.get("files") or claims.get("verify") or checks:
        lines += ["", "## Claimed (the worker's own words)", ""]
        for f in claims.get("files") or []:
            lines.append(f"- file: {f}")
        if claims.get("verify"):
            lines.append(f"- proof: `{claims['verify']}`")
        if not claims.get("files") and not claims.get("verify"):
            lines.append("- (no checkable claim; see the report below)")
        if claims.get("unsure"):
            lines += ["", "## UNSURE (verbatim, never smoothed)", "",
                      claims["unsure"]]
        lines += ["", "## Checked (mechanical, at reap)", ""]
        fc = (checks or {}).get("files") or []
        if not fc:
            lines.append("- files: none declared")
        for f in fc:
            if f.get("exists"):
                lines.append(f"- {f['path']}: present, {f.get('bytes', 0)} bytes, "
                             f"mtime {f.get('mtime_iso') or '?'}, "
                             f"sha256 {f.get('sha256', '')[:16]}…, "
                             f"{'after' if f.get('after_spawn') else 'BEFORE'} enrolment")
            else:
                lines.append(f"- {f['path']}: MISSING")
        v = (checks or {}).get("verify")
        if v:
            lines.append(f"- proof: `{v.get('cmd')}` → exit {v.get('rc')} "
                         f"in {v.get('secs', 0)}s"
                         + (f" (tail: {v.get('tail')!r})" if v.get("tail") else ""))
        lines.append(f"- evidence verdict: **{status}**")

    if tail:
        lines += ["", "## Last assistant text (excerpt, model text — not evidence)", "",
                  "```", _clip(tail, 1200), "```"]
    lines.append("")
    return "\n".join(lines)


def ledger_line(child, action, now):
    """One JSONL row. The ledger is append-only, deduped by (sessionId, action)."""
    import json
    return json.dumps({
        "ts": int(now),
        # The EVENT's instant, not this attempt's clock. A retry five seconds
        # later has a different `ts` and an identical `event_ts`, which is what
        # makes a duplicate row detectable instead of merely unlikely.
        # reap_started_at first: it is stamped on EVERY reap attempt, so it is
        # the only value two attempts at one death are guaranteed to share.
        "event_ts": int(child.get("reap_started_at") or child.get("reaped_at")
                        or child.get("closed_at") or now),
        "sessionId": child["sessionId"],
        "name": child.get("name"),
        "parent": child.get("parent_session_id"),
        "action": action,
        "why": child.get("why"),
        "digest": child.get("digest"),
        "evidence": (child.get("checks") or {}).get("status"),
    }, sort_keys=True)


def _sid8(sid):
    return (sid or "?")[:8]


def _iso(epoch):
    if epoch is None:
        return "?"
    import time
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def _clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[: n - 1] + "…"
