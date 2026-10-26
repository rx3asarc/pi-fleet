"""U5 — the gate, split into providers.

PURE. Fixtures only. No tmux, no git, no filesystem.

THE REGRESSION GUARD
`fleet reap` deletes a worktree and a git branch. The 7 engrane entries are the
only entries it has ever acted on. Their refusal lists were frozen from a real
`fleet reap --dry-run` on 2026-10-11 into fixtures/engrane_gate_baseline_*.json.
Every refusal must be reproduced WORD FOR WORD: a gate that quietly stops
refusing is a gate that reaps something it should not, and the failure is silent
until it is destructive.

THE SECOND HALF (this is why U5 had to follow U4)
Before U4 there was nothing to gate: `fleet` could not see a session. Now that it
can see 49 of them, the gate must give each class a REASON, not a technicality.
Every session-class entry used to be refused with

    not merged: None @ None is not an ancestor of origin/main

which is correct by luck and meaningless to a human -- a session with no branch
has no branch state to be "not merged".
"""
import json
import unittest
from pathlib import Path

from fleet_gate import gate, PROVIDERS

FIXTURES = Path(__file__).parent / "fixtures"
ENTRIES = FIXTURES / "engrane_baseline_20261011.json"
REFUSALS = FIXTURES / "engrane_gate_baseline_20261011.json"

BASE = "origin/main"


def engrane_entries():
    """The 7 frozen entries, as build_entries emitted them."""
    return json.loads(ENTRIES.read_text())


def session_entry(**kw):
    """A session-class entry: alive, no worktree, no branch.

    `owner_tmux` defaults to agreement with `session`, so an entry built here is
    one whose ownership has been VERIFIED. The mismatch and unresolved cases are
    explicit overrides, not the default.
    """
    e = {"name": "amber", "worktree": None, "session": "amber",
         "sessionId": "sid-1", "alive": True, "phase": "idle",
         "pending_ask": None, "reported": False, "owner_tmux": "amber",
         "base": BASE}
    e.update(kw)
    return e


# ── the guard ────────────────────────────────────────────────────────────────
class TestGitClassUnchanged(unittest.TestCase):
    """No behaviour change for the git class. Pinned before the refactor."""

    def test_the_seven_refusal_lists_are_word_for_word(self):
        want = json.loads(REFUSALS.read_text())
        got = {e["name"]: gate(e, worktree_exists=True, base=BASE)
               for e in engrane_entries()}
        self.assertEqual(got, want)

    def test_the_guard_can_actually_fail(self):
        """A guard nobody has seen fail is a guard nobody should trust.

        Perturb one entry and confirm the comparison notices.
        """
        want = json.loads(REFUSALS.read_text())
        entries = engrane_entries()
        for e in entries:
            if e["name"] == "design-u10-defects":
                e["dirty"] = 0
        got = {e["name"]: gate(e, worktree_exists=True, base=BASE) for e in entries}
        self.assertNotEqual(got, want)

    def test_a_merged_clean_reported_fleet_is_allowed(self):
        e = {"name": "x", "worktree": "/wt/x", "branch": "b", "head": "abc",
             "merged": True, "dirty": 0, "unpushed": 0, "alive": False,
             "reported": True, "base": BASE}
        self.assertEqual(gate(e, worktree_exists=True, base=BASE), [])


# ── the new behaviour ────────────────────────────────────────────────────────
class TestSessionClassRefusals(unittest.TestCase):
    def _busy(self, reasons):
        return any(r.startswith("busy") for r in reasons)

    def test_a_session_inside_a_tool_is_refused_as_busy(self):
        for phase in ("tool:bash", "tool:ask_user_question", "tool:edit"):
            with self.subTest(phase=phase):
                r = gate(session_entry(phase=phase), base=BASE)
                self.assertTrue(r)
                self.assertTrue(self._busy(r), r)

    def test_the_busy_reason_prefers_the_specific_tool(self):
        """`phase` is coarse ("tool"); the broker's status names it ("tool:bash").

        The human reading the refusal is deciding whether to reap a session, and
        "currently inside tool" does not help them decide. Prefer the specific.
        """
        r = gate(session_entry(phase="tool", pi_status="tool:bash"), base=BASE)
        self.assertTrue(any("tool:bash" in x for x in r), r)

    def test_the_busy_reason_falls_back_to_phase_when_there_is_no_status(self):
        r = gate(session_entry(phase="tool", pi_status=None), base=BASE)
        self.assertTrue(any(x.startswith("busy") for x in r), r)

    def test_a_session_with_an_unresolved_ask_is_refused(self):
        r = gate(session_entry(pending_ask="running ask_user_question"), base=BASE)
        self.assertTrue(any("ask" in x.lower() for x in r), r)

    def test_a_silent_session_is_refused_without_accept_silent(self):
        r = gate(session_entry(), base=BASE)
        self.assertTrue(any("has not reported" in x for x in r), r)

    def test_a_reported_session_is_allowed(self):
        self.assertEqual(gate(session_entry(reported=True), base=BASE), [])

    def test_accept_silent_allows_a_quiet_session(self):
        """The draining half of decision 8 needs this to be reachable."""
        self.assertEqual(gate(session_entry(), accept_silent=True, base=BASE), [])

    def test_accept_silent_does_NOT_override_busy(self):
        """Never reap something mid-thought, however loudly you ask."""
        r = gate(session_entry(phase="tool:bash"), accept_silent=True, base=BASE)
        self.assertTrue(self._busy(r), r)

    def test_accept_silent_does_NOT_override_an_unresolved_ask(self):
        r = gate(session_entry(pending_ask="running ask_user_question"),
                 accept_silent=True, base=BASE)
        self.assertTrue(r)

    def test_a_session_never_receives_a_git_reason(self):
        """The nonsense message this unit exists to delete."""
        for accept in (False, True):
            with self.subTest(accept_silent=accept):
                r = gate(session_entry(), accept_silent=accept, base=BASE)
                self.assertFalse([x for x in r if "merged" in x], r)

    def test_a_dead_session_with_no_worktree_is_not_a_candidate(self):
        """Already gone: nothing to protect, nothing to reap."""
        self.assertEqual(
            gate(session_entry(alive=False), worktree_exists=False, base=BASE), [])


