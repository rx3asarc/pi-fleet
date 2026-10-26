"""fleet_discovery — turn the world into ENTRIES.

Extracted from `fleet` unchanged in behaviour: git worktrees, git facts, the
pane context readout, the registry, the index join, and the name helpers. This is
the impure "what exists right now" half, and it is the only place that may run
git or tmux for that purpose -- `fleet_children` may not grow a second copy of
it, for the same reason a fifth session enumerator was forbidden in U4.

`entries()` is deliberately the SAME join `pi-tree` uses (`pi_index.build_index`)
with the fleet leg supplied from here, because `pi_sources.read_fleet()` would
call back into the CLI and recurse forever.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet_entries  # noqa: E402
import fleet_paths    # noqa: E402
import pi_index       # noqa: E402
import pi_sources     # noqa: E402
from fleet_util import clip, die, sh  # noqa: E402,F401

# THE REPO TARGET IS RESOLVED, NOT ASSUMED. This used to be a private project's
# absolute path, which meant a fresh clone pointed at somebody else's tree and
# said nothing about it. `fleet_paths.repo()` is $FLEET_REPO, else the repository
# you are standing in; None is an honest answer, and the git source is skipped
# (loudly, once) rather than run against a guess.
REPO = fleet_paths.repo()
BASE = os.environ.get("FLEET_BASE", "origin/main")
REGISTRY = Path(os.environ.get("FLEET_REGISTRY") or fleet_paths.control_dir() / "registry.json")
TRUST = Path(os.environ.get("FLEET_TRUST") or fleet_paths.agent_home() / "trust.json")

_repo_hint_printed = False


def repo_target(stream=None):
    """The repo to ask about, or None after saying once what to set.

    Silence was the old behaviour and it was the bug: `fleet list` against no
    target looked exactly like a fleet with no worktrees.
    """
    global _repo_hint_printed
    if REPO is not None:
        return REPO
    if not _repo_hint_printed:
        _repo_hint_printed = True
        print(f"  ! {fleet_paths.REPO_HINT} — worktree discovery from git is off; "
              f"session discovery still runs", file=stream or sys.stderr)
    return None


def git_worktrees(repo: Path):
    """Linked worktrees of `repo`: [{path, branch, head}] (main checkout excluded)."""
    rc, out, _ = sh(["git", "-C", str(repo), "worktree", "list", "--porcelain"])
    if rc != 0:
        return []
    wts, cur = [], {}
    for line in out.splitlines():
        if line.startswith("worktree "):
            cur = {"path": line[9:].strip()}
        elif line.startswith("branch "):
            cur["branch"] = line[7:].strip().replace("refs/heads/", "")
        elif line.startswith("HEAD "):
            cur["head"] = line[5:].strip()
        elif line == "" and cur:
            if "path" in cur:
                wts.append(cur)
            cur = {}
    if cur and "path" in cur:
        wts.append(cur)
    main = os.path.realpath(str(repo))
    return [w for w in wts if os.path.realpath(w["path"]) != main]


def git_facts(wt: str, base: str, repo: Path):
    """Derived, per-run git facts for one worktree."""
    f = {"branch": None, "head": None, "dirty": None, "ahead": None,
         "unpushed": None, "merged": None}

    rc, out, _ = sh(["git", "-C", wt, "rev-parse", "--abbrev-ref", "HEAD"])
    f["branch"] = out if rc == 0 else None

    rc, out, _ = sh(["git", "-C", wt, "rev-parse", "--short", "HEAD"])
    f["head"] = out if rc == 0 else None

    rc, out, _ = sh(["git", "-C", wt, "status", "--porcelain"])
    f["dirty"] = len([l for l in out.splitlines() if l.strip()]) if rc == 0 else None

    # ahead of base
    rc, out, _ = sh(["git", "-C", wt, "rev-list", "--count", f"{base}..HEAD"])
    f["ahead"] = int(out) if rc == 0 and out.isdigit() else None

    # merged? (HEAD is an ancestor of base)
    #
    # ASKED IN THE WORKTREE'S OWN REPO, NOT IN THE DEFAULT ONE (MEASURED 2026-10-26).
    # This used to resolve `base` in `repo`, which is fleet_paths.repo() -- ONE default
    # project's repo -- for every discovered worktree. For any worker whose work lives
    # in a different repo, `merge-base --is-ancestor <its head> origin/main` was asked of
    # the wrong repository and answered "fatal: Not a valid object name", which the gate
    # turned into "not merged: <branch> @ <head> is not an ancestor of origin/main" -- a
    # false refusal about a merge that HAD happened. Measured on scrub-fixtures: its tip
    # 423c165 is an ancestor of origin/main in its own clone (`echo $?` -> 0) and the gate
    # still refused it; the same command in the default repo exits 128. A refusal that
    # cannot be satisfied is an unreapable worker or a forced `--abandon`, so the question
    # is now asked where it can be answered: a worktree resolves origin/main through the
    # repo it shares with its siblings, and a plain clone resolves its own.
    if f["head"]:
        rc, _, _ = sh(["git", "-C", str(wt), "merge-base", "--is-ancestor",
                       f["head"], base])
        f["merged"] = (rc == 0)

    # unpushed: compare against the remote-tracking ref if it exists
    if f["branch"]:
        rc, _, _ = sh(["git", "-C", wt, "rev-parse", "--verify", "--quiet",
                       f"refs/remotes/origin/{f['branch']}"])
        if rc == 0:
            rc, out, _ = sh(["git", "-C", wt, "rev-list", "--count",
                             f"origin/{f['branch']}..HEAD"])
            f["unpushed"] = int(out) if rc == 0 and out.isdigit() else None
        else:
            # no upstream: every local commit is unpushed
            f["unpushed"] = f["ahead"]
    return f


def context_pct(session: str):
    """Parse pi's footer out of the tmux pane. None if unparseable."""
    rc, out, _ = sh(["tmux", "capture-pane", "-p", "-t", session])
    if rc != 0:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)%/(\d+[kM]?)", out)
    return f"{m.group(1)}%/{m.group(2)}" if m else None


