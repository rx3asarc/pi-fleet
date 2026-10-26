#!/usr/bin/env python3
"""fleet_paths — the ONE place that decides where fleet keeps its things.

Everything else under `bin/` asks this module instead of writing an absolute
path. A hardcoded path is a path that only works on the box it was written on,
and the fleet is meant to be installable by someone who is not the person who
wrote it.

Every location has an environment override, and a default that reproduces the
original behaviour when the tool runs as root on the machine it grew up on:

    FLEET_CONTROL_DIR   default ~/.fleet        lineage, registry, ledgers, verify-cache
    FLEET_WORK_ROOT     default ~/fleet         task scratch dirs, spawn worktrees
    PI_AGENT_HOME       default ~/.pi/agent     sessions, session-names, settings, trust
    FLEET_HANDOFF_DIR   default ~/handoffs      reaped digests, bundle output
    FLEET_REPORTS_DIR   default ~/docs/reports  rollup reports (the PUSH half)
    FLEET_BIN_DIR       default: the directory this file lives in
    FLEET_REPO          no default — see repo()

`~` is resolved from $HOME, so the defaults follow the USER rather than
asserting one home directory. Set the override in the environment (or in the
cron line) and nothing else has to change.

THE REPO TARGET IS THE ONE DEFAULT THAT DELIBERATELY CHANGED
The old default targeted a private project on the author's box, so a fresh
clone silently listed the worktrees of somebody else's repository. `repo()`
resolves: $FLEET_REPO, else the git repository you are standing in, else
`<work root>/repo` if that is a git repository, else None — and every caller
that cannot work without one says what to set (REPO_HINT) instead of guessing.

Shell scripts under `bin/` cannot import this module, so they compute the same
defaults from $HOME and the script's own directory. `test_fleet_paths.py`
asserts the two agree, because a seam that drifts in two directions is worse
than the constants it replaced.
"""
from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "home", "control_dir", "work_root", "agent_home", "handoff_dir",
    "reports_dir", "bin_dir", "repo", "require_repo", "describe",
    "REPO_HINT",
]

# What to tell a human when a verb needs a repository target and there is none.
REPO_HINT = ("no repo target: set FLEET_REPO=/path/to/repo, run the command inside "
             "the repository, or pass --repo")


def home() -> Path:
    """The user's home directory, as $HOME says, not as an author assumed."""
    return Path(os.path.expanduser("~"))


def _env(name: str):
    """An override path, or None when unset/empty. Empty means unset: a cron line
    that exports an unset variable (`FLEET_CONTROL_DIR=$STATE`) must not resolve
    to the current directory."""
    raw = os.environ.get(name)
    if not raw:
        return None
    return Path(os.path.expanduser(raw))


def control_dir() -> Path:
    """lineage.json, registry.json, reap-ledger.jsonl, verify-cache, backups."""
    return _env("FLEET_CONTROL_DIR") or home() / ".fleet"


def work_root() -> Path:
    """Spawn worktrees and task-kind scratch dirs (`<work root>/<name>`)."""
    return _env("FLEET_WORK_ROOT") or home() / "fleet"


def agent_home() -> Path:
    """pi's own state: sessions/, session-names/, settings, trust.json."""
    return _env("PI_AGENT_HOME") or home() / ".pi" / "agent"


def handoff_dir() -> Path:
    """Reaped digests (`_reaped/`) and other cross-session handoff files."""
    return _env("FLEET_HANDOFF_DIR") or home() / "handoffs"


def reports_dir() -> Path:
    """Where `fleet rollup` writes its assembled report."""
    return _env("FLEET_REPORTS_DIR") or home() / "docs" / "reports"


def bin_dir() -> Path:
    """The directory the CLIs live in — resolved from this file, never hardcoded.

    Resolved (`__file__.__resolve__`) so a symlink into ~/bin still finds the
    fleet that owns it, which is how a public install is likely to be assembled.
    """
    return _env("FLEET_BIN_DIR") or Path(__file__).resolve().parent


def _is_git(path: Path) -> bool:
    """A git checkout or a linked worktree: `.git` is a dir in one, a file in the
    other, and both count."""
    try:
        return (path / ".git").exists()
    except OSError:
        return False


def repo() -> Path | None:
    """The repository fleet operates on, or None when there is no honest answer.

    Order: $FLEET_REPO, the repository containing the current directory, then
    `<work root>/repo`. None is a real answer — callers must report REPO_HINT
    rather than defaulting to a stranger's tree.
    """
    override = _env("FLEET_REPO")
    if override:
        return override
    try:
        here = Path.cwd().resolve()
    except OSError:
        here = None
    if here is not None:
        for cand in (here, *here.parents):
            if _is_git(cand):
                return cand
    fallback = work_root() / "repo"
    if _is_git(fallback):
        return fallback
    return None


def require_repo() -> Path:
    """repo(), or a failure that names the thing to set. For verbs that cannot
    mean anything without a target (spawn, reap)."""
    r = repo()
    if r is None:
        raise SystemExit(f"  error: {REPO_HINT}")
    return r


def describe() -> dict:
    """Every resolved location, for `--version` style output and for tests."""
    r = repo()
    return {
        "home": str(home()),
        "control_dir": str(control_dir()),
        "work_root": str(work_root()),
        "agent_home": str(agent_home()),
        "handoff_dir": str(handoff_dir()),
        "reports_dir": str(reports_dir()),
        "bin_dir": str(bin_dir()),
        "repo": str(r) if r else "",
    }


def main(argv=None) -> int:
    """`fleet_paths.py [--json]` — print the resolved locations and where each
    came from, so "which store is it using?" is answerable without reading code."""
    import json
    import sys

    as_json = "--json" in (argv if argv is not None else sys.argv[1:])
    d = describe()
    envs = {
        "home": "HOME",
        "control_dir": "FLEET_CONTROL_DIR", "work_root": "FLEET_WORK_ROOT",
        "agent_home": "PI_AGENT_HOME", "handoff_dir": "FLEET_HANDOFF_DIR",
        "reports_dir": "FLEET_REPORTS_DIR", "bin_dir": "FLEET_BIN_DIR",
        "repo": "FLEET_REPO",
    }
    if as_json:
        out = {k: {"path": v, "from": ("env " + envs[k]) if os.environ.get(envs[k]) else "default"}
               for k, v in d.items()}
        print(json.dumps(out, indent=2))
        return 0
    width = max(len(k) for k in d)
    for k, v in d.items():
        src = "env " + envs[k] if os.environ.get(envs[k]) else "default"
        if k == "repo" and not v:
            v = "(none — " + REPO_HINT + ")"
        print(f"{k.ljust(width)}  {v}  [{src}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
