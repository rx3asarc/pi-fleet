#!/usr/bin/env python3
"""U2 tests — the single index (the join).

THE ACCEPTANCE TEST FOR THE WHOLE PROTOCOL.

Four sources arrive in four NATIVE shapes and disagree about identity. This suite
pins the join that reconciles them.

THE CORE INSIGHT
  tmux knows a session by its NAME. pi knows it by sessionId and pid. Those names
  have drifted apart in 5 live cases, so every tool that joins on NAME is wrong.
  The join must be on PID -- pane_pid == the pi process's pid. Then the drifted
  name stops mattering: it becomes an attribute of the row, not its identity.

PURE. Native-shape fixtures only. No tmux, no git, no /proc, no filesystem.
"""

import unittest

from pi_index import build_index, bare_tmux_shells

# ── the 5 KNOWN-DRIFTED sessions, observed live on 2026-10-11 via pi-tmux ─────
# (tmux session name, pane_pid, pi name, sessionId)
DRIFTED = [
    ("jasper-alder",  2865745, "amber",                "01a11ade-2f99-7230-9c83-e649c4009090"),
    ("globex-rowan", 431941,  "plover-wren",          "01a116b1-3f3a-76dd-b584-7accd993086f"),
    ("cobalt-rowan",  2439092, "cobalt",               "01a11a38-1111-2222-3333-444455556666"),
    ("larch",         2522560, "acme-goal-helper-6", "01a11a63-1111-2222-3333-444455556666"),
    ("nimbus",        2573667, "acme-goal-helper-7", "01a11a75-1111-2222-3333-444455556666"),
]


def sources(panes=None, live=None, registry=None, fleet=None):
    """Native shapes, exactly as the four real readers would return them."""
    return {
        "panes": panes or [],
        "live": live or [],
        "registry": registry or [],
        "fleet": fleet or [],
    }


def drifted_sources(n: int):
    """Fixture for the first `n` drift cases."""
    panes, live, registry = [], [], []
    for tmux_name, pid, pi_name, sid in DRIFTED[:n]:
        panes.append({"session_name": tmux_name, "pane_pid": pid, "cwd": "/root"})
        live.append({"sessionId": sid, "pid": pid, "name": pi_name,
                     "cwd": "/root", "phase": "idle", "lastPrompt": "x"})
        registry.append({"name": pi_name, "sessionId": sid, "pid": pid,
                         "cwd": "/root", "topic": "x"})
    return sources(panes, live, registry)


class TestTheFiveDriftCases(unittest.TestCase):
    """Each drifted session must resolve to its PI identity, not its tmux name."""

    def test_each_drift_case(self):
        for tmux_name, pid, pi_name, sid in DRIFTED:
            with self.subTest(tmux=tmux_name, pi=pi_name):
                idx = build_index(drifted_sources(DRIFTED.index(
                    (tmux_name, pid, pi_name, sid)) + 1))
                row = next(r for r in idx if r["tmux"] == tmux_name)
                self.assertEqual(row["sessionId"], sid)
                self.assertEqual(row["name"], pi_name,
                                 "must resolve to the pi name, not the tmux name")
                self.assertEqual(row["tmux"], tmux_name,
                                 "the tmux name is retained as an attribute")

    def test_all_five_together(self):
        idx = build_index(drifted_sources(5))
        self.assertEqual(len(idx), 5)
        self.assertEqual({r["name"] for r in idx},
                         {"amber", "plover-wren", "cobalt",
                          "acme-goal-helper-6", "acme-goal-helper-7"})

    def test_name_is_never_the_join_key(self):
        """If two panes carry the SAME name but different pids, they stay distinct."""
        s = sources(
            panes=[{"session_name": "same", "pane_pid": 1, "cwd": "/root"},
                   {"session_name": "same", "pane_pid": 2, "cwd": "/root"}],
            live=[{"sessionId": "sid-A", "pid": 1, "name": "alpha", "cwd": "/root"},
                  {"sessionId": "sid-B", "pid": 2, "name": "beta", "cwd": "/root"}],
        )
        idx = build_index(s)
        self.assertEqual({r["sessionId"] for r in idx}, {"sid-A", "sid-B"})
        self.assertEqual({r["name"] for r in idx}, {"alpha", "beta"})


