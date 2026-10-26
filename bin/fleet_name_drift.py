#!/usr/bin/env python3
"""fleet name drift — lineage name vs the name the box actually uses.

WHY THIS EXISTS (marrow, 2026-10-26). Record `01a16d22-3f0-7489` was stored as
`quartz` while the live pane and the registry both said `onboarding`. Nothing
reported it: `pi-tree --drift` compares tmux against pi (which agreed — both said
`onboarding`), so the disagreement lived entirely between the LINEAGE and the
WORLD and no tool was looking there. It was found by a human reading two lists,
which is not a mechanism.

So: a step that happens, not a step a human remembers. Read-only by default.
`--apply` performs the same audited repair marrow performed by hand --
`fleet adopt <live-name> --relabel --parent none` -- which records
`name_changed_from`, `name_changed_by` and preserves `protected`.

Never fabricates a parent edge: a relabel says WHAT the session is called, never
WHO it belongs to.
"""
import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def _live_names():
    """sessionId -> the name the world uses (registry first, then tmux pane)."""
    out = {}
    try:
        reg = json.loads(open("/root/.fleet/registry.json").read())
    except Exception:
        reg = {}
    for key, row in reg.items():
        if not isinstance(row, dict):
            continue
        sid = row.get("sessionId") or (key if str(key).startswith("01a") else None)
        nm = row.get("name")
        if sid and nm:
            out.setdefault(sid, nm)
    return out


def _panes():
    """cwd -> tmux session name, for an independent second opinion."""
    try:
        rc = subprocess.run(["tmux", "list-panes", "-a", "-F",
                             "#{session_name}\t#{pane_current_path}"],
                            capture_output=True, text=True, timeout=10)
    except Exception:
        return {}
    if rc.returncode != 0:
        return {}
    out = {}
    for ln in rc.stdout.splitlines():
        if "\t" in ln:
            nm, path = ln.split("\t", 1)
            out.setdefault(path, nm)
    return out


def drift(include_closed=False):
    import fleet_children as fc
    children = fc.load_lineage().get("children") or {}
    live = _live_names()
    panes = _panes()
    rows = []
    for sid, r in children.items():
        if not isinstance(r, dict):
            continue
        if not include_closed and (r.get("verdict") or r.get("closed_by")):
            continue
        stored = r.get("name")
        world = live.get(sid)
        if not world and r.get("worktree"):
            world = panes.get(os.path.realpath(str(r["worktree"])))
        if not stored or not world or stored == world:
            continue
        rows.append({"sessionId": sid, "lineage": stored, "world": world,
                     "worktree": r.get("worktree"), "pid": r.get("pid"),
                     "protected": bool(r.get("protected"))})
    rows.sort(key=lambda x: (str(x["world"]), str(x["lineage"])))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apply", action="store_true",
                    help="repair each drift with `fleet adopt <world> --relabel --parent none`")
    ap.add_argument("--closed", action="store_true", help="include closed records")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)

    rows = drift(include_closed=a.closed)
    if a.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        print("  no lineage-vs-world name drift"
              + ("" if a.closed else " among open records"))
        return 0
    for r in rows:
        print(f"  {r['sessionId'][:13]}  lineage={r['lineage']:<18} world={r['world']:<18}"
              f" pid={r['pid']} protected={r['protected']}")
    if not a.apply:
        print(f"\n  {len(rows)} drifted record(s). Nothing changed. "
              f"Run with --apply to relabel (records name_changed_from, preserves protected).")
        return 0
    bad = 0
    for r in rows:
        cmd = ["fleet", "adopt", str(r["world"]), "--relabel", "--parent", "none"]
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        ok = p.returncode == 0
        print(f"  {r['sessionId'][:13]} -> {r['world']}: {'relabelled' if ok else 'FAILED'}"
              f"{'' if ok else ' :: ' + (p.stderr or p.stdout).strip().splitlines()[-1]}")
        bad += 0 if ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
