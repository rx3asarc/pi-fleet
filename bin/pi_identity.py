"""U7 — identity repair and a single writer.

THE SINGLE WRITER
This module is the ONLY component permitted to write session identity. Everything
else reads. `offenders()` enforces that mechanically by scanning module sources for
a direct write to the identity store, because a rule that nothing checks is a rule
that holds until the first hurried change.

THE CORE INSIGHT, MEASURED RATHER THAN ASSUMED
The identity store already holds the pi name, NOT the drifted tmux name:
`session-names/01a116b1-...json` reads `name: "plover-wren"` while tmux still calls
that session `theme-rowan`. So the 5 known-drifted sessions need no repair at all
-- the drift lives between tmux and pi, U2 reconciles it, and U3 reports it as a
health metric.

What IS repairable is a record whose stored name disagrees with the canonical (the
pi) name: measured 2026-10-11, 1 of 53 records read `name: ""` while pi called that
session `willow`.

FORWARD-ONLY
Renaming a live tmux session is NOT repair: it manufactures the drift bug rather
than fixing it, and the objective forbids it outside a deliberate pass. Repair
writes the store forward and leaves every historical field exactly as it was. Only
`name` may move. There is a test that fails if a patch carries anything else.
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_paths  # noqa: E402

__all__ = ["WRITER", "WRITERS", "canonical_name", "plan_repair", "apply_repair",
           "offenders"]

WRITER = "pi_identity"

# The DECLARED writer set. The previous check pattern-matched source lines and was
# unsound in both directions: it missed `session-namer.ts`, which binds the store
# path on one line and writes it 300 lines later, and it flagged a test file that
# contained offending source only as fixture data. So the writer set is DATA here,
# auditable by eye, and the scan's job is to report anything writing the store that
# is not on this list.
WRITERS = {
    "bin/pi_identity.py":
        "implements the store half (plan + apply). Driven by pi-names-sync.",
    "bin/pi-names-sync":
        "the ONE repair entry point: tmux names and the store, in one run.",
    "extensions/session-namer.ts":
        "CREATES records on every naming event. Not repair; it is the origin.",
}
REGISTRY_DIR = os.path.join(str(fleet_paths.agent_home()), "session-names")

# Fields repair may never touch. Repair moves identity, not history.
HISTORY = ("topic", "startedAt", "updatedAt", "pid", "pidStart", "tty", "cwd")

# A write to the identity store, as opposed to a docstring that mentions it.
# Filesystem writes ONLY. Matching a bare `.write(` made `pi-peers` -- which
# writes to STDOUT (process.stdout.write) and merely reads the store -- look like
# a writer. False offenders are as corrosive as missed ones: a check that cries
# wolf gets ignored, and then it misses the real thing.
_WRITE_CALLS = (".write_text(", ".write_bytes(", "writeFileSync(", "writeFile(",
                "os.replace(", "shutil.copy", "shutil.move(", "json.dump(")

STORE_LITERAL = "session-names"


def _writes(line):
    """Does THIS line contain a write call? Filesystem-shaped, not semantic."""
    if any(call in line for call in _WRITE_CALLS):
        return True
    return "open(" in line and any(q in line for q in ("'w'", '"w"', "'a'", '"a"'))


def store_bound_names(src):
    """Names in this module that hold (or derive from) the identity store path.

    TRANSPORT is path arithmetic only: literal, or a path operation
    (`X / ...`, `Path(X, ...)`, `os.path.join(X, ...)`, `X.with_suffix(..)`,
    `X.parent`) on an already-bound name. A name rebound to something unrelated
    is CLEARED rather than kept.

    The first version tainted any name whose RHS mentioned a bound name, which
    propagated through ordinary variables (`p`, `name`, `path`) until almost every
    local looked store-bound and `fleet_children.py` was convicted by four writes
    that have nothing to do with identity. Taint that reaches everything detects
    nothing.

    This closure is the repair for the false-offender defect of 2026-10-20: the
    detector was file-scoped on "writes anything", so modules that only READ the
    store were convicted by their own unrelated writes. A guard that names false
    offenders teaches its reader to ignore the exit code, which is how the real
    one gets waved through.
    """
    bound = set()
    assign = re.compile(r"\s*([A-Za-z_][\w]*)\s*=\s*(.*)$")
    pathish = re.compile(r"(/|\bPath\(|os\.path\.join\(|\.with_suffix\(|\.parent\b)")
    for line in src.splitlines():
        m = assign.match(line)
        if not m:
            continue
        target, expr = m.group(1), m.group(2)
        if STORE_LITERAL in expr:
            bound.add(target)
            continue
        refers = any(re.search(rf"\b{re.escape(v)}\b", expr) for v in bound)
        if refers and pathish.search(expr):
            bound.add(target)
        elif target in bound:
            bound.discard(target)
    return bound


def canonical_name(row):
    """The canonical identity of a session: the pi name.

    The tmux name is an ATTRIBUTE of the row, never its identity -- which is the
    whole reason a drifted tmux name stopped mattering in U2.
    """
    return (row or {}).get("name") or ""


def plan_repair(rows, records):
    """Pure. What would change so the store agrees with the canonical name.

    Returns one item per live session that has a canonical name, sorted by
    sessionId so two runs are identical. `action` is "rewrite" only when the stored
    name actually differs; a session with no stored record gets no action, because
    creating records is not repair.
    """
    by_sid = {}
    for r in records or []:
        sid = r.get("sessionId")
        if sid and sid not in by_sid:
            by_sid[sid] = r

    plan = []
    for row in rows or []:
        sid = row.get("sessionId")
        want = canonical_name(row)
        if not sid or not want:
            continue
        stored = by_sid.get(sid)
        if stored is None:
            plan.append({"sessionId": sid, "current": None, "canonical": want,
                         "action": "none", "patch": {}})
            continue
        differs = stored.get("name") != want
        plan.append({
            "sessionId": sid,
            "current": stored.get("name"),
            "canonical": want,
            "action": "rewrite" if differs else "none",
            "patch": {"name": want} if differs else {},
        })
    plan.sort(key=lambda i: i["sessionId"])
    return plan


def apply_repair(plan, registry_dir=REGISTRY_DIR, dry_run=True):
    """THE ONLY WRITER of session identity. Forward-only.

    `dry_run` defaults to True. A write that silently changes who a session is, is
    exactly the kind of action this protocol exists to stop, so the default is to
    look and not touch.

    Returns the list of files that were (or would be) written. Files are located by
    the `sessionId` inside them rather than by filename, because the filename is a
    truncated prefix and guessing it would be a silent way to write the wrong file.
    """
    changes = [i for i in (plan or []) if i.get("action") == "rewrite"]
    if not changes:
        return []

    targets = {}
    for name in sorted(os.listdir(registry_dir)) if os.path.isdir(registry_dir) else []:
        if not name.endswith(".json"):
            continue
        path = os.path.join(registry_dir, name)
        try:
            with open(path) as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        sid = data.get("sessionId")
        if sid:
            targets.setdefault(sid, (path, data))

    written = []
    for item in sorted(changes, key=lambda i: i["sessionId"]):
        found = targets.get(item["sessionId"])
        if not found:
            continue
        path, data = found
        # Carry every historical field verbatim; move only what the patch names.
        updated = {**data, **{k: v for k, v in item["patch"].items() if k == "name"}}
        written.append(path)
        if dry_run:
            continue
        tmp = path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(updated, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
    return written


def offenders(sources, allow=None):
    """Components writing the identity store that are NOT on the declared list.

    `sources` maps a module path to its source text, so this is pure and
    fixture-testable.

    TWO conditions, both required: the line performs a write call, AND that line
    names the store -- by literal, or through a name that carries the store path.
    `store_bound_names()` supplies the transport.

    Store-adjacency without a write is just reading (the case that convicted two
    innocent modules). A write without store-adjacency is somebody else's file.
    Prose is still not an offence: a docstring mentioning the path carries no
    write CALL.
    """
    declared = WRITERS if allow is None else allow
    out = []
    for name, src in (sources or {}).items():
        src = src or ""
        if name in declared:
            continue
        if STORE_LITERAL not in src:
            continue
        bound = store_bound_names(src)
        for line in src.splitlines():
            if not _writes(line):
                continue
            if STORE_LITERAL in line or any(
                    re.search(rf"\b{re.escape(v)}\b", line) for v in bound):
                out.append(name)
                break
    return sorted(out)


def main():
    """Report any component writing identity outside the single writer.

    Scans BOTH the bin directory and ../extensions. The first version scanned only
    bin/ and therefore reported "no other writer" while `session-namer.ts` -- the
    component that CREATES these records -- sat one directory up. A scan whose
    scope is narrower than the truth is worse than no scan: it manufactures
    confidence.

    Test files are skipped: a test that proves the scanner WORKS necessarily
    contains offending source as string data.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [here, os.path.join(os.path.dirname(here), "extensions")]
    sources = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root)):
            path = os.path.join(root, name)
            if not os.path.isfile(path) or name.startswith("test_"):
                continue
            # Backups are not components. `pi-live.bak-*` is a 44KB copy that
            # contains a write and is not running code.
            if ".bak" in name or name.endswith(("~", ".orig", ".tmp")):
                continue
            if not (name.endswith((".py", ".ts", ".js", ".sh")) or name.startswith("pi-")):
                continue
            try:
                with open(path, errors="replace") as fh:
                    sources[os.path.relpath(path, os.path.dirname(here))] = fh.read()
            except OSError:
                continue
    bad = offenders(sources)
    if bad:
        print("components writing the identity store that are not declared "
              f"({len(sources)} files scanned):")
        for n in bad:
            print(f"  {n}")
        print("Declared writers:")
        for n, why in sorted(WRITERS.items()):
            print(f"  {n} — {why}")
        return 1
    print(f"no undeclared writer of the identity store in {len(sources)} files")
    for n, why in sorted(WRITERS.items()):
        print(f"  declared: {n} — {why}")
    return 0