# ── registry (intent/metadata only; never the source of truth) ───────────────
def load_registry():
    if not REGISTRY.exists():
        return {}
    try:
        return json.loads(REGISTRY.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_registry(reg):
    REGISTRY.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY.with_suffix(".tmp")
    tmp.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")
    tmp.replace(REGISTRY)


def seed_trust(path: str):
    """Pre-answer pi's 'Trust parent folder' prompt so spawn is unattended."""
    data = {}
    if TRUST.exists():
        try:
            data = json.loads(TRUST.read_text())
        except (json.JSONDecodeError, OSError):
            data = {}
    data[os.path.realpath(path)] = True
    TRUST.parent.mkdir(parents=True, exist_ok=True)
    TRUST.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


# ── the model ────────────────────────────────────────────────────────────────
def discover_worktrees(reg):
    """Worktree-shaped discovery. Each source is tried; first wins per path.

    `git_worktrees()` is one SOURCE, not the definition of a fleet -- that
    conflation is what made 42 of 49 live sessions invisible. Adding another
    source must not touch this control flow, and one source failing must not
    hide the others.
    """
    sources = (
        ("git", lambda: git_worktrees(repo_target()) if repo_target() else []),
        # Worktrees git no longer reports, but the registry still remembers.
        ("registry", lambda: [{"path": m.get("worktree"), "branch": m.get("branch")}
                               for m in reg.values() if m.get("worktree")]),
    )
    out, seen = [], set()
    for _label, fn in sources:
        try:
            rows = fn() or []
        except Exception:
            continue
        for w in rows:
            path = w.get("path")
            if not path:
                continue
            rp = os.path.realpath(path)
            if rp in seen:
                continue
            seen.add(rp)
            out.append(w)
    return out


def pi_status_by_session():
    """{sessionId: {name, status, model}} from the intercom broker.

    Keyed by the broker's `id`, which IS the pi sessionId (verified: 49 of 50
    broker ids appear in the pi-live ledger). It used to be keyed by
    realpath(cwd), the same class of mistake as keying identity by name: 48 of 50
    broker sessions report cwd=/root, so 48 sessions shared one status record.
    """
    rc, out, _ = sh(["pi-msg", "list", "--json"])
    if rc != 0:
        return {}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {}
    m = {}
    for s in data.get("sessions", []):
        sid = s.get("id")
        if sid:
            m[sid] = s
    return m


def entries():
    """Every live session, plus every fleet worktree. Keyed on sessionId.

    Discovery used to be `git_worktrees()` alone, so a session with no worktree
    did not exist: 42 of 49 live sessions were invisible here, and `fleet reap`
    could never see them. Discovery is now the same join `pi-tree` uses,
    `pi_index.build_index`, which is pure and fixture-tested.
    """
    reg = load_registry()
    wts = discover_worktrees(reg)

    git_by = {os.path.realpath(w["path"]): git_facts(w["path"], BASE, REPO)
              for w in wts if w.get("path") and os.path.isdir(w["path"])}

    rows = pi_index.build_index({
        "panes": pi_sources.read_panes(),
        "live": pi_sources.live_records(),
        "registry": pi_sources.read_registry(),
        # NOT pi_sources.read_fleet(): that runs `fleet list --json`, which is
        # this function, and would recurse without end. fleet's own "fleet"
        # source is the git worktrees it actually has.
        "fleet": [{"worktree": w["path"], **git_by.get(os.path.realpath(w["path"]), {})}
                  for w in wts if w.get("path")],
    })

    return fleet_entries.build_entries(
        rows=rows,
        worktrees=wts,
        git_by_path=git_by,
        status_by_session=pi_status_by_session(),
        context_by_session={r["tmux"]: context_pct(r["tmux"])
                            for r in rows if r.get("tmux")},
        registry=reg,
        base=BASE,
        namer=derive_name,
    )




def derive_name(worktree: str) -> str:
    """`/path/theme-foo` under a repo named `theme` -> `foo`.

    With no resolved repo there is no prefix to strip, so the directory name is
    the name: better a name with a prefix than a name derived from a repository
    nobody asked for.
    """
    stem = Path(worktree).name
    prefix = (REPO.name + "-") if REPO is not None else ""
    return stem[len(prefix):] if prefix and stem.startswith(prefix) else stem


def meta_name(worktree: str) -> str:
    return os.path.realpath(worktree)


def entry_key(e) -> str:
    """The registry key for an entry.

    realpath(worktree) when the entry has a worktree; the sessionId when it does
    not, because sessionId is the primary key. `realpath(None)` raises TypeError,
    which is how `fleet done <session>` would have crashed the moment the
    worktree became optional.
    """
    wt = e.get("worktree")
    if wt:
        return os.path.realpath(wt)
    return e.get("sessionId") or e.get("name") or ""


def find(name: str):
    for e in entries():
        if e["name"] == name:
            return e
    return None