class TestExactlyOnce(unittest.TestCase):
    """Every session appears exactly once. No duplicates, no drops."""

    def test_no_duplicates_across_a_realistic_mixture(self):
        s = sources(
            panes=[{"session_name": "iris", "pane_pid": 10, "cwd": "/root"},
                   {"session_name": "marrow", "pane_pid": 11, "cwd": "/root"}],
            live=[{"sessionId": "s-1", "pid": 10, "name": "iris", "cwd": "/root"},
                  {"sessionId": "s-2", "pid": 11, "name": "marrow", "cwd": "/root"},
                  {"sessionId": "s-3", "pid": 12, "name": "gale", "cwd": "/root"}],
            registry=[{"name": "iris", "sessionId": "s-1", "pid": 10},
                      {"name": "marrow", "sessionId": "s-2", "pid": 11},
                      {"name": "gale", "sessionId": "s-3", "pid": 12}],
        )
        idx = build_index(s)
        sids = [r["sessionId"] for r in idx]
        self.assertEqual(sorted(sids), ["s-1", "s-2", "s-3"])
        self.assertEqual(len(sids), len(set(sids)), "sessionId must be unique")

    def test_session_with_no_pane_is_still_indexed(self):
        """A stranded session (client vanished) still has a durable record."""
        s = sources(live=[{"sessionId": "s-solo", "pid": 99, "name": "opal", "cwd": "/root"}])
        idx = build_index(s)
        self.assertEqual(len(idx), 1)
        self.assertEqual(idx[0]["sessionId"], "s-solo")
        self.assertIsNone(idx[0]["tmux"])

    def test_every_row_has_a_sessionId(self):
        idx = build_index(sources(
            panes=[{"session_name": "shell-only", "pane_pid": 77, "cwd": "/root"}],
            live=[{"sessionId": "s-1", "pid": 10, "name": "iris", "cwd": "/root"}],
        ))
        for row in idx:
            self.assertTrue(row.get("sessionId"), "sessionId present on every row")

    def test_bare_tmux_shells_are_excluded_from_the_index_and_counted(self):
        """A tmux session with no pi in it holds no identity, so it is not a row."""
        s = sources(
            panes=[{"session_name": "globex-review-main", "pane_pid": 2584284, "cwd": "/root"},
                   {"session_name": "hermes", "pane_pid": 3098670, "cwd": "/root"},
                   {"session_name": "iris", "pane_pid": 10, "cwd": "/root"}],
            live=[{"sessionId": "s-1", "pid": 10, "name": "iris", "cwd": "/root"}],
        )
        idx = build_index(s)
        self.assertEqual([r["sessionId"] for r in idx], ["s-1"])
        self.assertEqual(bare_tmux_shells(s), ["globex-review-main", "hermes"])


class TestAttributes(unittest.TestCase):
    """Everything known about a session hangs off its row."""

    def test_registry_supplies_name_and_topic(self):
        s = sources(
            panes=[{"session_name": "marrow", "pane_pid": 10, "cwd": "/root"}],
            live=[{"sessionId": "s-1", "pid": 10, "name": "marrow", "cwd": "/root"}],
            registry=[{"name": "marrow", "sessionId": "s-1", "pid": 10,
                       "topic": "continue"}],
        )
        r = build_index(s)[0]
        self.assertEqual(r["name"], "marrow")
        self.assertEqual(r["registry_topic"], "continue")

    def test_fleet_supplies_worktree_and_branch(self):
        s = sources(
            live=[{"sessionId": "s-1", "pid": 10, "name": "basalt",
                   "cwd": "/mnt/fleet-volume/projects/globex-citations"}],
            fleet=[{"worktree": "/mnt/fleet-volume/projects/globex-citations",
                    "branch": "fix/citations", "merged": True}],
        )
        r = build_index(s)[0]
        self.assertEqual(r["worktree"], "/mnt/fleet-volume/projects/globex-citations")
        self.assertEqual(r["branch"], "fix/citations")

    def test_pane_cwd_is_used_when_live_cwd_is_missing(self):
        s = sources(
            panes=[{"session_name": "x", "pane_pid": 10, "cwd": "/somewhere"}],
            live=[{"sessionId": "s-1", "pid": 10, "name": "x"}],
        )
        self.assertEqual(build_index(s)[0]["cwd"], "/somewhere")


