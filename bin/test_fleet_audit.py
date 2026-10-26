"""Tests for fleet_audit -- one hand-built broken store per failure mode.

The rule this file enforces on the auditor: **catch exactly one thing**. A checker
that also complains about something else (or stays quiet) is a checker people
learn to ignore, which is the same as not having one. So every mutation below
asserts the ONE check that must fail AND that every other check still passes.

The baseline store is deliberately all-PASS (including the `enrolled_source`
field the real schema lacks), so any additional failure in a mutated store is a
false positive and fails the test.
"""

import json
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

import fleet_audit as A

SID = "01a1beef-beef-7000-9000-000000000001"
PARENT = "01a1beef-beef-7000-9000-0000000000ff"
NAME = "globex-citations"


def record(**kw):
    rec = {
        "sessionId": SID,
        "name": NAME,
        "unit": "audit the citations appendix",
        "done_criteria": "",
        "transcript": None,
        "spawned_at": 1792246000,
        "reported_at": 1792246100,
        "report": "audited 41 citations",
        "keep_until": None,
        "reaped_at": 1792246200,
        "verdict": "reap",
        "why": "process gone, and it reported its unit",
        "digest": None,                      # filled by the fixture
        "parent_session_id": PARENT,
        "parent_name": "globex-main",
        "protected": None,
        "role": None,
        "enrolled_source": "pi-tmux",
        "checks": {"status": "verified",
                   "files": [{"path": "/tmp/x", "exists": True}],
                   "verify": {"cmd": "true", "rc": 0}},
    }
    rec.update(kw)
    return rec


def reap_row(**kw):
    row = {"ts": 1792246200, "action": "reap", "sessionId": SID, "name": NAME,
           "parent": PARENT, "why": "process gone", "digest": "DIGEST",
           "evidence": "verified"}
    row.update(kw)
    return row


class Fixture:
    """A store on disk that passes every check, plus surgical breakage."""

    def __init__(self, tmp):
        self.dir = Path(tmp)
        self.lineage = self.dir / "lineage.json"
        self.ledger = self.dir / "reap-ledger.jsonl"
        self.digest = self.dir / "digest.md"
        self.digest.write_text(
            f"# {NAME}-{SID[:8]} — reaped 2026-10-17T15:00:00Z (session {SID})\n"
            f"- unit: audit the citations appendix\n\n## What it said it did\n\n"
            f"Audited 41 citations.\n")
        keep = record(parent_session_id=PARENT, parent_name="globex-main")
        keeper = record(**{"sessionId": PARENT, "name": "globex-main",
                           "parent_session_id": None, "parent_name": None,
                           "reported_at": None, "report": None, "reaped_at": None,
                           "verdict": None, "protected": True, "digest": None,
                           "checks": None})
        keep["digest"] = str(self.digest)
        self.children = {SID: keep, PARENT: keeper}
        self.rows = [reap_row(digest=str(self.digest))]
        self.write()

    def write(self, children=None, rows=None):
        self.children = children if children is not None else self.children
        self.rows = rows if rows is not None else self.rows
        self.lineage.write_text(json.dumps(
            {"version": 1, "children": self.children}, indent=2) + "\n")
        self.ledger.write_text("".join(json.dumps(r) + "\n" for r in self.rows))

    def audit(self):
        return A.run(self.lineage, self.ledger)

    def mutate(self, children=None, rows=None):
        self.write(children if children is not None else self.children,
                   rows if rows is not None else self.rows)
        return self.audit()


class AuditTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.f = Fixture(self._tmp.name)

    # -- the assertion that makes "exactly one thing" mean something ----------
    def only(self, report, key, status="FAIL"):
        got = {c["check"]: c["status"] for c in report["checks"]}
        self.assertIn(key, got)
        self.assertEqual(got[key], status, f"{key} should be {status}: "
                                           f"{[c['detail'] for c in report['checks'] if c['check'] == key]}")
        others = {k: v for k, v in got.items() if k != key and v != "PASS"}
        self.assertEqual(others, {}, f"{key} also broke: {others}")
        return next(c for c in report["checks"] if c["check"] == key)


class TestBaseline(AuditTestCase):
    def test_a_healthy_store_passes_everything(self):
        report = self.f.audit()
        self.assertEqual(report["verdict"], "PASS",
                         [c for c in report["checks"] if c["status"] != "PASS"])
        self.assertEqual(report["exit_code"], 0)

    def test_the_summary_line_is_one_line(self):
        line = A.summary_line(self.f.audit())
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("PASS", line)
        self.assertIn("2 records", line)


