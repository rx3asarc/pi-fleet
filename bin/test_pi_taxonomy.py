#!/usr/bin/env python3
"""U1 tests — the taxonomy resolver.

PURE. Fixtures only. No tmux, no git, no /proc, no filesystem.
If any test in this file needs the outside world, the seam is in the wrong place.
"""
import unittest

from pi_taxonomy import resolve


class TestDeclaredWins(unittest.TestCase):
    def test_declared_taxonomy_string_is_authoritative(self):
        r = resolve({"name": "gale", "cwd": "/root"}, declared="nordisk-theme-dup-create")
        self.assertEqual(r["domain"], "nordisk")
        self.assertEqual(r["topic"], "theme")
        self.assertEqual(r["subtopic"], "dup-create")
        self.assertEqual(r["source"], "declared")
        self.assertFalse(r["inferred"])

    def test_declared_beats_worktree(self):
        r = resolve({"name": "x", "worktree": "/mnt/projects/engrane-citations"},
                    declared="nordisk-theme")
        self.assertEqual(r["domain"], "nordisk")

    def test_declared_domain_only(self):
        r = resolve({"name": "gale"}, declared="engrane")
        self.assertEqual(r["domain"], "engrane")
        self.assertEqual(r["topic"], "")
        self.assertEqual(r["source"], "declared")


class TestConformingName(unittest.TestCase):
    """A --name that already encodes the taxonomy counts as declared (priority 1)."""

    def test_goal_helper_name(self):
        r = resolve({"name": "nordisk-goal-helper-2", "cwd": "/root"})
        self.assertEqual(r["domain"], "nordisk")
        self.assertEqual(r["topic"], "goal")
        self.assertEqual(r["subtopic"], "swarm.helper")
        self.assertEqual(r["source"], "declared")

    def test_goal_lead_name(self):
        r = resolve({"name": "nordisk-goal-lead-10", "cwd": "/root"})
        self.assertEqual((r["domain"], r["topic"], r["subtopic"]),
                         ("nordisk", "goal", "swarm.lead"))

    def test_storefront(self):
        r = resolve({"name": "nordisk-main", "cwd": "/root"})
        self.assertEqual((r["domain"], r["topic"]), ("nordisk", "storefront"))

    def test_engrane_worker(self):
        r = resolve({"name": "engrane-operations", "cwd": "/root"})
        self.assertEqual(r["domain"], "engrane")
        self.assertEqual(r["topic"], "operations")


class TestWorktree(unittest.TestCase):
    """Priority 2 — worktree name."""

    def test_citations_worktree(self):
        r = resolve({"name": "basalt",
                     "worktree": "/mnt/HC_Volume_105587324/projects/engrane-citations"})
        self.assertEqual(r["domain"], "engrane")
        self.assertEqual(r["topic"], "citations")
        self.assertEqual(r["source"], "worktree")

    def test_worktree_ahead_of_cwd(self):
        r = resolve({"name": "basalt", "cwd": "/root",
                     "worktree": "/mnt/HC_Volume_105587324/projects/engrane-try-section"})
        self.assertEqual(r["domain"], "engrane")
        self.assertEqual(r["topic"], "try-section")


class TestCwd(unittest.TestCase):
    """Priority 3 — working directory, but only when it is NOT generic."""

    def test_generic_root_is_not_a_source(self):
        r = resolve({"name": "marrow", "cwd": "/root"})
        self.assertEqual(r["domain"], "unscoped")

    def test_nordisk_cwd(self):
        r = resolve({"name": "marrow", "cwd": "/mnt/HC_Volume_105587324/nordisk"})
        self.assertEqual(r["domain"], "nordisk")
        self.assertEqual(r["source"], "cwd")

    def test_generic_dirs_all_ignored(self):
        for d in ("/root", "/tmp", "/home", "/usr", "/var", "/opt"):
            with self.subTest(cwd=d):
                self.assertEqual(resolve({"name": "marrow", "cwd": d})["domain"],
                                 "unscoped")


class TestInferred(unittest.TestCase):
    """Priority 4 — first user message keyword pass. MUST be flagged inferred."""

    def test_keyword_hit_is_inferred(self):
        r = resolve({"name": "marrow", "cwd": "/root",
                     "first_user_message": "fix the nordisk theme duplicate"})
        self.assertEqual(r["domain"], "nordisk")
        self.assertEqual(r["source"], "inferred")
        self.assertTrue(r["inferred"])

    def test_inferred_is_weaker_than_cwd(self):
        # a real cwd beats a keyword guess: same message, but cwd is meaningful
        r = resolve({"name": "marrow", "cwd": "/mnt/HC_Volume_105587324/nordisk",
                     "first_user_message": "totally unrelated words"})
        self.assertEqual(r["source"], "cwd")
        self.assertFalse(r["inferred"])


class TestUnscoped(unittest.TestCase):
    """Priority 5 — nothing derivable. Explicit bucket, never a guess."""

    def test_bare_word_names(self):
        for name in ("marrow", "gale", "iris", "basalt", "quartz", "opal"):
            with self.subTest(name=name):
                r = resolve({"name": name, "cwd": "/root"})
                self.assertEqual(r["domain"], "unscoped")
                self.assertTrue(r["inferred"])
                self.assertEqual(r["source"], "none")

    def test_unscoped_never_invents_a_topic(self):
        r = resolve({"name": "marrow", "cwd": "/root"})
        self.assertEqual(r["topic"], "")
        self.assertEqual(r["subtopic"], "")