class TestDerivedAttributes(unittest.TestCase):
    """Facts the join derives from the live record rather than looking up."""

    def _one(self, **record):
        base = {"sessionId": "s1", "pid": 10, "name": "x", "cwd": "/root"}
        base.update(record)
        return build_index(sources(live=[base]))[0]

    def test_a_pending_ask_is_needs_you(self):
        """An in-flight `ask_user_question` is a session blocked on a human."""
        self.assertTrue(self._one(detail="running ask_user_question")["pending_ask"])

    def test_waiting_for_input_is_NOT_a_pending_ask(self):
        """`waiting for input` is the NORMAL idle state -- every turn ends by
        waiting for input. Counting it marked 45 of 48 live sessions as
        needs-you, which reads the same as marking none.

        A fixture could not have told me this: the first version of this test
        asserted the opposite, and passed.
        """
        self.assertFalse(self._one(detail="waiting for input")["pending_ask"])

    def test_running_a_tool_is_not_a_pending_ask(self):
        for detail in ("running bash", "running edit", ""):
            with self.subTest(detail=detail):
                self.assertFalse(self._one(detail=detail)["pending_ask"])

    def test_detail_is_preserved_as_an_attribute(self):
        self.assertEqual(self._one(detail="running bash")["detail"], "running bash")

    def test_ctx_pct_is_none_not_a_guess(self):
        """ctx% comes from the in-process extension API and is NOT available to any
        external reader. Reporting None is honest; the view must not pretend."""
        self.assertIsNone(self._one()["ctx_pct"])

    def test_updated_at_is_carried_through(self):
        """`pi_view` sorts by recency *within* a state group, reading this key.

        The key never existed on a row, so the tiebreak silently fell through to
        alphabetical -- a sort that looks like it works and does not. Found by an
        independent audit of the U1-U3 build, not by any test here.
        """
        self.assertEqual(self._one(updatedAt=1791711850865)["updated_at"], 1791711850865)

    def test_missing_updated_at_is_none_not_zero(self):
        self.assertIsNone(self._one()["updated_at"])

    def test_fleet_supplies_unpushed(self):
        """`harvestable` reads this. Absent, the state can never fire."""
        got = build_index(sources(
            live=[{"sessionId": "s1", "pid": 1, "name": "x", "cwd": "/wt"}],
            fleet=[{"worktree": "/wt", "branch": "b", "merged": False,
                    "unpushed": 3}]))
        self.assertEqual(got[0]["unpushed"], 3)

    def test_missing_unpushed_is_none_not_zero(self):
        """None means unknown; 0 means 'nothing left to harvest'.

        Collapsing those two would let the view claim a session is harvested when
        it has no worktree at all and nobody has ever looked.
        """
        self.assertIsNone(self._one()["unpushed"])


class TestDeterminism(unittest.TestCase):
    """Running it twice produces identical output (idempotence)."""

    def test_stable_ordering(self):
        a = build_index(drifted_sources(5))
        b = build_index(drifted_sources(5))
        self.assertEqual(a, b)

    def test_source_order_does_not_matter(self):
        s = drifted_sources(3)
        shuffled = sources(panes=list(reversed(s["panes"])),
                           live=list(reversed(s["live"])),
                           registry=list(reversed(s["registry"])))
        self.assertEqual(build_index(s), build_index(shuffled))


