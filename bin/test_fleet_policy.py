#!/usr/bin/env python3
"""U6 — the three policies that keep a fleet from converging on a confident answer.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_policy -v

The three share one shape: each removes a way for the fleet to look better than it is.

  intercom  -- shared visibility turns five independent workers into one opinion, and
               the rollup then prints no conflict BECAUSE everyone agreed, wrongly.
  protect   -- `protected` suspends the evidence gate, so it is the one flag that must
               require a human and leave a name behind.
  human     -- "human-attached" must be narrow and checkable, or every long-lived
               session quietly becomes a human whose silence counts as consent.
"""
import unittest

import fleet_policy as fp

LEAD = {"sessionId": "01a1lead", "parent_session_id": None}
WORKER = {"sessionId": "01a1w1", "parent_session_id": "01a1lead"}
PEER = {"sessionId": "01a1w2", "parent_session_id": "01a1lead"}
STRANGER = {"sessionId": "01a1s", "parent_session_id": None}


class TestIntercomDefaultDeny(unittest.TestCase):
    def test_worker_to_peer_is_refused_by_default(self):
        ok, why = fp.may_message(WORKER, PEER)
        self.assertFalse(ok)
        self.assertIn("worker-to-worker messaging is off by default", why)
        self.assertIn("consensus", why)

    def test_worker_to_lead_is_allowed_because_reporting_up_is_the_point(self):
        ok, why = fp.may_message(WORKER, LEAD)
        self.assertTrue(ok, why)

    def test_lead_to_its_own_worker_is_allowed(self):
        ok, why = fp.may_message(LEAD, WORKER)
        self.assertTrue(ok, why)

    def test_an_outside_session_may_message_a_worker_only_via_the_lead(self):
        ok, why = fp.may_message(STRANGER, WORKER)
        self.assertFalse(ok)
        self.assertIn("Message its lead", why)

    def test_an_explicit_opt_in_is_honoured(self):
        ok, why = fp.may_message(WORKER, {**PEER, "intercom_optin": True})
        self.assertTrue(ok, why)

    def test_a_plain_session_is_never_policed(self):
        self.assertTrue(fp.may_message(WORKER, STRANGER)[0])


class TestProtectionIsAudited(unittest.TestCase):
    def test_a_fleet_child_may_not_protect_anything(self):
        ok, why = fp.may_protect(WORKER, human_attached=True)
        self.assertFalse(ok)
        self.assertIn("may not protect itself", why)

    def test_a_non_human_session_may_not_protect(self):
        ok, why = fp.may_protect(STRANGER, human_attached=False)
        self.assertFalse(ok)
        self.assertIn("human-attached", why)

    def test_a_human_may_protect_and_the_act_leaves_a_name(self):
        self.assertTrue(fp.may_protect(STRANGER, human_attached=True)[0])
        rec = fp.stamp_protection({}, by="01a1human", why="do not sweep", now=7)
        self.assertTrue(rec["protected"])
        self.assertEqual(rec["protected_set_by"], "01a1human")
        self.assertEqual(rec["protected_set_at"], 7)
        self.assertEqual(rec["protected_reason"], "do not sweep")


class TestHumanAttached(unittest.TestCase):
    def test_a_fleet_child_is_never_human_attached(self):
        self.assertFalse(fp.human_attached({**STRANGER, "parent_session_id": "x"}))

    def test_a_human_session_is(self):
        self.assertTrue(fp.human_attached({"human_attached": True}))
        self.assertTrue(fp.human_attached({"tmux": "pi-root"}))
        self.assertTrue(fp.human_attached({"tmux": "tg-operator"}))

    def test_a_long_lived_worker_is_not_a_human_however_old(self):
        self.assertFalse(fp.human_attached({"tmux": "pi-fleet-1",
                                            "spawned_at": 1}))


if __name__ == "__main__":
    unittest.main()
