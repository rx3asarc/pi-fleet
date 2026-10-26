"""U8 — the naming contract. RED first.

`validate_name()` is the ONE decision about whether a name may be spawned, shared
by the spawn path and the audit path. It is pure: live rows are passed in, nothing
is read, nothing is written.

The over-limit case deliberately does NOT block. Blocking a spawn teaches people to
work around the tool; a warning that names the sessions and identifies the one to
retire keeps the tool honest and keeps the fleet from growing a fourth nordisk
session nobody meant to keep.
"""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
import pathlib
import tempfile
import unittest

import pi_taxonomy


def row(name, state="idle", updated_at=None, pending_ask=None):
    return {"name": name, "state": state, "updated_at": updated_at,
            "pending_ask": pending_ask}


class TestTheNameIsAcceptedOrRefused(unittest.TestCase):
    def test_an_unscoped_name_is_refused_and_offers_the_way_out(self):
        d = pi_taxonomy.validate_name("marrow")
        self.assertFalse(d["ok"])
        self.assertIn("--unscoped", d["error"])
        self.assertIn("domain", d["error"])

    def test_the_refusal_names_the_taxonomy_rather_than_just_saying_no(self):
        d = pi_taxonomy.validate_name("marrow")
        # The message must be usable by someone who has never read the spec.
        self.assertIn("-", d["error"])
        self.assertIn("nordisk", d["error"])

    def test_a_scoped_name_is_accepted(self):
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01")
        self.assertTrue(d["ok"])
        self.assertIsNone(d["error"])
        self.assertEqual(d["domain"], "nordisk")
        self.assertEqual(d["topic"], "theme")
        self.assertEqual(d["subtopic"], "dup-create-01")

    def test_unscoped_is_allowed_when_it_is_asked_for(self):
        d = pi_taxonomy.validate_name("marrow", unscoped=True)
        self.assertTrue(d["ok"])
        self.assertIsNone(d["error"])
        self.assertIsNone(d["domain"])

    def test_a_bare_word_with_two_tokens_but_no_domain_is_still_refused(self):
        self.assertFalse(pi_taxonomy.validate_name("alder-ember")["ok"])

    def test_an_empty_name_is_refused(self):
        self.assertFalse(pi_taxonomy.validate_name("")["ok"])

    def test_the_decision_is_pure(self):
        rows = [row("nordisk-main")]
        a = pi_taxonomy.validate_name("nordisk-storefront-01", rows)
        b = pi_taxonomy.validate_name("nordisk-storefront-01", rows)
        self.assertEqual(a, b)


class TestTheWorkingLimitWarnsAndDoesNotBlock(unittest.TestCase):
    def setUp(self):
        self.three = [row("nordisk-main", updated_at=300),
                      row("nordisk-goal-helper-1", updated_at=200),
                      row("nordisk-ads-audit", updated_at=100)]

    def test_a_fourth_session_in_the_domain_still_spawns(self):
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", self.three)
        self.assertTrue(d["ok"], "the limit must never hard-block a spawn")
        self.assertIsNone(d["error"])

    def test_the_warning_names_the_sessions_already_live(self):
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", self.three)
        self.assertIsNotNone(d["warning"])
        for name in ("nordisk-main", "nordisk-goal-helper-1", "nordisk-ads-audit"):
            self.assertIn(name, d["warning"])

    def test_the_oldest_idle_session_is_the_one_marked(self):
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", self.three)
        self.assertEqual(d["accelerate"], "nordisk-ads-audit")

    def test_below_the_limit_there_is_no_warning(self):
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", self.three[:2])
        self.assertTrue(d["ok"])
        self.assertIsNone(d["warning"])
        self.assertIsNone(d["accelerate"])

    def test_the_limit_counts_only_the_same_domain(self):
        other = [row("engrane-operations", updated_at=1),
                 row("engrane-rowan", updated_at=2),
                 row("engrane-citations", updated_at=3)]
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", other)
        self.assertIsNone(d["warning"])

    def test_a_session_with_an_unresolved_ask_is_never_marked(self):
        rows = self.three + [row("nordisk-waiting", updated_at=50,
                                 pending_ask="which colour?")]
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", rows)
        self.assertNotEqual(d["accelerate"], "nordisk-waiting")

    def test_a_working_session_is_not_marked_while_an_idle_one_exists(self):
        # Three live, so the limit IS exceeded: two busy, one idle. The busy ones
        # are never the answer even though one of them is older.
        rows = [row("nordisk-busy", state="working", updated_at=1),
                row("nordisk-also-busy", state="working", updated_at=2),
                row("nordisk-calm", state="idle", updated_at=900)]
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", rows)
        self.assertIsNotNone(d["warning"])
        self.assertEqual(d["accelerate"], "nordisk-calm")

    def test_only_working_sessions_means_nothing_is_marked(self):
        rows = [row("nordisk-a", state="working", updated_at=1),
                row("nordisk-b", state="working", updated_at=2),
                row("nordisk-c", state="working", updated_at=3)]
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", rows)
        self.assertTrue(d["ok"])
        self.assertIsNotNone(d["warning"])
        self.assertIsNone(d["accelerate"],
                          "a busy fleet is a reason to warn, never a reason to kill")

    def test_an_unknown_state_is_not_treated_as_idle(self):
        rows = [dict(row("nordisk-mystery", updated_at=1), state=None),
                row("nordisk-a", state="working", updated_at=2),
                row("nordisk-b", state="working", updated_at=3)]
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", rows)
        self.assertIsNone(d["accelerate"],
                          "unknown is not idle; never retire what we cannot read")

    def test_the_spawned_name_itself_is_not_counted_among_the_live(self):
        rows = [row("nordisk-theme-dup-create-01", updated_at=1)]
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", rows)
        self.assertIsNone(d["warning"])

    def test_a_row_with_no_name_is_ignored(self):
        rows = [{"name": None, "state": "idle"}, {"state": "idle"}]
        d = pi_taxonomy.validate_name("nordisk-theme-dup-create-01", rows)
        self.assertIsNone(d["warning"])


