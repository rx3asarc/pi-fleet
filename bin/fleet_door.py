#!/usr/bin/env python3
"""The front door: "use fleet for <task>".

WHY (2026-10-26). Everything needed to run a fleet existed as 17 separate verbs, and
using them required knowing the SOP. This module is the one-sentence entry point, and
it is deliberately only three things:

  1. DECIDE, before creating anything, whether this fleet may exist at all. Every
     refusal is cheap and names what is holding the slot, because a refusal that
     arrives after `git worktree add` leaves a stray worktree and a stray name.
  2. RENDER the manager brief. The manager is the only worker whose brief is written
     by code rather than by a plan, so it is where the contract must be exact: the
     task verbatim, the four-part return, the plan rule, the cap, the verbs.
  3. STOP. The door does not decompose, does not poll, does not supervise. A door
     that also supervises is two tools in one coat, and the second one is always the
     one that rots.

Refusals enforced here (all four were prose in the SOP until today):

  cap     -- 5 concurrent workers per domain. The sprawl of 2026-10-26 was 23 engrane
             sessions; a cap that lives only in a document is not a cap.
  depth   -- a fleet child may not open a fleet. Two levels, and the second level is
             allowed to be shallow.
  name    -- never inherit an identity: a name held by a different sessionId is a
             refusal, not a reuse.
  kind    -- a task-kind unit cannot be verified by a git artifact, so its done-test
             must be a command or a file in its scratch dir, never "merged".
"""
from __future__ import annotations

import re

CAP_PER_DOMAIN = 5
ROLES = ("lead", "worker", "integration", "reviewer")


def domain_of(task, explicit=None):
    """A domain is a short slug, not a sentence. Explicit wins, always."""
    if explicit:
        return re.sub(r"[^a-z0-9]+", "-", str(explicit).lower()).strip("-")[:24]
    words = re.findall(r"[a-zA-Z]{3,}", str(task or ""))
    stop = {"the", "and", "for", "with", "from", "that", "this", "into", "make",
            "please", "use", "fleet", "audit", "then", "all", "our", "your"}
    keep = [w.lower() for w in words if w.lower() not in stop]
    return (keep[0] if keep else "fleet")[:24]


def live_in_domain(names, domain):
    """Session names that already occupy this domain, as `<domain>-<role>-<n>`."""
    out = []
    for n in names or []:
        if not n:
            continue
        if n == domain or n.startswith(domain + "-"):
            out.append(n)
    return sorted(out)


def decide(task, live_names, held_by=(), domain=None, kind="code", workers=None,
           cap=CAP_PER_DOMAIN, parent_is_child=False):
    """Can this fleet be opened? Returns a dict; never raises for a refusal.

    `held_by` is [(name, sessionId)] for every live session, so a name collision can
    be told apart from a free name.
    """
    task = (task or "").strip()
    if len(task) < 8:
        return {"ok": False, "why": "the task is too short to plan from — give the "
                                    "door the actual sentence"}
    dom = domain_of(task, domain)
    if kind not in ("code", "task"):
        return {"ok": False, "why": f"kind must be 'code' or 'task', not {kind!r}"}
    if parent_is_child:
        return {"ok": False, "why": "refused: a fleet child may not open a fleet. "
                                    "Report COULD NOT DO with the reason instead — a "
                                    "third level loses the thread and cannot be rolled "
                                    "up honestly."}
    already = live_in_domain(live_names, dom)
    # The cap counts WORKERS. A lead is a coordinator, not a worker, and counting it
    # would make a 5-worker domain unopenable exactly when it is fully used.
    workers_now = [n for n in already
                   if not re.search(r"-(lead|manager)(-\d+)?$", n)]
    if len(workers_now) >= cap:
        return {"ok": False,
                "why": f"refused: {len(workers_now)} workers already live in domain "
                       f"'{dom}' (cap {cap}): {', '.join(workers_now)}. Reap one, or "
                       f"open the fleet in a different --domain."}
    lead = f"{dom}-lead"
    taken = next((sid for n, sid in held_by if n == lead), None)
    if taken and not parent_is_child:
        return {"ok": False,
                "why": f"refused: the name {lead} is held by session {taken[:8]} — a "
                       f"name is an identity, never a slot. Use --domain to open a "
                       f"different fleet, or adopt that session instead."}
    return {"ok": True, "domain": dom, "lead": lead, "kind": kind,
            "workers_allowed": workers if workers is not None else cap,
            "workers_now": len(workers_now)}


