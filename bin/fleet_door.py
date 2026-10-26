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

  cap     -- 5 concurrent workers per domain, COUNTED FROM THE `domain` FIELD (the
             name prefix is read only for rows written before the field existed).
             The sprawl of 2026-10-26 was 23 sessions in one domain; a cap that lives only
             in a document is not a cap. Enforced in `decide()` here AND in `fleet
             spawn`, because a worker spawned directly walks around the door -- that
             double enforcement is what makes the number true rather than advisory.
             An exception is allowed (`over_cap`) and RECORDS itself.
  depth   -- a fleet child may not open a fleet. Two levels, and the second level is
             allowed to be shallow.
  name    -- never inherit an identity: a name held by a different sessionId is a
             refusal, not a reuse.
  kind    -- a task-kind unit cannot be verified by a git artifact, so its done-test
             must be a command or a file in its scratch dir, never "merged".

THE NAME IS THE SUBJECT; THE DOMAIN IS A FIELD (2026-10-26). `<domain>-<role>-<n>` was
never a law, only the shape names happened to take. `decide()` therefore counts the
cap from `record['domain']`, lets a worker be named anything (`audit-1`), and marks a
domain it derived from the task text as `inferred:` instead of letting it masquerade
as one the caller chose.
"""
from __future__ import annotations

import re

CAP_PER_DOMAIN = 5
ROLES = ("lead", "worker", "integration", "reviewer")

# A domain the DOOR derived from the task text is recorded with this prefix, so it
# can never be mistaken for one a human chose -- and so the cap does not silently
# start counting against a bucket nobody asked for.
INFERRED_PREFIX = "inferred:"

# A coordinator coordinates; it is not one of the workers the cap counts.
_COORDINATOR_RE = re.compile(r"-(lead|manager)(-\d+)?$")


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
    """Session NAMES that already occupy this domain, by the legacy name prefix.

    Kept because it is the only reading available for rows with no `domain` field.
    New counting goes through `workers_in_domain()`, which reads the FIELD.
    """
    out = []
    for n in names or []:
        if not n:
            continue
        if n == domain or n.startswith(domain + "-"):
            out.append(n)
    return sorted(out)


def is_coordinator(name):
    """A lead/manager is a coordinator, not one of the workers the cap counts."""
    return bool(_COORDINATOR_RE.search(str(name or "")))


def domain_of_record(rec):
    """(domain, source) for ONE lineage/registry row.

    FIELD FIRST, name prefix as a legacy fallback -- and the source is returned so a
    caller can tell a chosen domain from a derived or inherited one:

        'field'       `fleet spawn --domain <d>` wrote it
        'inferred'    the door derived it from the task text (`inferred:<d>`)
        'name prefix' written before the field existed; readable only off the name
        None          no domain at all, which is a legal way for a worker to exist
    """
    rec = rec or {}
    dom = str(rec.get("domain") or "").strip().lower()
    if dom.startswith(INFERRED_PREFIX):
        return dom[len(INFERRED_PREFIX):], "inferred"
    if dom:
        return dom, "field"
    name = str(rec.get("name") or "")
    if name and "-" in name:
        head = name.split("-", 1)[0].lower()
        # A prefix is a domain only when it IS one of the known domains: `meta-x-1`
        # is a legacy scoped name, `audit-1` is a subject with a sequence.
        try:
            import pi_taxonomy
            known = pi_taxonomy.known_domains()
        except Exception:                      # pure fallback: no set, no inference
            known = ()
        if head in known:
            return head, "name prefix"
    return None, None


def record_in_domain(rec, domain, include_inferred=False):
    """Is this record in `domain`? The FIELD answers first; the name only if absent.

    An `inferred:<d>` row does NOT count toward the chosen domain `<d>` unless the
    caller asks for it: the cap exists to bound a domain somebody deliberately
    opened, and a heuristic that guessed a slug is not that. Otherwise one task that
    mentioned "audit" would throttle every deliberate `--domain audit` fleet.
    """
    got, source = domain_of_record(rec)
    if not got:
        return False
    if source == "inferred" and not include_inferred:
        return False
    return got == str(domain or "").strip().lower()


def workers_in_domain(records, domain, exclude_name=None):
    """LIVE worker records in `domain`, counted from the FIELD. Sorted names.

    `records` are lineage/registry rows (dicts). Rows that are closed (a verdict or a
    close marker) are skipped, and so are coordinators. A row with no live marker is
    counted -- the caller decides what set it passes in, and this function does not
    invent a second opinion about liveness.
    """
    out = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        name = r.get("name")
        if not name or name == exclude_name or is_coordinator(name):
            continue
        if r.get("verdict") or r.get("closed_by") or r.get("reaped_at"):
            continue
        if record_in_domain(r, domain):
            out.append(name)
    return sorted(out)


def worker_names_in(names, domain):
    """The legacy path: workers in a domain judged by the NAME prefix alone."""
    return [n for n in live_in_domain(names, domain) if not is_coordinator(n)]


def cap_verdict(domain, live_count, cap=CAP_PER_DOMAIN, over_cap=False):
    """May ONE MORE worker be spawned in `domain`? Pure: the count is passed in.

    Returns {ok, over, why, override}. `override` is the field to RECORD on the row
    when a human forces the spawn -- an exception that does not record itself is an
    exception nobody can see afterwards.
    """
    if live_count < cap:
        return {"ok": True, "over": False, "why": None, "override": None}
    if over_cap:
        return {"ok": True, "over": True, "override": {"domain_cap_override": True},
                "why": (f"domain '{domain}' is over the cap ({live_count} live, cap "
                        f"{cap}) -- --over-cap recorded")}
    return {"ok": False, "over": True, "override": None,
            "why": (f"REFUSED: domain '{domain}' already has {live_count} live "
                    f"workers (cap {cap}). Finish or reap one, pick a different "
                    f"unit, or pass --over-cap to proceed -- the override is "
                    f"recorded, not silent.")}


def decide(task, live_names=(), held_by=(), domain=None, kind="code", workers=None,
           cap=CAP_PER_DOMAIN, parent_is_child=False, live_records=None,
           over_cap=False):
    """Can this fleet be opened? Returns a dict; never raises for a refusal.

    `held_by` is [(name, sessionId)] for every live session, so a name collision can
    be told apart from a free name.

    THE CAP IS COUNTED FROM THE FIELD. `live_records` (lineage/registry rows, each
    with a `name` and, on new rows, a `domain`) is the honest input; `live_names` is
    the legacy one and is read by name prefix, which only works while names carry a
    domain. When `live_records` is given it wins.

    A domain the caller did NOT pass is derived from the task text and marked
    `domain_inferred` / `domain_label == 'inferred:<d>'`, so it can never be shown --
    or counted -- as a domain somebody chose.
    """
    task = (task or "").strip()
    dom = domain_of(task, domain)
    inferred = not domain
    base = {"domain": dom, "domain_inferred": inferred,
            "domain_label": (INFERRED_PREFIX + dom) if inferred else dom,
            "domain_source": ("inferred from the task" if inferred
                              else "explicit --domain")}
    if len(task) < 8:
        return {"ok": False, **base,
                "why": "the task is too short to plan from — give the "
                       "door the actual sentence"}
    if kind not in ("code", "task"):
        return {"ok": False, **base,
                "why": f"kind must be 'code' or 'task', not {kind!r}"}
    if parent_is_child:
        return {"ok": False, **base,
                "why": "refused: a fleet child may not open a fleet. "
                       "Report COULD NOT DO with the reason instead — a "
                       "third level loses the thread and cannot be rolled "
                       "up honestly."}
    # The cap counts WORKERS. A lead is a coordinator, not a worker, and counting it
    # would make a 5-worker domain unopenable exactly when it is fully used.
    if live_records is not None:
        workers_now = workers_in_domain(live_records, dom)
    else:
        workers_now = worker_names_in(live_names, dom)
    if len(workers_now) >= cap:
        verdict = cap_verdict(dom, len(workers_now), cap=cap, over_cap=over_cap)
        if not verdict["ok"]:
            return {"ok": False, **base,
                    "why": f"refused: {len(workers_now)} workers already live in "
                           f"domain '{dom}' (cap {cap}): {', '.join(workers_now)}. "
                           f"Reap one, or open the fleet in a different --domain."}
    lead = f"{dom}-lead"
    taken = next((sid for n, sid in held_by if n == lead), None)
    if taken and not parent_is_child:
        return {"ok": False, **base,
                "why": f"refused: the name {lead} is held by session {taken[:8]} — a "
                       f"name is an identity, never a slot. Use --domain to open a "
                       f"different fleet, or adopt that session instead."}
    out = {"ok": True, **base, "lead": lead, "kind": kind,
           "workers_allowed": workers if workers is not None else cap,
           "workers_now": len(workers_now)}
    if len(workers_now) >= cap:
        out["domain_cap_override"] = True
    return out


def manager_brief(task, decision, report_to=None, repo=None):
    """The manager's brief. Written by code, because there is no plan yet.

    Everything the manager needs to be auditable is here: the task verbatim (so a
    mis-framing is visible at delivery), the plan rule that makes the fan-out
    checkable, the naming and cap rules, the exact verbs, and the report-up command.
    """
    dom = decision["domain"]
    label = decision.get("domain_label", dom)
    L = [f"# Manager brief — {label}", "",
         "You are the lead of a fleet. You do not do the work; you plan it, spawn one",
         "worker per unit, and report the assembled evidence upward.", ""]
    if decision.get("domain_inferred"):
        # Say it out loud. A derived domain that looks chosen is how the cap starts
        # counting sessions against a bucket nobody opened.
        L += [f"The domain `{dom}` was INFERRED from the task text — you did not choose",
              "it. It is recorded as `inferred:" + dom + "`, so it is not counted",
              "against a domain you did choose. Pass `--domain <your-domain>` if you",
              "know which domain this fleet belongs to.", ""]
    L += ["## The task (verbatim — do not paraphrase it in your plan)", "",
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
          "  `" + dom + "`, counted from the `domain` FIELD and not from the name.",
          "  Workers may be named `" + dom + "-<role>-<n>` or anything else that names",
          "  what they ARE (`audit-1`); you record the domain by passing `--domain " + dom
          + "`.",
          "- **One deliverable per unit.** If a unit feels like it needs a sub-fleet,",
          "  the unit is too big: split it, do not nest it.",
          "- **Size units to finish well inside one context window.** A worker that",
          "  runs out of context degrades before it reports.",
          "- The done-test must be able to FAIL. `true` is not a test.", ""]
    L += ["## Step 2 — spawn, one unit at a time", "",
          "```",
          f"fleet spawn {dom}-<role>-1 --domain {dom} --plan plan.md --unit 1 \\",
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
