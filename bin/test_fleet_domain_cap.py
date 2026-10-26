#!/usr/bin/env python3
"""U7 — the domain is a FIELD, the name is the subject, and the cap counts the field.

RUN:  cd bin && python3 test_fleet_domain_cap.py

WHY (naming change, 2026-10-26). The rule used to be `<domain>-<role>-<n>`, which made
the domain a NAME PREFIX. Two things broke because of it:

  * a stranger could not name a worker anything -- `audit-1` was refused as
    "unscoped" while the naming contract told people to prefer the subject;
  * the 5-per-domain cap could only see a domain while it was spelled into a name, so
    `fleet spawn --domain X` was a flag with no record behind it and the cap was
    enforced in `fleet_door.decide()` alone -- spawning directly walked around it.

What must now be true, each with a test below:

  1. a subject-only name (`audit-1`) is legal, and a bare word is still refused;
  2. `--domain` records `domain` as a FIELD, so the name carries no domain at all;
  3. the cap counts the FIELD (name prefix only for legacy rows);
  4. the 6th worker in one domain is refused, and an override RECORDS itself;
  5. a domain the door INFERRED from the task is labelled `inferred:<d>` and is not
     counted against a domain nobody chose;
  6. `FLEET_DOMAINS` changes the accepted set, and the example default is not deleted.
"""
import importlib.machinery
import importlib.util
import os
import pathlib
import unittest
from unittest import mock

import fleet_door as fd
import pi_taxonomy

HERE = pathlib.Path(__file__).resolve().parent


