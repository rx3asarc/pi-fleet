"""Tests for fleet_bundle -- the portable proof, and the thing that must not lie.

The bundle exists because a measured restore lost 41 of 309 fields, all of them in
the evidence layer. So the central test is not "export works": it is **round-trip
reproduces `claims` and `checks`**, the fields a rebuild cannot recover.

The second class of test is about verify's honesty: a single tampered byte must be
named by section, a bundle missing a receipt body must be refused, and verify must
never write. A verifier that writes is a repairer wearing a checker's coat.

RUN: cd /root/.pi/agent/bin && python3 -m unittest test_fleet_bundle
"""

import json
import tempfile
import unittest
from pathlib import Path

import fleet_bundle as B

SID = "01a1b0de-dead-7000-9000-000000000001"
PARENT = "01a1b0de-dead-7000-9000-0000000000ff"


def record(**kw):
    rec = {
        "sessionId": SID, "name": "engrane-citations", "unit": "audit citations",
        "done_criteria": "41 citations", "transcript": None,
        "spawned_at": 1792246000, "reported_at": 1792246100,
        "report": "audited 41 citations", "keep_until": None,
        "reaped_at": 1792246200, "verdict": "reap", "why": "reported its unit",
        "digest": None, "parent_session_id": PARENT, "parent_name": "engrane-main",
        "protected": None, "role": None,
        # the evidence layer: exactly what a restore cannot recover
        "checks": {"status": "verified",
                   "verify": {"cmd": "python3 -m unittest x", "rc": 0},
                   "files": [{"path": "/tmp/x", "exists": True, "bytes": 10}]},
        "claims": {"files": ["/tmp/x"], "unsure": "the second half is untested"},
    }
    rec.update(kw)
    return rec


class World:
    def __init__(self, root):
        self.root = Path(root)
        self.lineage = self.root / "lineage.json"
        self.ledger = self.root / "ledger.jsonl"
        self.digests = self.root / "digests"; self.digests.mkdir()
        self.backups = self.root / "backups"
        self.bundle = self.root / "bundle.jsonl"
        self.store = {}
        self.rows = []
        self.ledger.write_text("")

    def digest(self, sid, body="# receipt\n\nran 41 checks\n"):
        p = self.digests / f"2026-10-17-engrane-{sid[:8]}.md"
        p.write_text(body)
        return p

    def seed(self, kids, rows=None):
        self.store = kids
        self.lineage.write_text(json.dumps({"version": 1, "children": kids},
                                           indent=2) + "\n")
        self.rows = rows if rows is not None else [
            {"ts": 1792246200, "action": "reap", "sessionId": SID,
             "name": "engrane-citations", "parent": PARENT,
             "digest": str(self.digests / f"2026-10-17-engrane-{SID[:8]}.md")}]
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in self.rows))

    def export(self):
        lines, header = B.build_bundle(self.lineage, self.ledger, self.digests)
        self.bundle.write_text("\n".join(lines) + "\n")
        return lines, header

    def verify(self):
        return B.verify(self.bundle, self.lineage, self.ledger, self.digests)

    def import_(self, **kw):
        return B.import_bundle(self.bundle, self.lineage, self.ledger,
                               self.backups, **kw)

    def store_now(self):
        return json.loads(self.lineage.read_text())["children"]

    def ledger_now(self):
        return [json.loads(l) for l in self.ledger.read_text().splitlines()
                if l.strip()]


