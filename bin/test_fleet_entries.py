"""U4 — the fleet entry model.

PURE. Fixtures only. No tmux, no git, no filesystem beyond the frozen baseline
JSON (which is a fixture, not a reality source).

RED first. This file was written before `fleet_entries.py` existed, so every
test here failed with ModuleNotFoundError before the module was added.

The load-bearing test is `TestEngraneByteIdentical`. The 7 engrane entries are
the only entries `fleet reap` has ever acted on, and `reap` deletes a git branch
and a worktree. U4 rewrites discovery, so the guard is pinned BEFORE the change.
"""
import json
import os
import unittest
from pathlib import Path

from fleet_entries import build_entries
import fleet_entries  # noqa: E402  (the private liveness helper is part of the contract now)

FIXTURES = Path(__file__).parent / "fixtures"
BASELINE = FIXTURES / "engrane_baseline_20261011.json"

FIXTURES_MISSING = (
    f"the frozen baseline is not in this tree ({BASELINE}). It is a copy of a PRIVATE "
    "project's 7 entries (session names, branches, heads), so it is excluded from the "
    "shipped repo on purpose: it is another project's state, not an example. The "
    "byte-identity guard is therefore unarmed here; copy the fixture out of the private "
    "deployment's bin/fixtures/ (or point FLEET_GATE_FIXTURES at that directory) to arm "
    "it. Skipped, not passed: nothing has been verified."
)

_elsewhere = os.environ.get("FLEET_GATE_FIXTURES")
if _elsewhere:
    BASELINE = Path(_elsewhere) / BASELINE.name


class FixturesNeeded(unittest.TestCase):
    """Skip per test (not per class) so the count shows how many guards are unarmed."""

    def setUp(self):
        if not BASELINE.is_file():
            raise unittest.SkipTest(FIXTURES_MISSING)


def engrane_namer(path):
    """`derive_name` from fleet: strip the repo's own name prefix."""
    stem = os.path.basename(path)
    prefix = "engrane-"
    return stem[len(prefix):] if stem.startswith(prefix) else stem


def baseline_inputs():
    """Reconstruct build_entries' inputs from the frozen baseline output.

    With rows=[] there are no live sessions, so the output must be exactly the
    baseline -- which is what makes byte-identity a real assertion rather than a
    subset check.
    """
    base = json.loads(BASELINE.read_text())
    worktrees = [{"path": e["worktree"], "branch": e["branch"], "head": e["head"]}
                 for e in base]
    facts = {os.path.realpath(e["worktree"]):
             {k: e[k] for k in ("branch", "head", "dirty", "ahead", "unpushed", "merged")}
             for e in base}
    return worktrees, facts


def git_class_only(**kw):
    worktrees, facts = baseline_inputs()
    args = dict(rows=[], worktrees=worktrees, git_by_path=facts, status_by_session={},
                context_by_session={}, registry={}, base="origin/main",
                namer=engrane_namer)
    args.update(kw)
    return build_entries(**args)


_UNSET = object()


def row(session_id, name, cwd="/root", worktree=None, tmux=_UNSET, pid=None):
    return {"sessionId": session_id, "name": name, "cwd": cwd, "worktree": worktree,
            "tmux": name if tmux is _UNSET else tmux, "pid": pid, "phase": "idle"}


# ── the regression guard ─────────────────────────────────────────────────────
class TestEngraneByteIdentical(FixturesNeeded):
    """The 7 engrane entries must not move, gain keys, or change order."""

    def test_seven_engrane_entries_are_byte_identical(self):
        got = git_class_only()
        self.assertEqual(json.dumps(got, indent=2), BASELINE.read_text().rstrip("\n"))

    def test_they_round_trip_to_the_same_objects(self):
        self.assertEqual(json.loads(json.dumps(git_class_only())),
                         json.loads(BASELINE.read_text()))

    def test_sessionId_is_omitted_when_unknown(self):
        """The 7 fleets are dead: there is no session, so there is no sessionId.

        The file already omits facts that are None. Adding a null `sessionId`
        would break byte-identity and tell the reader nothing, so it is omitted
        for exactly the same reason `dirty` would be.
        """
        for e in git_class_only():
            with self.subTest(name=e["name"]):
                self.assertNotIn("sessionId", e)


