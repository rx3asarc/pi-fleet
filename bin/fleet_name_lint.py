#!/usr/bin/env python3
"""fleet_name_lint — a session name must be its subject.

Rule ("the name is the subject; the domain is a FIELD"):
  - no CATEGORY prefix (meta-, misc-, temp-, work-, session-, test-, new-, scratch-);
    those label a filing drawer, not a subject, and say nothing a shorter name cannot.
  - a DOMAIN prefix is allowed when the domain IS the subject. The domain set is not
    copied here: it is `pi_taxonomy.KNOWN_DOMAINS`, which is overridable through
    FLEET_DOMAINS, so this lint follows the fleet's own vocabulary instead of keeping a
    second copy that can drift from it.
  - the name must read correctly out of context, hours later, with no pane, no topic line.

Sources of names, in this order of authority:
  1. live pi sessions — what a human sees right now
  2. lineage rows — what the fleet believes ($FLEET_CONTROL_DIR/lineage.json)
  3. the session-names registry — what was last recorded
A name is reported once even if it appears in all three.

Exit: 0 clean · 1 live violation(s) · 2 could not read a source. A guard that cannot read
its own source must not report clean, so 2 is a distinct exit code and not a shrug.

Report-only, ON PURPOSE. This tool never renames anything, and it never relabels history:
live violations are reported, historical ones are counted, and old records are left as the
evidence they are. A linter that rewrites its own past cannot be trusted to describe it.

Paths come from `bin/fleet_paths.py` (the one module that resolves a location), never from
a literal in this file.

Usage:
  fleet_name_lint.py [--json] [--selftest] [--quiet] [--allow NAME ...]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet_paths  # noqa: E402  (the one place that resolves locations)

try:  # the domain set belongs to the taxonomy, not to this linter
    from pi_taxonomy import KNOWN_DOMAINS as _KNOWN_DOMAINS
except Exception:  # noqa: BLE001 - a lint that cannot import still works, just stricter
    _KNOWN_DOMAINS = ()

CATEGORY_PREFIXES = (
    "meta", "misc", "temp", "tmp", "work", "works", "session", "sessions",
    "test", "tests", "new", "scratch", "stuff", "miscellaneous",
)
# allowed domain prefixes: only where the domain is the actual subject, taken from the
# taxonomy so a stranger's FLEET_DOMAINS is what this lint enforces.
DOMAIN_PREFIXES = tuple(_KNOWN_DOMAINS)

LINEAGE = fleet_paths.control_dir() / "lineage.json"
NAMES_DIR = fleet_paths.agent_home() / "session-names"


def _peers_cli() -> str:
    """The packaged sibling if it is there, else whatever is on PATH."""
    sibling = fleet_paths.bin_dir() / "pi-peers"
    return str(sibling) if sibling.is_file() else "pi-peers"


def _live_names() -> list:
    out = subprocess.run([_peers_cli()], capture_output=True, text=True)
    names = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if parts:
            names.append(parts[0])
    return names


def _lineage_names() -> list:
    try:
        with open(LINEAGE) as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise RuntimeError("cannot read %s: %s" % (LINEAGE, exc))
    rows = data.get("children") if isinstance(data, dict) else None
    if not isinstance(rows, dict):
        return []
    out = []
    for row in rows.values():
        if isinstance(row, dict):
            name = row.get("name")
            if isinstance(name, str) and name:
                out.append(name)
    return out


def _registry_names() -> list:
    out = []
    try:
        files = os.listdir(str(NAMES_DIR))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise RuntimeError("cannot read %s: %s" % (NAMES_DIR, exc))
    for fn in files:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(str(NAMES_DIR), fn)) as fh:
                rec = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("name"), str):
            out.append(rec["name"])
    return out


def violations_of(name: str, allow=None) -> list:
    """Pure function: the reasons this name is not its subject."""
    allow = allow or set()
    if name in allow:
        return []
    reasons = []
    stem = re.split(r"[-.]", name, maxsplit=1)[0].lower()
    if stem in CATEGORY_PREFIXES:
        reasons.append(
            "category prefix '%s-' says nothing about the subject — "
            "drop it ('%s' is the same information, shorter)"
            % (stem, "-".join(name.split("-")[1:]) or name)
        )
    if stem in DOMAIN_PREFIXES and len(name.split("-")) < 2:
        reasons.append("domain prefix with no subject after it")
    if re.fullmatch(r"(?:0?x)?[0-9a-f]{6,}", name) or re.fullmatch(r"pi[-_]?[0-9]+", name):
        reasons.append("placeholder id, not a subject")
    if re.fullmatch(r"session[-_]?[0-9]*", name):
        reasons.append("'session' is not a subject")
    return reasons


def collect(allow=None):
    """Return (live_violations, history_violations, unreadable)."""
    live = {}
    history = {}
    unreadable = []
    for label, loader in (
        ("pi-peers", _live_names),
        ("lineage", _lineage_names),
        ("registry", _registry_names),
    ):
        try:
            names = loader()
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            unreadable.append("%s: %s" % (label, exc))
            continue
        bucket = live if label == "pi-peers" else history
        for name in names:
            rs = violations_of(name, allow)
            if not rs:
                continue
            target = live if name in live else bucket
            target.setdefault(name, [])
            for r in rs:
                if r not in target[name]:
                    target[name].append(r)
    # a name that is live is reported as live, never twice
    for name in list(history):
        if name in live:
            del history[name]
    return live, history, unreadable


SELFTESTS = [
    ({"name": "meta-fleet-records"}, True, "category prefix"),
    ({"name": "misc-scratch"}, True, "category prefix"),
    ({"name": "temp"}, True, "category prefix"),
    ({"name": "fleet-lineage-repair"}, False, "subject only, no prefix"),
    ({"name": "box-doctor-e2e"}, False, "subject only"),
    ({"name": "a1b2c3d4e5"}, True, "placeholder id"),
    ({"name": "x7f3a9b2"}, True, "placeholder id with x prefix"),
    ({"name": "0xdeadbeef"}, True, "placeholder id, 0x form"),
    ({"name": "deadbeef"}, True, "placeholder id, bare hex"),
    ({"name": "box-doctor"}, False, "hex-looking letters but a real word"),
    ({"name": "pi-12345"}, True, "placeholder id"),
    ({"name": "session-4"}, True, "'session' is not a subject"),
]


def _domain_cases():
    """A configured domain that IS the subject is allowed.

    Built from the taxonomy rather than written out, so the selftest does not smuggle one
    operator's product names into the package, and so it keeps testing whatever the running
    fleet's FLEET_DOMAINS actually says.
    """
    cases = []
    for domain in DOMAIN_PREFIXES:
        if domain in CATEGORY_PREFIXES:
            continue  # 'meta-ads' is a filing drawer, not a subject
        cases.append(({"name": domain + "-ads"}, False, "domain IS the subject"))
        if len(cases) == 2:
            break
    return cases


def selftest() -> int:
    ok = True
    cases = SELFTESTS + _domain_cases()
    for case, should_flag, why in cases:
        got = bool(violations_of(case["name"]))
        mark = "ok " if got == should_flag else "FAIL"
        if got != should_flag:
            ok = False
        print("  %s %-26s flag=%-5s expected=%-5s (%s)"
              % (mark, case["name"], got, should_flag, why))
    print(json.dumps({"selftest_ok": ok, "cases": len(cases)}, indent=1))
    return 0 if ok else 1


def main(argv) -> int:
    if "--selftest" in argv:
        return selftest()
    allow = set()
    if "--allow" in argv:
        idx = argv.index("--allow")
        allow = set(argv[idx + 1:])

    live, history, unreadable = collect(allow)
    if "--json" in argv:
        print(json.dumps({"live_violations": live, "history_violations": history,
                          "unreadable": unreadable}, indent=1))
    else:
        if live:
            print("LIVE session names that are not their subject:")
            for name in sorted(live):
                print("\n  %s" % name)
                for r in live[name]:
                    print("    - %s" % r)
            print("\n  fix: rename the session once it is idle. A dark session cannot be\n"
                  "       renamed while it runs: name it at resume.")
        elif "--quiet" not in argv:
            print("no live naming violations")
        if history and "--quiet" not in argv:
            # history is not renamed: an old record is evidence of how the fleet was, not a task
            print("\n  %d historical record(s) still carry a category prefix "
                  "(e.g. %s) — history is kept, not relabelled."
                  % (len(history), ", ".join(sorted(history)[:3])))
        for u in unreadable:
            print("  cannot read: %s" % u)

    if unreadable:
        return 2
    return 1 if live else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