class Base(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.w = World(td.name)
        self.digest = self.w.digest(SID)
        self.w.seed({SID: record(digest=str(self.digest)),
                     PARENT: record(**{"sessionId": PARENT, "name": "engrane-main",
                                       "parent_session_id": None, "digest": None,
                                       "checks": None, "claims": None})})


# ── round trip: the fields a restore loses must survive here ────────────────
class TestRoundTrip(Base):
    def test_export_then_import_into_an_empty_store_reproduces_every_field(self):
        self.w.export()
        original = self.w.store_now()
        self.w.lineage.write_text(json.dumps({"version": 1, "children": {}}) + "\n")
        out = self.w.import_(apply=True)
        self.assertTrue(out["written"])
        restored = self.w.store_now()
        self.assertEqual(sorted(restored), sorted(original))
        for sid, rec in original.items():
            for field, value in rec.items():
                self.assertEqual(restored[sid].get(field), value,
                                 f"{sid[:8]}.{field} did not survive the bundle")

    def test_claims_and_checks_specifically_survive(self):
        """The 14 fields the restore run proved are lost today."""
        self.w.export()
        self.w.lineage.write_text(json.dumps({"version": 1, "children": {}}) + "\n")
        self.w.import_(apply=True)
        rec = self.w.store_now()[SID]
        self.assertEqual(rec["checks"]["status"], "verified")
        self.assertEqual(rec["checks"]["verify"]["cmd"], "python3 -m unittest x")
        self.assertEqual(rec["claims"]["unsure"], "the second half is untested")

    def test_the_digest_body_is_in_the_bundle_not_just_its_path(self):
        lines, header = self.w.export()
        self.assertEqual(header["sections"]["digests"]["count"], 1)
        bodies = [json.loads(l) for l in lines if json.loads(l).get("kind") == "digests"]
        self.assertIn("ran 41 checks", bodies[0]["body"])

    def test_ledger_rows_round_trip(self):
        self.w.export()
        rows = [json.loads(l)["row"] for l in self.w.bundle.read_text().splitlines()[1:]
                if json.loads(l).get("kind") == "ledger"]
        self.assertEqual(rows, self.w.rows)

    def test_export_is_one_record_per_line_and_diffable(self):
        lines, _ = self.w.export()
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            self.assertNotIn("\n", line)
            json.loads(line)                       # every line is JSON on its own

    def test_export_refuses_when_a_receipt_body_is_missing(self):
        (self.w.digests / f"2026-10-17-engrane-{SID[:8]}.md").unlink()
        with self.assertRaises(ValueError) as ctx:
            B.build_bundle(self.w.lineage, self.w.ledger, self.w.digests)
        self.assertIn("without its receipts", str(ctx.exception))


# ── verify: names the section, never writes ─────────────────────────────────
class TestVerify(Base):
    def test_a_clean_bundle_verifies(self):
        self.w.export()
        report = self.w.verify()
        self.assertEqual(report["verdict"], "CLEAN")
        self.assertEqual(report["exit_code"], 0)
        self.assertTrue(all(s["ok"] for s in report["sections"].values()))

    def tamper(self, kind, old, new):
        text = self.w.bundle.read_text()
        self.assertIn(old, text)
        self.w.bundle.write_text(text.replace(old, new, 1))
        return self.w.verify()

    def test_a_tampered_lineage_section_is_named(self):
        self.w.export()
        report = self.tamper("lineage", '"unit":"audit citations"',
                             '"unit":"audited everything"')
        self.assertEqual(report["verdict"], "MISMATCH")
        self.assertIn("section lineage", " ".join(report["problems"]))
        self.assertFalse(report["sections"]["lineage"]["ok"])
        self.assertTrue(report["sections"]["ledger"]["ok"],
                        "an untouched section must stay ok")

    def test_a_tampered_ledger_section_is_named(self):
        self.w.export()
        report = self.tamper("ledger", '"action":"reap"', '"action":"dismiss"')
        self.assertIn("section ledger", " ".join(report["problems"]))
        self.assertFalse(report["sections"]["ledger"]["ok"])

    def test_a_single_added_byte_fails_even_if_the_value_is_unchanged(self):
        """Checksums cover raw lines: an edit is an edit, not a reformat."""
        self.w.export()
        text = self.w.bundle.read_text()
        self.w.bundle.write_text(text.replace('"action":"reap"',
                                              '"action":"reap" ', 1))
        report = self.w.verify()
        self.assertEqual(report["verdict"], "MISMATCH")
        self.assertFalse(report["sections"]["ledger"]["ok"],
                         "a whitespace-only edit must still fail the section")
        self.assertTrue(report["sections"]["lineage"]["ok"])

    def test_a_tampered_digest_body_is_named(self):
        self.w.export()
        report = self.tamper("digests", "ran 41 checks", "ran 0 checks")
        self.assertIn("section digests", " ".join(report["problems"]))
        self.assertFalse(report["sections"]["digests"]["ok"])

    def test_records_only_in_the_store_are_reported(self):
        self.w.export()
        kids = self.w.store_now()
        kids["01a1b0de-dead-7000-9000-000000000003"] = record(
            **{"sessionId": "01a1b0de-dead-7000-9000-000000000003",
               "name": "after-the-bundle"})
        self.w.lineage.write_text(json.dumps({"version": 1, "children": kids}) + "\n")
        report = self.w.verify()
        self.assertEqual(report["verdict"], "MISMATCH")
        self.assertIn("01a1b0de-dead-7000-9000-000000000003",
                      report["mismatches"]["only_in_store"])

    def test_a_disagreement_on_reaped_at_is_reported_by_field(self):
        self.w.export()
        kids = self.w.store_now()
        kids[SID]["reaped_at"] = 1792249999
        self.w.lineage.write_text(json.dumps({"version": 1, "children": kids}) + "\n")
        report = self.w.verify()
        self.assertIn(SID, report["mismatches"]["disagree"]["reaped_at"])

    def test_a_digest_that_differs_on_disk_is_reported(self):
        self.w.export()
        self.digest.write_text("# receipt\n\nrewritten after the bundle\n")
        report = self.w.verify()
        self.assertEqual(report["verdict"], "MISMATCH")
        self.assertTrue(report["mismatches"]["digests_differ"])

    def test_an_unreadable_bundle_exits_two(self):
        self.w.bundle.write_text("not a bundle at all\n")
        report = self.w.verify()
        self.assertEqual(report["verdict"], "UNREADABLE")
        self.assertEqual(report["exit_code"], 2)

    def test_a_missing_file_exits_two(self):
        report = B.verify(self.w.root / "nope.jsonl")
        self.assertEqual(report["exit_code"], 2)

    def test_verify_never_writes(self):
        self.w.export()
        def snap():
            return {p: (Path(p).stat().st_size, Path(p).stat().st_mtime_ns,
                        Path(p).read_bytes())
                    for p in (self.w.lineage, self.w.ledger, self.w.bundle,
                              self.digest)}
        before = snap()
        self.w.verify()
        self.assertEqual(snap(), before, "verify wrote something")
        self.assertFalse(self.w.backups.exists(), "verify created a backup dir")


# ── import: refuses to be dangerous ─────────────────────────────────────────
class TestImport(Base):
    def test_import_without_apply_is_refused(self):
        self.w.export()
        out = self.w.import_()
        self.assertFalse(out["written"])
        self.assertIn("--apply", out["reason"])

    def test_import_backs_up_first(self):
        self.w.export()
        before = self.w.store_now()
        out = self.w.import_(apply=True)
        backup = Path(out["backup"])
        self.assertTrue(backup.exists())
        self.assertEqual(json.loads(backup.read_text())["children"], before)

    def test_import_refuses_to_shrink(self):
        self.w.export()                       # bundle carries 2 records
        kids = self.w.store_now()             # the live store has grown to 4
        for i in (3, 4):
            sid = f"01a1b0de-dead-7000-9000-00000000000{i}"
            kids[sid] = record(**{"sessionId": sid, "name": f"grown-{i}",
                                  "digest": None, "checks": None, "claims": None})
        self.w.lineage.write_text(json.dumps({"version": 1, "children": kids}) + "\n")
        before = self.w.lineage.read_bytes()
        out = self.w.import_(apply=True)
        self.assertFalse(out["written"])
        self.assertIn("refusing to shrink", out["reason"])
        self.assertEqual(self.w.lineage.read_bytes(), before,
                         "a refused import writes nothing")

    def test_a_differing_field_is_reported_and_the_record_skipped(self):
        self.w.export()
        kids = self.w.store_now()
        kids[SID]["report"] = "a report written after the bundle"
        self.w.lineage.write_text(json.dumps({"version": 1, "children": kids}) + "\n")
        out = self.w.import_(apply=True)
        self.assertTrue(out["written"])
        self.assertEqual(out["conflict_count"], 1)
        self.assertEqual(self.w.store_now()[SID]["report"],
                         "a report written after the bundle",
                         "a differing field must never be overwritten silently")

    def test_force_overwrites_the_conflict(self):
        self.w.export()
        kids = self.w.store_now()
        kids[SID]["report"] = "later"
        self.w.lineage.write_text(json.dumps({"version": 1, "children": kids}) + "\n")
        out = self.w.import_(apply=True, force=True)
        self.assertEqual(out["merged"], 1)
        self.assertEqual(self.w.store_now()[SID]["report"], "audited 41 citations")

    def test_import_appends_exactly_one_ledger_row(self):
        self.w.export()
        self.w.import_(apply=True, now=1792250000)
        restores = [r for r in self.w.ledger_now() if r.get("action") == "restore"]
        self.assertEqual(len(restores), 1)
        self.assertEqual(restores[0]["scope"], "bundle")
        self.assertEqual(restores[0]["sessionId"], "*")
        self.assertEqual(restores[0]["ts"], 1792250000)
        self.assertEqual(restores[0]["records"], 2)

    def test_import_of_an_unreadable_bundle_is_refused(self):
        self.w.bundle.write_text("{}\n")
        out = self.w.import_(apply=True)
        self.assertFalse(out["written"])

    def test_import_does_not_touch_digest_bodies(self):
        self.w.export()
        before = self.digest.read_text()
        self.w.import_(apply=True)
        self.assertEqual(self.digest.read_text(), before,
                         "import restores records; receipts on disk are not its to edit")


class TestCli(Base):
    def run_cli(self, *argv):
        import contextlib, io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = B.main(list(argv))
        return code, buf.getvalue()

    def test_export_then_verify_round_trip_through_the_cli(self):
        out = self.w.root / "cli.jsonl"
        code, text = self.run_cli("export", "--out", str(out), "--json")
        self.assertEqual(code, 0)
        header = json.loads(text)
        self.assertEqual(header["schema"], B.SCHEMA)
        self.assertIn("sha256", header["sections"]["lineage"])
        code, text = self.run_cli("verify", str(out), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["verdict"], "CLEAN")

    def test_verify_exit_code_is_one_on_mismatch(self):
        self.w.export()
        kids = self.w.store_now()
        kids[SID]["verdict"] = "dismissed"
        self.w.lineage.write_text(json.dumps({"version": 1, "children": kids}) + "\n")
        code, _ = self.run_cli("verify", str(self.w.bundle))
        self.assertEqual(code, 1)

    def test_import_requires_apply_through_the_cli(self):
        self.w.export()
        code, text = self.run_cli("import", str(self.w.bundle), "--json")
        self.assertEqual(code, 1)
        self.assertIn("--apply", json.loads(text)["reason"])


if __name__ == "__main__":
    unittest.main()
