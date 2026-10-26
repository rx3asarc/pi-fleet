#!/usr/bin/env python3
"""pi_taxonomy — U1 of the Session Consolidation Protocol.

A PURE function: a session record in, a taxonomy out. No tmux, no git, no
/proc, no filesystem. Everything it needs must be handed to it.

WHY IT EXISTS
  Of 83 named session files, 71 carry no classification, so nothing can be
  grouped or sorted. And the names collide: the namer draws from a fixed
  40-word pool and every session started in a generic directory draws from the
  same pool, so eight names map to 3-4 distinct files each.

WHAT IT DELIVERS
  resolve(entry, declared) -> {domain, topic, subtopic, source, inferred}

  source is HOW the answer was derived. It exists so a derived classification
  can be reviewed rather than silently trusted:
    declared   the spawner said so (PI_TAXONOMY, or a conforming --name)
    worktree   from the git worktree name
    cwd        from the working directory (non-generic only)
    inferred   from a keyword pass over the first user message
    none       nothing derivable -> the explicit `unscoped` bucket

  inferred is True whenever the answer was not declared, i.e. it is a
  derivation and may be wrong.

THE ONE RULE THAT MATTERS
  Nothing derivable lands in an explicit `unscoped` bucket and is COUNTED.
  Never a guess. The unscoped count is the health metric.
"""

from __future__ import annotations

import os
import re

__all__ = ["resolve", "validate_name", "audit_names", "WORKING_LIMIT",
           "GENERIC_DIRS", "KNOWN_DOMAINS"]

# Directories that say nothing about what a session is about. `/root` is the
# important one: Maestro always works there, which is exactly why every session
# in it falls through to the random-word branch of the namer.
GENERIC_DIRS = frozenset({
    "root", "home", "usr", "tmp", "var", "opt", "etc", "mnt", "srv", "bin",
    "lib", "proc", "sys", "dev", "run", "app", "code", "src", "workspace",
    "node_modules", "pi", "projects",
})

# Domains this fleet actually uses. A prefix that is not one of these is not a
# domain -- which is why `alder-gale` is unscoped rather than domain `alder`.
KNOWN_DOMAINS = ("nordisk", "engrane", "enc1", "meta", "infra")

# Name shapes that carry a fixed meaning beyond <domain>-<topic>.
SUBTOPIC_ALIASES = {
    "helper": "swarm.helper",
    "lead": "swarm.lead",
}

OUT_UNSCOPED = {"domain": "unscoped", "topic": "", "subtopic": ""}


def _norm(value) -> str:
    return str(value or "").strip().lower()


def _split_declared(text: str):
    """`nordisk-theme-dup-create` -> (nordisk, theme, dup-create).

    Also the explicit `PI_TAXONOMY` form `domain/topic/subtopic`: a slash is a
    SEGMENT separator, so `nordisk/water/copy` is three parts rather than one
    domain string with two slashes in it (the shape it took before U8 spawned a
    child with a declaration).
    """
    text = _norm(text).replace("_", "-")
    if "/" in text:
        segs = [x for x in text.split("/") if x]
        if not segs or len(segs) > 3:
            return None
        domain = segs[0]
        topic = segs[1] if len(segs) > 1 else ""
        subtopic = segs[2] if len(segs) > 2 else ""
        return domain, topic, subtopic
    tokens = [t for t in text.split("-") if t]
    if not tokens:
        return None
    domain = tokens[0]
    topic = tokens[1] if len(tokens) > 1 else ""
    subtopic = "-".join(tokens[2:]) if len(tokens) > 2 else ""
    return domain, topic, subtopic


def _from_name(name: str):
    """A --name that already encodes the taxonomy counts as declared.

    `nordisk-goal-helper-2` -> nordisk / goal / swarm.helper
    `nordisk-main`         -> nordisk / storefront
    `engrane-operations`   -> engrane / operations
    `marrow`               -> None   (a bare word carries nothing)
    """
    tokens = [t for t in _norm(name).split("-") if t]
    if len(tokens) < 2:
        return None
    domain = tokens[0]
    if domain not in KNOWN_DOMAINS:
        return None

    rest = tokens[1:]
    # <domain>-goal-(helper|lead)[-N] -> goal / swarm.<role>
    if len(rest) >= 2 and rest[0] == "goal" and rest[1] in SUBTOPIC_ALIASES:
        return domain, "goal", SUBTOPIC_ALIASES[rest[1]]
    # <domain>-goal[-N] -> goal
    if rest[0] == "goal":
        return domain, "goal", ""
    # <domain>-main -> storefront (the storefront session)
    if rest[0] == "main":
        return domain, "storefront", ""
    return domain, rest[0], "-".join(rest[1:])


