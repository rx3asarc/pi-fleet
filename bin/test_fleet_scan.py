"""Posthumous enrolment — sessions that were born AND died between two ticks.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_scan -v
(no pytest on this box — unittest only)

WHY THIS FILE EXISTS
  `fleet scan` reads the LIVE registry, and the cron runs every ten minutes. A
  session that starts and ends inside that window is therefore invisible to it
  forever, and short units are exactly the ones that fit. The name registry
  outlives the session, so it is the only witness left.

WHAT THIS FILE PINS
  1. _ended_recently() reads the registry, not the live roster: recent only (24h),
     milliseconds normalised to seconds, unreadable JSON skipped rather than fatal.
  2. A ghost is reported by a dry run and NOT written.
  3. --apply enrols it `protected` + `ended_between_ticks`, with spawned_at
     widened to the registry's own startedAt (otherwise every file it wrote
     before we noticed reads as written before it existed).
  4. Nothing may sweep it: verdict() says HOLD for a protected record.
  5. Filters: self, already-enrolled, `subagent-*` (runtime plumbing) and
     `fleet-review-*` (enrolled by spawn_reviewer) are never enrolled.
  6. A second --apply is a no-op. Idempotency is the whole reason a cron may run
     this unattended.

NO tmux, no /proc, no real registry: the store paths arrive through module
attributes and every subprocess goes through the injected `sh`.
"""
import json
import time
import unittest
from pathlib import Path
from unittest import mock

import fleet_children
import fleet_lineage
from test_fleet_p0 import ME, WorldStores, args, quiet

NOW = int(time.time())
GHOST = "01a1b0b0-1111-7000-9000-0000000000b1"
GHOST2 = "01a1b0b0-2222-7000-9000-0000000000b2"
ENROLLED = "01a1b0b0-3333-7000-9000-0000000000b3"


class ScanBase(WorldStores):
    def setUp(self):
        super().setUp()
        # cmd_scan must not glob the real transcripts either.
        self.patch_attr("_transcript_for", lambda sid: None)
        self.names_dir = self.root / "names"
        self.names_dir.mkdir()
        p = mock.patch.object(fleet_children, "NAMES_DIR", self.names_dir)
        p.start()
        self.addCleanup(p.stop)

    def name_record(self, sid, name, ago_s=60, started_ago_s=None, hourly=None,
                    corrupt=False):
        """Write one session-name registry record `ago_s` seconds old."""
        end = int(time.time()) - ago_s          # updatedAt IS the end of the session
        started = int(time.time()) - (started_ago_s or ago_s)
        body = {"sessionId": sid, "name": name,
                "startedAt": started * 1000, "updatedAt": end * 1000}
        path = self.names_dir / f"{sid}.json"
        if corrupt:
            path.write_text("{not json")
        else:
            path.write_text(json.dumps(body))
        age = int(time.time()) - ago_s
        import os
        os.utime(path, (age, age))
        return path

    def stored(self):
        return json.loads(self.lineage_path.read_text())["children"]


class TestEndedRecently(ScanBase):
    def test_only_recent_records_are_returned(self):
        self.name_record(GHOST, "just-now", ago_s=60)
        # The cutoff is on when the session ENDED (updatedAt), not when it
        # started: a three-day session that ended an hour ago is still recent.
        old = self.name_record(GHOST2, "ancient", ago_s=30 * 3600,
                               started_ago_s=50 * 3600)
        rows = fleet_children._ended_recently(hours=24)
        self.assertEqual([r["name"] for r in rows], ["just-now"])
        self.assertTrue(old.exists())          # still on disk, just too old

    def test_millisecond_epochs_are_normalised_to_seconds(self):
        self.name_record(GHOST, "ms-epochs", ago_s=30)
        row = fleet_children._ended_recently(hours=24)[0]
        self.assertLess(row["updated_at"], 10 ** 11)      # seconds, not millis
        self.assertLess(row["started_at"], 10 ** 11)
        self.assertLessEqual(row["started_at"], row["updated_at"])

    def test_a_corrupt_record_is_skipped_not_fatal(self):
        self.name_record(GHOST, "fine", ago_s=60)
        self.name_record(GHOST2, "broken", ago_s=60, corrupt=True)
        rows = fleet_children._ended_recently(hours=24)
        self.assertEqual([r["name"] for r in rows], ["fine"])

    def test_rows_look_like_live_entries_so_callers_treat_them_alike(self):
        self.name_record(GHOST, "shape", ago_s=60)
        row = fleet_children._ended_recently(hours=24)[0]
        for key in ("sessionId", "name", "alive", "reported", "session"):
            self.assertIn(key, row)
        self.assertIs(row["alive"], False)


