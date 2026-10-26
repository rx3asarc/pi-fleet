"""U5 — the gate, split into providers.

PURE. No imports beyond `os` is not even needed here: no filesystem, no tmux, no
git, no subprocess. Everything the gate needs is a fact on the entry, plus one
boolean the caller supplies (`worktree_exists`). That is what makes the decision
testable without a repo, and the decision is the thing that deletes branches.

WHY PROVIDERS
The gate used to be one if-chain that asked git questions of every entry. U4 made
`worktree` optional, and at that moment a session with no branch started being
refused with

    not merged: None @ None is not an ancestor of origin/main

Correct by luck, meaningless to a human, and about to be loaded with a second
class of entry. Providers let each class answer only the questions that apply to
it, and they are registered in a list so adding a class does not touch control
flow.

THE RULE THAT MUST NOT WEAKEN
`accept_silent` means "reap a worker that never reported". It is not an override
for "busy", for "asked a human a question", or for "ownership unproven": none of
those is silence, and none becomes safe because someone passed a flag. There is a
test for each.

OWNERSHIP IS PROVEN BEFORE ANYTHING IS KILLED
The entry carries two independent resolutions of the same question:
  `session`    — what the index resolved, by joining on pane_pid
  `owner_tmux` — what `pi-tmux --live-owner` resolved, through its own guard
They are different implementations on purpose, and they must agree. If they
disagree, one join has drifted, and the cost of guessing which is right is killing
a live session. An unresolvable owner is NOT agreement.
"""
from __future__ import annotations

__all__ = ["gate", "PROVIDERS", "git_provider", "session_provider",
           "brief_provider", "review_provider",
           "ownership_provider", "DEFAULT_BASE"]

DEFAULT_BASE = "origin/main"


def git_provider(e, accept_silent=False, base=DEFAULT_BASE):
    """Git-class reasons. Silent when there is no worktree.

    A session-class entry has no branch, so it has no branch state: there is
    nothing here that could refuse or allow it, and inventing a reason about
    `None @ None` is how the gate stopped meaning anything.
    """
    if not e.get("worktree"):
        return []
    if not e.get("branch"):
        # ARTIFACT-KIND ("spawn --kind task"): it has a worktree to work in but no
        # branch exists and none was ever possible. "not merged: None @ None is not an
        # ancestor of origin/main" is a question that does not apply, and refusing with
        # it made every task-kind child unreapable — measured 2026-10-26 on larch's two:
        # meta-fleet-fork-audit-01 and meta-fleet-orphan-resume-01, both GATED on a merge
        # that could never happen. A task unit's delivery is its ARTIFACT, so gate on
        # that instead: declared files must exist, and a missing one is named.
        # The artifact CHECK is I/O, so it belongs to the caller (fleet_children
        # appends `missing_artifact` before asking us). This function stays pure: it
        # only refuses to ask a question that cannot apply.
        if e.get("missing_artifact"):
            return [f"missing artifact(s): {', '.join(map(str, e['missing_artifact']))} "
                    f"— a task unit delivers files, and these are not on disk"]
        return []
    b = []
    if not e.get("merged"):
        b.append(f"not merged: {e.get('branch')} @ {e.get('head')} is not an "
                 f"ancestor of {base}")
    if e.get("dirty"):
        b.append(f"dirty worktree: {e['dirty']} uncommitted file(s) in {e['worktree']}")
    if e.get("unpushed"):
        b.append(f"unpushed commits: {e['unpushed']} commit(s) not on "
                 f"origin/{e.get('branch')}")
    return b


def session_provider(e, accept_silent=False, base=DEFAULT_BASE):
    """Session-class reasons. Only about a session that is actually alive."""
    if not e.get("alive"):
        return []
    b = []
    # `pi_status` comes from the intercom broker and names the tool
    # ("tool:bash"); `phase` comes from the durable ledger and is coarse
    # ("tool"). Prefer the specific one: the human reading this refusal is
    # deciding whether to reap a session, and "currently inside tool" does not
    # help them decide. Fall back to `phase` when the broker has nothing.
    status = str(e.get("pi_status") or "")
    phase = str(e.get("phase") or "")
    activity = status if status.startswith("tool") else (
        phase if phase.startswith("tool") else "")
    if activity:
        b.append(f"busy: currently inside {activity} — not mid-thought reaping")
    if e.get("pending_ask"):
        b.append(f"asked a question: {e['pending_ask']} — a human is being "
                 f"waited on")
    if not e.get("reported") and not accept_silent:
        b.append("has not reported: mark it with `fleet done <name>` "
                 "(or pass --accept-silent to reap a silent worker)")
    return b


def ownership_provider(e, accept_silent=False, base=DEFAULT_BASE):
    """Refuse unless ownership of the tmux session has been PROVEN.

    Silent when the entry names no tmux session: there is nothing to kill, so
    there is no ownership question. That is what keeps the 7 engrane entries
    (all `session: null`) byte-identical -- none of them has a session to own.

    An empty or absent `owner_tmux` is NOT agreement. A caller that never asked
    the question must not be read as having answered it, and an owner stranded
    outside tmux is a distinct, unsafe state rather than a blank name.
    """
    session = e.get("session")
    if not session:
        return []
    owner = e.get("owner_tmux")
    if not owner:
        return ["ownership unverified: pi-tmux reports no live pi owning this "
                "session, so the tmux session named here cannot be shown to be "
                "the one we mean — refusing to kill it"]
    if owner != session:
        return [f"ownership mismatch: the entry names tmux session '{session}', "
                f"but pi-tmux puts session {e.get('sessionId')} in '{owner}' — "
                f"refusing to kill a session that is not the one we mean"]
    return []


# Registered in a list so a new class is a new entry here, not a new branch
# inside gate(). No provider imports another. Ownership comes FIRST because it is
# the precondition to killing anything, not a detail of one class.
def brief_provider(e, accept_silent=False, base=DEFAULT_BASE):
    """Refuse to sweep a worker that rewrote its own brief.

    The brief carries the frozen done-test and the rules. A worker that edits it has
    rewritten its own contract, so the evidence it reports is evidence against a
    document it authored — which is the self-certification this design exists to
    remove. Detected by hash (fleet_brief.drift), reported here, never guessed.
    """
    why = e.get("brief_drift")
    if why:
        return [f"brief integrity: {why} — refusing to sweep until a human notes it"]
    return []


def review_provider(e, accept_silent=False, base=DEFAULT_BASE):
    """Refuse to sweep a unit an independent reader says FAILS.

    A green self-test is not evidence that the work is right; an adversarial reader
    that ran the artifact in a clean tree is. When that reader says FAILS, the unit is
    not swept quietly -- the parent is told, and a human decides.
    """
    why = e.get("review_fails")
    if why:
        return [f"reviewed FAILS: {why} — the gate cannot sweep work a clean-tree "
                f"reader disproved"]
    return []


PROVIDERS = (ownership_provider, brief_provider, review_provider, git_provider,
             session_provider)


def gate(entry, worktree_exists=False, accept_silent=False, base=DEFAULT_BASE):
    """Every provider's reasons, concatenated. [] means teardown is allowed.

    `worktree_exists` is supplied by the caller because checking the filesystem
    is I/O, and this function is the one that must stay testable without a repo.
    """
    if not entry.get("alive") and not worktree_exists:
        return []          # already effectively gone; nothing to protect
    reasons = []
    for provide in PROVIDERS:
        reasons.extend(provide(entry, accept_silent=accept_silent, base=base))
    return reasons
