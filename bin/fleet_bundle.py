#!/usr/bin/env python3
"""fleet_bundle.py — a portable, checksummed home for the evidence layer.

Why this exists (from a measurement, not a hunch): a restore run over the real
store recovered 33/33 records but could not recover 41 of 309 non-null fields --
every one of them in the evidence layer (`checks`, `claims`) or a runtime flag.
The conclusion was written into the constitution: **anything that must survive a
rebuild cannot live only in lineage.json.**

So a bundle carries, in one file: the full store verbatim, the ledger rows, and the
digest BODIES (not paths). JSON Lines, one record per line, so `diff` is readable.
A header names the schema version, the creation time, the host, and a sha256 for
each section.

    python3 fleet_bundle.py export [--out PATH] [--json]
    python3 fleet_bundle.py verify PATH [--json]
    python3 fleet_bundle.py import PATH --apply [--allow-shrink] [--force] [--json]

VERIFY is the sentence a human wants before trusting a restore: it recomputes every
checksum, then cross-checks the bundle against the live stores -- which records
exist in one and not the other, which disagree on reaped_at/closed_at, which
digests differ. It never writes. Exit 0 clean, 1 mismatch, 2 unreadable.

IMPORT is `--apply` only (a restore you can trigger by accident is a weapon): it
backs up first, refuses to shrink unless told, never overwrites a differing field
silently (it reports the record and skips it unless --force), and appends exactly
one ledger row with action=restore, scope=bundle.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import socket
import sys
from pathlib import Path

import fleet_paths

SCHEMA = 1

LINEAGE = fleet_paths.control_dir() / "lineage.json"
LEDGER = fleet_paths.control_dir() / "reap-ledger.jsonl"
DIGESTS_DIR = fleet_paths.handoff_dir() / "_reaped"
BUNDLES_DIR = fleet_paths.control_dir() / "bundles"
BACKUPS_DIR = fleet_paths.control_dir() / "backups"

# Fields that are store-only today; they are the reason this file exists.
EVIDENCE_FIELDS = ("checks", "claims", "protected", "role", "ended_between_ticks",
                   "last_notified", "last_notified_at", "report", "reported_at",
                   "keep_until", "done_criteria", "grace_until", "blocked",
                   "blocked_at", "review_sid", "review_pending")


def _canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _sha(lines):
    h = hashlib.sha256()
    for line in lines:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _now_iso(now=None):
    ts = (now if now is not None
          else _dt.datetime.now(_dt.timezone.utc).timestamp())
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------- export

def build_bundle(lineage=LINEAGE, ledger=LEDGER, digests_dir=DIGESTS_DIR,
                 now=None, host=None):
    """The bundle as a list of text lines. Raises if a receipt body is missing."""
    try:
        kids = json.loads(Path(lineage).read_text()).get("children") or {}
    except (OSError, json.JSONDecodeError) as e:
        raise ValueError(f"cannot read lineage: {e}")
    rows = []
    try:
        for line in Path(ledger).read_text().splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except OSError:
        rows = []
    except json.JSONDecodeError as e:
        raise ValueError(f"ledger is not valid JSON Lines: {e}")

    lineage_lines, ledger_lines, digest_lines = [], [], []
    for sid in sorted(kids):
        rec = kids[sid]
        lineage_lines.append(_canon({"kind": "lineage", "sessionId": sid,
                                     "record": rec}))
    for row in rows:
        ledger_lines.append(_canon({"kind": "ledger", "row": row}))

    missing = []
    for sid in sorted(kids):
        rec = kids[sid]
        path = rec.get("digest")
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            missing.append(f"{sid}: digest body missing at {path}")
            continue
        digest_lines.append(_canon({"kind": "digests", "sessionId": sid,
                                    "name": rec.get("name"),
                                    "path": str(path),
                                    "body": p.read_text(errors="replace")}))
    if missing:
        raise ValueError("refusing to write a bundle without its receipts: "
                         + "; ".join(missing[:3]))

    header = {"kind": "header", "schema": SCHEMA, "created_at": _now_iso(now),
              "host": host or socket.gethostname(),
              "sections": {
                  "lineage": {"count": len(lineage_lines),
                              "sha256": _sha(lineage_lines)},
                  "ledger": {"count": len(ledger_lines),
                             "sha256": _sha(ledger_lines)},
                  "digests": {"count": len(digest_lines),
                              "sha256": _sha(digest_lines)}}}
    lines = [_canon(header)] + lineage_lines + ledger_lines + digest_lines
    return lines, header


def _atomic_write(path, text):
    """Stage, fsync, rename. A killed export must leave NO half bundle at the path.

    `Path.write_text` in place is the same fault the store had one layer down: a
    process killed mid-call leaves a prefix on disk, and nothing distinguishes it
    from a complete file except running verify -- which is exactly the receipt a
    half-written receipt cannot be trusted to provide.

    Written through `Path(...)` deliberately (not a bare `open`) so the fault
    suite can inject a mid-write death into the staged file.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    Path(tmp).write_text(text)
    fd = os.open(tmp, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)             # atomic within one filesystem
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)                 # the rename itself must be durable
    finally:
        os.close(dfd)