def _live_rows():
    """Live rows from the one join (U2). Read-only."""
    import subprocess
    here = os.path.dirname(os.path.abspath(__file__))
    try:
        out = subprocess.run([os.path.join(here, "pi-tree"), "--json"],
                             capture_output=True, text=True, timeout=180, errors="replace")
        return json.loads(out.stdout) if out.returncode == 0 else []
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _records(registry_dir):
    out = []
    if not os.path.isdir(registry_dir):
        return out
    for name in sorted(os.listdir(registry_dir)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(registry_dir, name)) as fh:
                out.append(json.load(fh))
        except (OSError, json.JSONDecodeError):
            continue
    return out


def repair_store(dry_run=True, registry_dir=REGISTRY_DIR):
    """The STORE half of repair: plan from live rows, then apply (or not).

    `pi-names-sync` is the one repair tool and calls this; the store half lives
    here so the logic exists once. The tmux half stays in pi-names-sync.
    """
    plan = plan_repair(_live_rows(), _records(registry_dir))
    written = apply_repair(plan, registry_dir=registry_dir, dry_run=dry_run)
    return plan, written


def report_repair(dry_run=True):
    plan, written = repair_store(dry_run=dry_run)
    rewrites = [i for i in plan if i["action"] == "rewrite"]
    verb = "would rewrite" if dry_run else "rewrote"
    print(f"  store: {len(plan)} live session(s), {verb} {len(rewrites)} record(s)")
    for i in rewrites:
        print(f"    {i['sessionId'][:18]}  {i['current']!r} -> {i['canonical']!r}")
    if dry_run and rewrites:
        print("    (dry run - nothing written)")
    return len(written)


if __name__ == "__main__":
    import sys
    if "--apply" in sys.argv:
        raise SystemExit(0 if report_repair(dry_run=False) >= 0 else 1)
    if "--plan" in sys.argv:
        report_repair(dry_run=True)
        raise SystemExit(0)
    raise SystemExit(main())