# ── enumerate beyond worktrees (the point of U4) ─────────────────────────────
class TestEnumerateAll(unittest.TestCase):
    def test_a_session_with_no_worktree_becomes_an_entry(self):
        got = build_entries(rows=[row("sid-1", "nordisk-goal-helper-2")],
                            worktrees=[], git_by_path={}, status_by_session={},
                            context_by_session={}, registry={}, base="origin/main",
                            namer=engrane_namer)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["sessionId"], "sid-1")
        self.assertEqual(got[0]["name"], "nordisk-goal-helper-2")
        self.assertIsNone(got[0]["worktree"])

    def test_the_swarm_appears_in_full(self):
        rows = [row(f"sid-{i}", f"nordisk-goal-helper-{i}") for i in range(23)]
        got = build_entries(rows=rows, worktrees=[], git_by_path={},
                            status_by_session={}, context_by_session={}, registry={},
                            base="origin/main", namer=engrane_namer)
        self.assertEqual(len(got), 23)

    def test_a_worktree_backed_session_is_one_entry_not_two(self):
        """A live session inside a fleet worktree is the SAME entry, enriched.

        Two rows for one session is the duplicate-identity bug this whole
        protocol exists to remove.
        """
        wt = "/mnt/HC_Volume_105587324/projects/engrane-citations"
        got = build_entries(
            rows=[row("sid-c", "citations", cwd=wt, worktree=wt)],
            worktrees=[{"path": wt, "branch": "fix/citations", "head": "abc1234"}],
            git_by_path={os.path.realpath(wt): {"branch": "fix/citations",
                                                "head": "abc1234", "dirty": 0,
                                                "ahead": 0, "unpushed": 0,
                                                "merged": True}},
            status_by_session={}, context_by_session={}, registry={},
            base="origin/main", namer=engrane_namer)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["sessionId"], "sid-c")
        self.assertEqual(got[0]["branch"], "fix/citations")

    def test_alive_means_a_tmux_pane_exists(self):
        got = build_entries(rows=[row("sid-1", "amber"), row("sid-2", "orphan", tmux=None)],
                            worktrees=[], git_by_path={}, status_by_session={},
                            context_by_session={}, registry={}, base="origin/main",
                            namer=engrane_namer)
        by_id = {e["sessionId"]: e for e in got}
        self.assertTrue(by_id["sid-1"]["alive"])
        self.assertFalse(by_id["sid-2"]["alive"])

    def test_a_missing_pane_name_is_not_evidence_of_death(self):
        """Measured 2026-10-26: four live workers listed dead because their registry
        row never resolved `fleet-<name>` to the pane pi renamed. 'Reads dead' is what
        a reap proposal is made of, so /proc is allowed to overrule a missing pane."""
        live_pid = os.getpid()
        got = build_entries(rows=[row("sid-1", "amber", tmux=None, pid=live_pid)],
                            worktrees=[], git_by_path={}, status_by_session={},
                            context_by_session={}, registry={}, base="origin/main",
                            namer=engrane_namer)
        self.assertTrue(got[0]["alive"])

    def test_unknown_is_not_reported_as_alive(self):
        """No pane AND no pid: nothing to ask. Stays not-alive (callers may act on
        False), and `_liveness` names it 'unknown' rather than calling it dead."""
        got = build_entries(rows=[row("sid-1", "amber", tmux=None, pid=None)],
                            worktrees=[], git_by_path={}, status_by_session={},
                            context_by_session={}, registry={}, base="origin/main",
                            namer=engrane_namer)
        self.assertFalse(got[0]["alive"])
        self.assertEqual(fleet_entries._liveness(None, {"pid": None}), "unknown")


