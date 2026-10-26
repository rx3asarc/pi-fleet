#!/usr/bin/env python3
"""U4 — bounded plan acknowledgement.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_ack -v

WHY (2026-10-26, pi-moa review objection 2). I built gates on everything downstream of
the plan and none on the plan — the interface I had just called the quality multiplier.
The permitted failure is quiet: the plan mis-frames the task, every worker produces
impeccable evidence for the wrong thing, and the deterministic rollup faithfully reports
the wrong thing was accomplished. Nothing is UNSURE.

Two constraints collide and both are real: a fleet-wide mis-frame costs far more than
one human round-trip, AND blocking an autonomous lead on a human is exactly how
`acme-main` froze ~18h on 2026-10-15/16. The resolution under test is a BOUNDED
window whose expiry is recorded, never silently assumed.
"""
import unittest

import fleet_ack as ack

NOW = 1_000_000


class TestStates(unittest.TestCase):
    def test_a_record_with_no_plan_has_no_ack_state(self):
        self.assertEqual(ack.state({}, NOW), "none")

    def test_a_human_caller_opens_an_awaiting_window(self):
        rec = ack.arm({}, now=NOW, minutes=15, required=True)
        self.assertEqual(ack.state(rec, NOW), "awaiting")
        self.assertEqual(rec["plan_ack_deadline"], NOW + 900)

    def test_a_non_human_caller_never_waits(self):
        rec = ack.arm({}, now=NOW, required=False)
        self.assertEqual(ack.state(rec, NOW), "not-required")
        ok, why = ack.can_spawn(rec, NOW)
        self.assertTrue(ok, why)

    def test_an_ack_clears_the_flag_and_keeps_the_history(self):
        rec = ack.arm({}, now=NOW)
        rec = ack.ack(rec, now=NOW + 60, by="01a1human")
        self.assertEqual(ack.state(rec, NOW + 61), "acked")
        self.assertEqual(rec["plan_acked_by"], "01a1human")
        self.assertFalse(rec["plan_unacked"])

    def test_a_rejection_is_terminal_for_spawning(self):
        rec = ack.reject(ack.arm({}, now=NOW), now=NOW + 60, note="wrong metric")
        ok, why = ack.can_spawn(rec, NOW + 61)
        self.assertFalse(ok)
        self.assertIn("rejected", why)


class TestTheWindow(unittest.TestCase):
    def test_spawning_before_an_ack_is_refused_with_the_seconds_left(self):
        rec = ack.arm({}, now=NOW, minutes=15)
        ok, why = ack.can_spawn(rec, NOW + 60)
        self.assertFalse(ok)
        self.assertIn("840s left", why)
        self.assertIn("nobody has read", why)

    def test_expiry_allows_spawning_and_records_that_it_was_unacked(self):
        rec = ack.arm({}, now=NOW, minutes=15)
        ok, why = ack.can_spawn(rec, NOW + 901)
        self.assertTrue(ok, why)
        expired = ack.expire(rec, NOW + 901)
        self.assertTrue(expired["plan_unacked"])
        self.assertEqual(expired["plan_ack_state"], "expired")
        self.assertEqual(expired["plan_expired_at"], NOW + 901)

    def test_expire_does_nothing_to_a_window_still_open(self):
        rec = ack.arm({}, now=NOW, minutes=15)
        self.assertEqual(ack.expire(rec, NOW + 10), rec)

    def test_expire_does_nothing_to_an_acked_plan(self):
        rec = ack.ack(ack.arm({}, now=NOW), now=NOW + 5, by="human")
        self.assertEqual(ack.expire(rec, NOW + 9999), rec)
        self.assertFalse(rec.get("plan_unacked", False))


if __name__ == "__main__":
    unittest.main()