class TestPurity(unittest.TestCase):
    def test_does_not_mutate_inputs(self):
        s = drifted_sources(2)
        snapshot = {k: [dict(d) for d in v] for k, v in s.items()}
        build_index(s)
        self.assertEqual(s, snapshot)

    def test_empty_sources(self):
        self.assertEqual(build_index(sources()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestContextPercentageComesFromTheFooter(unittest.TestCase):
    """The auditor's objection, 2026-10-20: U7's deliverable ("this is where
    ctx_pct / session-state can finally be recorded") was called unimplementable
    because "no external reader can reach ctx.getContextUsage()". That was false:
    pi prints `39.8%/262k` in the tmux footer and `fleet_discovery.context_pct()`
    was already parsing it. The state `exhausted` (context >= 90%) was therefore
    unarmed for a reason that did not exist.
    """

    def test_a_pane_footer_yields_its_percentage(self):
        from pi_sources import parse_footer_pct
        self.assertEqual(parse_footer_pct("some output\n 39.8%/262k  ..."), 39.8)
        self.assertEqual(parse_footer_pct("12%/1M"), 12.0)

    def test_an_unparseable_footer_is_none_not_zero(self):
        from pi_sources import parse_footer_pct
        self.assertIsNone(parse_footer_pct("no footer here"))
        self.assertIsNone(parse_footer_pct(""))

    def test_the_rows_carry_the_measured_percentage(self):
        s = sources(
            panes=[{"session_name": "alpha", "pane_pid": 1, "cwd": "/root"}],
            live=[{"sessionId": "sid-A", "pid": 1, "name": "alpha", "cwd": "/root"}])
        s["ctx_pct"] = {"alpha": 39.8}
        idx = build_index(s)
        self.assertEqual(idx[0]["ctx_pct"], 39.8)

    def test_a_session_with_no_measurable_footer_stays_none(self):
        s = sources(live=[{"sessionId": "sid-A", "pid": 1, "name": "alpha", "cwd": "/root"}])
        s["ctx_pct"] = {}
        self.assertIsNone(build_index(s)[0]["ctx_pct"])

    def test_exhausted_fires_at_ninety_and_not_before(self):
        from pi_view import state_of
        self.assertEqual(state_of({"ctx_pct": 90.0, "phase": "idle"}), "exhausted")
        self.assertEqual(state_of({"ctx_pct": 89.9, "phase": "idle"}), "idle")


class TestTheDeclarationReachesTheRow(unittest.TestCase):
    """The chain the auditor found missing: spawn env -> registry record -> row ->
    resolver. Each link is trivial; the chain being absent is what kept 20 of 34
    sessions unscoped."""

    def test_the_index_carries_the_stored_taxonomy(self):
        s = sources(
            live=[{"sessionId": "sid-A", "pid": 1, "name": "alpha", "cwd": "/root"}],
            registry=[{"sessionId": "sid-A", "pid": 1, "name": "alpha",
                       "taxonomy": "acme/ads/pixel"}])
        self.assertEqual(build_index(s)[0]["taxonomy"], "acme/ads/pixel")

    def test_a_record_with_no_declaration_carries_none(self):
        s = sources(
            live=[{"sessionId": "sid-A", "pid": 1, "name": "alpha", "cwd": "/root"}],
            registry=[{"sessionId": "sid-A", "pid": 1, "name": "alpha"}])
        self.assertIsNone(build_index(s)[0]["taxonomy"])

    def test_the_row_resolver_uses_the_declaration(self):
        from pi_rows import build_rows
        s = sources(
            panes=[{"session_name": "alpha", "pane_pid": 1, "cwd": "/root"}],
            live=[{"sessionId": "sid-A", "pid": 1, "name": "alpha", "cwd": "/root"}],
            registry=[{"sessionId": "sid-A", "pid": 1, "name": "alpha",
                       "taxonomy": "globex/media/factcheck"}])
        row = build_rows(s, first_messages={})[0]
        self.assertEqual((row["domain"], row["topic"], row["subtopic"]),
                         ("globex", "media", "factcheck"))
        self.assertEqual(row["source"], "declared")
