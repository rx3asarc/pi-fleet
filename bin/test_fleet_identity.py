"""I14 — liveness is IDENTITY, not existence.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_identity -v
(no pytest on this box — unittest only)

THE BUG THIS CLOSES
  `alive` used to mean "a process with that pid is in state not-Z". Pids are
  recycled, so a dead worker's pid can be handed to an unrelated process
  (a browser, a build, another agent) and the record still reads alive -- and
  every verdict in the tree leans on `alive`. Recorded at enrolment now:
  (pid, start-time), where start-time is field 22 of /proc/<pid>/stat, measured
  in clock ticks since boot. That pair cannot be reused, so it is an identity.

THREE ANSWERS, NOT TWO
  alive    pid exists AND its start-time equals the recorded one
  gone     pid does not exist, OR exists with a DIFFERENT start-time (recycled),
           OR is a zombie/dead state
  unknown  no recorded start-time (a record enrolled before this field), or
           /proc could not be read (EACCES). Unknown resolves towards NOT
           REAPING, in every path -- measured, not assumed, in TestUnknownNeverKills.

NO real /proc except where the test says so (three cases read os.getpid(), which
is hermetic in the sense that matters: it is a process this test owns).
"""
import os
import time
import unittest
from unittest import mock

import fleet_children
import fleet_lineage
from test_fleet_p0 import (ME, PARENT_SID, PID_START, SID, WorldStores, args,
                           child, facts, good_deps, quiet)

ALIVE, GONE, UNKNOWN = (fleet_children.ALIVE, fleet_children.GONE,
                        fleet_children.UNKNOWN)
# Captured before any test patches it: the three real-/proc tests must call the
# real reader, not the fixture's fake.
_REAL_PROC = fleet_children._proc_identity


def ok(start=PID_START, state="S"):
    return ("ok", state, start)


class IdentityBase(WorldStores):
    def setUp(self):
        super().setUp()
        self.patch_attr("_transcript_for", lambda sid: None)

    def proc(self, *answers):
        """Patch the proc reader; each call pops the next answer, then repeats."""
        seq = list(answers)

        def reader(pid):
            return seq.pop(0) if len(seq) > 1 else seq[0]

        self.patch_attr("_proc_identity", reader)
        return reader

    def rec(self, **kw):
        c = fleet_lineage.new_child(SID, "engrane-citations", PARENT_SID,
                                    "engrane-main", "audit appendix", int(time.time()))
        c.update(kw)
        return c


class TestProcIdentity(IdentityBase):
    def test_a_real_process_yields_its_own_start_time(self):
        pid = os.getpid()
        kind, state, start = _REAL_PROC(pid)
        self.assertEqual(kind, "ok")
        self.assertNotIn(state, ("Z", "X"))
        self.assertIsInstance(start, int)
        self.assertGreater(start, 0)
        # The same pid asked twice is the same identity: start-time is stable.
        self.assertEqual(_REAL_PROC(pid)[2], start)

    def test_a_missing_pid_is_missing_not_denied(self):
        kind, _state, _start = _REAL_PROC(999999999)
        self.assertEqual(kind, "missing")

    def test_an_unreadable_proc_entry_is_denied_not_dead(self):
        # EACCES and friends: a question we could not ask is not the answer "gone".
        boom = PermissionError(13, "Permission denied")
        with mock.patch.object(fleet_children.Path, "read_text", side_effect=boom):
            kind, _state, _start = _REAL_PROC(os.getpid())
        self.assertEqual(kind, "denied")


class TestIdentityState(IdentityBase):
    def test_same_pid_and_same_start_time_is_alive(self):
        self.proc(ok())
        c = self.rec(pid=4242, pid_start=PID_START)
        self.assertEqual(fleet_children._identity_state(c, 4242), ALIVE)

    def test_a_real_live_process_still_reads_alive(self):
        # Regression guard: the ordinary case must not be collateral damage.
        self.patch_attr("_proc_identity", _REAL_PROC)   # the real reader, no fake
        pid = os.getpid()
        start = _REAL_PROC(pid)[2]
        c = self.rec(pid=pid, pid_start=start)
        self.assertEqual(fleet_children._identity_state(c, pid), ALIVE)

    def test_same_pid_with_a_different_start_time_is_gone(self):
        # THE RECYCLED CASE. Constructed by patching the reader, never by racing
        # a real process: a test that needs a pid to be reused is a flaky test.
        self.proc(ok(start=PID_START + 1))
        c = self.rec(pid=4242, pid_start=PID_START)
        self.assertEqual(fleet_children._identity_state(c, 4242), GONE)

    def test_a_zombie_reads_gone(self):
        self.proc(ok(state="Z"))
        c = self.rec(pid=4242, pid_start=PID_START)
        self.assertEqual(fleet_children._identity_state(c, 4242), GONE)

    def test_a_missing_recorded_start_time_is_unknown(self):
        self.proc(ok())
        c = self.rec(pid=4242)                    # enrolled before identity existed
        self.assertEqual(fleet_children._identity_state(c, 4242), UNKNOWN)

    def test_an_unknown_pid_is_unknown_not_gone(self):
        self.proc(("denied", None, None))
        c = self.rec(pid=4242, pid_start=PID_START)
        self.assertEqual(fleet_children._identity_state(c, 4242), UNKNOWN)

    def test_a_missing_process_is_gone_even_with_a_recorded_identity(self):
        self.proc(("missing", None, None))
        c = self.rec(pid=4242, pid_start=PID_START)
        self.assertEqual(fleet_children._identity_state(c, 4242), GONE)