class TestSchema(AuditTestCase):
    def test_a_record_missing_a_field_is_caught(self):
        rec = record(digest=str(self.f.digest))
        del rec["name"]
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]}),
                        "schema")
        self.assertIn("missing 'name'", " ".join(chk["ids"]))

    def test_a_key_that_is_not_a_session_id_is_caught(self):
        # A record keyed by a pid is a real defect (adopt once keyed by pid). It is
        # kept otherwise consistent so ONLY the schema check complains.
        rec = record(**{"sessionId": "pid-1483429", "digest": None,
                        "reaped_at": None, "verdict": None})
        kids = {"pid-1483429": rec, SID: self.f.children[SID],
                PARENT: self.f.children[PARENT]}
        chk = self.only(self.f.mutate(kids, self.f.rows), "schema")
        self.assertIn("not a session id", " ".join(chk["ids"]))

    def test_a_non_integer_timestamp_is_caught(self):
        rec = record(digest=str(self.f.digest), spawned_at="yesterday")
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]}),
                        "schema")
        self.assertIn("timestamp", " ".join(chk["ids"]))

    def test_a_record_predating_the_field_is_noted_not_failed(self):
        rec = record(digest=str(self.f.digest))
        del rec["enrolled_source"]
        report = self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]})
        chk = self.only(report, "enrolled_source", "NOTE")
        self.assertTrue("predate" in chk["detail"] or "does not emit" in chk["detail"],
                        chk["detail"])
        self.assertEqual(report["exit_code"], 0, "history must not block the gate")


class TestWriteAhead(AuditTestCase):
    def test_reaped_with_no_ledger_row_at_all(self):
        chk = self.only(self.f.mutate(rows=[]), "i1_write_ahead")
        self.assertIn("no ledger row", chk["detail"])

    def test_reap_row_whose_record_was_never_marked(self):
        rec = record(digest=str(self.f.digest), reaped_at=None, verdict=None)
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]}),
                        "i1_write_ahead")
        self.assertIn("write-ahead never completed", chk["detail"])

    def test_reaped_at_written_by_dismiss_is_its_own_defect(self):
        rows = [reap_row(action="dismiss")]
        chk = self.only(self.f.mutate(rows=rows), "i1_write_ahead")
        self.assertIn("dismiss", chk["detail"])
        self.assertIn("disagree", chk["detail"])


class TestPruneReconciliation(AuditTestCase):
    """A `prune` row is the tombstone: a record MAY be gone if the ledger says why.

    Tundra pruned four rehearsal fixtures by deleting them, which broke I1 until the
    four `prune` rows were written. The auditor must tell three cases apart:
    reconciled (fine), unexplained (FAIL), and a prune row for a record that still
    exists (FAIL -- the row claims a deletion that never happened).
    """

    def test_a_prune_row_reconciles_a_missing_record(self):
        # keep the reap row, delete the record, add the tombstone
        kids = {PARENT: self.f.children[PARENT]}
        rows = self.f.rows + [reap_row(action="prune",
                                       why="rehearsal fixture, record deleted")]
        report = self.f.mutate(kids, rows)
        chk = next(c for c in report["checks"] if c["check"] == "i1_write_ahead")
        self.assertEqual(chk["status"], "PASS", chk["detail"])
        self.assertIn("reconciled", chk["detail"])
        self.assertEqual(report["verdict"], "PASS",
                         [c for c in report["checks"] if c["status"] != "PASS"])

    def test_without_the_prune_row_the_same_state_still_fails(self):
        kids = {PARENT: self.f.children[PARENT]}
        report = self.f.mutate(kids, self.f.rows)          # reap row, no tombstone
        chk = self.only(report, "i1_write_ahead")
        self.assertIn("write-ahead never completed", chk["detail"])
        self.assertIn(SID, " ".join(chk["ids"]))

    def test_a_prune_row_for_a_record_that_still_exists_is_a_different_bug(self):
        rows = self.f.rows + [reap_row(action="prune", why="claimed deletion")]
        chk = self.only(self.f.mutate(rows=rows), "i1_write_ahead")
        self.assertIn("deletion that never happened", chk["detail"])
        self.assertIn("prune row claims a deletion", " ".join(chk["ids"]))

    def test_a_prune_row_does_not_excuse_a_closed_by_dismiss_record(self):
        # dismiss + reaped_at stays its own defect even if a tombstone also exists
        rows = [reap_row(action="dismiss"), reap_row(action="prune")]
        rec = record(digest=str(self.f.digest))
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]},
                                      rows), "i1_write_ahead")
        self.assertIn("dismiss", chk["detail"])


