"""F1 / F3 / F7 — the three integrity faults, and the LAYER each is fixed at.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_integrity -v
(no pytest on this box — unittest only)

WHY THE ORDER OF THESE CLASSES MATTERS
  F1 and F3 pull in opposite directions if they are fixed at the same place:
    F1  a truncated store must be REFUSED at load time (store layer)
    F3  one malformed record must be SKIPPED at per-record time (record layer)
  Drop bad records to satisfy F3 and a store that parses to zero records looks
  like a tidy fleet -- F1 goes quiet exactly when it is most needed. So the two
  live in different classes here, and TestLayers pins the separation: skipping
  never removes a record, and a store with zero children is still refused.

NO tmux, no real /proc, no production store: every path is a temp file.
"""
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

import fleet_children
import fleet_lineage
from test_fleet_p0 import PID_START, SID, WorldStores, child, facts, quiet


class StoreBase(WorldStores):
    def populate(self, n=3):
        kids = {}
        for i in range(n):
            sid = f"01a1f100-0000-7000-9000-00000000000{i}"
            kids[sid] = fleet_lineage.new_child(
                sid, f"worker-{i}", None, None, f"unit {i}", 1792242000)
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": kids}, indent=2) + "\n")
        return kids


# ── F1: the store layer ─────────────────────────────────────────────────────
class TestLoadRefuses(StoreBase):
    """A truncated store used to load as an EMPTY one, and the next write then
    persisted that emptiness -- one bad read erased the fleet."""

    def test_a_truncated_store_is_refused(self):
        self.populate(3)
        raw = self.lineage_path.read_text()
        self.lineage_path.write_text(raw[: len(raw) // 2])      # truncation
        with self.assertRaises(SystemExit) as cm:
            fleet_children.load_lineage()
        self.assertIn("not valid JSON", str(cm.exception))

    def test_an_empty_file_is_refused(self):
        self.populate(3)
        self.lineage_path.write_text("")
        with self.assertRaises(SystemExit) as cm:
            fleet_children.load_lineage()
        self.assertIn("EMPTY", str(cm.exception))

    def test_zero_children_is_refused_unless_the_emptiness_is_intended(self):
        self.populate(3)
        self.lineage_path.write_text('{"version": 1, "children": {}}\n')
        with self.assertRaises(SystemExit):
            fleet_children.load_lineage()
        self.assertEqual(fleet_children.load_lineage(allow_empty=True)["children"], {})

    def test_a_missing_store_is_a_new_fleet_not_a_failure(self):
        self.assertFalse(self.lineage_path.exists())
        self.assertEqual(fleet_children.load_lineage()["children"], {})

    def test_a_valid_store_still_loads(self):
        kids = self.populate(3)
        self.assertEqual(list(fleet_children.load_lineage()["children"]), list(kids))

    def test_a_write_that_would_drop_records_is_refused(self):
        self.populate(3)
        with self.assertRaises(SystemExit) as cm:
            fleet_children.save_lineage({"version": 1, "children": {}})
        self.assertIn("refusing to write 0 records over 3", str(cm.exception))
        self.assertEqual(len(fleet_children.load_lineage()["children"]), 3)

    def test_a_deliberate_shrink_is_allowed_when_asked_for(self):
        self.populate(3)
        fleet_children.save_lineage({"version": 1, "children": {}},
                                    allow_shrink=True)
        self.assertEqual(fleet_children.load_lineage(allow_empty=True)["children"], {})


# ── F3: the record layer ────────────────────────────────────────────────────
class TestOneBadRecordDoesNotStopTheTick(StoreBase):
    def tick(self):
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(SimpleNamespace(
                apply=True, accept_silent=False, idle_after_min=30))
        return closed, out.getvalue()

    def malformed_pair(self):
        """Two good children plus two records a tick cannot process."""
        kids = self.populate(2)
        good = list(kids)
        kids["not-a-record"] = "i am a string, not a dict"
        bad = fleet_lineage.new_child("01a1f100-ffff-7000-9000-0000000000ff",
                                      "no-sid", None, None, "unit", 1792242000)
        bad.pop("sessionId", None)
        kids["01a1f100-eeee-7000-9000-0000000000ee"] = bad
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": kids}, indent=2) + "\n")
        return good

    def test_the_good_records_are_still_processed(self):
        good = self.malformed_pair()
        self.world.live_entries = []
        closed, text = self.tick()
        # They never reported, so the right outcome is a DECIDE, not a kill.
        # What matters is that they were PROCESSED at all: each is named in the
        # report with a verdict, despite the junk sitting beside it.
        for sid in good:
            self.assertIn(sid[:8], text, "a malformed record stopped a good one")
        self.assertIn("SKIP", text)
        self.assertIn("not processed, not removed", text)

    def test_a_skipped_record_is_still_in_the_store(self):
        self.malformed_pair()
        self.world.live_entries = []
        self.tick()
        kids = json.loads(self.lineage_path.read_text())["children"]
        self.assertIn("not-a-record", kids)
        self.assertIn("01a1f100-eeee-7000-9000-0000000000ee", kids)


# ── the layer rule: F1 and F3 must not cancel each other ────────────────────
class TestLayers(StoreBase):
    def test_skipping_never_turns_a_truncated_store_into_a_quiet_no_op(self):
        """The trap in one test: F3 must not be able to satisfy F1.

        A store that PARSES but is empty is refused even though every record in
        it would have been skippable -- the store layer does not consult the
        record layer about whether the fleet is whole.
        """
        self.lineage_path.write_text('{"version": 1, "children": {}}\n')
        with self.assertRaises(SystemExit):
            fleet_children._cascade(SimpleNamespace(
                apply=True, accept_silent=False, idle_after_min=30))

    def test_a_malformed_record_does_not_change_the_store_count(self):
        # F3 skips in memory; nothing about it reaches save_lineage.
        kids = self.populate(2)
        kids["junk"] = 42
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": kids}, indent=2) + "\n")
        before = len(fleet_children.load_lineage()["children"])
        self.world.live_entries = []
        with quiet():
            fleet_children._cascade(SimpleNamespace(
                apply=True, accept_silent=False, idle_after_min=30))
        self.assertEqual(len(fleet_children.load_lineage()["children"]), before)


# ── F7: the kill path ───────────────────────────────────────────────────────
class TestKillNeedsIdentity(StoreBase):
    def test_a_single_pane_owned_by_a_stranger_is_refused(self):
        self.patch_attr("_owner_pid", lambda sid: (999, "dune-fork-1"))
        self.patch_attr("sh", lambda argv, **kw: (
            (0, "0.0 222\n", "") if argv[:2] == ["tmux", "list-panes"]
            else (0, "", "")))
        ok, how = fleet_children.safe_close(SID, "dune-fork-1", wait_s=0)
        self.assertFalse(ok)
        self.assertIn("is not the owner pid", how)

    def test_the_matching_pane_is_still_closed(self):
        self.patch_attr("_owner_pid", lambda sid: (222, "dune-fork-1"))
        calls = []

        def fake_sh(argv, **kw):
            calls.append(list(argv))
            if argv[:2] == ["tmux", "list-panes"]:
                return 0, "0.0 222\n", ""
            return 0, "", ""

        self.patch_attr("sh", fake_sh)
        self.patch_attr("_proc_state", lambda pid: "gone")
        ok, how = fleet_children.safe_close(SID, "dune-fork-1", wait_s=0)
        self.assertTrue(ok, how)
        # One matching pane: the documented single-pane path is pi-tmux --close.
        self.assertIn(["pi-tmux", "--close", "dune-fork-1"], calls)


class TestD1MissingPidIsUnknown(StoreBase):
    """Absence of a recorded pid is absence of EVIDENCE, not evidence of death.

    Live proof, 2026-10-17: probe fleet-probe-160234 reported with a verified
    claim, was reaped as a dead process, and was STILL RUNNING. This class pins
    the rule; the probe pins the consequence ("session gone").
    """

    def test_no_recorded_pid_is_unknown_not_gone(self):
        rec = child()
        rec.pop("pid", None)
        rec.pop("pid_start", None)
        self.assertEqual(fleet_children._identity_state(rec, None),
                         fleet_children.UNKNOWN)

    def test_a_record_with_no_pid_that_is_live_in_the_registry_learns_it(self):
        rec = dict(fleet_lineage.mark_reported(child(), "done", 1792242000 - 60),
                   pid=None, pid_start=None)
        self.lineage_path.write_text(json.dumps(
            {"version": 1, "children": {SID: rec}}, indent=2) + "\n")
        self.world.live_entries = [{"sessionId": SID, "name": "globex-citations",
                                   "alive": True, "pid": 4242, "reported": True,
                                   "session": "dune-fork-1", "pi_status": "",
                                   "phase": "", "pending_ask": None}]
        self.patch_attr("_live_pid", lambda sid: 4242)     # entries() has no pid
        self.patch_verify("verified")
        closed = self.patch_close()
        with quiet():
            fleet_children._cascade(SimpleNamespace(
                apply=True, accept_silent=False, idle_after_min=30))
        saved = json.loads(self.lineage_path.read_text())["children"][SID]
        self.assertEqual(saved["pid_start"], PID_START, "identity was not learned")
        self.assertIsNotNone(saved["reaped_at"])
        self.assertEqual(closed, [SID], "a live session was reaped without a kill")


class TestF2EventKeyedLedger(StoreBase):
    """One death is one row, even when the retry's clock has moved."""

    def rows(self):
        return [json.loads(l) for l in self.ledger.read_text().splitlines()]

    def test_a_retry_at_a_different_second_is_the_same_event(self):
        rec = fleet_lineage.mark_reported(child(), "done", 1792242000 - 60)
        first = fleet_lineage.ledger_line(dict(rec, reaped_at=1792242000), "reap",
                                          1792242000)
        second = fleet_lineage.ledger_line(dict(rec, reaped_at=1792242000), "reap",
                                           1792242005)          # +5s retry
        self.assertNotEqual(first, second)                       # byte-different
        self.assertTrue(fleet_children._append_ledger(first))
        self.assertFalse(fleet_children._append_ledger(second),
                         "the same death was recorded twice")
        self.assertEqual([r["action"] for r in self.rows()], ["reap"])

    def test_a_different_event_still_lands(self):
        rec = fleet_lineage.mark_reported(child(), "done", 1792242000 - 60)
        a = fleet_lineage.ledger_line(dict(rec, reaped_at=1792242000), "reap", 1792242000)
        b = fleet_lineage.ledger_line(dict(rec, reaped_at=1792242100), "reap", 1792242100)
        self.assertTrue(fleet_children._append_ledger(a))
        self.assertTrue(fleet_children._append_ledger(b))
        self.assertEqual(len(self.rows()), 2)

    def test_reap_failed_is_its_own_event(self):
        rec = fleet_lineage.mark_reported(child(), "done", 1792242000 - 60)
        a = fleet_lineage.ledger_line(dict(rec, reaped_at=1792242000), "reap", 1792242000)
        b = fleet_lineage.ledger_line(dict(rec, reaped_at=1792242000), "reap-failed",
                                      1792242000)
        self.assertTrue(fleet_children._append_ledger(a))
        self.assertTrue(fleet_children._append_ledger(b))
        self.assertEqual([r["action"] for r in self.rows()], ["reap", "reap-failed"])


class TestDigestNamesTheSession(StoreBase):
    """A receipt exists to answer "which session was this", so line 1 says it.

    Receipts written before 2026-10-17 name the session on line 6 and are never
    rewritten -- history stays as written -- so the template note says so in the
    digest's own facts.
    """

    def digest(self, **kw):
        rec = fleet_lineage.new_child(SID, "globex-citations", None, None,
                                      "audit appendix", 1792242000, **kw)
        return fleet_lineage.digest_md(rec, {"alive": False}, 1792242000,
                                       "2026-10-17T13:00:00Z")

    def test_line_one_carries_the_session_id(self):
        self.assertIn(SID, self.digest().splitlines()[0])

    def test_the_template_note_explains_the_older_receipts(self):
        self.assertIn("receipts before 2026-10-17", self.digest())

    def test_enrolled_source_is_written_when_the_door_names_itself(self):
        self.assertIn("- enrolled via: scan", self.digest(enrolled_source="scan"))

    def test_an_unrecorded_door_says_so_instead_of_guessing(self):
        self.assertIn("not recorded", self.digest())


class TestEnrolledSource(StoreBase):
    def test_new_child_defaults_to_absent_not_invented(self):
        rec = fleet_lineage.new_child(SID, "x", None, None, "u", 1792242000)
        self.assertIn("enrolled_source", rec)
        self.assertIsNone(rec["enrolled_source"])

    def test_scan_stamps_the_door_that_enrolled(self):
        self.patch_attr("_transcript_for", lambda sid: None)
        self.name_record = None
        with quiet():
            fleet_children.cmd_scan(SimpleNamespace(apply=True))
        # no live sessions in this fixture: nothing to enrol, nothing invented
        self.assertFalse(self.lineage_path.exists())


if __name__ == "__main__":
    unittest.main()