def export(out=None, **kw):
    lines, header = build_bundle(**kw)
    if out:
        _atomic_write(out, "\n".join(lines) + "\n")
    return lines, header


# ---------------------------------------------------------------- parse/verify

class Unreadable(Exception):
    pass


def parse(lines):
    """-> (header, sections) or raise Unreadable. Checksums are NOT checked here.

    sections map kind -> the RAW text of each line, so a checksum mismatch means a
    byte changed. Values are parsed from those same lines by the cross-check.
    """
    if not lines:
        raise Unreadable("empty bundle")
    try:
        header = json.loads(lines[0])
    except json.JSONDecodeError as e:
        raise Unreadable(f"header is not JSON: {e}")
    if header.get("kind") != "header" or header.get("schema") != SCHEMA:
        raise Unreadable(f"bad header (kind={header.get('kind')!r}, "
                         f"schema={header.get('schema')!r})")
    sections = {"lineage": [], "ledger": [], "digests": []}
    for i, line in enumerate(lines[1:], 2):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            raise Unreadable(f"line {i} is not JSON: {e}")
        kind = {"digest": "digests"}.get(obj.get("kind"), obj.get("kind"))
        if kind not in sections:
            raise Unreadable(f"line {i} has unknown kind {kind!r}")
        # Checksums cover the RAW line: verify must notice a changed byte, not just
        # a changed value. (Re-canonicalising here would silently excuse an edit.)
        sections[kind].append(line)
    return header, sections


def read_bundle(path):
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise Unreadable(f"cannot read {path}: {e}")
    return parse(text.splitlines())


