"""U4 — the fleet entry model.

PURE. The only import is `os.path`. No subprocess, no git, no tmux, no repo.
Every input arrives in its NATIVE shape (the same seam contract pi_index uses).

Why this module exists
----------------------
`fleet` discovered fleets by asking git which worktrees exist. A session with no
worktree therefore did not exist, so 42 of 49 live sessions were invisible to
`fleet list` and `fleet reap` could never see them at all. Discovery ran
backwards: it enumerated the durable objects and treated sessions as a detail of
them, when the session is the thing that has identity.

This module enumerates sessions first and makes the worktree an ATTRIBUTE. That
is the whole of U4, and it is why the unit had to come before the gate.

The worktree is optional from here on
-------------------------------------
`worktree` may be None. `gate()` already tolerated that, but `entries()` could
not produce it. Callers must not assume a worktree, a branch, or a realpath.

Key order is load-bearing
-------------------------
The 7 baseline entries are pinned byte-for-byte against the baseline fixture
captured 2026-10-11, before this module existed (that fixture is internal and
is not shipped; see docs/SCAFFOLD-NOTES.md).
They are the only entries `fleet reap` has ever acted on, and reap deletes a
worktree and a git branch. `sessionId` is therefore inserted only when it is
KNOWN, and `updated_at` is appended only when known: a null key would move the
pinned bytes for no information. This matches the file's existing convention,
which already omits git facts whose value is None.
"""
import os

__all__ = ["build_entries", "FACT_KEYS"]

# Emitted, in this order, when not None. This is the order fleet has always used.
FACT_KEYS = ("branch", "head", "dirty", "ahead", "unpushed", "merged")


def _rp(p):
    return os.path.realpath(p) if p else None


def _liveness(sess, row):
    """"alive" | "alive-no-pane" | "dead" | "unknown" for one entry.

    A missing tmux name used to be written straight to `alive=False`, so a worker
    whose registry row never resolved its pane read DEAD while it was running — and
    "reads dead" is what a reap proposal is made of (measured 2026-10-26: four live
    workers, all listed dead; the broker had already proposed reaping one). The pane
    is ONE witness, not the only one: if /proc knows the pid, a missing pane name is
    not evidence of death.

    "unknown" stays reserved for "there is nothing to ask" — no pane, no pid. Those
    still count as not-alive for callers that may act (`alive` means "provably"), but
    they now say so by name instead of pretending to be dead.
    """
    if sess:
        return "alive"
    pid = (row or {}).get("pid")
    try:
        pid = int(pid) if pid is not None else None
    except (TypeError, ValueError):
        pid = None
    if pid is None:
        return "unknown"
    try:
        with open(f"/proc/{pid}/stat") as fh:
            fh.read(1)
        return "alive-no-pane"              # the process is there; the pane name is not
    except OSError:
        return "dead"


def _entry(name, worktree, session, session_id, alive, status, context,
           base, reported, note, facts, updated_at=None, phase=None,
           pending_ask=None):
    """One fleet entry, built in the pinned key order."""
    e = {"name": name, "worktree": worktree, "session": session}
    if session_id is not None:
        e["sessionId"] = session_id
    e["alive"] = alive
    e["pi_status"] = (status or {}).get("status")
    e["model"] = (status or {}).get("model")
    e["context"] = context
    e["base"] = base
    e["reported"] = reported
    e["note"] = note
    for k in FACT_KEYS:
        v = facts.get(k)
        if v is not None:
            e[k] = v
    if updated_at is not None:
        e["updated_at"] = updated_at
    # Facts the U5 gate reads. Omitted when unknown, so the 7 pinned baseline
    # entries (which have no live session and therefore no phase) do not move.
    if phase is not None:
        e["phase"] = phase
    if pending_ask is not None:
        e["pending_ask"] = pending_ask
    return e