class TestShape(unittest.TestCase):
    """Every result has the same keys, always."""

    KEYS = {"domain", "topic", "subtopic", "source", "inferred"}
    SOURCES = {"declared", "worktree", "cwd", "inferred", "none"}

    def test_shape_is_stable(self):
        cases = [
            ({"name": "marrow", "cwd": "/root"}, None),
            ({"name": "gale"}, "nordisk-theme"),
            ({"name": "x", "worktree": "/p/engrane-citations"}, None),
            ({"name": "nordisk-goal-helper-2"}, None),
            ({}, None),
        ]
        for entry, declared in cases:
            with self.subTest(entry=entry):
                r = resolve(entry, declared=declared)
                self.assertEqual(set(r), self.KEYS)
                self.assertIn(r["source"], self.SOURCES)

    def test_empty_entry_is_unscoped_not_a_crash(self):
        r = resolve({})
        self.assertEqual(r["domain"], "unscoped")

    def test_is_pure_does_not_mutate_input(self):
        entry = {"name": "marrow", "cwd": "/root"}
        snapshot = dict(entry)
        resolve(entry, declared="nordisk-theme")
        self.assertEqual(entry, snapshot)


class TestCollisionCases(unittest.TestCase):
    """Spec: fixtures "must include the collision cases".

    The hyphenated two-word names are COLLISION ARTIFACTS of the namer's fixed
    40-word pool, not taxonomies -- 47 sessions drawing from 40 words in the same
    generic directory is a pigeonhole, so the collision handler appends a second
    word. Treating those words as a domain/topic pair would invent structure that
    does not exist, which is why the first token must be a KNOWN domain.
    """

    COLLISIONS = ["alder-gale", "sable-ember", "falcon-moss", "umber-marrow",
                  "plover-wren", "cobalt-rowan", "jasper-alder",
                  "larch", "nimbus", "marrow", "gale", "iris", "amber"]

    def test_collision_words_are_not_domains(self):
        for name in self.COLLISIONS:
            with self.subTest(name=name):
                r = resolve({"name": name, "cwd": "/root"})
                self.assertEqual(r["domain"], "unscoped")
                self.assertTrue(r["inferred"])
                self.assertEqual(r["source"], "none")

    def test_a_real_domain_prefix_still_parses(self):
        """`engrane-rowan` looks like a collision but is NOT one: its first token
        is a known domain, so engrane/rowan is the honest reading. What separates
        the two cases is membership in KNOWN_DOMAINS, not the hyphen."""
        r = resolve({"name": "engrane-rowan", "cwd": "/root"})
        self.assertEqual(r["domain"], "engrane")
        self.assertEqual(r["topic"], "rowan")
        self.assertEqual(r["source"], "declared")
        self.assertFalse(r["inferred"])

    def test_collision_name_still_yields_to_a_real_worktree(self):
        """A junk name must not mask a real signal from where the session sits."""
        r = resolve({"name": "falcon-moss",
                     "cwd": "/mnt/HC_Volume_105587324/projects/engrane-citations"})
        self.assertEqual(r["domain"], "engrane")
        self.assertEqual(r["topic"], "citations")
        self.assertEqual(r["source"], "cwd")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestSpawnTimeDeclaration(unittest.TestCase):
    """U8: "Declaration must be a byproduct of spawning, never a separate manual
    act" (spec, Derivation priority). The transport is `PI_TAXONOMY` at spawn:
    `fleet spawn` exports it, the session-namer persists it, the resolver reads it
    back as `declared`.

    The auditor's 2026-10-20 objection was that none of that existed -- prevention
    covered only `fleet spawn`'s name check, and 20 of 34 live sessions were still
    unscoped with no way for a spawner to declare anything.
    """

    def test_a_conforming_name_yields_a_declaration(self):
        from pi_taxonomy import declaration_of_name
        self.assertEqual(declaration_of_name("nordisk-ads-pixel"), "nordisk/ads/pixel")
        self.assertEqual(declaration_of_name("engrane-citations"), "engrane/citations")

    def test_a_name_with_no_domain_declares_nothing(self):
        from pi_taxonomy import declaration_of_name
        self.assertIsNone(declaration_of_name("marrow"))
        self.assertIsNone(declaration_of_name(""))

    def test_a_valid_declaration_round_trips(self):
        from pi_taxonomy import parse_declaration
        self.assertEqual(parse_declaration("nordisk/ads/pixel"), "nordisk/ads/pixel")
        self.assertEqual(parse_declaration("  Nordisk/Ads  "), "nordisk/ads")

    def test_a_malformed_declaration_is_dropped_not_coerced(self):
        from pi_taxonomy import parse_declaration
        for bad in ("", None, "has space", "UPPER/CASE/IS/FINE?", "a/b/c/d", "../etc"):
            self.assertIsNone(parse_declaration(bad), bad)

    def test_a_declaration_outranks_every_derived_guess(self):
        from pi_taxonomy import resolve
        got = resolve({"name": "engrane-citations", "cwd": "/root/nordisk"},
                      declared="nordisk/water/copy")
        self.assertEqual((got["domain"], got["topic"], got["subtopic"]),
                         ("nordisk", "water", "copy"))
        self.assertEqual(got["source"], "declared")
        self.assertFalse(got["inferred"])
