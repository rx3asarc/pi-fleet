#!/usr/bin/env python3
"""U2 — the brief is the artifact workers read, so it is gated like one.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_brief -v

WHY (2026-10-26, pi-moa review objection 4). I had been treating the PLAN as the shared
artifact. No worker reads the plan: each reads a brief the lead distilled from one
slice, and that distillation is lossy and per-worker. A perfect plan with lossy briefs
produces bad workers, and nothing measured the loss. These tests pin the three
properties that make the brief trustworthy: it comes from ONE slice, it carries the
frozen done-test verbatim, and an edit after issue is visible.
"""
import unittest
from pathlib import Path

import fleet_brief as fb

PLAN = """# Plan — make the API faster

## U1 — Profile and find the tail
Run the profiler against the p99 path and record the top 3 contributors.
done-test: sh scripts/profile.sh --json

## U2 — Fix the connection pool
The pool is size 5; the p99 path opens three connections per request.
- done-test: `python3 -m unittest tests.test_pool`

## U3 — Write it up
done-test: file:docs/perf-report.md

## U4 — No test at all
Some prose but no done-test line.
"""


class TestParsePlan(unittest.TestCase):
    def test_units_are_read_in_order_with_their_tests(self):
        units = fb.parse_plan(PLAN)
        self.assertEqual([u[0] for u in units], [1, 2, 3, 4])
        self.assertEqual(units[0][3], "sh scripts/profile.sh --json")
        self.assertEqual(units[1][3], "python3 -m unittest tests.test_pool",
                         "backticks around the command must not survive")
        self.assertEqual(units[2][3], "file:docs/perf-report.md")

    def test_a_unit_without_a_done_test_is_reported_not_dropped(self):
        units = fb.parse_plan(PLAN)
        self.assertIsNone(units[3][3])
        with self.assertRaises(fb.PlanError) as ctx:
            fb.slice_plan(PLAN, 4)
        self.assertIn("U4", str(ctx.exception))
        self.assertIn("done-test", str(ctx.exception))

    def test_a_missing_unit_names_the_ones_that_exist(self):
        with self.assertRaises(fb.PlanError) as ctx:
            fb.slice_plan(PLAN, 9)
        self.assertIn("U1, U2, U3, U4", str(ctx.exception))

    def test_a_plan_with_no_units_is_refused(self):
        with self.assertRaises(fb.PlanError):
            fb.slice_plan("just prose, no headings", 1)


class TestRenderBrief(unittest.TestCase):
    def setUp(self):
        self.unit = fb.slice_plan(PLAN, 1)
        self.text = fb.render(self.unit, task="make the API faster",
                              domain="api", parent="api-lead")

    def test_the_frozen_done_test_appears_verbatim(self):
        self.assertIn("sh scripts/profile.sh --json", self.text)
        self.assertIn("FROZEN", self.text)

    def test_the_task_is_quoted_verbatim(self):
        self.assertIn("make the API faster", self.text)

    def test_the_four_part_contract_is_present(self):
        for part in ("RESULT", "ARTIFACTS", "COULD NOT DO", "UNSURE"):
            self.assertIn(part, self.text)

    def test_another_units_business_is_not_in_the_brief(self):
        self.assertNotIn("connection pool", self.text)
        self.assertNotIn("perf-report.md", self.text)

    def test_rendering_is_deterministic(self):
        again = fb.render(self.unit, task="make the API faster",
                          domain="api", parent="api-lead")
        self.assertEqual(self.text, again)
        self.assertEqual(fb.sha(self.text), fb.sha(again))

    def test_a_file_kind_test_is_reported_with_file(self):
        text = fb.render(fb.slice_plan(PLAN, 3), task="t")
        self.assertIn("--file docs/perf-report.md", text)
        self.assertNotIn("--verify", text)

    def test_the_no_sub_fleet_rule_is_stated(self):
        self.assertIn("Do not spawn a fleet", self.text)


class TestDrift(unittest.TestCase):
    def setUp(self):
        self.p = Path("/tmp/fleet-brief-drift-test.md")
        self.text = fb.render(fb.slice_plan(PLAN, 1), task="t")
        self.p.write_text(self.text)
        self.sha = fb.sha(self.text)

    def tearDown(self):
        self.p.unlink(missing_ok=True)

    def test_an_unedited_brief_has_no_drift(self):
        self.assertIsNone(fb.drift(str(self.p), self.sha))

    def test_an_edited_brief_is_flagged(self):
        self.p.write_text(self.text + "\nPS: I changed my own done-test.\n")
        why = fb.drift(str(self.p), self.sha)
        self.assertIsNotNone(why)
        self.assertIn("rewrote its own contract", why)

    def test_a_deleted_brief_is_flagged(self):
        self.p.unlink()
        self.assertIn("missing", fb.drift(str(self.p), self.sha))

    def test_a_legacy_child_with_no_issued_brief_is_not_drift(self):
        self.assertIsNone(fb.drift("/nonexistent", ""))


class TestIssueAHandWrittenBrief(unittest.TestCase):
    """A `--brief` file is a contract, so it must be issued like a rendered one.

    Measured 2026-10-26: every --brief spawn recorded brief_sha="", so drift()
    answered "nothing was issued" and the arm that detects a worker rewriting its
    own contract never ran for the commonest briefing path.
    """

    def setUp(self):
        self.p = Path("/tmp/fleet-brief-issue-test.md")

    def tearDown(self):
        self.p.unlink(missing_ok=True)

    def test_a_written_brief_is_issued_with_its_sha(self):
        self.p.write_text("# BRIEF\n\nDo the thing.\n")
        issued = fb.issue(str(self.p))
        self.assertEqual(issued, fb.sha(self.p.read_text()))
        # the pair (path, sha) is what the row records, and it round-trips cleanly
        self.assertIsNone(fb.drift(str(self.p), issued))

    def test_an_edit_after_issue_is_drift(self):
        self.p.write_text("# BRIEF\n\nDo the thing.\n")
        issued = fb.issue(str(self.p))
        self.p.write_text("# BRIEF\n\nDo the thing -- but the done-test is optional now.\n")
        why = fb.drift(str(self.p), issued)
        self.assertIsNotNone(why)
        self.assertIn("rewrote its own contract", why)

    def test_a_missing_brief_is_refused_not_silently_unhashed(self):
        with self.assertRaises(ValueError) as ctx:
            fb.issue("/nonexistent/BRIEF.md")
        self.assertIn("not found", str(ctx.exception))

    def test_an_empty_brief_is_refused(self):
        self.p.write_text("   \n\n")
        with self.assertRaises(ValueError) as ctx:
            fb.issue(str(self.p))
        self.assertIn("empty", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
