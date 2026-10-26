#!/usr/bin/env python3
"""pi_view — U3 of the Session Consolidation Protocol.

Groups rows by domain -> topic -> subtopic, sorts by state -> ctx -> recency, and
renders them.

WHY THIS EXISTS
  Reported symptom: "43 is far too many". The fix is not a shorter list; it is a
  list where 23 sessions from one finished goal are ONE line with a count, and the
  number that actually needs attention is at the top.

THE STATE VOCABULARY (drives the sort)
  needs-you   a pending ask addressed to Maestro          <- ALWAYS first
  working     currently inside a tool call
  exhausted   ctx >= 90% -- cannot carry another unit; must be harvested
  harvestable holds unpushed commits
  idle        parked, nothing open

  `exhausted` is not the same as `idle`. One is finished; the other is stuck.
  Collapsing them is how a reaper loses work.

PURE. Rows in, text out. No I/O.
"""

from __future__ import annotations

# The naming law lives in pi_taxonomy and is shared with the spawn path, so the
# audit view and `fleet spawn` can never disagree about what a valid name is.
# This module already read taxonomy's output shape (row["domain"]); the import
# makes that dependency visible instead of implicit.
import time

import pi_taxonomy

__all__ = ["state_of", "group_rows", "summary", "render", "STATE_RANK",
           "is_drifted"]

STATE_RANK = ["needs-you", "working", "exhausted", "harvestable", "idle"]
_RANK = {s: i for i, s in enumerate(STATE_RANK)}

EXHAUSTED_AT = 90.0
_MAX_MEMBERS_SHOWN = 3


def state_of(row: dict) -> str:
    """The single state of a row. Precedence is the STATE_RANK order."""
    if row.get("pending_ask"):
        return "needs-you"
    if str(row.get("phase") or "").startswith("tool"):
        return "working"
    try:
        if float(row.get("ctx_pct") or 0) >= EXHAUSTED_AT:
            return "exhausted"
    except (TypeError, ValueError):
        pass
    try:
        if int(row.get("unpushed") or 0) > 0:
            return "harvestable"
    except (TypeError, ValueError):
        pass
    return "idle"


def _sort_key(row):
    """state rank, then ctx descending, then recency (newest first), then name."""
    return (
        _RANK.get(state_of(row), len(STATE_RANK)),
        -float(row.get("ctx_pct") or 0),
        -float(row.get("updated_at") or 0),
        row.get("name") or "",
    )


def group_rows(rows) -> dict:
    """domain -> topic -> subtopic -> [rows], each level sorted by _sort_key."""
    out = {}
    for row in rows or []:
        d = row.get("domain") or "unscoped"
        t = row.get("topic") or ""
        s = row.get("subtopic") or ""
        out.setdefault(d, {}).setdefault(t, {}).setdefault(s, []).append(row)
    for d in out:
        for t in out[d]:
            for s in out[d][t]:
                out[d][t][s].sort(key=_sort_key)
    return out


def with_states(rows):
    """Rows with their derived state attached.

    ONE place attaches state. The name contract (U8) needs to know which sessions
    are idle, and if it derived that itself the fleet would have two opinions about
    what "idle" means -- which is how four tools started disagreeing in the first
    place.
    """
    return [dict(r, state=state_of(r)) for r in (rows or [])]


def is_drifted(row: dict) -> bool:
    """The tmux name and the pi name disagree for this session.

    Drift is not a curiosity -- it is the symptom that makes every name-keyed
    tool wrong. Its count is a health metric, reported next to `unscoped`.
    """
    tmux = row.get("tmux")
    name = row.get("name")
    return bool(tmux) and bool(name) and tmux != name


def summary(rows) -> dict:
    """Totals, the two health metrics (unscoped, drift), and the state histogram."""
    rows = list(rows or [])
    by_state = {s: 0 for s in STATE_RANK}
    for r in rows:
        by_state[state_of(r)] += 1
    return {
        "total": len(rows),
        "unscoped": sum(1 for r in rows if (r.get("domain") or "unscoped") == "unscoped"),
        "drift": sum(1 for r in rows if is_drifted(r)),
        "over_limit": pi_taxonomy.audit_names(with_states(rows))["over_limit"],
        "by_state": by_state,
    }


def _subtopic_rank(subs: dict) -> tuple:
    """Best state rank under {subtopic: [rows]}, then size descending."""
    best = len(STATE_RANK)
    size = 0
    for members in subs.values():
        size += len(members)
        for m in members:
            best = min(best, _RANK.get(state_of(m), len(STATE_RANK)))
    return (best, -size)


def _topic_rank(subs: dict) -> tuple:
    return _subtopic_rank(subs)


def _domain_rank(topics: dict) -> tuple:
    """Best state rank under {topic: {subtopic: [rows]}}, then size descending."""
    best = len(STATE_RANK)
    size = 0
    for subs in topics.values():
        rank, neg_size = _subtopic_rank(subs)
        best = min(best, rank)
        size += -neg_size
    return (best, -size)


