"""pi_sources — the impure half of the seam, in ONE place.

U4. Extracted from `pi-tree` so `fleet` and `pi-tree` cannot drift into two
different readers of the same four sources. Two implementations of a reader is
how the four-join-keys problem started; this is the one place that may touch
tmux, /proc, the transcript directory, or git.

THE CONTRACT (unchanged from U2, and the reason this file is thin)
    Every reader returns its source in its NATIVE shape and keys NOTHING.
    tmux panes    [{"session_name", "pane_pid", "cwd"}]
    pi-live       the durable ledger, one record per session
    registry      /root/.pi/agent/session-names/*.json, as written
    fleet         whatever the `fleet` CLI prints -- entries, NOT rows
    Reconciliation is `pi_index.build_index`, which is pure and fixture-tested.
    Anything clever in here would be untestable by construction.

RECURSION HAZARD -- read this before calling load_sources()
    `pi-tree` reads its fleet source by running `fleet list --json`. Once `fleet`
    consumes `build_index`, that call would re-enter this loader and loop
    forever. `fleet` therefore MUST pass its own `fleet=` reader (the git
    worktrees, which is what it actually has). The default is for everyone else.

READ-ONLY. Nothing here writes. Nothing here signals. `_alive` uses signal 0,
which reports process existence and sends nothing.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess

__all__ = [
    "first_user_message_from_lines", "read_first_user_message", "read_live",
    "read_panes", "read_registry", "read_fleet", "load_sources", "live_records",
]

HOME = os.path.expanduser("~")
REGISTRY_DIR = os.path.join(HOME, ".pi/agent/session-names")
PI_LIVE = os.path.join(HOME, ".pi/agent/bin/pi-live")
FLEET = os.path.join(HOME, ".pi/agent/bin/fleet")
SESSIONS_DIR = os.path.join(HOME, ".pi/agent/sessions")

_MAX_MESSAGE = 1000


def _run(args, timeout=60):
    """Run a command, return stdout or ''. Never raises."""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return p.stdout if p.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _load_json(out, expect_list=True):
    try:
        data = json.loads(out)
    except (json.JSONDecodeError, TypeError):
        return [] if expect_list else {}
    if expect_list and not isinstance(data, list):
        return []
    return data


# ── tmux ─────────────────────────────────────────────────────────────────────
def read_panes():
    """Live tmux panes. `pane_pid` is the pi process's pid -- the join key."""
    out = _run(["tmux", "list-panes", "-a", "-F",
                "#{session_name}\t#{pane_pid}\t#{pane_current_path}"])
    rows = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        rows.append({"session_name": parts[0], "pane_pid": pid, "cwd": parts[2]})
    return rows


FOOTER_PCT = re.compile(r"(\d+(?:\.\d+)?)%/(\d+[kM]?)")


def parse_footer_pct(text):
    """PURE. The context percentage pi prints in its tmux footer.

    `... 39.8%/262k ...` -> 39.8. Unparseable -> None, never 0.0: an unmeasured
    context must not read as an empty one, because `exhausted` is defined as
    "measured >= 90%" and a false 0 would silently disarm it.

    This is the reader U7 was told did not exist. The spec's own implementation
    note claimed "no external reader can reach ctx.getContextUsage()", which was
    false: the number is on screen. `fleet_discovery.context_pct()` had already
    been parsing it; the join simply never asked.
    """
    if not text:
        return None
    m = FOOTER_PCT.search(text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def read_ctx_pct(panes=None):
    """Impure. tmux name -> measured context percentage, for panes that show one."""
    out = {}
    for pane in (panes or []):
        name = pane.get("session_name")
        if not name:
            continue
        pct = parse_footer_pct(_run(["tmux", "capture-pane", "-p", "-t", name]))
        if pct is not None:
            out[name] = pct
    return out


def _alive(pid) -> bool:
    """Impure on purpose. Liveness belongs in the loader, not the pure join.

    Signal 0 sends no signal: it asks the kernel whether the pid exists.
    """
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, TypeError, ValueError):
        return False


# ── pi-live (the durable ledger) ─────────────────────────────────────────────
def live_records(live_only=True):
    """`live_only` is the default because the index is about what is RUNNING.

    The full ledger (~400 records) is the archive behind it; proving has been
    compacted, so an `ended` record is not a live session.
    """
    data = _load_json(_run([PI_LIVE, "--json"]))
    if not live_only:
        return data
    return [r for r in data
            if r.get("sessionId")
            and r.get("state") != "ended"
            and _alive(r.get("pid"))]


def read_live(live_only=True):
    return live_records(live_only=live_only)


# ── the registry (intent/metadata; never the source of truth) ────────────────
def read_registry():
    rows = []
    for path in glob.glob(os.path.join(REGISTRY_DIR, "*.json")):
        try:
            with open(path) as fh:
                rows.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


# ── fleet ────────────────────────────────────────────────────────────────────
def read_fleet():
    """`fleet list --json`. NOTE: fleet itself must NOT use this default."""
    return _load_json(_run([FLEET, "list", "--json"]))


def load_sources(fleet=None):
    """All four sources, NATIVE shapes. The loader keys nothing.

    `fleet` overrides the fleet reader. `fleet` the program must pass its own,
    or this call re-enters `fleet list --json` and recurses without end.
    """
    panes = read_panes()
    return {
        "panes": panes,
        "live": live_records(),
        "registry": read_registry(),
        "fleet": (fleet or read_fleet)(),
        # U7's ctx_pct: measured from each pane's footer, in the one place allowed
        # to touch tmux. Carried as a source so the join stays pure.
        "ctx_pct": read_ctx_pct(panes),
    }


# ── the first user message (pure parser + I/O wrapper) ───────────────────────
def first_user_message_from_lines(lines):
    """The first user message in a transcript, or "".

    PURE. Takes any iterable of raw lines; stops at the first user message, so a
    generator is never advanced past it.

    `content` is either a string or a list of parts, and only `type == "text"`
    carries text -- image and tool parts are skipped rather than stringified,
    because a base64 blob in the taxonomy input would match nothing and cost a
    lot to carry around.
    """
    for raw in lines:
        if not isinstance(raw, str):
            raw = str(raw)
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
        if isinstance(content, str):
            return content[:_MAX_MESSAGE]
        if isinstance(content, list):
            text = " ".join(p.get("text", "") for p in content
                            if isinstance(p, dict) and p.get("type") == "text")
            return text[:_MAX_MESSAGE]
        return ""
    return ""


def read_first_user_message(path):
    """I/O wrapper. Returns "" on any failure -- an unreadable transcript is a
    missing signal, not a reason to stop indexing a session."""
    if not path:
        return ""
    try:
        with open(path, errors="replace") as fh:
            return first_user_message_from_lines(fh)
    except OSError:
        return ""


def first_user_messages(records):
    """{sessionId: first user message} for a list of live records."""
    out = {}
    for r in records or []:
        sid = r.get("sessionId")
        if sid and sid not in out:
            out[sid] = read_first_user_message(r.get("transcript"))
    return out