# ── the derived-attribute wiring the audit found broken ──────────────────────
class TestWiringGaps(unittest.TestCase):
    def _one(self, **kw):
        r = row("sid-1", "amber")
        r.update(kw)
        got = build_entries(rows=[r], worktrees=[], git_by_path={},
                            status_by_session={}, context_by_session={}, registry={},
                            base="origin/main", namer=engrane_namer)
        return got[0]

    def test_updated_at_is_carried_through(self):
        """Without this the recency sort in pi_view silently falls back to
        alphabetical -- a tiebreak that looks like it works and does not."""
        self.assertEqual(self._one(updated_at=1770000000000)["updated_at"], 1770000000000)

    def test_unpushed_is_carried_through(self):
        """`harvestable` reads this. Absent, it can never fire."""
        self.assertEqual(self._one(unpushed=3)["unpushed"], 3)

    def test_missing_updated_at_is_not_invented(self):
        e = self._one()
        self.assertTrue(e.get("updated_at") is None or "updated_at" not in e)

    def test_phase_is_carried_through(self):
        """The gate must be able to see a session is mid-tool."""
        self.assertEqual(self._one(phase="tool:bash")["phase"], "tool:bash")

    def test_pending_ask_is_carried_through(self):
        """A session blocked on a human must never be reaped."""
        self.assertEqual(
            self._one(pending_ask="running ask_user_question")["pending_ask"],
            "running ask_user_question")

    def test_missing_phase_and_ask_are_not_invented(self):
        e = self._one()
        self.assertIsNone(e.get("pending_ask"))

    def test_status_is_keyed_by_session_not_by_cwd(self):
        """48 of 50 broker sessions report cwd=/root.

        Keying status by cwd collapses nearly all of them into one record. It is
        the same class of bug as keying identity by name: it looks correct right
        up until two sessions share a directory, which on this box is 48 of them.
        """
        a = row("sid-a", "session-a", cwd="/root")
        b = row("sid-b", "session-b", cwd="/root")
        got = build_entries(
            rows=[a, b], worktrees=[], git_by_path={},
            status_by_session={"sid-a": {"status": "idle", "model": "m-a"},
                               "sid-b": {"status": "tool:bash", "model": "m-b"}},
            context_by_session={}, registry={}, base="origin/main",
            namer=engrane_namer)
        by_id = {e["sessionId"]: e for e in got}
        self.assertEqual(by_id["sid-a"]["pi_status"], "idle")
        self.assertEqual(by_id["sid-b"]["pi_status"], "tool:bash")
        self.assertEqual(by_id["sid-a"]["model"], "m-a")
        self.assertEqual(by_id["sid-b"]["model"], "m-b")


# ── shape and purity ─────────────────────────────────────────────────────────
class TestShapeAndPurity(FixturesNeeded):
    def test_registry_overrides_the_derived_name(self):
        wt, facts = baseline_inputs()
        target = wt[0]["path"]
        got = build_entries(rows=[], worktrees=wt, git_by_path=facts, status_by_session={},
                            context_by_session={},
                            registry={os.path.realpath(target): {"name": "renamed",
                                                                 "reported": True,
                                                                 "note": "all good"}},
                            base="origin/main", namer=engrane_namer)
        e = [x for x in got if x["worktree"] == target][0]
        self.assertEqual(e["name"], "renamed")
        self.assertTrue(e["reported"])
        self.assertEqual(e["note"], "all good")

    def test_registry_may_key_a_session_class_entry_by_sessionId(self):
        """`sessionId` is the primary key, so `fleet done <session>` has to persist against it.

        Keying the registry only by realpath(worktree) meant a session-class entry
        could never be marked reported at all -- and `fleet done` on one crashed,
        because realpath(None) is a TypeError.
        """
        r = row("sid-1", "amber")
        got = build_entries(rows=[r], worktrees=[], git_by_path={},
                            status_by_session={}, context_by_session={},
                            registry={"sid-1": {"name": "amber-renamed",
                                                "reported": True, "note": "done"}},
                            base="origin/main", namer=engrane_namer)
        self.assertEqual(got[0]["name"], "amber-renamed")
        self.assertTrue(got[0]["reported"])
        self.assertEqual(got[0]["note"], "done")

    def test_a_worktree_entry_still_reads_its_worktree_registry_key(self):
        """The existing realpath(worktree) keying must keep working."""
        wt, facts = baseline_inputs()
        target = wt[0]["path"]
        got = build_entries(rows=[], worktrees=wt, git_by_path=facts,
                            status_by_session={}, context_by_session={},
                            registry={os.path.realpath(target): {"name": "still-here",
                                                                 "reported": True}},
                            base="origin/main", namer=engrane_namer)
        e = [x for x in got if x["worktree"] == target][0]
        self.assertEqual(e["name"], "still-here")
        self.assertTrue(e["reported"])

    def test_inputs_are_never_mutated(self):
        rows = [row("sid-1", "amber")]
        worktrees, facts = baseline_inputs()
        before = json.dumps([rows, worktrees, facts], sort_keys=True)
        build_entries(rows=rows, worktrees=worktrees, git_by_path=facts,
                      status_by_session={}, context_by_session={}, registry={},
                      base="origin/main", namer=engrane_namer)
        self.assertEqual(json.dumps([rows, worktrees, facts], sort_keys=True), before)

    def test_output_is_deterministic(self):
        rows = [row(f"sid-{i}", f"session-{i}") for i in range(5)]
        a = build_entries(rows=rows, worktrees=[], git_by_path={}, status_by_session={},
                          context_by_session={}, registry={}, base="origin/main",
                          namer=engrane_namer)
        b = build_entries(rows=list(reversed(rows)), worktrees=[], git_by_path={},
                          status_by_session={}, context_by_session={}, registry={},
                          base="origin/main", namer=engrane_namer)
        self.assertEqual(json.dumps(a, sort_keys=True), json.dumps(b, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
