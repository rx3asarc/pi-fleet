#!/usr/bin/env python3
"""fleet_audit.py — an independent, READ-ONLY auditor of the fleet stores.

Written for the moment a human does not trust the fleet. It is deliberately a
separate program from `fleet`: it imports nothing from fleet_children /
fleet_lineage / fleet_util, because an auditor that shares code with the thing it
audits inherits its bugs and its blind spots. Only the standard library.

It never writes, kills, repairs, or "fixes". A repairer that can be wrong is worse
than a checker that can say "I don't know", and a checker that quietly repairs is
indistinguishable from one that hides. If something is broken, this says so and
exits non-zero.

    python3 fleet_audit.py [--json] [--lineage PATH] [--ledger PATH]

Statuses: PASS (verified good), FAIL (a defect, with the offending ids), UNKNOWN
(cannot be determined from the data -- e.g. a required field the code never
writes). Exit: 0 all pass, 1 any FAIL, 2 UNKNOWN only.

The distinction matters: a cron gate should cry wolf on FAIL and stay quiet on
UNKNOWN, otherwise people learn to ignore it.

Read-only, proved rather than promised: the store files' (size, mtime_ns, sha256)
are captured before and after the audit and compared. If the audit itself changed
the store, that is reported as UNKNOWN (a bug in the auditor) rather than passed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PASS, FAIL, UNKNOWN, NOTE = "PASS", "FAIL", "UNKNOWN", "NOTE"

# ── cutovers ────────────────────────────────────────────────────────────────
# Two residual conditions are HISTORY: receipts written before the digest template
# put the session id on line 1, and records enrolled before `new_child` carried an
# `enrolled_source`. Asking history to satisfy a rule that did not exist when it was
# written produces a permanent false alarm, and a permanent false alarm is how a
# checker gets ignored.
#
# A cutover is NOT a grandfather clause: it is an instant. Before it, the older
# shape is NOTED and does not fail. After it, the older shape is a FAIL -- the same
# regression the check exists to catch. Each cutover must be moved, in the same
# commit as the writer change, to that commit's timestamp; TestCutovers refuses one
# pushed more than an hour into the future, so it cannot be swept under the rug.
# Both are the MEASURED landing instant of the code that satisfies them, not an
# estimate: fork-1's `fleet_lineage.digest_md` puts the sid on line 1, and
# `new_child` takes `enrolled_source`, from 16:29:00Z. A cutover later than the code
# it describes is a forward push that excuses a present regression -- which is what
# 17:10:00Z was, and why the guard below now allows only 5 minutes of it.
DIGEST_LAYOUT_CUTOVER_ISO = "2026-10-17T16:29:00Z"    # digest_md: sid on line 1
ENROLLED_SOURCE_CUTOVER_ISO = "2026-10-17T16:29:00Z"  # new_child: enrolled_source


def _epoch(iso):
    import datetime as _dt
    return int(_dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")
               .replace(tzinfo=_dt.timezone.utc).timestamp())


def _writer_emits_enrolled_source():
    """Does fleet_lineage.new_child actually write the field yet?

    If the writer does not emit it, NO record can have it, so absence is not a
    record-level defect -- it is the requirement arriving before the code. That is
    NOTED for everyone rather than FAIL for everyone. The moment the writer lands,
    the post-cutover rule starts biting, with no second edit here.
    """
    try:
        src = (Path(__file__).resolve().parent / "fleet_lineage.py").read_text()
    except OSError:
        return False
    start = src.find("def new_child")
    if start < 0:
        return False
    end = src.find("\ndef ", start + 5)
    return "enrolled_source" in src[start:end if end > 0 else None]

import re

import fleet_paths  # noqa: E402  (the one place that resolves locations)

SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

DEFAULT_LINEAGE = fleet_paths.control_dir() / "lineage.json"
DEFAULT_LEDGER = fleet_paths.control_dir() / "reap-ledger.jsonl"


class Check:
    def __init__(self, key, title, status, detail, ids=None):
        self.key, self.title, self.status = key, title, status
        self.detail = detail
        self.ids = list(ids or [])

    def as_dict(self):
        return {"check": self.key, "title": self.title, "status": self.status,
                "detail": self.detail, "ids": self.ids}


def _fingerprint(path):
    if not path.exists():
        return None
    data = path.read_bytes()
    return {"bytes": len(data), "mtime_ns": path.stat().st_mtime_ns,
            "sha256": hashlib.sha256(data).hexdigest()}


def load(lineage_path, ledger_path):
    """Read both stores. A malformed store is a finding, not a crash."""
    problems = []
    children = {}
    try:
        raw = json.loads(lineage_path.read_text())
        children = raw.get("children") or {}
        if not isinstance(children, dict):
            problems.append("lineage.json: 'children' is not an object")
            children = {}
    except FileNotFoundError:
        problems.append(f"lineage.json missing at {lineage_path}")
    except json.JSONDecodeError as e:
        problems.append(f"lineage.json is not valid JSON: {e}")
    rows = []
    try:
        for i, line in enumerate(ledger_path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as e:
                problems.append(f"ledger line {i} is not valid JSON: {e}")
                continue
            if not isinstance(row, dict):
                problems.append(f"ledger line {i} is not an object")
                continue
            row["_line"] = i
            rows.append(row)
    except FileNotFoundError:
        problems.append(f"reap-ledger.jsonl missing at {ledger_path}")
    return children, rows, problems


# ---------------------------------------------------------------- checks

def check_schema(children):
    """Fields the code indexes without .get(), per fleet_lineage.new_child."""
    broken = []
    for sid, rec in children.items():
        if not isinstance(rec, dict):
            broken.append(f"{sid}: record is {type(rec).__name__}, not an object")
            continue
        if not sid or not isinstance(sid, str):
            broken.append(f"{sid!r}: sessionId is not a non-empty string")
        for field in ("sessionId", "name", "unit"):
            if field not in rec:
                broken.append(f"{sid}: missing '{field}'")
            elif not isinstance(rec[field], str):
                broken.append(f"{sid}: '{field}' is {type(rec[field]).__name__}")
        # The timestamp is spelled `spawned_at` in the code; `enrolled_at` is the
        # name the audit spec uses. Accept either, require one.
        stamp = rec.get("enrolled_at", rec.get("spawned_at"))
        if stamp is None:
            broken.append(f"{sid}: no enrolment timestamp (enrolled_at/spawned_at)")
        elif not isinstance(stamp, int):
            broken.append(f"{sid}: enrolment timestamp is {type(stamp).__name__}")
        if not SESSION_ID_RE.match(sid):
            broken.append(f"{sid!r}: the store key is not a session id "
                          f"({rec.get('name')!r})")
        if rec.get("sessionId") and rec["sessionId"] != sid:
            broken.append(f"{sid}: sessionId says {rec['sessionId']} (key disagrees)")
    if broken:
        return Check("schema", "record schema", FAIL,
                     f"{len(broken)} schema defect(s)", broken)
    return Check("schema", "record schema", PASS, f"{len(children)} record(s) well formed")


def check_enrolled_source(children):
    """Was this record's enrolment source recorded? Cutover rules, not a blanket note.

    before the cutover -> NOTED ("prefixes the field"); after -> FAIL (the actual bug).
    While the writer itself does not emit the field, everything is NOTED: the
    requirement arrived before the code, and blaming 34 records for that is noise.
    """
    cutover = _epoch(ENROLLED_SOURCE_CUTOVER_ISO)
    writer = _writer_emits_enrolled_source()
    noted, failed = [], []
    for sid, rec in children.items():
        if not isinstance(rec, dict) or rec.get("enrolled_source"):
            continue
        stamp = rec.get("enrolled_at", rec.get("spawned_at"))
        label = f"{sid} ({rec.get('name')}): no enrolled_source"
        if not writer:
            noted.append(f"{label} — the writer (fleet_lineage.new_child) does not "
                         f"emit the field yet, so no record can have it")
        elif isinstance(stamp, int) and stamp >= cutover:
            failed.append(f"{label}, enrolled after the {ENROLLED_SOURCE_CUTOVER_ISO} "
                          f"cutover — this is the bug the check exists for")
        else:
            noted.append(f"{label} — enrolled before the cutover "
                         f"({ENROLLED_SOURCE_CUTOVER_ISO}); predates the field")
    if failed:
        return Check("enrolled_source", "enrolment source recorded", FAIL,
                     f"{len(failed)} record(s) enrolled after the cutover without a "
                     f"source; {len(noted)} predate it", failed + noted[:3])
    if noted:
        return Check("enrolled_source", "enrolment source recorded", NOTE,
                     f"{len(noted)}/{len(children)} predate the field"
                     + ("" if writer else " (the writer does not emit it yet)"),
                     [])
    return Check("enrolled_source", "enrolment source recorded", PASS,
                 "every record names where it was enrolled from")


def check_write_ahead(children, rows):
    """I1: a reap row and a reaped_at are two halves of one event."""
    reap_rows, other_rows, prune_rows = {}, {}, {}
    for r in rows:
        sid = r.get("sessionId")
        if not isinstance(sid, str) or not sid:
            continue
        if r.get("action") == "reap":
            reap_rows.setdefault(sid, []).append(r["_line"])
        elif r.get("action") == "prune":
            prune_rows.setdefault(sid, []).append(r["_line"])
        else:
            other_rows.setdefault(sid, []).append((r.get("action"), r["_line"]))
    reaped = {sid: rec for sid, rec in children.items()
              if isinstance(rec, dict) and rec.get("reaped_at")}
    # A `prune` row is the EXPLANATION for a record that is gone: the deletion is
    # named in the ledger, so the hole is reconciled rather than hidden. A reap row
    # with no record and no prune row stays a failure -- that is an unexplained
    # disappearance, which is the whole thing these invariants exist to forbid.
    ledger_only = sorted(sid for sid in set(reap_rows) - set(reaped)
                         if sid not in prune_rows)
    reconciled = sorted(sid for sid in set(reap_rows) - set(reaped)
                        if sid in prune_rows)
    # ...and the other direction of the same lie: a prune row for a record that
    # still exists claims a deletion that never happened.
    phantom_prune = sorted(sid for sid in prune_rows if sid in children)
    store_only = sorted(set(reaped) - set(reap_rows))      # closed, never recorded
    # A third case, and it is not the same bug: a record closed by `dismiss` still
    # carries reaped_at, so the store says "reaped" while the ledger says "dismissed".
    mislabelled = [(sid, other_rows[sid]) for sid in store_only if sid in other_rows]
    mislabelled_sids = [sid for sid, _ in mislabelled]
    silently = [sid for sid in store_only if sid not in other_rows]
    if ledger_only or store_only or phantom_prune:
        bits = []
        if ledger_only:
            bits.append(f"{len(ledger_only)} reap row(s) with no reaped_at in the "
                        f"store (write-ahead never completed)")
        if phantom_prune:
            bits.append(f"{len(phantom_prune)} prune row(s) for records that still "
                        f"exist (a deletion that never happened)")
        if mislabelled_sids:
            verbs = sorted({a for _, rs in mislabelled for a, _ in rs})
            bits.append(f"{len(mislabelled_sids)} record(s) with reaped_at but "
                        f"closed by {verbs} instead of reap -- the store and the "
                        f"ledger disagree about what happened")
        if silently:
            bits.append(f"{len(silently)} reaped record(s) with no ledger row at all "
                        f"(the ledger lost an event)")
        ids = [f"{sid}: prune row claims a deletion, but the record is present"
               for sid in phantom_prune] + \
              [f"{sid}: reaped_at, ledger says "
               f"{'/'.join(sorted({a for a, _ in other_rows[sid]}))}"
               for sid in mislabelled_sids] + silently + ledger_only
        return Check("i1_write_ahead", "I1 reap row <-> reaped_at", FAIL,
                     "; ".join(bits), ids)
    note = f"{len(reaped)} reaped record(s), each with exactly one ledger event" \
        if reaped else "nothing has been reaped yet"
    if reconciled:
        note += (f"; {len(reconciled)} deletion(s) reconciled by a prune row "
                 f"(record gone, reason named)")
    return Check("i1_write_ahead", "I1 reap row <-> reaped_at", PASS, note)


def check_corrective_rows(children, rows):
    """I2: at most one corrective row per reap, and never without the reap."""
    by_sid = {}
    for r in rows:
        sid = r.get("sessionId")
        if not isinstance(sid, str):
            continue          # i4 owns malformed subjects; I2 is about verbs
        by_sid.setdefault(sid, []).append(r)
    broken = []
    for sid, rs in by_sid.items():
        if not sid:
            continue
        reaps = [r for r in rs if r.get("action") == "reap"]
        fails = [r for r in rs if r.get("action") == "reap-failed"]
        if len(fails) > 1:
            broken.append(f"{sid}: {len(fails)} corrective rows for "
                          f"{len(reaps)} reap(s)")
        if fails and not reaps:
            broken.append(f"{sid}: reap-failed with no preceding reap")
        if fails and reaps and fails[0]["_line"] < reaps[0]["_line"]:
            broken.append(f"{sid}: reap-failed (line {fails[0]['_line']}) precedes "
                          f"its reap (line {reaps[0]['_line']})")
    if broken:
        return Check("i2_corrective", "I2 corrective rows", FAIL,
                     f"{len(broken)} corrective-row defect(s)", broken)
    return Check("i2_corrective", "I2 corrective rows", PASS,
                 "no reap has more than one correction, none is orphaned")


def check_revive(children, rows):
    """I3: reaped_at plus a live verdict is only legal after an explicit revive."""
    broken = []
    for sid, rec in children.items():
        if not isinstance(rec, dict) or not rec.get("reaped_at"):
            continue
        if rec.get("verdict") not in ("active", "hold"):
            continue
        later = [r for r in rows
                 if r.get("sessionId") == sid and r.get("action") == "revive"]
        if not later:
            broken.append(f"{sid} ({rec.get('name')}): reaped but verdict="
                          f"{rec.get('verdict')} with no revive row")
    if broken:
        return Check("i3_revive", "I3 reaped vs live verdict", FAIL,
                     f"{len(broken)} record(s) contradict the ledger", broken)
    return Check("i3_revive", "I3 reaped vs live verdict", PASS,
                 "no reaped record claims to be live without a revive")


def check_row_subject(rows):
    """I4: a ledger row is about exactly one session, or it is about nothing."""
    broken = []
    for r in rows:
        sid = r.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            continue
        if isinstance(sid, list):
            broken.append(f"line {r['_line']}: names {len(sid)} sessions")
        else:
            broken.append(f"line {r['_line']}: action {r.get('action')!r} names "
                          f"no sessionId")
        if len(broken) > 25:
            broken.append("... truncated")
            break
    if broken:
        return Check("i4_subject", "I4 one row, one session", FAIL,
                     f"{len(broken)} row(s) without a single subject", broken)
    return Check("i4_subject", "I4 one row, one session", PASS,
                 f"{len(rows)} row(s), each naming one session")


def _digest_instant(path, text):
    """When was this receipt written? header stamp, else filename date, else mtime."""
    import datetime as _dt
    import re as _re
    head = "\n".join(text.splitlines()[:3])
    m = _re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", head)
    if m:
        try:
            return _epoch(m.group(1))
        except ValueError:
            pass
    m = _re.match(r"^(\d{4}-\d{2}-\d{2})-", path.name)
    if m:
        try:
            return _epoch(m.group(1) + "T00:00:00Z")
        except ValueError:
            pass
    try:
        return int(path.stat().st_mtime)
    except OSError:
        return None


def check_digests(children):
    """The digest is the receipt: it must name the session it is about.

    Line 1 was only ever a PROXY for that. So: line 1 naming the session PASSES;
    the legacy layout (the sid on its `- session:` line) PASSES-WITH-NOTE when the
    receipt predates DIGEST_LAYOUT_CUTOVER and FAILS when it does not; a receipt
    that never names its session, or names a different one, always FAILS.
    """
    cutover = _epoch(DIGEST_LAYOUT_CUTOVER_ISO)
    ok = legacy = 0
    missing, unnamed, wrong, regressed = [], [], [], []
    for sid, rec in children.items():
        if not isinstance(rec, dict) or not rec.get("reaped_at"):
            continue
        path = rec.get("digest")
        if not path:
            missing.append(f"{sid} ({rec.get('name')}): reaped with no digest")
            continue
        p = Path(path)
        if not p.exists():
            missing.append(f"{sid} ({rec.get('name')}): digest missing at {path}")
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError as e:
            missing.append(f"{sid}: digest unreadable ({e.__class__.__name__})")
            continue
        lines = text.splitlines()
        first = lines[0] if lines else ""
        if sid in first:
            ok += 1
            continue
        session_line = next((l for l in lines[:16] if l.startswith("- session:")), None)
        named_here = session_line and sid in session_line
        if named_here:
            instant = _digest_instant(p, text)
            if instant is not None and instant < cutover:
                legacy += 1          # pre-cutover layout: noted, not failed
                continue
            regressed.append(f"{sid} ({rec.get('name')}): written after the "
                             f"{DIGEST_LAYOUT_CUTOVER_ISO} template cutover but uses "
                             f"the old layout (sid on '- session:', not line 1)")
            continue
        other = next((l for l in lines[:16] if l.startswith("- session:")), None)
        if other:
            wrong.append(f"{sid} ({rec.get('name')}): the receipt names "
                         f"{other.split(':', 1)[1].strip()[:40]!r} instead")
        else:
            unnamed.append(f"{sid} ({rec.get('name')}): the digest names no session "
                           f"anywhere")
    defects = missing + regressed + wrong + unnamed
    split = f"{legacy} legacy layout, {len(unnamed)} unnamed, {len(wrong)} wrong"
    if defects:
        return Check("digests", "digest receipts", FAIL,
                     f"{len(missing)} missing, {len(regressed)} post-cutover legacy, "
                     f"{split}", defects)
    if legacy:
        return Check("digests", "digest receipts", NOTE,
                     f"{ok} on line 1, {split} (all predate the "
                     f"{DIGEST_LAYOUT_CUTOVER_ISO} cutover)", [])
    return Check("digests", "digest receipts", PASS,
                 f"{ok} receipt(s), each naming its session on line 1")


def check_dangling(children):
    """The two ways a session becomes unowned."""
    dangling, unowned = [], []
    for sid, rec in children.items():
        if not isinstance(rec, dict):
            continue
        parent = rec.get("parent_session_id")
        if parent and parent not in children:
            dangling.append(f"{sid} ({rec.get('name')}): parent {parent} "
                            f"({rec.get('parent_name')}) is not in the store")
        if not parent and not rec.get("protected") and not rec.get("role"):
            unowned.append(f"{sid} ({rec.get('name')}): no parent, not protected, "
                           f"not a reviewer")
    defects = dangling + unowned
    if defects:
        return Check("dangling", "parentage", FAIL,
                     f"{len(dangling)} dangling parent(s), {len(unowned)} unowned",
                     defects)
    return Check("dangling", "parentage", PASS,
                 "every record is either parented, protected, or a reviewer")


def check_evidence(children):
    """Not a pass/fail: how much of this record rests on a re-run proof."""
    counts = {"verified": 0, "failed": 0, "partial": 0, "stale": 0,
              "unclaimed": 0, "unreported": 0}
    for rec in children.values():
        if not isinstance(rec, dict):
            continue
        if not rec.get("reported_at"):
            counts["unreported"] += 1
            continue
        checks = rec.get("checks")
        status = checks.get("status") if isinstance(checks, dict) else None
        counts[status if status in counts else "unclaimed"] += 1
    total = sum(counts.values())
    proven = counts["verified"]
    detail = ", ".join(f"{k} {v}" for k, v in counts.items())
    detail += f" ({proven}/{total} rest on a re-run proof)"
    if counts["failed"] or counts["stale"]:
        detail += (" -- failed/stale means a claim did NOT hold: for a liar that is "
                   "the system working, for a worker it is work that did not land. "
                   "This check counts; a human decides.")
    return Check("evidence", "evidence coverage", PASS, detail)


def exit_code_for(checks):
    """0 all pass (NOTE is history, not a defect) · 1 any FAIL · 2 UNKNOWN only."""
    if any(c.status == FAIL for c in checks):
        return 1
    if any(c.status == UNKNOWN for c in checks):
        return 2
    return 0


def run(lineage_path=DEFAULT_LINEAGE, ledger_path=DEFAULT_LEDGER):
    lineage_path, ledger_path = Path(lineage_path), Path(ledger_path)
    before = (_fingerprint(lineage_path), _fingerprint(ledger_path))
    children, rows, problems = load(lineage_path, ledger_path)

    checks = []
    if problems:
        checks.append(Check("store", "store readable", FAIL,
                            "; ".join(problems[:6]), problems[:6]))
    checks += [
        check_schema(children),
        check_enrolled_source(children),
        check_write_ahead(children, rows),
        check_corrective_rows(children, rows),
        check_revive(children, rows),
        check_row_subject(rows),
        check_digests(children),
        check_dangling(children),
        check_evidence(children),
    ]

    after = (_fingerprint(lineage_path), _fingerprint(ledger_path))
    if before != after:
        checks.append(Check("read_only", "auditor is read-only", UNKNOWN,
                            "the store changed during the audit -- this auditor is "
                            "supposed to be incapable of that"))
    else:
        checks.append(Check("read_only", "auditor is read-only", PASS,
                            "no store byte changed during the audit"))

    fails = [c for c in checks if c.status == FAIL]
    unknowns = [c for c in checks if c.status == UNKNOWN]
    notes = [c for c in checks if c.status == NOTE]
    code = exit_code_for(checks)
    return {"checks": [c.as_dict() for c in checks],
            "counts": {"records": len(children), "ledger_rows": len(rows),
                       "pass": sum(1 for c in checks if c.status == PASS),
                       "fail": len(fails), "unknown": len(unknowns),
                       "noted": len(notes)},
            "verdict": FAIL if fails else (UNKNOWN if unknowns else PASS),
            "exit_code": code,
            "lineage": str(lineage_path), "ledger": str(ledger_path)}


def summary_line(report):
    c = report["counts"]
    bad = [x for x in report["checks"] if x["status"] != PASS]
    head = (f"{report['verdict']}: {c['pass']} pass, {c['fail']} fail, "
            f"{c['unknown']} unknown, {c['noted']} noted — {c['records']} records, "
            f"{c['ledger_rows']} ledger rows")
    if not bad:
        return head + " — nothing to see"
    return head + " | " + " | ".join(f"{x['check']}={x['status']}" for x in bad)


def main(argv=None):
    ap = argparse.ArgumentParser(description="read-only fleet store audit")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--lineage", default=str(DEFAULT_LINEAGE))
    ap.add_argument("--ledger", default=str(DEFAULT_LEDGER))
    a = ap.parse_args(argv)
    report = run(a.lineage, a.ledger)
    if a.json:
        print(json.dumps(report, indent=2))
    else:
        print(summary_line(report))
        for chk in report["checks"]:
            print(f"  [{chk['status']:<7}] {chk['check']:<18} {chk['detail']}")
            for offender in chk["ids"][:8]:
                print(f"            - {offender}")
            if len(chk["ids"]) > 8:
                print(f"            ... {len(chk['ids']) - 8} more")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