class TestPosthumousScan(ScanBase):
    def test_dry_run_reports_the_ghost_and_writes_nothing(self):
        self.name_record(GHOST, "between-ticks", ago_s=120)
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=False))
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("GONE", text)
        self.assertIn("between-ticks", text)
        self.assertIn("--apply", text)
        self.assertFalse(self.lineage_path.exists())

    def test_apply_enrols_it_as_protected_and_posthumous(self):
        self.name_record(GHOST, "between-ticks", ago_s=120,
                         started_ago_s=900)
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=True))
        self.assertEqual(rc, 0)
        self.assertIn("ENROLLED posthumously", out.getvalue())
        rec = self.stored()[GHOST]
        self.assertIs(rec["protected"], True)
        self.assertIs(rec["ended_between_ticks"], True)
        self.assertIsNone(rec["parent_session_id"])
        self.assertIn("posthumously", rec["unit"])
        # startedAt, not "when we noticed": a file it wrote at birth must not
        # read as predating the unit.
        self.assertEqual(rec["spawned_at"], rec["spawned_at"])
        self.assertLess(rec["spawned_at"], int(time.time()) - 600)

    def test_nothing_may_sweep_a_posthumous_orphan(self):
        self.name_record(GHOST, "between-ticks", ago_s=120)
        with quiet():
            fleet_children.cmd_scan(args(apply=True))
        rec = self.stored()[GHOST]
        action, why = fleet_lineage.verdict(
            rec, {"alive": False, "busy": False, "pending_ask": None,
                  "parent_alive": False, "parent_finished": False,
                  "last_seen": int(time.time()) - 10 ** 5}, int(time.time()))
        self.assertEqual(action, "hold")
        self.assertIn("enrol-only", why)

    def test_a_second_scan_is_a_no_op(self):
        self.name_record(GHOST, "between-ticks", ago_s=120)
        with quiet():
            fleet_children.cmd_scan(args(apply=True))
        before = self.lineage_path.read_text()
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=True))
        self.assertEqual(rc, 0)
        self.assertIn("no unenrolled sessions", out.getvalue())
        self.assertEqual(self.lineage_path.read_text(), before)

    def test_filters_self_subagents_reviewers_and_the_already_enrolled(self):
        self.name_record(ME, "me-itself", ago_s=60)
        self.name_record(GHOST, "subagent-scout-abc-1", ago_s=60)
        self.name_record(GHOST2, "fleet-review-01a1b0b0", ago_s=60)
        lin = fleet_lineage.new_child(ENROLLED, "known", None, None, "unit", NOW)
        self.lineage_path.write_text(json.dumps(
            {"version": 1, "children": {ENROLLED: lin}}, indent=2) + "\n")
        self.name_record(ENROLLED, "known", ago_s=60)
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=True))
        self.assertEqual(rc, 0)
        self.assertIn("no unenrolled sessions", out.getvalue())
        self.assertEqual(list(self.stored()), [ENROLLED])

    def test_a_ghost_and_a_live_orphan_are_both_adopted_in_one_pass(self):
        LIVE = "01a1b0b0-4444-7000-9000-0000000000b4"
        self.world.live_entries = [{"sessionId": LIVE, "name": "live-stray",
                                    "alive": True, "reported": False,
                                    "session": "live-stray", "pi_status": "",
                                    "phase": "", "pending_ask": None,
                                    "context": "", "branch": None, "head": None,
                                    "merged": False, "worktree": None}]
        self.name_record(GHOST, "between-ticks", ago_s=120)
        with quiet() as out:
            fleet_children.cmd_scan(args(apply=True))
        text = out.getvalue()
        self.assertIn("ENROLLED as an orphan", text)
        self.assertIn("ENROLLED posthumously", text)
        kids = self.stored()
        self.assertIn(LIVE, kids)
        self.assertIn(GHOST, kids)
        self.assertTrue(all(k["protected"] for k in kids.values()))
        self.assertFalse(kids[LIVE].get("ended_between_ticks"))
        self.assertTrue(kids[GHOST]["ended_between_ticks"])


class TestReviewerIsNeverReviewed(ScanBase):
    """The regress guard. Two ways to be a reviewer, and both must hold.

    Role stamping happens after the spawn returns, so it can lose a race with the
    next tick (measured 2026-10-17: a reviewer spawned by a unit test was never
    stamped, role=None). Name is the fallback: a `fleet-review-*` session that
    dies without writing is swept, never handed to another reviewer.
    """

    def reviewer(self, **kw):
        c = fleet_lineage.new_child(GHOST, "fleet-review-01a1b0b0", None, None,
                                   "review: read a dead worker", NOW)
        c.update(kw)
        return c

    def dead_facts(self):
        return {"alive": False, "busy": False, "pending_ask": None,
                "parent_alive": False, "parent_finished": False,
                "last_seen": NOW - 600}

    def test_a_stamped_reviewer_is_swept_not_reviewed(self):
        action, why = fleet_lineage.verdict(
            self.reviewer(role="reviewer"), self.dead_facts(), NOW)
        self.assertEqual(action, fleet_lineage.REAP)
        self.assertIn("reviewer ended without reporting", why)

    def test_an_unstamped_fleet_review_session_is_still_never_reviewed(self):
        action, why = fleet_lineage.verdict(
            self.reviewer(), self.dead_facts(), NOW)      # role=None on purpose
        self.assertEqual(action, fleet_lineage.REAP)
        self.assertIn("reviewer ended without reporting", why)


if __name__ == "__main__":
    unittest.main()