class TestUnknownNeverKills(IdentityBase):
    """UNKNOWN resolves to HOLD in EVERY path, not just the friendly one.

    The auditor's point: unreported, unclaimed records are the common case, so a
    single guard in the default path would not be enough.
    """

    def unknown_child(self, **kw):
        # Denied, not merely "no start recorded": a readable process is LEARNED
        # (backfilled) and then legitimately alive. The dangerous UNKNOWN is the
        # one that stays unknown.
        self.proc(("denied", None, None))
        return self.rec(pid=4242, **kw)

    def cascade(self, c, **kw):
        self.write_children({c["sessionId"]: c})
        entry = {"sessionId": c["sessionId"], "name": c.get("name"),
                 "alive": True, "pid": 4242, "reported": bool(c.get("reported_at")),
                 "session": "dune-fork-1", "pi_status": "", "phase": "",
                 "pending_ask": None, "context": "", "branch": None, "head": None,
                 "merged": False, "worktree": None}
        self.world.live_entries = [entry]
        self.world.records = [{"sessionId": c["sessionId"],
                               "updatedAt": (int(time.time()) - 120) * 1000}]
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(**{"apply": True, **kw}))
        return closed, out.getvalue()

    def test_unknown_is_never_reaped_in_the_default_path(self):
        closed, text = self.cascade(self.unknown_child())
        self.assertEqual(closed, [])
        self.assertIn("identity unknown", text)
        self.assertIsNone(self._reload()["reaped_at"])

    def test_unknown_is_not_reaped_even_with_accept_silent(self):
        closed, text = self.cascade(self.unknown_child(), accept_silent=True)
        self.assertEqual(closed, [])
        self.assertIsNone(self._reload()["reaped_at"])

    def test_unknown_is_not_reaped_even_when_the_grace_expired(self):
        c = self.unknown_child(grace_until=int(time.time()) - 1)
        closed, text = self.cascade(c)
        self.assertEqual(closed, [])
        self.assertIsNone(self._reload()["reaped_at"])

    def test_unknown_is_not_reaped_even_when_it_reported(self):
        c = fleet_lineage.mark_reported(self.unknown_child(), "done", int(time.time()) - 60)
        closed, text = self.cascade(c)
        self.assertEqual(closed, [])
        self.assertIsNone(self._reload()["reaped_at"])

    def test_verdict_says_hold_and_says_why(self):
        action, why = fleet_lineage.verdict(
            self.unknown_child(), {"alive": False, "alive_state": "unknown",
                                   "busy": False, "pending_ask": None,
                                   "parent_alive": True, "parent_finished": False,
                                   "last_seen": int(time.time()) - 60},
            int(time.time()))
        self.assertEqual(action, fleet_lineage.HOLD)
        self.assertIn("identity unknown", why)


class TestIdentityIsRecorded(IdentityBase):
    def test_adopt_records_pid_and_start_time(self):
        e = {"sessionId": SID, "name": "engrane-citations", "alive": True,
             "pid": 4242, "reported": False, "session": "dune-fork-1",
             "pi_status": "", "phase": "", "pending_ask": None, "context": "",
             "branch": None, "head": None, "merged": False, "worktree": None,
             "transcript": None}
        self.world.live_entries = [e]
        self.proc(ok())
        with quiet():
            fleet_children.cmd_adopt(args(name="engrane-citations",
                                          unit="audit appendix", parent=None,
                                          done_criteria=None))
        rec = self._reload()
        self.assertEqual(rec["pid"], 4242)
        self.assertEqual(rec["pid_start"], PID_START)

    def test_a_legacy_record_seen_alive_is_backfilled_once(self):
        # Learn-on-sight: a record enrolled before identity existed, whose
        # process we can still see, stops being UNKNOWN without anyone guessing.
        c = self.rec(pid=4242)                 # no pid_start
        self.proc(ok())                        # readable, so it can be learned
        TestUnknownNeverKills.cascade(self, c)
        self.assertEqual(self._reload()["pid_start"], PID_START)
        # ... and with the identity learned it is no longer UNKNOWN, so the
        # ordinary verdict tree applies again (a live parent reaps it at once).
        facts = {"alive": True, "alive_state": "alive", "busy": False,
                 "pending_ask": None, "parent_alive": True, "parent_finished": False,
                 "last_seen": int(time.time()) - 60}
        rec = self._reload()
        action, why = fleet_lineage.verdict(rec, facts, int(time.time()))
        self.assertNotEqual(why, "identity unknown (no recorded pid start-time, "
                                "or /proc was unreadable) — not reaping",
                            "the record is still unknown after learning its pid")


if __name__ == "__main__":
    unittest.main()
