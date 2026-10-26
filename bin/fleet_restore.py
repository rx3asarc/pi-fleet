#!/usr/bin/env python3
"""fleet_restore.py — rebuild lineage.json from evidence, or say what cannot be rebuilt.

The auditor (fleet_audit.py) can refuse a corrupt store. A refusal with no recovery
path is a dead end, and lineage.json is currently the only home of several facts:
who a session's parent was, its unit, when it was enrolled, whether it was reaped,
and which digest is its receipt. This is the recovery path.

THE RULE THIS FILE IS BUILT AROUND
----------------------------------
A field is written only if some input PROVES it. Anything else is `null` plus a
reason in the record's `_unproven` map. An invented `reaped_at` is worse than a
missing one: the missing one asks a question, the invented one answers it wrongly
and then a digest check trusts the answer. So the output is allowed to be
incomplete, and it is never allowed to be confident without evidence.

INPUT TRUST ORDER (first source that can prove a field wins)
------------------------------------------------------------
1. backups   <control dir>/backups/lineage-*.json -- a previous store: proves
              everything it contains, including reported_at and protected
2. names     <agent home>/session-names/*.json    -- name, startedAt, endedAt
3. ledger    <control dir>/reap-ledger.jsonl      -- reap/reap-failed/dismiss/revive
              events, the parent of the row, the digest path
4. digests   <handoff dir>/_reaped/*.md           -- name, unit, parent NAME,
              spawned, verdict, and the reap timestamp in the header
5. sessions  <agent home>/sessions/*/<ts>_<sid>.jsonl -- the transcript path

Those five locations are resolved by `fleet_paths`, not by this file: see its
docstring for the environment variables that move them.

READ-ONLY BY DEFAULT. `--apply` is the only writer, and it always takes a
timestamped backup first, refuses to shrink the store unless `--allow-shrink` is
passed explicitly, and records a `restore` row in the ledger (a `restore` row is
store-level, not session-level, and names its subject as "*", scope "store").

    python3 fleet_restore.py [--dry-run] [--apply] [--json] [--allow-shrink]

The headline it prints answers one question: "if lineage.json vanished right now,
what would come back, and what would be lost?"
"""

from __future__ import annotations

import argparse
import datetime as _dt
import glob
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import fleet_paths

NAMES_DIR = fleet_paths.agent_home() / "session-names"
SESSIONS_GLOB = str(fleet_paths.agent_home() / "sessions" / "*" / "*.jsonl")
LEDGER = fleet_paths.control_dir() / "reap-ledger.jsonl"
BACKUPS_DIR = fleet_paths.control_dir() / "backups"
DIGESTS_DIR = fleet_paths.handoff_dir() / "_reaped"
LINEAGE = fleet_paths.control_dir() / "lineage.json"

SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DIGEST_NAME_RE = re.compile(r"^(?P<date>\d{4}-\d{2}-\d{2})-(?P<name>.+)-(?P<sid8>[0-9a-f]{8})\.md$")
BACKUP_NAME_RE = re.compile(r"^lineage-(?P<stamp>\d{8}T\d{6}Z)\.json$")

FIELDS = ("sessionId", "name", "unit", "done_criteria", "transcript", "spawned_at",
          "reported_at", "report", "keep_until", "reaped_at", "verdict", "why",
          "digest", "parent_session_id", "parent_name", "protected", "role")


# ---------------------------------------------------------------- inputs

def load_backups(backups_dir=BACKUPS_DIR):
    """Newest first: a later backup is a later truth, but only for what it holds."""
    out = []
    for path in sorted(Path(backups_dir).glob("lineage-*.json"), reverse=True):
        if not BACKUP_NAME_RE.match(path.name):
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        kids = data.get("children") or {}
        if isinstance(kids, dict):
            out.append((path, kids))
    return out


def load_names(names_dir=NAMES_DIR):
    out = {}
    for path in sorted(Path(names_dir).glob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        sid = rec.get("sessionId")
        if isinstance(sid, str) and SID_RE.match(sid):
            out[sid] = rec
    return out


def load_ledger(ledger=LEDGER):
    rows = []
    try:
        text = Path(ledger).read_text()
    except OSError:
        return rows
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("sessionId"), str):
            rows.append(row)
    return rows


