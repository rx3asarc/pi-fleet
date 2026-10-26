#!/usr/bin/env python3
"""U1 — typed claims and the deterministic conflict rollup.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_rollup -v

WHY (2026-10-26, pi-moa review). Two rules were unimplementable as first written:

  * "Print contradictions instead of resolving them" needs contradiction to be
    COMPUTABLE. Over free-text prose it is not — contradiction is a property of
    meaning. It becomes computable only when workers emit typed claims, and then it
    is a `GROUP BY assertion_key HAVING COUNT(DISTINCT (value, polarity)) > 1`.
  * "Never one blended voice" needs enforcement, not intent. The enforcement here is
    a test that asserts this module imports nothing that can talk to a model: if a
    model cannot be reached, the rollup cannot be smoothed.
"""
import sys
import unittest

import fleet_rollup as fr


def child(name, **over):
    rec = {"name": name, "unit": f"unit {name}", "spawned_at": 1}
    rec.update(over)
    return (name, rec)


class TestClaimSchema(unittest.TestCase):
    def test_a_well_formed_claim_list_passes(self):
        clean, errs = fr.validate_claims([
            {"claim_id": "c1", "assertion_key": "p99_ms", "value": 120,
             "polarity": "is", "evidence_ref": "log#12"}])
        self.assertEqual(errs, [])
        self.assertEqual(clean[0]["polarity"], "is")

    def test_polarity_accepts_booleans_and_rejects_nonsense(self):
        clean, errs = fr.validate_claims([
            {"claim_id": "a", "assertion_key": "k", "value": 1, "polarity": True},
            {"claim_id": "b", "assertion_key": "k2", "value": 1, "polarity": "maybe"}])
        self.assertEqual(len(clean), 1)
        self.assertEqual(clean[0]["polarity"], "is")
        self.assertTrue(any("polarity" in e for e in errs))

    def test_missing_fields_and_duplicate_ids_are_refused(self):
        _, errs = fr.validate_claims([
            {"claim_id": "a", "assertion_key": "k"},
            {"claim_id": "b", "assertion_key": "k", "value": 1, "polarity": "is"},
            {"claim_id": "b", "assertion_key": "k", "value": 2, "polarity": "is"}])
        joined = " | ".join(errs)
        self.assertIn("missing", joined)
        self.assertIn("duplicate claim_id", joined)


class TestConflicts(unittest.TestCase):
    def test_two_workers_disagreeing_produces_one_conflict_with_both_refs(self):
        kids = [child("w1", claims_v2=[{"claim_id": "1", "assertion_key": "p99_ms",
                                       "value": 120, "polarity": "is",
                                       "evidence_ref": "log#12"}]),
                child("w2", claims_v2=[{"claim_id": "2", "assertion_key": "p99_ms",
                                       "value": 240, "polarity": "is",
                                       "evidence_ref": "log#77"}])]
        cons = fr.conflicts(kids)
        self.assertEqual(len(cons), 1)
        self.assertEqual(cons[0]["assertion_key"], "p99_ms")
        refs = sorted(e["evidence_ref"] for e in cons[0]["entries"])
        self.assertEqual(refs, ["log#12", "log#77"])

    def test_agreement_produces_no_conflict(self):
        kids = [child("w1", claims_v2=[{"claim_id": "1", "assertion_key": "k",
                                       "value": 1, "polarity": "is"}]),
                child("w2", claims_v2=[{"claim_id": "2", "assertion_key": "k",
                                       "value": 1, "polarity": "is"}])]
        self.assertEqual(fr.conflicts(kids), [])

    def test_same_value_opposite_polarity_is_a_conflict(self):
        kids = [child("w1", claims_v2=[{"claim_id": "1", "assertion_key": "k",
                                       "value": True, "polarity": "is"}]),
                child("w2", claims_v2=[{"claim_id": "2", "assertion_key": "k",
                                       "value": True, "polarity": "is-not"}])]
        self.assertEqual(len(fr.conflicts(kids)), 1,
                         "'X is true' and 'X is not true' must not silently agree")