def verify(path, lineage=LINEAGE, ledger=LEDGER, digests_dir=DIGESTS_DIR):
    """Recompute checksums, then cross-check against the live stores. Writes nothing."""
    if str(path).endswith(".tmp"):
        return {"verdict": "UNREADABLE", "exit_code": 2, "sections": {},
                "mismatches": {},
                "problems": [f"{path} is a staging file (*.tmp), not a bundle: an "
                             f"export that was killed mid-write leaves one. Never "
                             f"treat it as a receipt -- re-export instead."]}
    try:
        header, sections = read_bundle(path)
    except Unreadable as e:
        return {"verdict": "UNREADABLE", "exit_code": 2, "problems": [str(e)],
                "sections": {}, "mismatches": {}}

    problems, section_report = [], {}
    for name, want in (header.get("sections") or {}).items():
        got = sections.get(name, [])
        digest = _sha(got)
        ok = (digest == want.get("sha256") and len(got) == want.get("count"))
        section_report[name] = {"ok": ok, "count": len(got),
                                "expected_count": want.get("count"),
                                "sha256": digest,
                                "expected_sha256": want.get("sha256")}
        if not ok:
            problems.append(f"section {name} does not match its checksum "
                            f"({len(got)} lines, expected {want.get('count')})")

    # Cross-check against the live stores (read-only).
    store = {}
    try:
        store = json.loads(Path(lineage).read_text()).get("children") or {}
    except (OSError, json.JSONDecodeError):
        problems.append("live store is unreadable; cross-check skipped")
    bundle_records = {}
    for line in sections.get("lineage", []):
        obj = json.loads(line)
        bundle_records[obj["sessionId"]] = obj["record"]
    only_bundle = sorted(set(bundle_records) - set(store))
    only_store = sorted(set(store) - set(bundle_records))
    disagree = {}
    for sid in sorted(set(bundle_records) & set(store)):
        for field in ("reaped_at", "closed_at", "verdict", "digest"):
            if bundle_records[sid].get(field) != store[sid].get(field):
                disagree.setdefault(field, []).append(sid)

    digest_diff, digest_missing = [], []
    for line in sections.get("digests", []):
        obj = json.loads(line)
        p = Path(obj.get("path") or "")
        if not p.exists():
            digest_missing.append(f"{obj['sessionId']}: body for {p} is gone from disk")
        elif p.read_text(errors="replace") != obj.get("body"):
            digest_diff.append(f"{obj['sessionId']}: {p} differs from the bundle")

    if only_bundle or only_store or disagree or digest_diff or digest_missing:
        if only_bundle:
            problems.append(f"{len(only_bundle)} record(s) only in the bundle")
        if only_store:
            problems.append(f"{len(only_store)} record(s) only in the live store")
        for field, sids in disagree.items():
            problems.append(f"{len(sids)} record(s) disagree on {field}")
        if digest_diff:
            problems.append(f"{len(digest_diff)} digest(s) differ from the bundle")
        if digest_missing:
            problems.append(f"{len(digest_missing)} digest body/bodies missing on disk")

    clean = not problems
    return {"verdict": "CLEAN" if clean else "MISMATCH",
            "exit_code": 0 if clean else 1,
            "problems": problems, "sections": section_report,
            "mismatches": {"only_in_bundle": only_bundle, "only_in_store": only_store,
                           "disagree": disagree, "digests_differ": digest_diff,
                           "digests_missing": digest_missing},
            "header": header}


# ---------------------------------------------------------------- import