def load_digests(digests_dir=DIGESTS_DIR):
    """Parse the header block of each digest: the receipt is structured."""
    out = {}
    for path in sorted(Path(digests_dir).glob("*.md")):
        m = DIGEST_NAME_RE.match(path.name)
        try:
            text = path.read_text(errors="replace")
        except OSError:
            continue
        head = text.splitlines()[:14]
        sid = None
        fields = {}
        for line in head:
            if line.startswith("- session:"):
                candidate = line.split(":", 1)[1].strip()
                if SID_RE.match(candidate):
                    sid = candidate
            elif line.startswith("- unit:"):
                fields["unit"] = line.split(":", 1)[1].strip()
            elif line.startswith("- parent:"):
                fields["parent"] = line.split(":", 1)[1].strip()
            elif line.startswith("- spawned:"):
                fields["spawned"] = line.split(":", 1)[1].strip()
            elif line.startswith("- verdict:"):
                fields["verdict"] = line.split(":", 1)[1].strip()
            elif line.startswith("# ") and "reaped" in line:
                stamp = re.search(r"reaped (\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z)", line)
                if stamp:
                    fields["reaped"] = stamp.group(1)
        if not sid:
            continue          # a digest that names no session proves nothing here
        out.setdefault(sid, []).append({"path": path, "name": m.group("name") if m else None,
                                        **fields})
    for sid, entries in out.items():
        entries.sort(key=lambda e: e.get("reaped") or "", reverse=True)
    return out


def load_transcripts(pattern=SESSIONS_GLOB):
    out = {}
    for path in glob.glob(pattern):
        m = re.search(r"_([0-9a-f-]{36})\.jsonl$", path)
        if m and SID_RE.match(m.group(1)):
            out.setdefault(m.group(1), []).append(path)
    for sid, paths in out.items():
        paths.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return out