class TestRender(unittest.TestCase):
    def kids(self):
        return [child("w1", done_test={"kind": "cmd", "cmd": "sh check.sh"},
                      claims={"files": [], "verify": "sh check.sh", "unsure": None},
                      checks={"verify": {"cmd": "sh check.sh", "rc": 0},
                              "verify_clean": {"rc": 0, "cached": True}},
                      report="I think it works.",
                      claims_v2=[{"claim_id": "1", "assertion_key": "p99_ms",
                                  "value": 120, "polarity": "is"}]),
                child("w2", done_test={"kind": "cmd", "cmd": "pytest -q"},
                      claims={"files": [], "verify": "pytest -q",
                              "unsure": "unsure the migration is complete"},
                      checks={"verify": {"cmd": "pytest -q", "rc": 1}},
                      report="Tests are red.",
                      claims_v2=[{"claim_id": "2", "assertion_key": "p99_ms",
                                  "value": 240, "polarity": "is"}])]

    def test_the_original_task_is_quoted_verbatim_at_the_top(self):
        text = fr.render("lead", 'make the "API" faster — p99', self.kids())
        self.assertIn('**task as given** (verbatim): make the "API" faster — p99', text)

    def test_a_conflict_is_printed_not_resolved(self):
        text = fr.render("lead", "t", self.kids())
        self.assertIn("## CONFLICTS", text)
        self.assertIn("### p99_ms", text)
        self.assertIn("**120**", text)
        self.assertIn("**240**", text)

    def test_unsure_is_verbatim_and_in_its_own_section(self):
        text = fr.render("lead", "t", self.kids())
        self.assertIn("unsure the migration is complete", text)
        head = text.split("## UNSURE (verbatim)")[1]
        self.assertIn("unsure the migration is complete", head.split("##")[0])

    def test_narrative_is_quarantined_and_never_inside_a_deterministic_section(self):
        text = fr.render("lead", "t", self.kids())
        before_narrative = text.split("## WORKER NARRATIVE")[0]
        self.assertNotIn("I think it works.", before_narrative)
        self.assertNotIn("Tests are red.", before_narrative)

    def test_a_failed_check_is_a_section_not_a_footnote(self):
        text = fr.render("lead", "t", self.kids())
        self.assertIn("## FAILED CHECKS", text)
        failed = text.split("## FAILED CHECKS")[1].split("## ")[0]
        self.assertIn("pytest -q", failed)
        self.assertIn("exited 1", failed)

    def test_a_legacy_unit_is_labelled_weaker_not_quiet(self):
        text = fr.render("lead", "t", [child("old", claims={"files": []})])
        self.assertIn("legacy (no frozen test)", text)

    def test_cached_and_live_evidence_are_distinguishable(self):
        text = fr.render("lead", "t", self.kids())
        self.assertIn("clean:cached", text)

    def test_aborted_and_unacked_are_visible_in_the_header(self):
        text = fr.render("lead", "t", self.kids(), aborted=True, unacked=True)
        self.assertTrue(text.startswith("# ABORTED (partial)"))
        self.assertIn("PLAN UNACKED", text)

    def test_an_unrun_integration_check_is_printed(self):
        self.assertIn("**integration check**: UNRUN", fr.render("lead", "t", []))


class TestNoModelCanReachTheRollup(unittest.TestCase):
    """The enforcement behind "never one blended voice". A module that cannot import
    a client cannot summarise, so this assertion is the architecture."""

    ALLOWED = {"__future__", "time", "json", "os", "re", "typing", "datetime"}

    def test_the_module_imports_only_the_standard_library(self):
        """Parse the imports rather than grepping the text: a whitelist cannot be
        satisfied accidentally by a comment, and a new client import fails loudly."""
        import ast
        src = sys.modules["fleet_rollup"].__file__
        tree = ast.parse(open(src).read())
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertTrue(names <= self.ALLOWED,
                        f"fleet_rollup imported {sorted(names - self.ALLOWED)} — a "
                        f"module that can reach a model can smooth the evidence")


if __name__ == "__main__":
    unittest.main()