class TestSpawnRefusesBeforeItDoesAnything(unittest.TestCase):
    """A refusal that arrives after `git worktree add` leaves a stray worktree.

    So the name is checked FIRST -- before the fetch, before the worktree, before
    tmux. Proven by pointing --repo at a path that does not exist: if the name
    check ran second, the error would be about the repo.
    """

    @classmethod
    def setUpClass(cls):
        # The REPO'S OWN fleet, resolved from this file. A test in a package that
        # ships cannot read /root/.pi/agent/bin: that path exists on exactly one
        # machine, and on that machine it is a copy this repo does not own.
        path = str(pathlib.Path(__file__).resolve().parent / "fleet")
        loader = importlib.machinery.SourceFileLoader("fleetmod", path)
        spec = importlib.util.spec_from_loader("fleetmod", loader)
        cls.fleet = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.fleet)

    def _spawn(self, name, **kw):
        a = type("A", (), {"name": name, "brief": self.brief,
                           "repo": "/nonexistent-repo-xyz",
                           "branch": None, "model": None,
                           "base": "origin/main",
                           "unscoped": False})()
        for k, v in kw.items():
            setattr(a, k, v)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.fleet.cmd_spawn(a)
        return err.getvalue()

    def setUp(self):
        fd, path = tempfile.mkstemp(suffix=".md")
        os.write(fd, b"brief")
        os.close(fd)
        self.brief = path
        self.addCleanup(os.unlink, path)

    def test_an_unscoped_name_is_refused_before_the_repo_is_touched(self):
        msg = self._spawn("marrow")
        self.assertIn("--unscoped", msg)
        self.assertNotIn("nonexistent-repo", msg,
                         "the name must be judged before anything else happens")

    def test_the_message_spawn_shows_comes_from_validate_name(self):
        # One source of truth for the words: if spawn composed its own message,
        # the audit path and the refusal would drift apart.
        expected = pi_taxonomy.validate_name("marrow")["error"]
        self.assertIn(expected, self._spawn("marrow"))

    def test_unscoped_is_honoured_by_the_flag(self):
        msg = self._spawn("marrow", unscoped=True)
        self.assertNotIn("--unscoped", msg,
                         "with the flag given, the name is accepted")
        self.assertIn("nonexistent-repo", msg,
                      "and the run proceeds far enough to fail on the repo")


if __name__ == "__main__":
    unittest.main()


class TestTheAuditPathUsesTheSameContract(unittest.TestCase):
    """REFACTOR: one validate_name(), used by spawn AND the audit view.

    The audit does not refuse anything -- those sessions are already running. It
    reports the naming health of the live fleet using the same law spawn enforces,
    so the two can never disagree about what a valid name is.
    """

    def test_unscoped_live_sessions_are_listed(self):
        rows = [row("marrow"), row("nordisk-main")]
        a = pi_taxonomy.audit_names(rows)
        self.assertEqual(a["unscoped"], ["marrow"])

    def test_a_domain_over_the_limit_is_reported_with_its_victim(self):
        # FOUR, not three. The limit fires when a domain already holds
        # WORKING_LIMIT sessions and one more is asked for, so three live sessions
        # sit AT the limit and are not over it. The reported count is the domain's
        # own size -- an audit saying "3" while four run would be invisible-wrong.
        rows = [row("nordisk-a", updated_at=1), row("nordisk-b", updated_at=2),
                row("nordisk-c", updated_at=3), row("nordisk-d", updated_at=4)]
        a = pi_taxonomy.audit_names(rows)
        self.assertIn("nordisk", a["over_limit"])
        self.assertEqual(a["over_limit"]["nordisk"]["live"], 4)
        self.assertEqual(a["over_limit"]["nordisk"]["accelerate"], "nordisk-a")

    def test_exactly_the_limit_is_not_over_it(self):
        rows = [row("nordisk-a", updated_at=1), row("nordisk-b", updated_at=2),
                row("nordisk-c", updated_at=3)]
        a = pi_taxonomy.audit_names(rows)
        self.assertEqual(a["over_limit"], {},
                         f"{pi_taxonomy.WORKING_LIMIT} live is the limit, not over it")

    def test_a_domain_under_the_limit_is_not_reported(self):
        a = pi_taxonomy.audit_names([row("nordisk-a"), row("nordisk-b")])
        self.assertEqual(a["over_limit"], {})

    def test_an_empty_fleet_is_not_an_error(self):
        a = pi_taxonomy.audit_names([])
        self.assertEqual(a, {"unscoped": [], "over_limit": {}})

    def test_a_session_that_cannot_be_named_does_not_crash_the_audit(self):
        a = pi_taxonomy.audit_names([{"state": "idle"}, {"name": None}])
        self.assertEqual(a["unscoped"], [])

    def test_the_same_domain_is_counted_once_not_once_per_session(self):
        rows = [row("nordisk-a", updated_at=1), row("nordisk-b", updated_at=2),
                row("nordisk-c", updated_at=3), row("nordisk-d", updated_at=4)]
        a = pi_taxonomy.audit_names(rows)
        self.assertEqual(len(a["over_limit"]), 1)

    def test_the_audit_is_pure_and_order_stable(self):
        rows = [row("marrow"), row("zephyr"), row("nordisk-a")]
        first = pi_taxonomy.audit_names(rows)
        second = pi_taxonomy.audit_names(rows)
        self.assertEqual(first, second)
        self.assertEqual(first["unscoped"], ["marrow", "zephyr"])