DECLARATION_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*){0,2}$")


def declaration_of_name(name: str):
    """The DECLARATION a spawn should export for this name, as `d/t[/s]`.

    None when the name carries no domain. This is the spawn-side half of the U8
    contract: `PI_TAXONOMY` at spawn is how a declaration becomes a byproduct of
    spawning instead of a tagging act somebody has to remember.
    """
    parts = _from_name(name)
    if not parts:
        return None
    domain, topic, subtopic = parts
    segs = [x for x in (domain, topic, subtopic) if x]
    return "/".join(segs) if len(segs) > 1 else None


def parse_declaration(text):
    """PURE. Validate a `PI_TAXONOMY`-shaped declaration; None when it is not one.

    Deliberately strict: lowercase, digits and hyphens, one to three slash-separated
    segments. A malformed declaration is REJECTED rather than coerced, because a
    coerced value lands in the identity store where nobody will ever look at it
    again -- and an unreadable record is worse than an unscoped one, which at least
    shows up in the unscoped count.
    """
    if not text:
        return None
    value = str(text).strip().lower()
    if not DECLARATION_RE.match(value):
        return None
    return value


def _from_path(path: str):
    """`/mnt/.../projects/engrane-citations` -> engrane / citations.

    Either the leading token is a known domain, or the basename IS one.
    """
    base = os.path.basename(_norm(path).rstrip("/"))
    if not base:
        return None
    tokens = [t for t in base.split("-") if t]
    if not tokens:
        return None
    if tokens[0] in KNOWN_DOMAINS:
        return tokens[0], "-".join(tokens[1:]), ""
    if base in KNOWN_DOMAINS:
        return base, "", ""
    return None


def _from_message(message: str):
    """Keyword pass. Deliberately weak, and always flagged `inferred`."""
    text = _norm(message)
    if not text:
        return None
    for domain in KNOWN_DOMAINS:
        if domain in text:
            return domain, "", ""
    # The storefront work is referred to by its own vocabulary.
    if any(w in text for w in ("shopify", "theme", "storefront", "produkt")):
        return "nordisk", "", ""
    return None


def _from_cwd(cwd: str):
    """Working directory — but ONLY when it is not generic.

    `/root` is the important exclusion: Maestro always works there, which is
    exactly why every session in it falls through to the namer's random word.
    """
    if not cwd:
        return None
    base = os.path.basename(_norm(cwd).rstrip("/"))
    if not base or base in GENERIC_DIRS:
        return None
    return _from_path(cwd)


# ── the priority order, as DATA ───────────────────────────────────────────────
# Read top to bottom: the first one to produce an answer wins. Keeping this as
# an ordered list rather than an if-chain makes the priority auditable at a
# glance, and lets a later unit add a source without touching the control flow.
#
# Each entry is (source_label, extractor). Every extractor is PURE.
def _resolvers(entry: dict, declared):
    name = entry.get("name", "")
    return [
        # 1. declared: an explicit value, or a --name that already encodes it
        ("declared", _split_declared(declared) if declared else None),
        ("declared", _from_name(name)),
        # 2. worktree name
        ("worktree", _from_path(entry["worktree"]) if entry.get("worktree") else None),
        # 3. working directory, non-generic only
        ("cwd", _from_cwd(entry.get("cwd", ""))),
        # 4. keyword pass over the first user message (a guess)
        ("inferred", _from_message(entry.get("first_user_message", ""))),
    ]


def resolve(entry: dict, declared=None) -> dict:
    """Resolve one session record to a taxonomy.

    Priority: declared -> conforming name -> worktree -> cwd -> message -> unscoped.
    Pure: never mutates `entry`, never touches the outside world.
    """
    entry = entry or {}

    for source, parts in _resolvers(entry, declared):
        if parts:
            domain, topic, subtopic = parts
            return {
                "domain": domain,
                "topic": topic,
                "subtopic": subtopic,
                "source": source,
                # `inferred` means the answer is a GUESS, not merely un-declared.
                # A worktree or cwd answer is a structural fact about where the
                # session is; only the keyword pass and the nothing-found case
                # are guesses.
                "inferred": source in ("inferred", "none"),
            }

    # 5. nothing derivable. Explicit bucket, counted, never guessed.
    return dict(OUT_UNSCOPED, source="none", inferred=True)


if __name__ == "__main__":  # tiny manual probe; the tests are the contract
    import json
    import sys

    for line in sys.stdin:
        line = line.strip()
        if line:
            print(json.dumps(resolve(json.loads(line))))


