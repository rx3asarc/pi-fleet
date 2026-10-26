#!/usr/bin/env python3
"""U7 — ground truth is the world; the store is a cache of it.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_reconcile -v

THE BUG THIS PINS (measured 2026-10-26). A live pid was paired with a dead record's
sessionId because the tmux NAME had been reused by a new `pi`. That single mis-pairing
produced 17 records whose "identity" was `pid-<n>`, four names mapping to both a dead
and a live session, and 16 live sessions with no heartbeat. A manager reading that
store reaps unfinished work and keeps dead work.

The rules under test:
  * identity is sessionId; a pid is a hint that must be CONFIRMED, never used to
    re-identify a different session;
  * rows the world does not confirm are marked stale, NEVER deleted and NEVER
    re-identified — a cache may be corrected, not reaped.
"""
import unittest

import fleet_ground_truth as gt


class FakePi:
    def __init__(self, live=(), panes=()):
        self._live = list(live)
        self._panes = list(panes)
        self.live_calls = 0

    def live_records(self, live_only=True):
        self.live_calls += 1
        return list(self._live)

    def read_panes(self):
        return list(self._panes)


ALIVE = {100: True, 200: True, 300: False}


def alive(pid):
    return bool(ALIVE.get(int(pid), False))


class TestTruth(unittest.TestCase):
    def test_truth_is_keyed_by_sessionId_and_confirmed_by_panes(self):
        pi = FakePi(live=[{"sessionId": "s1", "name": "alder", "pid": 100}],
                    panes=[{"sessionId": "s1", "session": "alder", "pid": 100}])
        truth = gt.truth_from(pi, alive=alive)
        self.assertEqual(list(truth), ["s1"])
        self.assertEqual(truth["s1"]["tmux"], "alder")
        self.assertTrue(truth["s1"]["alive"])

    def test_a_pane_without_a_sessionId_invents_nothing(self):
        """Names must never become identities — that is the original bug."""
        pi = FakePi(live=[], panes=[{"session": "alder", "pid": 100}])
        self.assertEqual(gt.truth_from(pi, alive=alive), {})


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.pi = FakePi(live=[{"sessionId": "live1", "name": "alder", "pid": 100}],
                         panes=[{"sessionId": "live1", "session": "alder"}])
        self.truth = gt.truth_from(self.pi, alive=alive)

    def test_a_reused_pid_is_stale_and_never_re_identified(self):
        """pid 100 is alive, but it belongs to live1. The record must NOT inherit it."""
        store = {"pid-100": {"sessionId": "pid-100", "name": "alder", "pid": 100,
                             "protected": False}}
        rep = gt.reconcile(store, self.truth, alive=alive)
        self.assertEqual(len(rep["stale_pid"]), 1)
        row = rep["stale_pid"][0]
        self.assertEqual(row["now_owned_by"], "live1")
        self.assertIn("pid reuse", row["reason"])
        self.assertEqual(rep["stale_rows"], [])

    def test_a_dead_record_is_stale_not_deleted(self):
        store = {"dead-sid": {"sessionId": "dead-sid", "name": "dune", "pid": 300}}
        rep = gt.reconcile(store, self.truth, alive=alive)
        self.assertEqual([r["key"] for r in rep["stale_rows"]], ["dead-sid"])
        out, touched = gt.apply(store, rep, now=1234)
        self.assertEqual(touched, 1)
        self.assertIn("dead-sid", out, "a cache row is history; it is never deleted")
        self.assertTrue(out["dead-sid"]["stale"])
        self.assertEqual(out["dead-sid"]["stale_at"], 1234)
        self.assertEqual(out["dead-sid"]["name"], "dune", "content is preserved")

    def test_an_ended_record_never_yields_a_live_row(self):
        """The record says ended; a live pid with the same NAME must not revive it."""
        self.pi = FakePi(live=[{"sessionId": "live1", "name": "nordisk-ads", "pid": 100}])
        self.truth = gt.truth_from(self.pi, alive=alive)
        store = {"old-sid": {"sessionId": "old-sid", "name": "nordisk-ads",
                             "pid": 200, "phase": "ended"}}
        rep = gt.reconcile(store, self.truth, alive=alive)
        self.assertNotIn("old-sid", rep["live"])
        self.assertEqual([r["key"] for r in rep["stale_rows"]], ["old-sid"])

    def test_terminal_records_are_history_and_not_reported(self):
        store = {"closed": {"sessionId": "closed", "reaped_at": 5},
                 "dismissed": {"sessionId": "dismissed", "closed_at": 5}}
        rep = gt.reconcile(store, self.truth, alive=alive)
        self.assertEqual(rep["stale_rows"], [])
        self.assertEqual(rep["stale_pid"], [])

    def test_an_alive_pid_with_no_resolvable_id_is_not_evidence_of_death(self):
        store = {"weird": {"sessionId": "weird", "name": "w", "pid": 200}}
        rep = gt.reconcile(store, self.truth, alive=alive)
        self.assertEqual(rep["stale_rows"], [], "an alive pid blocks the stale marking")

    def test_a_live_session_with_no_record_is_reported_not_created(self):
        rep = gt.reconcile({}, self.truth, alive=alive)
        self.assertEqual([r["sessionId"] for r in rep["unrecorded_live"]], ["live1"])
        out, touched = gt.apply({}, rep)
        self.assertEqual((out, touched), ({}, 0), "reconcile must not enrol anything")

    def test_a_pid_mismatch_on_a_live_session_is_a_note_not_a_stale_mark(self):
        store = {"live1": {"sessionId": "live1", "name": "alder", "pid": 999}}
        rep = gt.reconcile(store, self.truth, alive=alive)
        self.assertIn("live1", rep["pid_owner"])
        self.assertEqual(rep["stale_rows"], [])
        self.assertEqual(rep["stale_pid"], [], "the session is alive; only the pid is history")


if __name__ == "__main__":
    unittest.main()
