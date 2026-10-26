#!/usr/bin/env python3
"""U3 tests — grouped, sorted view.

Groups domain -> topic -> subtopic. Sorts state -> ctx -> recency.

The point of this unit: 23 sessions from ONE finished goal-swarm must collapse to
ONE line with a count, and the unscoped count must be visible as the health metric.
That is the "43 is too many" problem rendered as something you can act on.

PURE. Row fixtures only.
"""
import unittest

from pi_view import state_of, group_rows, render, summary

STATE_RANK = ["needs-you", "working", "exhausted", "harvestable", "idle"]


def row(sid, name="x", domain="acme", topic="", subtopic="", **kw):
    r = {"sessionId": sid, "name": name, "domain": domain, "topic": topic,
         "subtopic": subtopic, "phase": "idle", "ctx_pct": 10.0,
         "pending_ask": None, "unpushed": 0, "registry_topic": "x", "tmux": None}
    r.update(kw)
    return r


def swarm(n):
    return [row(f"s{i}", name=f"acme-goal-helper-{i}", domain="acme",
                topic="goal", subtopic="swarm.helper") for i in range(n)]


class TestStateVocabulary(unittest.TestCase):
    def test_pending_ask_is_needs_you(self):
        self.assertEqual(state_of(row("a", pending_ask="May I rotate the secret?")),
                         "needs-you")

    def test_tool_call_is_working(self):
        self.assertEqual(state_of(row("a", phase="tool:bash")), "working")

    def test_ctx_over_90_is_exhausted(self):
        self.assertEqual(state_of(row("a", ctx_pct=99.4)), "exhausted")
        self.assertEqual(state_of(row("a", ctx_pct=90.0)), "exhausted")

    def test_ctx_89_is_not_exhausted(self):
        self.assertEqual(state_of(row("a", ctx_pct=89.9)), "idle")

    def test_unpushed_is_harvestable(self):
        self.assertEqual(state_of(row("a", unpushed=2)), "harvestable")

    def test_otherwise_idle(self):
        self.assertEqual(state_of(row("a")), "idle")

    def test_precedence_needs_you_beats_everything(self):
        self.assertEqual(
            state_of(row("a", phase="tool:bash", ctx_pct=99.9, unpushed=5,
                         pending_ask="answer me")), "needs-you")

    def test_working_beats_exhausted(self):
        self.assertEqual(state_of(row("a", phase="tool:edit", ctx_pct=99.9)), "working")


class TestGrouping(unittest.TestCase):
    def test_groups_by_domain_topic_subtopic(self):
        rows = [row("a", domain="acme", topic="theme", subtopic="dup-create"),
                row("b", domain="acme", topic="theme", subtopic="dup-create"),
                row("c", domain="acme", topic="theme", subtopic="other"),
                row("d", domain="globex", topic="review", subtopic="")]
        g = group_rows(rows)
        self.assertEqual(len(g["acme"]["theme"]["dup-create"]), 2)
        self.assertEqual(len(g["acme"]["theme"]["other"]), 1)
        self.assertEqual(len(g["globex"]["review"][""]), 1)

    def test_the_23_swarm_collapse_to_one_group(self):
        g = group_rows(swarm(23))
        self.assertEqual(len(g["acme"]["goal"]["swarm.helper"]), 23)

    def test_unscoped_is_its_own_group(self):
        g = group_rows([row("a", domain="unscoped", topic="", subtopic="")])
        self.assertIn("unscoped", g)


class TestSorting(unittest.TestCase):
    def test_needs_you_sorts_first(self):
        rows = [row("s1", name="idle1"),
                row("s2", name="ask", pending_ask="q"),
                row("s3", name="idle2")]
        out = render(rows)
        self.assertLess(out.index("ask"), out.index("idle1"))
        self.assertLess(out.index("ask"), out.index("idle2"))

    def test_groups_sort_by_best_state_rank(self):
        """A domain holding a needs-you session sorts above an all-idle domain."""
        rows = [row("a", domain="engine", topic="t"),
                row("b", domain="askdom", topic="t", pending_ask="q")]
        out = render(rows)
        self.assertLess(out.index("askdom"), out.index("engine"))


class TestSummary(unittest.TestCase):
    def test_summary_reports_the_health_metric(self):
        rows = swarm(23) + [row("u1", domain="unscoped"), row("u2", domain="unscoped"),
                            row("u3", domain="unscoped")]
        s = summary(rows)
        self.assertEqual(s["total"], 26)
        self.assertEqual(s["unscoped"], 3)
        self.assertEqual(s["by_state"]["idle"], 26)

    def test_summary_counts_each_state(self):
        rows = [row("a", pending_ask="q"), row("b", phase="tool:bash"),
                row("c", ctx_pct=99.0), row("d", unpushed=1), row("e")]
        s = summary(rows)
        for st in STATE_RANK:
            self.assertEqual(s["by_state"][st], 1, st)

    def test_drift_is_reported_alongside_unscoped(self):
        """Spec: "The number of known-drifted sessions is itself a health metric
        and is reported alongside the `unscoped` count."""
        rows = [row("s1", name="amber", tmux="jasper-alder"),          # drifted
                row("s2", name="plover-wren", tmux="globex-rowan"),    # drifted
                row("s3", name="iris", tmux="iris"),                    # fine
                row("s4", name="marrow", domain="unscoped")]            # no pane
        s = summary(rows)
        self.assertEqual(s["drift"], 2)
        self.assertEqual(s["unscoped"], 1)

    def test_render_reports_drift_alongside_unscoped(self):
        out = render([row("s1", name="amber", tmux="jasper-alder")])
        self.assertIn("drift 1", out)


