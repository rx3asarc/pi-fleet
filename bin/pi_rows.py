"""pi_rows — the ONE assembly of the index + taxonomy + state.

Extracted from `pi-tree` in U9. `pi-tree` and `pi-sentinel` both need the same
array: every live session, keyed on sessionId, with its taxonomy and its state.
U1-U3's refactor rule is that the JSON from the join is the only data path — a
second reader would be the fifth enumerator this protocol exists to delete.

PURE except for the loaders it is handed; `pi_sources.load_sources()` is the
default and is the ONLY place that touches tmux, /proc, the transcript dir or
git. Nothing here writes.
"""
from __future__ import annotations

from pi_index import build_index
from pi_sources import first_user_messages, load_sources
from pi_taxonomy import resolve
from pi_view import state_of

__all__ = ["build_rows", "user_messages", "MAX_PROMPTS"]


def build_rows(sources=None, first_messages=None):
    """The one array: join on sessionId, then attach the taxonomy and state."""
    sources = sources if sources is not None else load_sources()
    rows = build_index(sources)

    # The FIRST user message, read from the transcript -- not the registry's
    # `topic`, which is a truncated LAST-prompt preview. The old wiring labelled
    # one thing and passed another; the first message is also the more stable
    # signal, because what a session was opened to do does not drift the way its
    # latest prompt does.
    fm = (first_messages if first_messages is not None
          else first_user_messages(sources.get("live", [])))

    for row in rows:
        tax = resolve({
            # A spawn-time declaration outranks every derived guess (U8).
            "name": row.get("name"),
            "cwd": row.get("cwd"),
            "worktree": row.get("worktree"),
            "first_user_message": fm.get(row["sessionId"]) or "",
        }, declared=row.get("taxonomy"))
        row.update(tax)
        row["state"] = state_of(row)
    return rows


# ── the prompt reader (impure; the classifier is in pi_sentinel) ─────────────
MAX_PROMPTS = 60          # enough to see whether a human ever typed
MAX_LINES = 4000          # a bounded read: transcripts reach tens of MB


def user_messages(path, limit=MAX_PROMPTS, max_lines=MAX_LINES):
    """Every user-turn text in a transcript, capped.

    Only `role == "user"` with text parts; tool results and base64 image parts
    are skipped rather than stringified. Line-bounded because the sentinel runs
    this for ~34 sessions every ten minutes and a full read of a 40 MB
    transcript is not a tick, it is a bill.

    An unreadable transcript returns [] — which the classifier reports as
    "unknown", and unknown is never harvested.
    """
    import json

    out = []
    if not path:
        return out
    try:
        with open(path, errors="replace") as fh:
            for n, raw in enumerate(fh):
                if n >= max_lines or len(out) >= limit:
                    break
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(rec, dict) or rec.get("type") != "message":
                    continue
                message = rec.get("message")
                if not isinstance(message, dict) or message.get("role") != "user":
                    continue
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content
                        if isinstance(p, dict) and p.get("type") == "text")
                out.append(content if isinstance(content, str) else "")
    except OSError:
        return []
    return out