def import_bundle(path, lineage=LINEAGE, ledger=LEDGER, backups_dir=BACKUPS_DIR,
                  apply=False, allow_shrink=False, force=False, now=None):
    if not apply:
        return {"written": False,
                "reason": "import requires --apply (a restore you can trigger by "
                          "accident is a weapon)"}
    try:
        header, sections = read_bundle(path)
    except Unreadable as e:
        return {"written": False, "reason": str(e)}
    bundle_records = {}
    for line in sections.get("lineage", []):
        obj = json.loads(line)
        bundle_records[obj["sessionId"]] = obj["record"]
    if not bundle_records:
        return {"written": False, "reason": "the bundle carries no records"}

    try:
        existing = json.loads(Path(lineage).read_text()).get("children") or {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    if len(bundle_records) < len(existing) and not allow_shrink:
        return {"written": False,
                "reason": f"refusing to shrink the store: {len(existing)} existing, "
                          f"{len(bundle_records)} in the bundle (--allow-shrink)"}

    added, merged, skipped, conflicts = [], [], [], {}
    for sid, rec in bundle_records.items():
        if sid not in existing:
            existing[sid] = rec
            added.append(sid)
            continue
        diffs = {f: (existing[sid].get(f), rec.get(f)) for f in rec
                 if existing[sid].get(f) != rec.get(f)}
        if not diffs:
            skipped.append(sid)
            continue
        if force:
            existing[sid].update(rec)
            merged.append(sid)
        else:
            conflicts[sid] = diffs          # reported, record skipped, nothing written
    if conflicts and not force:
        # A conflict is not a failure to import the rest; it is a decision to defer.
        pass

    backup = None
    if Path(lineage).exists():
        Path(backups_dir).mkdir(parents=True, exist_ok=True)
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = Path(backups_dir) / f"lineage-bundle-{stamp}.json"
        backup.write_bytes(Path(lineage).read_bytes())
    Path(lineage).write_text(json.dumps({"version": 1, "children": existing},
                                        indent=2, sort_keys=True) + "\n")
    row = {"ts": int(now if now is not None else
                     _dt.datetime.now(_dt.timezone.utc).timestamp()),
           "action": "restore", "scope": "bundle", "sessionId": "*",
           "name": "bundle import", "records": len(bundle_records),
           "before": len(bundle_records) - len(added) if added else len(existing),
           "added": len(added), "merged": len(merged), "skipped": len(skipped),
           "conflicts": len(conflicts), "bundle": str(path),
           "backup": str(backup) if backup else None,
           "why": f"imported bundle {header.get('created_at')} "
                  f"({len(added)} added, {len(merged)} merged, "
                  f"{len(conflicts)} deferred)"}
    with open(ledger, "a") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")
    return {"written": True, "added": len(added), "merged": len(merged),
            "skipped": len(skipped), "conflicts": {k: v for k, v in
                                                   list(conflicts.items())[:5]},
            "conflict_count": len(conflicts), "backup": str(backup) if backup else None}


# ---------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description="portable checksummed fleet bundle")
    sub = ap.add_subparsers(dest="cmd")

    e = sub.add_parser("export")
    e.add_argument("--out")
    e.add_argument("--json", action="store_true")

    v = sub.add_parser("verify")
    v.add_argument("path")
    v.add_argument("--json", action="store_true")

    i = sub.add_parser("import")
    i.add_argument("path")
    i.add_argument("--apply", action="store_true")
    i.add_argument("--allow-shrink", action="store_true")
    i.add_argument("--force", action="store_true")
    i.add_argument("--json", action="store_true")

    a = ap.parse_args(argv)
    if a.cmd == "export":
        out = a.out
        if not out:
            Path(BUNDLES_DIR).mkdir(parents=True, exist_ok=True)
            out = str(Path(BUNDLES_DIR) /
                      f"fleet-{_now_iso().replace(':', '')}.jsonl")
        try:
            lines, header = export(out)
        except ValueError as e2:
            print(f"refused: {e2}")
            return 2
        if a.json:
            print(json.dumps(header, indent=2))
        else:
            s = header["sections"]
            print(f"exported to {out}: {s['lineage']['count']} records, "
                  f"{s['ledger']['count']} ledger rows, {s['digests']['count']} "
                  f"digest bodies (schema {header['schema']})")
        return 0
    if a.cmd == "verify":
        report = verify(a.path)
        if a.json:
            print(json.dumps(report, indent=2))
        else:
            print(f"{report['verdict']} — {a.path}")
            for p in report["problems"]:
                print(f"  - {p}")
            for name, s in report["sections"].items():
                print(f"  [{('ok' if s['ok'] else 'BAD'):>3}] {name}: {s['count']} "
                      f"line(s), sha {s['sha256'][:12]}")
        return report["exit_code"]
    if a.cmd == "import":
        out = import_bundle(a.path, apply=a.apply, allow_shrink=a.allow_shrink,
                            force=a.force)
        if a.json:
            print(json.dumps(out, indent=2))
        elif out.get("written"):
            print(f"imported: {out['added']} added, {out['merged']} merged, "
                  f"{out['skipped']} unchanged, {out['conflict_count']} deferred "
                  f"(conflicts); backup {out['backup']}")
        else:
            print(f"not imported: {out['reason']}")
        return 0 if out.get("written") else 1
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