class TestCorrectiveRows(AuditTestCase):
    def test_two_corrections_for_one_reap(self):
        rows = self.f.rows + [reap_row(action="reap-failed", why="first"),
                              reap_row(action="reap-failed", why="second")]
        chk = self.only(self.f.mutate(rows=rows), "i2_corrective")
        self.assertIn("2 corrective rows", " ".join(chk["ids"]))

    def test_a_correction_without_a_reap(self):
        rec = record(digest=str(self.f.digest), reaped_at=None, verdict=None)
        rows = [reap_row(action="reap-failed", why="close failed")]
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]},
                                      rows), "i2_corrective")
        self.assertIn("no preceding reap", " ".join(chk["ids"]))


class TestRevive(AuditTestCase):
    def test_reaped_but_claims_to_be_live(self):
        rec = record(digest=str(self.f.digest), verdict="hold")
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]}),
                        "i3_revive")
        self.assertIn("no revive row", " ".join(chk["ids"]))

    def test_a_revive_row_makes_it_legal(self):
        rec = record(digest=str(self.f.digest), verdict="active")
        rows = self.f.rows + [reap_row(action="revive", why="brought back")]
        report = self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]}, rows)
        got = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(got["i3_revive"], "PASS")


class TestRowSubject(AuditTestCase):
    def test_a_row_with_no_session_is_caught(self):
        rows = self.f.rows + [reap_row(sessionId=None)]
        chk = self.only(self.f.mutate(rows=rows), "i4_subject")
        self.assertIn("names no sessionId", " ".join(chk["ids"]))

    def test_a_row_naming_several_sessions_is_caught(self):
        rows = self.f.rows + [reap_row(sessionId=[SID, PARENT])]
        chk = self.only(self.f.mutate(rows=rows), "i4_subject")
        self.assertIn("names 2 sessions", " ".join(chk["ids"]))


class TestDigests(AuditTestCase):
    def test_a_missing_digest_file(self):
        self.f.digest.unlink()
        chk = self.only(self.f.mutate(), "digests")
        self.assertIn("missing at", " ".join(chk["ids"]))

    def test_a_reaped_record_with_no_digest_path(self):
        rec = record(digest=None)
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]}),
                        "digests")
        self.assertIn("no digest", " ".join(chk["ids"]))

    def test_a_pre_cutover_digest_on_the_session_line_is_noted_not_failed(self):
        """The legacy layout predates the template rule: history, not a defect."""
        self.f.digest.write_text(f"# reaped — 2026-10-17T15:00:00Z\n\nunit\n\n"
                                 f"## facts\n\n- session: {SID}\n")
        report = self.f.mutate()
        chk = self.only(report, "digests", "NOTE")
        self.assertIn("legacy layout", chk["detail"])
        self.assertEqual(report["exit_code"], 0, "history must not block the gate")

    def test_a_post_cutover_digest_on_the_old_layout_fails(self):
        """The pin that stops the cutover becoming a grandfather clause."""
        self.f.digest.write_text(f"# reaped — 2026-10-18T09:00:00Z\n\nunit\n\n"
                                 f"## facts\n\n- session: {SID}\n")
        chk = self.only(self.f.mutate(), "digests")
        self.assertIn("post-cutover legacy", chk["detail"])

    def test_a_digest_naming_a_different_session_fails(self):
        other = "01a1beef-beef-7000-9000-0000000000dd"
        self.f.digest.write_text(f"# reaped — 2026-10-17T15:00:00Z\n\n- session: {other}\n")
        chk = self.only(self.f.mutate(), "digests")
        self.assertIn("1 wrong", chk["detail"])

    def test_a_digest_that_never_names_the_session(self):  # noqa: D401
        self.f.digest.write_text("# reaped — 2026-10-17T15:00:00Z\n\nsomeone's digest\n")
        chk = self.only(self.f.mutate(), "digests")
        self.assertIn("names no session anywhere", " ".join(chk["ids"]))


