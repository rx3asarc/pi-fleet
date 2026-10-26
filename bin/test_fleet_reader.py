#!/usr/bin/env python3
"""U5 — the independent reader and the integration gate.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_reader -v

WHY (2026-10-26, pi-moa review objection 6). An evidence gate proves an artifact exists,
not that it is correct. A second reader is THEATRE if it reads the same text as the
worker: correlated inputs produce correlated errors, so the review becomes a confidence
amplifier for a shared misunderstanding. The correlation source that matters is the
brief, not the model family.

So the mechanism under test is: the reader reads the ORIGINAL task, is briefed
adversarially, is never shown the worker's reasoning or self-authored tests, and runs in
a tree the worker never touched. Plus a second denominator — integration — because two
units can each pass and still not compose.
"""
import unittest

import fleet_reader as fr

TASK = "make the API faster — the human means p99 tail latency"
WORKER_NARRATIVE = "I added a cache and everything looks great, tests are green."
PLAN_SLICE = "Profile the p99 path and record the top 3 contributors."


class TestTheatreTest(unittest.TestCase):
    def setUp(self):
        self.text = fr.brief(TASK, "api-worker-1", artifact="/tmp/wt",
                             unit_title="profile the p99 path")

    def test_the_reader_reads_the_original_task(self):
        self.assertIn(TASK, self.text)

    def test_the_reader_is_not_shown_the_workers_narrative(self):
        self.assertNotIn(WORKER_NARRATIVE, self.text)

    def test_the_reader_is_not_shown_the_plan_slice(self):
        """This is the theatre test: same inputs, same blind spot."""
        self.assertNotIn(PLAN_SLICE, self.text)

    def test_the_reader_is_told_what_it_was_not_given(self):
        self.assertIn("NOT given, on purpose", self.text)
        self.assertIn("framing mistake", self.text)

    def test_the_reader_is_adversarial_not_confirmatory(self):
        self.assertIn("find where it is wrong", self.text)
        self.assertIn("Try to break it", self.text)
        self.assertIn("A reader that never fails anything", self.text)

    def test_it_checks_against_the_question_asked_not_the_build(self):
        self.assertIn("what the human ASKED", self.text)

    def test_a_missing_task_is_a_finding_not_a_blank(self):
        text = fr.brief("", "w")
        self.assertIn("treat that as a finding", text)


class TestVerdicts(unittest.TestCase):
    def test_fails_records_the_reason_that_blocks_the_reap(self):
        rec = fr.record_fails({}, "empty body returns 500", now=1, by="reader-1")
        self.assertEqual(rec["review_verdict"], "fails")
        self.assertIn("empty body", rec["review_fails"])
        self.assertEqual(rec["reviewed_at"], 1)

    def test_passes_clears_a_previous_failure(self):
        rec = fr.record_fails({}, "was broken")
        rec = fr.record_passes(rec, "ran in a clean tree, broke nothing")
        self.assertEqual(rec["review_verdict"], "passes")
        self.assertNotIn("review_fails", rec)


class TestIntegration(unittest.TestCase):
    def test_two_reported_units_require_an_integration_check(self):
        kids = [("w1", {"reported_at": 1}), ("w2", {"reported_at": 2})]
        need, why = fr.integration_required(kids)
        self.assertTrue(need)
        self.assertIn("2 units reported", why)

    def test_one_unit_does_not_require_it(self):
        need, _ = fr.integration_required([("w1", {"reported_at": 1})])
        self.assertFalse(need)

    def test_unrun_integration_is_printed_as_a_finding(self):
        self.assertEqual(fr.integration_line({}), "UNRUN")

    def test_a_cached_run_says_it_was_cached(self):
        lead = fr.record_integration({}, 0, "sh it.sh", now=5, cached=True)
        self.assertEqual(fr.integration_line(lead), "ran rc=0 (cached)")

    def test_a_failing_integration_is_visible_with_its_code(self):
        lead = fr.record_integration({}, 2, "sh it.sh")
        self.assertIn("rc=2", fr.integration_line(lead))


if __name__ == "__main__":
    unittest.main()