class TestRender(unittest.TestCase):
    def test_render_collapses_the_swarm_with_a_count(self):
        out = render(swarm(23))
        self.assertIn("23", out)
        # 23 rows must NOT produce 23 identical lines
        self.assertLess(out.count("acme-goal-helper-"), 23)

    def test_mixed_subtopic_swarm_collapses_to_ONE_group_with_23(self):
        """The REAL fleet shape, which the identical-fixture test above hides.

        Live reality: 13 swarm.helper + 9 swarm.lead + 1 bare `acme-goal`,
        all topic=goal. That is 23 sessions from ONE goal-swarm, and the done
        criterion is that they collapse to ONE group with a count -- not three.
        """
        rows = ([row(f"h{i}", name=f"acme-goal-helper-{i}", topic="goal",
                     subtopic="swarm.helper") for i in range(13)]
                + [row(f"l{i}", name=f"acme-goal-lead-{i}", topic="goal",
                       subtopic="swarm.lead") for i in range(9)]
                + [row("g", name="acme-goal", topic="goal", subtopic="")])
        self.assertEqual(len(rows), 23)
        out = render(rows)
        self.assertIn("goal  (23)", out,
                      "all 23 goal sessions must collapse to one counted group")

    def test_render_shows_unscoped_count(self):
        out = render([row("u", domain="unscoped")] * 17)
        self.assertIn("17", out)

    def test_needs_you_topic_sorts_above_idle_topics(self):
        """`needs-you` is sorted first, and that must hold at the topic level too -
        not only within a topic. Alphabetical topic order would bury it."""
        rows = [row("a", topic="idlework"),
                row("b", topic="urgent", pending_ask="q")]
        out = render(rows)
        self.assertLess(out.index("urgent"), out.index("idlework"))

    def test_render_is_deterministic(self):
        rows = swarm(5) + [row("z", domain="globex")]
        self.assertEqual(render(rows), render(rows))

    def test_render_empty(self):
        self.assertIsInstance(render([]), str)


class TestGoldenOutput(unittest.TestCase):
    """Spec: "Grouping and sorting -- pure, golden-output fixture.

    This fixture earned its keep immediately: it is what revealed that a topic
    holding a `needs-you` session was sorting by alphabet rather than by state.
    Keep it exact -- a golden test that tolerates drift is not a golden test.
    """

    ROWS = [
        row("s1", name="acme-main", topic="storefront", phase="tool:bash"),
        row("s2", name="alder", topic="theme", pending_ask="approve?"),
        row("s3", name="acme-goal-helper-1", topic="goal", subtopic="swarm.helper"),
        row("s4", name="acme-goal-helper-2", topic="goal", subtopic="swarm.helper"),
        row("s5", name="marrow", domain="unscoped"),
    ]

    GOLDEN = """\
pi-tree — 5 live sessions  (needs-you 1, unscoped 1, drift 0)

▸ acme  [4]  needs-you:1 working:1 idle:2
   ├─ theme  (1)
   │    ◆ alder
   ├─ storefront  (1)
   │    ▶ acme-main
   ├─ goal  (2)
   │  ├─ swarm.helper  (2)
   │  │    · acme-goal-helper-1
   │  │    · acme-goal-helper-2

▸ unscoped  [1]  idle:1
   ├─ -  (1)
   │    · marrow
"""

    def test_golden_output(self):
        self.assertEqual(render(self.ROWS), self.GOLDEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestAgeUnits(unittest.TestCase):
    """The auditor's objection, 2026-10-20: the live join carries `updated_at` in
    epoch MILLISECONDS, while `_age` assumed seconds. Every row rendered `0s`, and
    `pi-tree --sort=age` -- the verb U10's playbook advertises -- printed `0s`
    while real ages ran 5s..77.8h. Ordering happened to survive, which is exactly
    why `TestPlaybookVerbs` (idempotency only) never caught it: a wrong number that
    sorts correctly still reads as a working view.
    """

    def _age(self):
        from pi_view import _age
        return _age

    def test_epoch_milliseconds_render_a_real_age(self):
        import time
        age = self._age()
        self.assertEqual(age((time.time() - 3 * 86400) * 1000.0), "3d")

    def test_epoch_seconds_still_render(self):
        import time
        self.assertEqual(self._age()(time.time() - 2 * 3600), "2h")

    def test_a_garbage_timestamp_stays_unknown(self):
        age = self._age()
        self.assertEqual(age("not a number"), "unknown")
        self.assertEqual(age(None), "unknown")

    def test_the_age_view_prints_the_real_age_not_zero_seconds(self):
        """End to end over the verb, because the defect was in the verb's output."""
        import time
        from pi_view import render_flat
        ms = (time.time() - 77.8 * 3600) * 1000.0
        text = render_flat([row("s-old", name="old-one", state="idle", updated_at=ms)],
                           sort="age")
        self.assertNotIn("0s", text)
        self.assertIn("3d", text)