def build_entries(rows, worktrees=(), git_by_path=None, status_by_session=None,
                  context_by_session=None, registry=None, base=None, namer=None):
    """Join session rows with git facts into fleet entries.

    rows       from pi_index.build_index — sessionId-keyed; `worktree` is optional
    worktrees  native shape: [{"path", "branch", "head"}] from git worktree list
    git_by_path    {realpath(worktree): {branch, head, dirty, ahead, unpushed, merged}}
    status_by_session {sessionId: {status, model}}       (the intercom broker)
    context_by_session {tmux_name: "12%/>200k"}       (parsed from the pane)
    registry   {key: {name, reported, note}} — intent, not truth. A key is
               realpath(worktree) when the entry has a worktree, and the
               sessionId when it does not; both are consulted.
    namer      callable(worktree_path) -> fallback name

    A live session inside a fleet worktree is the SAME entry, enriched — never a
    second one. Two entries for one session is the duplicate-identity bug this
    protocol exists to remove.
    """
    git_by_path = git_by_path or {}
    status_by_session = status_by_session or {}
    context_by_session = context_by_session or {}
    registry = registry or {}
    rows = list(rows or [])

    # A row and a worktree are the same thing when they point at the same path.
    by_wt, used = {}, set()
    for r in rows:
        p = _rp(r.get("worktree") or r.get("cwd"))
        if p is not None and p not in by_wt:
            by_wt[p] = r

    def _status(r):
        # Keyed by sessionId, NOT by cwd. 48 of 50 broker sessions report
        # cwd=/root; keying by cwd collapses nearly all of them into one row.
        return status_by_session.get((r or {}).get("sessionId"))

    out = []
    for w in sorted((worktrees or []), key=lambda w: _rp(w.get("path")) or ""):
        wpath = w.get("path")
        rp = _rp(wpath)
        if not wpath or rp is None:
            continue
        r = by_wt.get(rp)
        if r is not None:
            used.add(id(r))
        # realpath(worktree) is the historical key; sessionId is the primary one.
        meta = (registry.get(rp) or registry.get((r or {}).get("sessionId")) or {})
        sess = (r or {}).get("tmux")
        out.append(_entry(
            name=meta.get("name") or (r or {}).get("name") or (namer(wpath) if namer else wpath),
            worktree=wpath,
            session=sess,
            session_id=(r or {}).get("sessionId"),
            alive=bool(sess) or _liveness(sess, r) == "alive-no-pane",
            status=_status(r),
            context=context_by_session.get(sess) if sess else None,
            base=base,
            reported=bool(meta.get("reported")),
            note=meta.get("note"),
            facts=git_by_path.get(rp) or {},
            updated_at=(r or {}).get("updated_at"),
            phase=(r or {}).get("phase"),
            pending_ask=(r or {}).get("pending_ask"),
        ))

    # Everything else is a session that is not backed by a worktree. This is the
    # 42-of-49 that were invisible before U4.
    rest = [r for r in rows if id(r) not in used]
    rest.sort(key=lambda r: (r.get("name") or "", r.get("sessionId") or ""))
    for r in rest:
        sess = r.get("tmux")
        facts = {}
        if r.get("unpushed") is not None:
            facts["unpushed"] = r["unpushed"]
        # A session-class entry has no worktree, so its registry key is its
        # sessionId. Without this, `fleet done <session>` wrote a record that was
        # never read back and `reported` stayed False forever.
        meta = registry.get(r.get("sessionId")) or {}
        out.append(_entry(
            name=meta.get("name") or r.get("name"),
            worktree=r.get("worktree"),
            session=sess,
            session_id=r.get("sessionId"),
            alive=bool(sess) or _liveness(sess, r) == "alive-no-pane",
            status=_status(r),
            context=context_by_session.get(sess) if sess else None,
            base=base,
            reported=bool(meta.get("reported")),
            note=meta.get("note"),
            facts=facts,
            updated_at=r.get("updated_at"),
            phase=r.get("phase"),
            pending_ask=r.get("pending_ask"),
        ))
    return out
