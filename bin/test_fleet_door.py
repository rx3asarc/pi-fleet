#!/usr/bin/env python3
"""U3 — the door: `fleet for "<task>"`.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_door -v

WHY (2026-10-26). Every refusal here was prose in the SOP until today, and prose is
what the 23-session sprawl grew through. A cap that lives in a document is not a cap;
a name collision that is "handled" by an operator remembering is not handled.

The four refusals, each cheap and each BEFORE anything is created:
  * cap      — 5 workers per domain, and the lead does not count toward it (counting
               the coordinator would make a full domain unopenable exactly when it is
               full);
  * depth    — a fleet child may not open a fleet;
  * name     — a name is an identity, never a slot;
  * kind     — task-kind work is verified by a command or a file, never by "merged".
"""
import unittest

import fleet_door as fd


def names(*n):
    return list(n)


class TestDomain(unittest.TestCase):
    def test_explicit_domain_wins_and_is_slugged(self):
        self.assertEqual(fd.domain_of("anything at all", "U10 Geometry Gate"),
                         "u10-geometry-gate")

    def test_a_domain_is_short_and_derived_from_real_words(self):
        d = fd.domain_of("audit the U10 branch cluster and tell me which one to keep")
        self.assertTrue(d)
        self.assertLessEqual(len(d), 24)
        self.assertNotIn("the", d)

    def test_a_task_with_no_usable_words_still_yields_a_domain(self):
        self.assertEqual(fd.domain_of("do it"), "fleet")

    def test_live_names_are_grouped_by_domain_prefix_not_by_fuzzy_match(self):
        self.assertEqual(
            fd.live_in_domain(names("engrane-a-1", "engrane", "engineering-x-1",
                                    "other-1"), "engrane"),
            ["engrane", "engrane-a-1"])


class TestDecide(unittest.TestCase):
    def test_a_clean_domain_opens_and_names_the_lead(self):
        d = fd.decide("audit the U10 branches", names(), domain="u10")
        self.assertTrue(d["ok"])
        self.assertEqual(d["lead"], "u10-lead")

    def test_the_cap_refuses_and_names_the_holders(self):
        live = [f"u10-worker-{i}" for i in range(1, 6)]
        d = fd.decide("audit the U10 branches", live, domain="u10")
        self.assertFalse(d["ok"])
        self.assertIn("cap 5", d["why"])
        self.assertIn("u10-worker-5", d["why"])

    def test_the_lead_does_not_count_toward_the_cap(self):
        live = [f"u10-worker-{i}" for i in range(1, 5)] + ["u10-lead"]
        d = fd.decide("audit the U10 branches", live, domain="u10")
        self.assertTrue(d["ok"], "4 workers + a lead is not 5 workers")

    def test_a_fleet_child_may_not_open_a_fleet(self):
        d = fd.decide("audit the U10 branches", names(), domain="u10",
                      parent_is_child=True)
        self.assertFalse(d["ok"])
        self.assertIn("third level", d["why"])

    def test_a_name_held_by_a_different_session_is_a_refusal_not_a_reuse(self):
        d = fd.decide("audit the U10 branches", names("u10-lead"),
                      held_by=[("u10-lead", "01a1deadbeef0000")], domain="u10")
        self.assertFalse(d["ok"])
        self.assertIn("01a1dead", d["why"])
        self.assertIn("never a slot", d["why"])

    def test_a_task_too_short_to_plan_from_is_refused(self):
        d = fd.decide("fix", names())
        self.assertFalse(d["ok"])
        self.assertIn("too short", d["why"])

    def test_an_unknown_kind_is_refused(self):
        d = fd.decide("audit the U10 branches", names(), domain="u10", kind="magic")
        self.assertFalse(d["ok"])
        self.assertIn("kind", d["why"])

    def test_the_workers_allowance_is_carried_into_the_brief(self):
        d = fd.decide("audit the U10 branches", names(), domain="u10", workers=3)
        self.assertEqual(d["workers_allowed"], 3)


class TestManagerBrief(unittest.TestCase):
    def setUp(self):
        self.dec = fd.decide("audit the U10 branch cluster", names(), domain="u10")
        self.text = fd.manager_brief("audit the U10 branch cluster", self.dec,
                                     report_to="01a1caller")

    def test_the_task_is_quoted_verbatim(self):
        self.assertIn("audit the U10 branch cluster", self.text)

    def test_the_plan_shape_is_spelled_out_so_spawn_can_parse_it(self):
        self.assertIn("## U1 — <title>", self.text)
        self.assertIn("done-test:", self.text)

    def test_the_plan_rule_states_that_a_unit_without_a_test_cannot_be_spawned(self):
        self.assertIn("Every unit needs a `done-test:`", self.text)

    def test_the_cap_and_the_naming_rule_are_stated(self):
        self.assertIn("At most 5 concurrent workers", self.text)
        self.assertIn("u10-<role>-<n>", self.text)

    def test_the_report_up_command_is_exact(self):
        self.assertIn("fleet rollup", self.text)
        self.assertIn("01a1caller", self.text)

    def test_the_manager_is_told_not_to_hand_write_a_done_test(self):
        self.assertIn("You never pass `--done-test`", self.text)

    def test_the_four_part_return_is_present(self):
        for part in ("RESULT", "ARTIFACTS", "COULD NOT DO", "UNSURE"):
            self.assertIn(part, self.text)

    def test_the_no_sub_fleet_rule_is_stated(self):
        self.assertIn("Do not spawn a fleet of your own", self.text)


if __name__ == "__main__":
    unittest.main()