class TestDangling(AuditTestCase):
    def test_a_parent_that_is_not_in_the_store(self):
        rec = record(digest=str(self.f.digest),
                     parent_session_id="01a1beef-dead-7000-9000-000000000000")
        kids = {SID: rec, PARENT: self.f.children[PARENT]}
        chk = self.only(self.f.mutate(kids), "dangling")
        self.assertIn("is not in the store", " ".join(chk["ids"]))

    def test_a_record_that_is_neither_parented_nor_protected(self):
        rec = record(digest=str(self.f.digest), parent_session_id=None,
                     parent_name=None, protected=None, role=None)
        chk = self.only(self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]}),
                        "dangling")
        self.assertIn("no parent, not protected", " ".join(chk["ids"]))

    def test_a_reviewer_is_allowed_to_have_no_parent(self):
        rec = record(digest=str(self.f.digest), parent_session_id=None,
                     parent_name=None, role="reviewer")
        report = self.f.mutate({SID: rec, PARENT: self.f.children[PARENT]})
        got = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(got["dangling"], "PASS")


class TestEvidenceCensus(AuditTestCase):
    def test_counts_are_a_census_not_a_verdict(self):
        kids = dict(self.f.children)
        kids["01a1beef-beef-7000-9000-000000000002"] = record(
            **{"sessionId": "01a1beef-beef-7000-9000-000000000002",
               "name": "liar", "reaped_at": None, "digest": None,
               "parent_session_id": PARENT,
               "checks": {"status": "failed", "detail": "nothing held"}})
        kids["01a1beef-beef-7000-9000-000000000003"] = record(
            **{"sessionId": "01a1beef-beef-7000-9000-000000000003",
               "name": "unclaimed", "reaped_at": None,
               "digest": None, "parent_session_id": PARENT,
               "checks": None})
        kids["01a1beef-beef-7000-9000-000000000004"] = record(
            **{"sessionId": "01a1beef-beef-7000-9000-000000000004",
               "name": "quiet", "reported_at": None, "report": None,
               "reaped_at": None, "verdict": None, "digest": None,
               "parent_session_id": PARENT, "checks": None})
        rows = self.f.rows
        report = self.only(self.f.mutate(kids, rows), "evidence", "PASS")
        self.assertIn("verified 1", report["detail"])
        self.assertIn("failed 1", report["detail"])
        self.assertIn("unclaimed 1", report["detail"])
        self.assertIn("unreported 2", report["detail"])  # the parent + the quiet one
        self.assertIn("human decides", report["detail"])


class TestReadOnly(AuditTestCase):
    def test_the_audit_changes_no_byte_and_creates_no_file(self):
        before = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                  for p in sorted(self.f.dir.iterdir())}
        report = self.f.audit()
        after = {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                 for p in sorted(self.f.dir.iterdir())}
        self.assertEqual(before, after)
        got = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(got["read_only"], "PASS")

    def test_the_auditor_is_named_as_the_only_writer_of_nothing(self):
        # A repairer would have a write path. This test is the policy: no write
        # call is allowed to appear in the module at all.
        src = Path(A.__file__).read_text()
        for forbidden in ("write_text(", "unlink(", "os.remove", "shutil.",
                          "subprocess", "os.kill", "rename("):
            self.assertNotIn(forbidden, src, f"fleet_audit must not contain {forbidden}")