# ── the naming contract (U8) ─────────────────────────────────────────────────

# How many live sessions one domain may have before a spawn starts complaining.
# It is deliberately a WARNING threshold, never a gate: blocking a spawn teaches
# people to work around the tool, and then the tool stops seeing reality.
WORKING_LIMIT = 3

_NAME_HELP = (
    "names are <domain>-<topic>[-<subtopic>][-<seq>], with domain one of "
    + ", ".join(KNOWN_DOMAINS)
    + " -- e.g. nordisk-theme-dup-create-01. "
    "Pass --unscoped to spawn anyway."
)


def _ts(value):
    """Epoch-ish value as a number; unknown sorts LAST, so it is never 'oldest'."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("inf")


def _read_state(row):
    """The row's state, or None when it cannot be read. Never guesses `idle`.

    State is attached by the caller (`pi_view.with_states`), which is the ONE
    place state is derived. Re-deriving it here would be a second opinion about
    what "idle" means, and two opinions are how the fleet started disagreeing.
    """
    state = row.get("state")
    return state if isinstance(state, str) and state else None


def validate_name(name, live_rows=(), unscoped=False):
    """May this name be spawned? Pure: live rows are passed in.

    Returns a decision, never raises:

        ok             may the spawn proceed
        domain/topic/subtopic   what the name declares
        error          why not (None when ok)
        warning        the working limit was exceeded (never a refusal)
        accelerate     the oldest idle session in the domain, or None
        live_in_domain how many are already live there

    Over the limit is a warning, not an error, and `accelerate` is DERIVED rather
    than stored: the condition is computable from live rows at any time, so no
    new state and no new writer is needed to remember it.
    """
    parsed = _from_name(name) or (None, None, None)
    out = {
        "name": name, "ok": True,
        "domain": parsed[0], "topic": parsed[1], "subtopic": parsed[2],
        "error": None, "warning": None, "accelerate": None,
        "live_in_domain": 0,
    }

    if not out["domain"]:
        if not unscoped:
            out["ok"] = False
            out["error"] = f"unscoped name: {name!r}. {_NAME_HELP}"
        return out

    live = [
        r for r in (live_rows or [])
        if r.get("name") and r.get("name") != name
        and (_from_name(r["name"]) or (None,))[0] == out["domain"]
    ]
    out["live_in_domain"] = len(live)

    if len(live) < WORKING_LIMIT:
        return out

    out["warning"] = (
        f"{out['domain']} already has {len(live)} live session(s): "
        + ", ".join(r["name"] for r in live)
        + f" (working limit {WORKING_LIMIT}). Spawning anyway."
    )

    # A session waiting on a human is never the one to retire, and a session
    # mid-thought is never the one either. Only a confirmed idle session that
    # owes nobody an answer is a candidate.
    candidates = [
        r for r in live
        if _read_state(r) == "idle" and not r.get("pending_ask")
    ]
    if candidates:
        oldest = min(candidates, key=lambda r: _ts(r.get("updated_at")))
        out["accelerate"] = oldest["name"]
        out["warning"] += (
            f" Oldest idle: {oldest['name']} -- marked for accelerated reaping."
        )
    return out


def audit_names(rows):
    """Naming health of a LIVE fleet, judged by the contract spawn enforces.

    The audit refuses nothing -- those sessions are already running. It answers the
    other half of the same question: which live names would be refused today, and
    which domains are over the working limit and by whose retirement.

    This exists so spawn and the audit view share ONE law about names. Two
    components each deciding "is this a good name" is how they start disagreeing.
    """
    live = [r for r in (rows or []) if r.get("name")]
    unscoped, over_limit = [], {}
    for r in live:
        # `unscoped=True` because an existing unscoped name is a finding to report,
        # not an error to raise. We are auditing, not refusing.
        d = validate_name(r["name"], live, unscoped=True)
        if not d["domain"]:
            if r["name"] not in unscoped:
                unscoped.append(r["name"])
        elif d["warning"]:
            # Count the domain's OWN size, not `live_in_domain`, which counts the
            # others (it is measured from the name being spawned, which is not yet
            # live). An audit that says "nordisk 23" when 24 sessions are running is
            # wrong in a way a human cannot see.
            over_limit[d["domain"]] = {
                "live": sum(
                    1 for x in live
                    if (_from_name(x["name"]) or (None,))[0] == d["domain"]
                ),
                "accelerate": d["accelerate"],
            }
    return {"unscoped": sorted(unscoped), "over_limit": over_limit}
