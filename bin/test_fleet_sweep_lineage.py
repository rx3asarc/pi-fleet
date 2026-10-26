#!/usr/bin/env python3
"""Tests for `fleet sweep-lineage` — the verb that closes the lineage books.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_sweep_lineage -v

WHY THIS FILE EXISTS (2026-10-26). `sweep-lineage` closed 379 records against the
live store the day it was written, and it shipped with no test of its own. The
SOP's rule is that a verb which writes a 500-record store earns tests before it
earns a cron slot. These are those tests.

THE ONE RULE EVERY CASE DEFENDS: a record whose session is alive is NEVER closed,
and a record whose pid is alive is never closed even when its sessionId cannot be
resolved. A `pid-<n>` key is not a session id -- on this box 17 records were
written with key = sessionId = `pid-<n>` and pid = None, and the same sessions
also had a proper sessionId-keyed record. Treating those keys as dead would have
closed the books on a live session.
"""
import json
import unittest
from types import SimpleNamespace
from unittest import mock

import fleet_children
from test_fleet_children import TempStores, good_deps, World


class SweepCase(TempStores):
    def setUp(self):
        super().setUp()
        self.world = World()
        self.patch_deps(good_deps(entries=self.world.entries,
                                  find=self.world.find,
                                  sh=self.world.sh,
                                  owner_tmux_of=self.world.owner_tmux_of))
        # alive pids are whatever the test says; nothing here touches /proc
        self.alive_pids = set()
        p = mock.patch.object(fleet_children.pi_sources, "_alive",
                              lambda pid: int(pid) in self.alive_pids)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(fleet_children.pi_sources, "live_records",
                              lambda live_only=True: self.live)
        p.start()
        self.addCleanup(p.stop)
        self.live = []                      # rows pi-live would return
        self.store = {"version": 1, "children": {}}
        self.write_store()

    # -- store helpers ------------------------------------------------------
    def write_store(self):
        self.lineage_path.write_text(json.dumps(self.store, indent=2))

    def load(self):
        return json.loads(self.lineage_path.read_text())["children"]

    def record(self, key, **fields):
        rec = {"sessionId": key, "name": fields.pop("name", "w"),
               "protected": False, "reaped_at": None, "closed_at": None,
               "unit": "", "parent_session_id": None, "pid": None}
        rec.update(fields)
        self.store["children"][key] = rec
        self.write_store()

    def sweep(self, apply=False, note=None):
        return fleet_children.cmd_sweep_lineage(
            SimpleNamespace(apply=apply, note=note))


class TestNothingIsDestroyed(SweepCase):
    def test_dry_run_writes_nothing(self):
        self.record("a" * 8 + "-0000-0000-0000-000000000000")
        before = self.lineage_path.read_text()
        self.sweep(apply=False)
        self.assertEqual(self.lineage_path.read_text(), before)

    def test_a_live_session_is_never_closed(self):
        sid = "01a16e1a-ed51-7205-a0c3-04cc9ce99c2b"
        self.record(sid, name="opal")
        self.live = [{"sessionId": sid, "pid": 111}]
        self.sweep(apply=True)
        self.assertIsNone(self.load()[sid]["closed_at"], "a live session was closed")

    def test_a_live_pid_protects_a_record_whose_sid_is_unknown(self):
        """No resolvable sessionId, but the pid is alive: that is not evidence of death."""
        key = "«REDACTED:uuid-credential»"
        self.record(key, pid=4242)
        self.alive_pids = {4242}
        self.sweep(apply=True)
        self.assertIsNone(self.load()[key]["closed_at"])

    def test_a_terminal_record_is_not_re_judged(self):
        sid = "01a16e1a-1111-7205-a0c3-04cc9ce99c2b"
        self.record(sid, closed_at=123, closed_by="dismiss", verdict="dismissed")
        self.sweep(apply=True)
        self.assertEqual(self.load()[sid]["closed_by"], "dismiss",
                         "a terminal record must keep its original disposition")


class TestClosingAndRepair(SweepCase):
    def test_a_dead_record_is_closed_with_a_disposition(self):
        sid = "01a16e1a-2222-7205-a0c3-04cc9ce99c2b"
        self.record(sid, name="gone-worker", pid=999999)
        self.sweep(apply=True, note="nobody claimed it")
        rec = self.load()[sid]
        self.assertEqual(rec["closed_by"], "sweep")
        self.assertEqual(rec["verdict"], "dismissed")
        self.assertEqual(rec["why"], "nobody claimed it")
        self.assertIsNotNone(rec["closed_at"])

    def test_a_phantom_pid_key_merges_into_the_real_session_record(self):
        """`pid-<n>` is a key an old enrolment path invented, not an identity."""
        sid = "01a172c6-a523-73dc-9907-e076c651cc8e"
        self.record(sid, name="globex-ui-lead", unit="brief: ui lead")
        self.record("pid-1461108", name="globex-ui-lead",
                    sessionId="pid-1461108", unit="")
        self.live = [{"sessionId": sid, "pid": 1461108}]
        self.alive_pids = {1461108}
        self.sweep(apply=True)
        kids = self.load()
        self.assertNotIn("pid-1461108", kids, "the phantom key must be gone")
        self.assertIn(sid, kids)
        self.assertEqual(kids[sid]["unit"], "brief: ui lead")

    def test_an_adopted_orphan_is_made_sweepable(self):
        sid = "01a16e1a-3333-7205-a0c3-04cc9ce99c2b"
        self.record(sid, name="child", protected=True,
                    parent_session_id="01a16d23-0000-7205-a0c3-04cc9ce99c2b",
                    parent_name="coordinator")
        self.live = [{"sessionId": sid, "pid": 222}]
        self.sweep(apply=True)
        self.assertFalse(self.load()[sid]["protected"],
                         "a record with a parent must not stay 'never swept'")

    def test_a_protected_orphan_with_no_parent_keeps_its_protection(self):
        sid = "01a16e1a-4444-7205-a0c3-04cc9ce99c2b"
        self.record(sid, name="stranger", protected=True)
        self.live = [{"sessionId": sid, "pid": 333}]
        self.sweep(apply=True)
        self.assertTrue(self.load()[sid]["protected"])


class TestAdoptClearsProtection(SweepCase):
    def test_adopt_with_a_parent_unprotects(self):
        """`fleet adopt` used to enrol a child into something nothing could reap."""
        sid = "01a16e1a-5555-7205-a0c3-04cc9ce99c2b"
        self.record(sid, name="orphan", protected=True)
        self.world.live_entries = [{"sessionId": sid, "name": "orphan", "pid": 555}]
        self.world.owner[sid] = "tmux-orphan"
        fleet_children.cmd_adopt(SimpleNamespace(
            name="orphan", parent="none", unit="a real unit", done_criteria=None))
        self.assertTrue(self.load()[sid]["protected"],
                        "--parent none is still an orphan")

        fleet_children.cmd_adopt(SimpleNamespace(
            name="orphan", parent="none", unit="a real unit", done_criteria=None))
        # now with a parent
        self.record(sid, name="orphan", protected=True)
        fleet_children.cmd_adopt(SimpleNamespace(
            name="orphan", parent="coordinator", unit="a real unit",
            done_criteria=None))
        self.assertFalse(self.load()[sid]["protected"],
                         "adopting must clear protection")


if __name__ == "__main__":
    unittest.main()