def render(rows) -> str:
    """The grouped view. Deterministic: same rows -> same text, always."""
    rows = list(rows or [])
    if not rows:
        return "pi-tree — no live sessions."

    s = summary(rows)
    lines = []
    lines.append(f"pi-tree — {s['total']} live sessions  "
                 f"(needs-you {s['by_state']['needs-you']}, "
                 f"unscoped {s['unscoped']}, drift {s['drift']})")
    # A domain holding more than the working limit is the consolidation problem in
    # one line. Reported here, by the same law spawn enforces.
    for domain, info in sorted(s["over_limit"].items()):
        victim = info.get("accelerate")
        tail = f" — retire {victim}" if victim else " — nothing idle to retire"
        lines.append(f"  over limit: {domain} {info['live']} live "
                     f"(limit {pi_taxonomy.WORKING_LIMIT}){tail}")
    lines.append("")

    groups = group_rows(rows)
    for domain in sorted(groups, key=lambda d: (_domain_rank(groups[d]), d)):
        topics = groups[domain]
        dom_count = sum(len(m) for st in topics.values() for m in st.values())
        dom_states = {}
        for st in topics.values():
            for members in st.values():
                for m in members:
                    k = state_of(m)
                    dom_states[k] = dom_states.get(k, 0) + 1
        badge = " ".join(f"{k}:{v}" for k, v in
                         sorted(dom_states.items(), key=lambda x: _RANK[x[0]]))
        lines.append(f"▸ {domain}  [{dom_count}]  {badge}")

        for topic in sorted(topics, key=lambda t: (_topic_rank(topics[t]), t)):
            subs = topics[topic]
            total = sum(len(m) for m in subs.values())
            # The TOPIC is the collapse unit: one line, carrying the total across
            # all its subtopics. This is what makes 23 goal-swarm sessions read as
            # one thing instead of three.
            lines.append(f"   ├─ {topic or '-'}  ({total})")

            # A topic whose only subtopic is unnamed needs no second level.
            flat = len(subs) == 1 and "" in subs
            for subtopic in sorted(subs, key=lambda s: (_subtopic_rank({s: subs[s]}), s)):
                members = subs[subtopic]
                if flat:
                    indent = "   │    "
                else:
                    lines.append(f"   │  ├─ {subtopic or '-'}  ({len(members)})")
                    indent = "   │  │    "
                for m in members[:_MAX_MEMBERS_SHOWN]:
                    mark = {"needs-you": "◆", "working": "▶", "exhausted": "✗",
                            "harvestable": "*", "idle": "·"}[state_of(m)]
                    lines.append(f"{indent}{mark} {m.get('name') or '?':<26}"
                                 f"{str(m.get('lastPrompt') or '')[:40]}")
                if len(members) > _MAX_MEMBERS_SHOWN:
                    tag = "" if flat else f" ({subtopic})"
                    lines.append(f"{indent}… +{len(members) - _MAX_MEMBERS_SHOWN}"
                                 f" more{tag}")
        lines.append("")
    return "\n".join(line.rstrip() for line in lines).rstrip() + "\n"


def render_flat(rows, sort="state") -> str:
    """A flat list in the order the given question needs. Deterministic.

    `sort=state` is TRIAGE: everything that needs the operator first.
    `sort=age`   is the REAPING QUEUE: the longest-parked session first. It is
                 sorted oldest-first by the caller's key and never re-sorted
                 here, because the caller owns the policy and this owns the
                 rendering.

    Grouping is deliberately absent: a grouped view answers "what is running",
    and both of these verbs answer a different question.
    """
    rows = list(rows or [])
    if not rows:
        return "pi-tree — no matching sessions."
    width = max((len(str(r.get("name") or "")) for r in rows), default=0)
    width = min(max(width, 12), 34)
    lines = [f"{'state':<12}{'name':<{width + 2}}" + ("age" if sort == "age"
                                                    else "domain/topic")]
    for r in rows:
        state = state_of(r)
        mark = {"needs-you": "◆", "working": "▶", "exhausted": "✗",
                "harvestable": "*", "idle": "·"}.get(state, "·")
        if sort == "age":
            ts = r.get("updated_at")
            when = _age(ts)
        else:
            when = " / ".join(x for x in (r.get("domain"), r.get("topic")) if x)
        lines.append(f"{mark} {state:<11}{(r.get('name') or '?'):<{width + 2}}"
                     f"{when}")
    return "\n".join(lines)


def _age(ts):
    """Human age of an epoch timestamp, in SECONDS or MILLISECONDS.

    The live join carries `updated_at` in milliseconds (pi's own clock), and this
    function assumed seconds -- so every row rendered `0s` while real ages ran
    5s..77.8h. Ordering survived because all rows were wrong by the same factor,
    which is why an idempotency test never caught it. Epoch seconds stay below
    1e11 until the year 5138, so anything larger is milliseconds.
    """
    try:
        value = float(ts)
    except (TypeError, ValueError):
        return "unknown"
    if value > 1e11:
        value /= 1000.0
    try:
        seconds = max(0.0, time.time() - value)
    except (TypeError, ValueError):
        return "unknown"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{int(seconds // size)}{unit}"
    return f"{int(seconds)}s"


if __name__ == "__main__":
    import json
    import sys

    print(render([json.loads(line) for line in sys.stdin if line.strip()]))