class TestCutovers(unittest.TestCase):
    """A cutover is an instant, not a grandfather clause.

    These pins exist so the two constants in fleet_audit.py cannot be moved to make a
    check go quiet: pushed into the future they would excuse a post-cutover regression,
    pulled into the past they would blame history for a rule that did not exist.
    """

    def test_neither_cutover_is_smuggled_into_the_future(self):
        """Five minutes is clock skew, not a budget for excusing the present.

        This was 3600 and it let a real forward push through: both constants sat 38
        minutes ahead of the code they described, so for 38 minutes a record written
        without a source was excused rather than caught (found by tundra, 16:32Z).
        """
        now = int(time.time())
        for name in ("DIGEST_LAYOUT_CUTOVER_ISO", "ENROLLED_SOURCE_CUTOVER_ISO"):
            stamp = A._epoch(getattr(A, name))
            self.assertLessEqual(stamp, now + 300,
                                 f"{name} sits more than 5 minutes in the future: a "
                                 f"timestamp ahead of now is never evidence about now")

    def test_neither_cutover_is_dragged_before_this_work(self):
        floor = A._epoch("2026-10-17T00:00:00Z")
        for name in ("DIGEST_LAYOUT_CUTOVER_ISO", "ENROLLED_SOURCE_CUTOVER_ISO"):
            self.assertGreaterEqual(
                A._epoch(getattr(A, name)), floor,
                f"{name} predates the template/field work: it would blame history "
                f"for a rule that did not exist when it was written")

    def test_a_post_cutover_record_without_a_source_still_fails(self):
        """The guard: even once the writer emits the field, old records only are excused."""
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(Path(d))
            rec = record(digest=str(f.digest))
            del rec["enrolled_source"]
            rec["spawned_at"] = A._epoch(A.ENROLLED_SOURCE_CUTOVER_ISO) + 60
            with mock.patch.object(A, "_writer_emits_enrolled_source",
                                   return_value=True):
                report = f.mutate({SID: rec, PARENT: f.children[PARENT]})
            chk = next(c for c in report["checks"] if c["check"] == "enrolled_source")
            self.assertEqual(chk["status"], "FAIL", chk["detail"])
            self.assertEqual(report["exit_code"], 1)

    def test_a_pre_cutover_record_without_a_source_is_only_noted(self):
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(Path(d))
            rec = record(digest=str(f.digest))
            del rec["enrolled_source"]
            rec["spawned_at"] = A._epoch(A.ENROLLED_SOURCE_CUTOVER_ISO) - 60
            with mock.patch.object(A, "_writer_emits_enrolled_source",
                                   return_value=True):
                report = f.mutate({SID: rec, PARENT: f.children[PARENT]})
            chk = next(c for c in report["checks"] if c["check"] == "enrolled_source")
            self.assertEqual(chk["status"], "NOTE", chk["detail"])
            self.assertEqual(report["exit_code"], 0)

    def test_a_filename_dated_receipt_falls_back_to_its_filename(self):
        """No header stamp: the date in the receipt's own name decides its age."""
        with tempfile.TemporaryDirectory() as d:
            f = Fixture(Path(d))
            old = f.digest.parent / "2026-10-16-legacy-receipt.md"
            old.write_text(f"# receipt\n\n- session: {SID}\n")
            f.children[SID]["digest"] = str(old)
            chk = self._digest(f.mutate())
            self.assertEqual(chk["status"], "NOTE", chk["detail"])
            self.assertIn("legacy layout", chk["detail"])

    @staticmethod
    def _digest(report):
        return next(c for c in report["checks"] if c["check"] == "digests")


class TestExitCodes(AuditTestCase):
    def test_fail_is_one(self):
        rows = []
        self.assertEqual(self.f.mutate(rows=rows)["exit_code"], 1)

    def test_unknown_only_is_two(self):
        self.assertEqual(A.exit_code_for([A.Check("x", "x", A.UNKNOWN, "")]), 2)
        self.assertEqual(A.exit_code_for([A.Check("x", "x", A.PASS, ""),
                                          A.Check("y", "y", A.UNKNOWN, "")]), 2)

    def test_note_alone_is_zero(self):
        self.assertEqual(A.exit_code_for([A.Check("x", "x", A.NOTE, ""),
                                          A.Check("y", "y", A.PASS, "")]), 0)

    def test_a_fail_beats_an_unknown(self):
        self.assertEqual(A.exit_code_for([A.Check("x", "x", A.UNKNOWN, ""),
                                          A.Check("y", "y", A.FAIL, "")]), 1)

    def test_pass_is_zero(self):
        self.assertEqual(self.f.audit()["exit_code"], 0)


class TestMalformedStore(AuditTestCase):
    def test_invalid_json_is_reported_not_raised(self):
        self.f.lineage.write_text("{ not json")
        report = self.f.audit()
        got = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(got["store"], "FAIL")
        self.assertEqual(report["exit_code"], 1)

    def test_a_missing_ledger_is_reported_not_raised(self):
        self.f.ledger.unlink()
        report = self.f.audit()
        got = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(got["store"], "FAIL")
        store = next(c for c in report["checks"] if c["check"] == "store")
        self.assertIn("missing", store["detail"] + " ".join(store["ids"]))


class TestCommandLine(AuditTestCase):
    def test_json_output_is_machine_readable(self):
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = A.main(["--json", "--lineage", str(self.f.lineage),
                           "--ledger", str(self.f.ledger)])
        payload = json.loads(buf.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["counts"]["records"], 2)
        self.assertIn("checks", payload)

    def test_human_output_names_the_offenders(self):
        import contextlib
        import io
        self.f.mutate(rows=[])
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = A.main(["--lineage", str(self.f.lineage),
                           "--ledger", str(self.f.ledger)])
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("[FAIL   ]", out)
        self.assertIn(SID, out)


if __name__ == "__main__":
    unittest.main()