def _iso(epoch):
    if not isinstance(epoch, int) or epoch <= 0:
        return None
    return _dt.datetime.fromtimestamp(epoch, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


def _epoch_iso(text):
    """'2026-10-17T15:31:43Z' -> epoch seconds, or None."""
    if not isinstance(text, str):
        return None
    try:
        return int(_dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")
                   .replace(tzinfo=_dt.timezone.utc).timestamp())
    except ValueError:
        return None


# ---------------------------------------------------------------- proposal

class Proposal:
    def __init__(self):
        self.children = {}
        self.candidates = []
        self.provenance = {}     # sid -> {field: source}
        self.unproven = {}       # sid -> {field: reason}

    def put(self, sid, field, value, source):
        rec = self.children.setdefault(sid, {})
        if value is None or value == "":
            return False
        if field in self.provenance.get(sid, {}):
            return False                    # first (most trusted) source wins
        rec[field] = value
        self.provenance.setdefault(sid, {})[field] = source
        return True

    def finish(self):
        now_missing = []
        for sid, rec in self.children.items():
            rec.setdefault("sessionId", sid)
            for field in FIELDS:
                if field in rec and rec[field] is not None:
                    continue
                self.unproven.setdefault(sid, {})[field] = _reason(field)
                rec.setdefault(field, None)
            rec["_provenance"] = self.provenance.get(sid, {})
            rec["_unproven"] = self.unproven.get(sid, {})
            now_missing.append(sid)
        return {"version": 1, "children": dict(sorted(self.children.items())),
                "_restored_at": int(_dt.datetime.now(_dt.timezone.utc).timestamp()),
                "_note": "rebuilt by fleet_restore.py; _unproven names what no "
                         "input could prove (null is deliberate, never invented)"}


def _reason(field):
    return {
        "spawned_at": "no input records when this session was enrolled",
        "name": "no input knows a name for this session",
        "unit": "no input declares a unit",
        "parent_session_id": "digests record only the first 8 chars of a parent id",
        "parent_name": "the parent's name is unknown",
        "reported_at": "only a lineage snapshot records reports; none covers it",
        "report": "the report text existed only in the store",
        "reaped_at": "no reap event and no digest header names a reap",
        "verdict": "no event and no digest record a verdict",
        "why": "the reason lived only in the store",
        "digest": "no receipt was found for this session",
        "protected": "protection is a store-only flag",
        "role": "no input records a role",
        "keep_until": "keep windows are store-only",
        "done_criteria": "done criteria lived only in the store",
        "transcript": "no transcript file matches this session id",
    }.get(field, "no input proves this field")


def build(names_dir=NAMES_DIR, sessions_glob=SESSIONS_GLOB, ledger=LEDGER,
          backups_dir=BACKUPS_DIR, digests_dir=DIGESTS_DIR):
    backups = load_backups(backups_dir)
    names = load_names(names_dir)
    rows = load_ledger(ledger)
    digs = load_digests(digests_dir)
    transcripts = load_transcripts(sessions_glob)

    p = Proposal()

    # A FLEET record needs fleet evidence: a backup entry, a ledger event, or a
    # digest. A name-registry or transcript entry alone is a *candidate* -- the box
    # has hundreds of sessions that were never part of a spawn lineage, and
    # restoring them would inflate the store by 10x and make the shrink check
    # meaningless. Candidates are reported, never written.
    core = {r["sessionId"] for r in rows if SID_RE.match(r["sessionId"])}
    core |= set(digs)
    for _, kids in backups:
        core |= {sid for sid in kids if SID_RE.match(sid)}
    for sid in sorted(core):
        p.children.setdefault(sid, {"sessionId": sid})
    p.candidates = sorted((set(names) | set(transcripts)) - core)

    # 1. backups: a previous store proves whatever it holds.
    for path, kids in backups:
        src = f"backup:{path.name}"
        for sid, rec in kids.items():
            if not isinstance(rec, dict) or sid not in p.children:
                continue
            for field in FIELDS:
                p.put(sid, field, rec.get(field), src)

    # 2. names registry: names and enrolment time.
    for sid, rec in names.items():
        if sid not in p.children:
            continue
        p.put(sid, "name", rec.get("name"), "names-registry")
        started = rec.get("startedAt")
        if isinstance(started, int) and started > 10**12:      # milliseconds
            p.put(sid, "spawned_at", started // 1000, "names-registry:startedAt")

    # 3. ledger: parentage, digest path, reap/dismiss/revive events.
    for row in rows:
        sid = row["sessionId"]
        if sid not in p.children:
            continue
        src = f"ledger:{row.get('action')}"
        p.put(sid, "name", row.get("name"), "ledger")
        p.put(sid, "parent_session_id", row.get("parent"), "ledger")
        p.put(sid, "digest", row.get("digest"), "ledger")
        if row.get("action") == "reap":
            p.put(sid, "reaped_at", row.get("ts"), "ledger:reap")
            p.put(sid, "verdict", "reap", "ledger:reap")
            p.put(sid, "why", row.get("why"), "ledger:reap")

    # 4. digests: the receipt is structured, so it proves its own fields.
    for sid, entries in digs.items():
        if sid not in p.children:
            continue
        e = entries[0]                       # newest receipt for this session
        p.put(sid, "name", e.get("name"), "digest")
        p.put(sid, "unit", e.get("unit"), "digest")
        p.put(sid, "digest", str(e["path"]), "digest")
        p.put(sid, "spawned_at", _epoch_iso(e.get("spawned")), "digest")
        p.put(sid, "reaped_at", _epoch_iso(e.get("reaped")), "digest:header")
        if e.get("verdict"):
            p.put(sid, "verdict", e["verdict"].split(" —")[0].strip(), "digest")
            p.put(sid, "why", e["verdict"].split("—", 1)[-1].strip(), "digest")
        parent = (e.get("parent") or "").strip()
        if parent:
            name_only = parent.split("(", 1)[0].strip()
            if name_only and name_only not in ("?", "-", "none"):
                p.put(sid, "parent_name", name_only, "digest")

    # 5. transcripts: a path that exists is a fact about the filesystem.
    for sid, paths in transcripts.items():
        if sid not in p.children:
            continue
        p.put(sid, "transcript", paths[0], "sessions")

    doc = p.finish()
    doc["_candidates"] = p.candidates
    return doc, {"names": len(names), "ledger_rows": len(rows),
                        "digests": len(digs), "transcripts": len(transcripts),
                        "backups": [str(b[0].name) for b in backups]}


# ---------------------------------------------------------------- reporting

def existing_store(lineage=LINEAGE):
    try:
        data = json.loads(Path(lineage).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    kids = data.get("children")
    return kids if isinstance(kids, dict) else {}


def compare(proposed, existing):
    restored = proposed["children"]
    lost = [f"{sid} ({rec.get('name')})" for sid, rec in (existing or {}).items()
            if sid not in restored]
    essential = ("name", "spawned_at")
    partial = {sid: [f for f in essential if f in rec["_unproven"]]
               for sid, rec in restored.items()}
    partial = {sid: f for sid, f in partial.items() if f}
    complete = len(restored) - len(partial)
    return {"records": len(restored), "complete": complete, "partial": len(partial),
            "lost": lost, "partial_fields": partial,
            "existing": len(existing or {}),
            "candidates": len(proposed.get("_candidates") or []),
            "not_in_store": sorted(set(restored) - set(existing or {}))}


def headline(proposal, cmp):
    lost = cmp["lost"]
    line = (f"if lineage.json vanished now: {cmp['records']} record(s) come back "
            f"({cmp['complete']} complete, {cmp['partial']} partial); "
            f"{len(lost)} would be LOST")
    if lost:
        line += ": " + ", ".join(x.split(" (")[0] + " (" + x.split("(")[-1]
                                 for x in lost[:6])
        if len(lost) > 6:
            line += f", +{len(lost) - 6} more"
    return line


# ---------------------------------------------------------------- writing

def _sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def apply_proposal(proposal, lineage=LINEAGE, backups_dir=BACKUPS_DIR,
                   ledger=LEDGER, allow_shrink=False, now=None):
    """Write the store. Backup first, refuse to shrink, record the act."""
    lineage = Path(lineage)
    existing = existing_store(lineage)
    proposed = proposal["children"]
    backup_path = None
    if lineage.exists():
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        Path(backups_dir).mkdir(parents=True, exist_ok=True)
        backup_path = Path(backups_dir) / f"lineage-restore-{stamp}.json"
        backup_path.write_bytes(lineage.read_bytes())
    if len(proposed) < len(existing) and not allow_shrink:
        return {"written": False, "reason": f"refusing to shrink the store: "
                f"{len(existing)} existing, {len(proposed)} proposed "
                f"(pass --allow-shrink to override)", "backup": str(backup_path)}
    lineage.parent.mkdir(parents=True, exist_ok=True)
    lineage.write_text(json.dumps(proposal, indent=2, sort_keys=True) + "\n")
    row = {"ts": int(now if now is not None else
                     _dt.datetime.now(_dt.timezone.utc).timestamp()),
           "action": "restore", "sessionId": "*", "scope": "store",
           "name": "lineage restore", "records": len(proposed),
           "before": len(existing),
           "backup": str(backup_path) if backup_path else None,
           "why": f"rebuilt {len(proposed)} record(s) from evidence"}
    with open(ledger, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return {"written": True, "records": len(proposed),
            "backup": str(backup_path) if backup_path else None}


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description="rebuild lineage.json from evidence")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="the default")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--allow-shrink", action="store_true")
    ap.add_argument("--lineage", default=str(LINEAGE))
    ap.add_argument("--ledger", default=str(LEDGER))
    ap.add_argument("--backups", default=str(BACKUPS_DIR))
    ap.add_argument("--names", default=str(NAMES_DIR))
    ap.add_argument("--digests", default=str(DIGESTS_DIR))
    ap.add_argument("--sessions", default=SESSIONS_GLOB)
    a = ap.parse_args(argv)

    before = _sha(a.lineage)
    proposal, inputs = build(a.names, a.sessions, a.ledger, a.backups, a.digests)
    existing = existing_store(a.lineage)
    cmp = compare(proposal, existing)
    result = {"headline": headline(proposal, cmp), "comparison": cmp,
              "inputs": inputs, "proposed": proposal}
    wrote = {"written": False, "reason": "dry run (default)"}
    if a.apply:
        wrote = apply_proposal(proposal, a.lineage, a.backups, a.ledger,
                               allow_shrink=a.allow_shrink)
    result["apply"] = wrote
    after = _sha(a.lineage)
    result["read_only"] = (before == after) if not a.apply else True

    if a.json:
        print(json.dumps(result, indent=2))
    else:
        print(result["headline"])
        print(f"  inputs: {inputs['names']} names, {inputs['ledger_rows']} ledger "
              f"rows, {inputs['digests']} digests, {inputs['transcripts']} "
              f"transcripts, {len(inputs['backups'])} backups")
        print(f"  left out: {cmp['candidates']} session(s) known only to the name "
              f"registry or the transcripts -- not fleet records, not written")
        if cmp["not_in_store"]:
            print(f"  note: {len(cmp['not_in_store'])} restored record(s) are not in "
                  f"the current store (a backup is older than the prune)")
        if cmp["lost"]:
            print("  would be lost:")
            for item in cmp["lost"][:10]:
                print(f"    - {item}")
        if cmp["partial_fields"]:
            print("  fields no input can prove (written as null, never invented):")
            for sid, fields in list(cmp["partial_fields"].items())[:6]:
                print(f"    - {sid[:8]}: {', '.join(fields)}")
        print(f"  apply: {wrote.get('reason') or wrote}")
        print(f"  read-only: {result['read_only']}")

    # An --apply that REFUSED must not exit 0. A gate reading the exit code would
    # otherwise conclude "restored" when nothing was written (found by fork-3
    # while documenting exit codes; it had to mark the tool un-gateable).
    if a.apply and not wrote.get("written"):
        sys.stderr.write("restore refused: " + str(wrote.get("reason")) + "\n")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
