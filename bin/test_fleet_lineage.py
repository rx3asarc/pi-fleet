"""U11 — lineage, heartbeat, digest, cascade reap.

PURE. Fixtures only. No tmux, no git, no /proc, no clock, no filesystem.

THE RULE UNDER TEST
`verdict()` returns a PROPOSAL. The gate still decides whether anything dies.
Two properties therefore must hold, and both are asserted below:
  * `accept_silent` silences SILENCE only — it must never turn a `hold` (busy,
    pending ask, kept, already reaped) into a `reap`.
  * the digest is deterministic, so a repeated tick cannot rewrite history and
    "idempotent" is proved by bytes rather than asserted in a comment.
"""
import json
import unittest

from fleet_lineage import (
    REAP, HOLD, NOTIFY, new_child, mark_reported, keep_until_epoch,
    heartbeat_age, verdict, digest_md, ledger_line, DEFAULT_IDLE_AFTER_S,
)

NOW = 1792242000


def child(**kw):
    c = new_child("01a1dead-beef-7000-9000-000000000001", "globex-citations",
                  "01a10000-aaaa-7000-9000-0000000000aa", "globex-main",
                  "audit the citations appendix", NOW)
    c.update(kw)
    return c


def facts(**kw):
    base = {"alive": True, "busy": False, "pending_ask": None,
            "parent_alive": True, "parent_finished": False,
            "last_seen": NOW - 5}
    base.update(kw)
    return base


class TestRecord(unittest.TestCase):
    def test_session_id_is_the_primary_key(self):
        with self.assertRaises(ValueError):
            new_child("", "x", "p", "parent", "unit", NOW)

    def test_report_is_idempotent_and_last_wins(self):
        c = mark_reported(child(), "first", NOW)
        c = mark_reported(c, "second", NOW + 60)
        self.assertEqual(c["report"], "second")
        self.assertEqual(c["reported_at"], NOW + 60)
        self.assertIsNone(child()["reported_at"])

    def test_empty_report_is_recorded_as_absent_not_empty_string(self):
        self.assertIsNone(mark_reported(child(), "   ", NOW)["report"])

    def test_keep_until_is_hours_from_now(self):
        self.assertEqual(keep_until_epoch(NOW, 2), NOW + 7200)
        self.assertEqual(keep_until_epoch(NOW, 0.5), NOW + 1800)


class TestHeartbeat(unittest.TestCase):
    def test_age_is_now_minus_last_seen(self):
        self.assertEqual(heartbeat_age(NOW - 90, NOW), 90)

    def test_never_seen_is_none_not_zero(self):
        # None must stay None: treating "no heartbeat" as "just now" is how a
        # dead-but-unreaped child looks alive forever.
        self.assertIsNone(heartbeat_age(None, NOW))

    def test_clock_skew_never_yields_negative_age(self):
        self.assertEqual(heartbeat_age(NOW + 500, NOW), 0)


