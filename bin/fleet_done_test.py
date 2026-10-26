#!/usr/bin/env python3
"""Frozen done-tests: the difference between verification and self-certification.

WHY THIS MODULE EXISTS (2026-10-26, from an adversarial review of this fleet).
`fleet done --verify CMD` accepted ANY command. So a worker could report
`--verify "pytest the test I just wrote"` and the reaper would re-run a command that
exits 0 and record `verified`. The evidence chain was self-graded end to end: the
gate proved that *some* command the worker chose passed, not that the unit was done.

The fix is one rule and it is not negotiable:

    The command that decides whether a unit is done is authored ONCE, by the plan,
    before the worker starts, and it is FROZEN in the lineage record. The worker
    supplies confirmation, never the command.

Three consequences follow, and each is a function here:

  frozen()        -- the frozen test, or None for a legacy record that predates this
  bind()          -- write it once; refusals live in check_binding()
  check_binding() -- a claimed command that is not the frozen one is a REFUSAL, not a
                     pass, and the drift is recorded so it cannot be smoothed later
  run_clean()     -- run the frozen test in a tree the worker never touched, cached per
                     (repo, commit, command) so a 1300-test suite is paid once per
                     commit rather than once per reap

The clean-tree run exists because a worker can pass its own test by editing the world
around it: a green suite in a dirty tree proves the tree is green, not that the commit
is good. `run_clean` checks out the child's base commit detached and runs there.

Caching is deliberate and its limits are stated in the result: a cached pass is
reported as `cached: true`, and the rollup prints that word, because "we verified this
commit an hour ago" and "we verified it just now" are different claims.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

CACHE_DIR = Path(os.environ.get("FLEET_VERIFY_CACHE", "/root/.fleet/verify-cache"))
# Absence of a frozen test is LEGACY, not failure: records written before this rule
# existed must keep working, and the rollup is required to label them as weaker
# evidence rather than letting them pass as equals.
STRICT = os.environ.get("REQUIRE_FROZEN_DONE_TEST", "1") != "0"

# A test whose whole content is one of these cannot fail, so it cannot verify. Same
# list the lineage module refuses for `--verify`; duplicated deliberately -- this
# module must be importable without the lineage store.
_TRIVIAL = {"true", ":", "exit 0", "/bin/true", "sleep 0"}


def frozen(rec):
    """The frozen done-test dict, or None. Never invents one."""
    dt = (rec or {}).get("done_test")
    if not isinstance(dt, dict):
        return None
    if dt.get("kind") not in ("cmd", "file"):
        return None
    if dt["kind"] == "cmd" and not str(dt.get("cmd") or "").strip():
        return None
    if dt["kind"] == "file" and not str(dt.get("path") or "").strip():
        return None
    return dt


def is_legacy(rec):
    """A record that predates frozen tests. Weaker evidence, said out loud."""
    return frozen(rec) is None


def bind(rec, kind, value, authored_by=None, now=None, cwd=None):
    """Freeze a done-test on a record. Refuses a test that cannot fail.

    `kind="cmd"` -- `value` is the command, run at reap in a clean tree.
    `kind="file"` -- `value` is a path that must exist and be non-empty.
    """
    kind = (kind or "").strip().lower()
    value = str(value or "").strip()
    if kind not in ("cmd", "file"):
        raise ValueError(f"done-test kind must be 'cmd' or 'file', not {kind!r}")
    if not value:
        raise ValueError("a done-test with no command or path verifies nothing")
    if kind == "cmd" and value in _TRIVIAL:
        raise ValueError(f"{value!r} always exits 0, so it cannot verify anything")
    dt = {"kind": kind, "cmd": None, "path": None, "cwd": cwd,
          "authored_by": authored_by, "frozen_at": int(now or time.time())}
    dt["cmd" if kind == "cmd" else "path"] = value
    out = dict(rec)
    out["done_test"] = dt
    return out


def check_binding(rec, claimed=None, kind="cmd"):
    """Is the claimed evidence the frozen evidence? Returns (ok, message).

    Called by `fleet done` BEFORE anything is recorded. A refusal here is the point:
    a worker that cannot name the frozen test has not read its brief, and a worker
    naming a different one is either guessing or gaming.
    """
    dt = frozen(rec)
    claimed = str(claimed or "").strip()
    if dt is None:
        if STRICT and claimed:
            return True, ""          # legacy record: nothing to bind against
        return True, ""
    if dt["kind"] != kind:
        return False, (f"this unit's done-test is a {dt['kind']} "
                       f"({dt.get('cmd') or dt.get('path')!r}), not a {kind}")
    want = str(dt.get("cmd") or dt.get("path") or "").strip()
    if not claimed:
        return False, f"this unit's done-test must be reported: {want!r}"
    if _norm(claimed) != _norm(want):
        return False, (f"claimed evidence is not the frozen done-test.\n"
                       f"      frozen: {want}\n      claimed: {claimed}\n"
                       f"      The test is authored by the plan, not by the worker; "
                       f"report the frozen command (or ask the lead to re-plan).")
    return True, ""


def _norm(s):
    return " ".join(shlex.split(str(s or "")))


def drift(rec, claims):
    """The recorded drift, if any: a worker claiming a command it was not given."""
    dt = frozen(rec)
    if not dt or dt["kind"] != "cmd":
        return None
    claimed = str((claims or {}).get("verify") or "").strip()
    if claimed and _norm(claimed) != _norm(dt.get("cmd")):
        return {"expected": dt.get("cmd"), "claimed": claimed}
    return None


# ── the clean-tree run ───────────────────────────────────────────────────────

def _key(repo, sha, spec):
    h = hashlib.sha256(f"{repo}|{sha}|{spec}".encode()).hexdigest()[:16]
    return Path(CACHE_DIR) / f"{h}.json"


def cached(repo, sha, spec):
    p = _key(repo, sha, spec)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def run(cmd, cwd, timeout=900):
    """Run a command. Returns rc/out_tail; never raises on a non-zero exit."""
    t0 = time.time()
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True,
                           text=True, timeout=timeout)
        rc, out = p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return {"rc": None, "timed_out": True, "secs": int(time.time() - t0),
                "out_tail": f"timed out after {timeout}s"}
    return {"rc": rc, "timed_out": False, "secs": int(time.time() - t0),
            "out_tail": out[-2000:]}


def run_clean(repo, sha, spec, timeout=900, use_cache=True):
    """Run `spec` in a detached worktree of `repo` at `sha`. Cached per commit.

    The worker never sees this tree, so a pass here cannot be manufactured by
    editing the worker's own checkout, and it cannot be faked by leaving an
    untracked conftest.py lying around.

    Returns a dict that always says HOW it knows: `cached`, `rc`, `at`, `sha`.
    A cache hit is not silently equal to a live run -- `cached: true` travels into
    the rollup.
    """
    spec = str(spec or "").strip()
    ck = f"{spec}"
    if use_cache:
        hit = cached(repo, sha, ck)
        if hit:
            return {**hit, "cached": True}
    tmp = Path(f"/tmp/fleet-clean-{os.getpid()}-{int(time.time())}")
    add = run(f"git worktree add --detach {shlex.quote(str(tmp))} {shlex.quote(sha)}",
              cwd=repo, timeout=300)
    if add["rc"] != 0:
        return {"rc": None, "cached": False, "sha": sha,
                "error": "could not create a clean worktree",
                "out_tail": add.get("out_tail", "")}
    try:
        if spec.startswith("file:"):
            rel = spec.split(":", 1)[1].strip()
            p = tmp / rel
            res = {"rc": 0 if (p.exists() and p.stat().st_size > 0) else 1,
                   "timed_out": False, "secs": 0,
                   "out_tail": f"{rel}: exists={p.exists()} "
                               f"bytes={p.stat().st_size if p.exists() else 0}"}
        else:
            res = run(spec, cwd=str(tmp), timeout=timeout)
    finally:
        run(f"git worktree remove --force {shlex.quote(str(tmp))}",
            cwd=repo, timeout=300)
    out = {**res, "sha": sha, "cached": False, "at": int(time.time()), "repo": repo}
    try:
        Path(CACHE_DIR).mkdir(parents=True, exist_ok=True)
        _key(repo, sha, ck).write_text(json.dumps(out))
    except OSError:
        pass
    return out