# ── ownership must be proven before anything is killed ───────────────────────
class TestOwnershipBeforeTheKill(unittest.TestCase):
    """Two independent resolutions must agree before a tmux session dies.

    `session` is what the index resolved (join on pane_pid). `owner_tmux` is what
    `pi-tmux --live-owner` resolved through its own guard. They are different
    implementations on purpose: if they disagree, one join has drifted, and the
    cost of guessing is killing a live session.

    Chosen reading of U6's ambiguous fourth RED bullet: teardown refuses when the
    live owner of this sessionId is NOT the tmux session named on the entry.
    """

    def _ownership(self, reasons):
        return [r for r in reasons if r.startswith("ownership")]

    def test_no_session_to_kill_raises_no_ownership_question(self):
        """The 7 engrane entries have session=None. They must stay untouched."""
        e = session_entry(session=None, owner_tmux=None, alive=False)
        self.assertEqual(gate(e, worktree_exists=True, base=BASE), [])

    def test_agreement_allows_the_kill(self):
        e = session_entry(reported=True, owner_tmux="amber")
        self.assertEqual(gate(e, base=BASE), [])

    def test_a_mismatch_refuses(self):
        e = session_entry(reported=True, owner_tmux="something-else")
        r = self._ownership(gate(e, base=BASE))
        self.assertTrue(r, "a mismatched owner must refuse")
        self.assertIn("amber", r[0])
        self.assertIn("something-else", r[0])

    def test_an_unresolvable_owner_refuses(self):
        """Unknown is NOT agreement."""
        r = self._ownership(gate(session_entry(reported=True, owner_tmux=None),
                                 base=BASE))
        self.assertTrue(r)

    def test_an_absent_owner_field_refuses(self):
        """A caller that never asked the question must not be read as agreeing."""
        e = session_entry(reported=True)
        e.pop("owner_tmux")
        self.assertTrue(self._ownership(gate(e, base=BASE)))

    def test_an_owner_not_in_tmux_refuses(self):
        """pi-tmux reports the owner exists but is stranded outside tmux, so the
        tmux session named on the entry cannot be that owner."""
        self.assertTrue(self._ownership(gate(session_entry(reported=True,
                                                          owner_tmux=""), base=BASE)))

    def test_accept_silent_does_NOT_override_ownership(self):
        r = self._ownership(gate(session_entry(reported=True, owner_tmux="other"),
                                 accept_silent=True, base=BASE))
        self.assertTrue(r)

    def test_the_seven_engrane_entries_raise_no_ownership_reason(self):
        for e in engrane_entries():
            with self.subTest(name=e["name"]):
                self.assertEqual(self._ownership(gate(e, worktree_exists=True,
                                                      base=BASE)), [])


# ── provider registration ────────────────────────────────────────────────────
class TestProviders(unittest.TestCase):
    def test_providers_are_registered_in_a_list(self):
        self.assertIsInstance(PROVIDERS, (list, tuple))
        self.assertGreaterEqual(len(PROVIDERS), 2)

    def test_gate_is_the_concatenation_of_its_providers(self):
        e = session_entry()
        combined = [x for p in PROVIDERS for x in p(e, accept_silent=False)]
        self.assertEqual(gate(e, base=BASE), combined)

    def test_gate_does_not_mutate_the_entry(self):
        e = session_entry()
        before = json.dumps(e, sort_keys=True)
        gate(e, base=BASE)
        self.assertEqual(json.dumps(e, sort_keys=True), before)

    def test_gate_is_deterministic(self):
        e = session_entry(phase="tool:bash")
        self.assertEqual(gate(e, base=BASE), gate(e, base=BASE))


if __name__ == "__main__":
    unittest.main()