class TestVerdict(unittest.TestCase):
    def test_already_reaped_holds(self):
        self.assertEqual(verdict(child(reaped_at=NOW), facts(), NOW)[0], HOLD)

    def test_kept_child_holds_even_if_reported(self):
        c = mark_reported(child(keep_until=NOW + 3600), "done", NOW)
        action, why = verdict(c, facts(), NOW)
        self.assertEqual(action, HOLD)
        self.assertIn("kept", why)

    def test_keep_expires(self):
        c = mark_reported(child(keep_until=NOW - 1), "done", NOW)
        self.assertEqual(verdict(c, facts(), NOW)[0], REAP)

    def test_dead_and_reported_is_reaped(self):
        c = mark_reported(child(), "audited 41 citations", NOW - 60)
        action, why = verdict(c, facts(alive=False), NOW)
        self.assertEqual(action, REAP)
        self.assertIn("reported", why)

    def test_dead_and_silent_notifies_a_human(self):
        action, why = verdict(child(), facts(alive=False), NOW)
        self.assertEqual(action, NOTIFY)
        self.assertIn("never reported", why)

    def test_dead_and_silent_with_accept_silent_is_reaped(self):
        action, _ = verdict(child(), facts(alive=False), NOW, accept_silent=True)
        self.assertEqual(action, REAP)

    def test_reported_child_is_reaped_even_while_parent_runs(self):
        c = mark_reported(child(), "done", NOW - 10)
        self.assertEqual(verdict(c, facts(), NOW)[0], REAP)

    def test_pending_ask_holds_and_accept_silent_does_not_override(self):
        for silent in (False, True):
            action, why = verdict(child(), facts(pending_ask="ask_user_question"),
                                  NOW, accept_silent=silent)
            self.assertEqual(action, HOLD, silent)
            self.assertIn("human", why)

    def test_busy_holds_and_accept_silent_does_not_override(self):
        for silent in (False, True):
            self.assertEqual(
                verdict(child(), facts(busy=True), NOW, accept_silent=silent)[0],
                HOLD, silent)

    def test_parent_finished_reaps_only_a_cold_child(self):
        warm = verdict(child(), facts(parent_finished=True,
                                      last_seen=NOW - 10), NOW)
        cold = verdict(child(), facts(parent_finished=True,
                                      last_seen=NOW - DEFAULT_IDLE_AFTER_S - 1), NOW)
        self.assertEqual(warm[0], HOLD)
        self.assertEqual(cold[0], REAP)
        self.assertIn("parent finished", cold[1])

    def test_orphan_reaps_after_the_orphan_window(self):
        warm = verdict(child(), facts(parent_alive=False, last_seen=NOW - 60), NOW)
        cold = verdict(child(), facts(parent_alive=False, last_seen=NOW - 3600), NOW)
        self.assertEqual(warm[0], HOLD)
        self.assertEqual(cold[0], REAP)
        self.assertIn("parent gone", cold[1])

    def test_live_parent_and_idle_child_notifies_the_parent(self):
        action, why = verdict(child(), facts(last_seen=NOW - 3600), NOW)
        self.assertEqual(action, NOTIFY)
        self.assertIn("idle 3600s", why)

    def test_fresh_child_holds(self):
        action, why = verdict(child(), facts(last_seen=NOW - 5), NOW)
        self.assertEqual(action, HOLD)
        self.assertIn("active", why)

    def test_child_with_no_heartbeat_is_not_mistaken_for_fresh(self):
        # No record => "fresh" is a lie, but it is a lie in the SAFE direction:
        # hold, not reap. Assert the safe direction explicitly.
        self.assertEqual(verdict(child(), facts(last_seen=None), NOW)[0], HOLD)


class TestDigest(unittest.TestCase):
    def _c(self):
        c = mark_reported(child(), "audited 41 citations; 3 broken", NOW)
        c["verdict"] = REAP
        c["why"] = "reported its unit"
        return c

    def test_digest_is_deterministic(self):
        a = digest_md(self._c(), facts(), NOW, "2026-10-17T13:00:00Z",
                      touched=["docs/a.md"], commits=["abc123 fix"])
        b = digest_md(self._c(), facts(), NOW, "2026-10-17T13:00:00Z",
                      touched=["docs/a.md"], commits=["abc123 fix"])
        self.assertEqual(a, b)

    def test_digest_carries_resume_and_report(self):
        md = digest_md(self._c(), facts(), NOW, "2026-10-17T13:00:00Z")
        self.assertIn("pi --session 01a1dead-beef-7000-9000-000000000001", md)
        self.assertIn("audited 41 citations; 3 broken", md)
        self.assertIn("reported its unit", md)

    def test_never_reported_says_so(self):
        c = child()
        md = digest_md(c, facts(alive=False), NOW, "2026-10-17T13:00:00Z")
        self.assertIn("_never reported_", md)

    def test_tail_is_marked_as_not_evidence(self):
        md = digest_md(self._c(), facts(), NOW, "2026-10-17T13:00:00Z",
                       tail="I think I am done")
        self.assertIn("not evidence", md)
        self.assertIn("I think I am done", md)

    def test_ledger_line_is_stable_json(self):
        c = self._c()
        c["digest"] = "/root/handoffs/_reaped/x.md"
        a = ledger_line(c, REAP, NOW)
        b = ledger_line(c, REAP, NOW)
        self.assertEqual(a, b)
        row = json.loads(a)
        self.assertEqual(row["sessionId"], c["sessionId"])
        self.assertEqual(row["action"], REAP)


if __name__ == "__main__":
    unittest.main()