def load_fleet():
    """The repo's own `fleet` script as a module (it is a script, not a package)."""
    loader = importlib.machinery.SourceFileLoader("fleetmod", str(HERE / "fleet"))
    spec = importlib.util.spec_from_loader("fleetmod", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def live(name, domain=None, **kw):
    """One lineage row as `fleet spawn` writes it."""
    row = {"name": name, "alive": True}
    if domain is not None:
        row["domain"] = domain
    row.update(kw)
    return row


class TestTheNameIsTheSubject(unittest.TestCase):
    def test_a_subject_only_name_is_legal(self):
        d = pi_taxonomy.validate_name("audit-1")
        self.assertTrue(d["ok"], d.get("error"))
        self.assertIsNone(d["domain"], "no domain was passed, so none is invented")
        self.assertEqual(d["subject"], "audit")

    def test_a_bare_word_and_a_random_pair_are_still_refused(self):
        # The old namer's shapes: one word, or two words with nothing to do with what
        # the session IS. Eight of those collided across 3-4 files each.
        for bad in ("marrow", "alder-ember"):
            d = pi_taxonomy.validate_name(bad)
            self.assertFalse(d["ok"], bad)
            self.assertIn("--unscoped", d["error"])

    def test_a_domain_passed_as_a_field_accepts_any_subject_name(self):
        d = pi_taxonomy.validate_name("dsl-rework", domain="meta")
        self.assertTrue(d["ok"], d.get("error"))
        self.assertEqual(d["domain"], "meta")
        self.assertEqual(d["domain_source"], "field")

    def test_a_name_that_still_carries_a_domain_prefix_is_read_that_way(self):
        # Legacy rows: the domain exists only in the name. The set is whatever
        # FLEET_DOMAINS says, which is also how this test stays free of any one
        # deployment's vocabulary.
        with mock.patch.dict(os.environ, {"FLEET_DOMAINS": "acme"}, clear=False):
            d = pi_taxonomy.validate_name("acme-theme-dup-create-01")
            self.assertTrue(d["ok"])
            self.assertEqual(d["domain"], "acme")
            self.assertEqual(d["domain_source"], "name prefix")


class TestTheCapCountsTheField(unittest.TestCase):
    def test_five_subject_only_workers_in_one_domain_refuse_the_sixth(self):
        rows = [live(f"audit-{i}", "meta") for i in range(1, 6)]
        d = fd.decide("audit the U10 branches", [], domain="meta", live_records=rows)
        self.assertFalse(d["ok"], "5 live workers in the FIELD is the cap")
        self.assertIn("cap 5", d["why"])

    def test_subject_only_workers_with_no_field_are_in_no_domain(self):
        # Every one of these names would have been "domain audit" under the old rule.
        rows = [live(f"audit-{i}") for i in range(1, 6)]
        d = fd.decide("audit the U10 branches", [], domain="meta", live_records=rows)
        self.assertTrue(d["ok"], d.get("why"))
        self.assertEqual(d["workers_now"], 0)

    def test_the_field_wins_over_the_name_prefix(self):
        # The names say `meta-...`; the field says they belong to `other`. The FIELD
        # answers, because that is the record a count can trust.
        rows = [live(f"meta-worker-{i}", "other") for i in range(1, 6)]
        d = fd.decide("audit the U10 branches", [], domain="meta", live_records=rows)
        self.assertTrue(d["ok"], d.get("why"))
        self.assertEqual(d["workers_now"], 0)

    def test_a_legacy_row_with_no_field_is_read_from_its_name(self):
        rows = [live(f"meta-worker-{i}") for i in range(1, 6)]
        self.assertEqual(fd.workers_in_domain(rows, "meta"),
                         [f"meta-worker-{i}" for i in range(1, 6)])

    def test_a_coordinator_does_not_count_and_a_closed_row_does_not_count(self):
        rows = ([live("meta-lead", "meta")]
                + [live(f"meta-worker-{i}", "meta") for i in range(1, 5)]
                + [live("meta-worker-9", "meta", verdict="done")])
        self.assertEqual(len(fd.workers_in_domain(rows, "meta")), 4)

    def test_the_override_is_recorded_not_silent(self):
        refused = fd.cap_verdict("meta", 5)
        self.assertFalse(refused["ok"])
        self.assertIn("--over-cap", refused["why"])
        self.assertIsNone(refused["override"])
        forced = fd.cap_verdict("meta", 5, over_cap=True)
        self.assertTrue(forced["ok"])
        self.assertEqual(forced["override"], {"domain_cap_override": True})

    def test_below_the_cap_nothing_is_refused_and_nothing_is_recorded(self):
        v = fd.cap_verdict("meta", 4)
        self.assertTrue(v["ok"])
        self.assertIsNone(v["override"])


class TestTheInferredDomainIsLabelled(unittest.TestCase):
    def test_a_task_derived_domain_is_marked_inferred(self):
        d = fd.decide("audit the U10 branch cluster")
        self.assertTrue(d["domain_inferred"])
        self.assertEqual(d["domain_label"], "inferred:" + d["domain"])
        self.assertIn("inferred", d["domain_source"])

    def test_a_chosen_domain_is_never_labelled_inferred(self):
        d = fd.decide("audit the U10 branch cluster", domain="u10")
        self.assertFalse(d["domain_inferred"])
        self.assertEqual(d["domain_label"], "u10")

    def test_the_brief_tells_the_lead_its_domain_was_inferred(self):
        d = fd.decide("audit the U10 branch cluster")
        text = fd.manager_brief("audit the U10 branch cluster", d)
        self.assertIn("INFERRED", text)
        self.assertIn("inferred:" + d["domain"], text)

    def test_an_inferred_row_does_not_count_against_a_chosen_domain(self):
        rows = [live(f"audit-{i}", "inferred:audit") for i in range(1, 6)]
        self.assertEqual(fd.workers_in_domain(rows, "audit"), [])
        # ... and it IS counted when a caller asks for the inferred bucket by name.
        self.assertFalse(fd.record_in_domain(rows[0], "audit"))
        self.assertTrue(fd.record_in_domain(rows[0], "audit", include_inferred=True))


class TestTheExampleDomainSetIsOverridable(unittest.TestCase):
    def test_fleet_domains_changes_the_accepted_set(self):
        with mock.patch.dict(os.environ, {"FLEET_DOMAINS": "web, data"}, clear=False):
            self.assertEqual(pi_taxonomy.known_domains(), ("web", "data"))
            self.assertEqual(pi_taxonomy.declared_domain("web-copy-1"), "web")
            self.assertEqual(pi_taxonomy.validate_name("web-copy-1")["domain"], "web")
            # A default domain is no longer a domain: it is a subject with a dash in
            # it, which is exactly what "the domain is a field" means.
            self.assertIsNone(pi_taxonomy.declared_domain("meta-copy-1"))

    def test_the_default_set_is_kept_and_the_override_is_documented(self):
        # THE FIVE ARE PINNED ON PURPOSE. `check`/the unit says: make the set
        # configurable, do not delete the values this box runs on. A configurable
        # default with nothing pinning it is a default any refactor can erase.
        with mock.patch.dict(os.environ, {"FLEET_DOMAINS": ""}, clear=False):
            self.assertEqual(pi_taxonomy.known_domains(), pi_taxonomy.KNOWN_DOMAINS)
        for d in ("nordisk", "engrane", "enc1", "meta", "infra"):
            self.assertIn(d, pi_taxonomy.KNOWN_DOMAINS)
        src = pathlib.Path(pi_taxonomy.__file__).read_text()
        self.assertIn("FLEET_DOMAINS", src)
        self.assertIn("sk-", src, "the domain-vs-`sk-` scanner false positive is recorded")


class TestSpawnRecordsTheField(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fleet = load_fleet()

    def test_a_domain_flag_becomes_a_field(self):
        self.assertEqual(self.fleet._domain_patch("Meta", "audit-1"),
                         {"domain": "meta", "domain_source": "spawn --domain"})

    def test_a_legacy_name_prefix_is_recorded_as_the_source(self):
        with mock.patch.dict(os.environ, {"FLEET_DOMAINS": "acme"}, clear=False):
            self.assertEqual(self.fleet._domain_patch(None, "acme-copy-1"),
                             {"domain": "acme", "domain_source": "name prefix (legacy)"})

    def test_neither_flag_nor_prefix_records_nothing(self):
        # No field is INVENTED: a worker in no domain is legal, and inventing one is
        # how the cap starts counting against a bucket nobody chose.
        self.assertEqual(self.fleet._domain_patch(None, "audit-1"), {})

    def test_the_live_count_is_taken_from_the_field(self):
        children = {f"01a1{i:04d}": live(f"audit-{i}", "meta") for i in range(1, 7)}
        self.assertEqual(self.fleet._domain_load("meta", children=children), 6)
        # Same names, no field: they are in no domain, so nothing is counted.
        bare = {f"01a1{i:04d}": live(f"audit-{i}") for i in range(1, 7)}
        self.assertEqual(self.fleet._domain_load("meta", children=bare), 0)

    def test_a_closed_or_coordinator_row_is_not_counted(self):
        children = {"01a1aaaa": live("meta-lead", "meta"),
                    "01a1bbbb": live("meta-worker-1", "meta", verdict="done"),
                    "01a1cccc": live("meta-worker-2", "meta")}
        self.assertEqual(self.fleet._domain_load("meta", children=children), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