def manager_brief(task, decision, report_to=None, repo=None):
    """The manager's brief. Written by code, because there is no plan yet.

    Everything the manager needs to be auditable is here: the task verbatim (so a
    mis-framing is visible at delivery), the plan rule that makes the fan-out
    checkable, the naming and cap rules, the exact verbs, and the report-up command.
    """
    dom = decision["domain"]
    L = [f"# Manager brief — {dom}", "",
         "You are the lead of a fleet. You do not do the work; you plan it, spawn one",
         "worker per unit, and report the assembled evidence upward.", "",
         "## The task (verbatim — do not paraphrase it in your plan)", "",
         task.strip(), ""]
    L += ["## Step 1 — write the plan BEFORE spawning anything", "",
          "Write `plan.md` in your working directory. One section per unit, in this",
          "exact shape, because `fleet spawn` parses it:", "",
          "```",
          "## U1 — <title>",
          "<what this unit must achieve, in enough detail that a worker who reads",
          " only this section can do it>",
          "done-test: <a command that exits 0 only if the unit is done>",
          "```", "",
          "Rules for the plan, and they are not style advice:", "",
          "- **Every unit needs a `done-test:`.** A unit without one cannot be spawned;",
          "  the door refuses it, because a worker with no checkable test will certify",
          "  itself.",
          f"- **At most {decision['workers_allowed']} concurrent workers** in the domain",
          "  `" + dom + "`. Workers are `" + dom + "-<role>-<n>`.",
          "- **One deliverable per unit.** If a unit feels like it needs a sub-fleet,",
          "  the unit is too big: split it, do not nest it.",
          "- **Size units to finish well inside one context window.** A worker that",
          "  runs out of context degrades before it reports.",
          "- The done-test must be able to FAIL. `true` is not a test.", ""]
    L += ["## Step 2 — spawn, one unit at a time", "",
          "```",
          f"fleet spawn {dom}-<role>-1 --plan plan.md --unit 1 \\",
          f"    --repo {repo or '<path>'} --task '<the task, verbatim>'",
          "```", "",
          "The brief is rendered from that unit's slice, and the done-test is frozen",
          "from the plan. You never pass `--done-test` yourself when `--plan` is given:",
          "the plan authors the test, and a test you hand-write at spawn time is not a",
          "plan.", ""]
    L += ["## Step 3 — wait for evidence, do not poll", "",
          "Workers call `fleet done <name> --verify '<their frozen done-test>'`.",
          "A worker that reports `--unsure` is telling you something: pass it upward",
          "verbatim, do not summarise it away.", ""]
    L += ["## Step 4 — report up", "",
          "```",
          f"fleet rollup <your-session-id> --report-to {report_to or '<caller>'}",
          "```", "",
          "That assembles every unit's evidence deterministically, prints conflicts",
          "between typed claims, and quarantines your workers' prose. Do not write your",
          "own summary INSTEAD of it; add your interpretation to the report only after",
          "the assembled evidence.", ""]
    L += ["## The four-part return, when you are done", "",
          "1. **RESULT** — what is now true.",
          "2. **ARTIFACTS** — paths, one per line.",
          "3. **COULD NOT DO** — with the reason.",
          "4. **UNSURE** — verbatim, never smoothed.", ""]
    L += ["## Hard rules", "",
          "- Do not spawn a fleet of your own, and do not spawn more than the cap.",
          "- Do not edit your workers' briefs or their frozen done-tests.",
          "- Do not message workers directly to change their task; re-plan and re-spawn.",
          "- A worker that stops talking is NOT done. It is swept only by evidence; if",
          "  it owes you a claim, the fleet waits and you say so in COULD NOT DO.",
          ""]
    return "\n".join(L)
