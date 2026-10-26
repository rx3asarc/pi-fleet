"""U11 (impure half) — fleet_children: configure, safe_close, digest, ledger, cascade.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_children -v
(no pytest on this box — unittest only)

WHAT THIS FILE IS FOR
`fleet_children` is the half of spawn lineage that touches tmux, /proc, the
lineages store and the digest directory. It is kept testable by one design
decision: every impure dependency arrives through `configure(**deps)`. So the
tests inject fakes and assert the CALL SHAPE — argv arrays, call order, and what
lands on disk in a temp dir. No tmux, no git, no /proc, no network, no sleeps.

THE BUG THAT MATTERS MOST (measured 2026-10-17)
`pi-tmux --close <session>` resolves its victim with `list-panes | head -1`. For a
fork that shares its parent's tmux session, head -1 is the PARENT, so a close of
`dune-fork-1` would have torn down the parent pane. Every assertion in
TestSafeClose exists to pin the replacement: resolve the pane whose `pane_pid`
equals the owner pid, and `kill-pane` that pane only.
"""
import contextlib
import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fleet_children
import fleet_lineage

PID_START = 777
NOW = 1792242000                       # 2026-10-17T13:00:00Z
SID = "01a1dead-beef-7000-9000-000000000001"
PARENT_SID = "01a10000-aaaa-7000-9000-0000000000aa"
DEPS = ("entries", "find", "sh", "owner_tmux_of", "die", "clip", "base")
SHIMS = ("sh", "entries", "find", "owner_tmux_of", "clip", "die", "_base")

LIST_PANES_FMT = "#{window_index}.#{pane_index} #{pane_pid}"


# ── fixtures ─────────────────────────────────────────────────────────────────
def good_deps(**over):
    """A full, inert dependency set. Inert = nothing here can touch the machine."""
    d = {
        "entries": lambda: [],
        "find": lambda name: None,
        "sh": lambda argv, **kw: (1, "", "not implemented in this fixture"),
        "owner_tmux_of": lambda sid: "",
        "die": lambda msg: (_ for _ in ()).throw(SystemExit(msg)),
        "clip": lambda s, n: str(s or "")[:n],
        "spawn_reviewer": lambda c, n: None,
        "base": "origin/main",
    }
    d.update(over)
    return d


def owner_out(pid, tmux):
    """The `pi-tmux --live-owner` tab layout: field 0 is pid, field 5 is tmux."""
    return (0, "\t".join([str(pid), "-", "-", "-", "-", tmux]) + "\t", "")


def child(**kw):
    c = fleet_lineage.new_child(SID, "globex-citations", PARENT_SID,
                               "globex-main", "audit the citations appendix",
                               NOW, transcript=None)
    c.update(kw)
    return c


def facts(**kw):
    base = {"alive": False, "busy": False, "pending_ask": None,
            "parent_alive": False, "parent_finished": True, "last_seen": NOW - 120}
    base.update(kw)
    return base


def args(**kw):
    a = SimpleNamespace(apply=False, accept_silent=False, idle_after_min=30)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


@contextlib.contextmanager
def quiet():
    """_cascade prints its notes; capture them instead of spamming the run."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        yield buf


class World:
    """The injected world: records every subprocess argv, answers by prefix."""

    def __init__(self):
        self.calls = []
        self.live_entries = []
        self.records = []
        self.owner = {}
        self.owner_calls = []
        self.died = []
        self.answer = {}               # "pi-tmux --live-owner" -> (rc, out, err)
        self.base = "origin/main"

    # deps
    def sh(self, argv, **kw):
        self.calls.append(list(argv))
        key = " ".join(argv[:2])
        return self.answer.get(key, (0, "", ""))

    def entries(self):
        return self.live_entries

    def find(self, name):
        return next((e for e in self.live_entries if e.get("name") == name), None)

    def owner_tmux_of(self, sid):
        self.owner_calls.append(sid)
        return self.owner.get(sid, "")

    def die(self, msg):
        self.died.append(msg)
        raise SystemExit(msg)

    @staticmethod
    def clip(s, n):
        return str(s or "")[:n]

    def argv_matching(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


class TempStores(unittest.TestCase):
    """Temp dirs for LINEAGE / DIGESTS / LEDGER / SENTINEL_REPORT.

    The module reads those four as module-level Paths (defaults under
    /root/.fleet and /root/handoffs/_reaped, env-overridable as FLEET_*). They
    are patched here so no test can ever touch the real stores; create=True keeps
    the patch working whether or not a given build of the module declares them.
    Nothing is written outside the temp dir.
    """

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)          # registered first => runs last
        self.root = Path(td.name)
        self.lineage_path = self.root / "lineage.json"
        self.digests = self.root / "digests"
        self.ledger = self.root / "ledger.jsonl"
        self.sentinel = self.root / "sentinel-report.json"
        for name, path in (("LINEAGE", self.lineage_path), ("DIGESTS", self.digests),
                           ("LEDGER", self.ledger),
                           ("SENTINEL_REPORT", self.sentinel)):
            p = mock.patch.object(fleet_children, name, path, create=True)
            p.start()
            self.addCleanup(p.stop)

    # -- helpers ------------------------------------------------------------
    def patch_deps(self, deps):
        p = mock.patch.object(fleet_children, "_D", {})
        p.start()
        self.addCleanup(p.stop)
        fleet_children.configure(**deps)

    def patch_attr(self, name, value):
        p = mock.patch.object(fleet_children, name, value)
        p.start()
        self.addCleanup(p.stop)


# ── 1. configure() and the unconfigured shims ────────────────────────────────
class TestConfigure(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(fleet_children, "_D", {})
        p.start()
        self.addCleanup(p.stop)

    def test_configure_refuses_every_missing_dep_by_name(self):
        # A half-configured module must fail loudly, not guess. All seven.
        for missing in DEPS:
            deps = good_deps()
            deps.pop(missing)
            with self.subTest(missing=missing):
                with self.assertRaises(SystemExit) as ctx:
                    fleet_children.configure(**deps)
                self.assertIn(missing, str(ctx.exception))
                self.assertIn("missing", str(ctx.exception))

    def test_configure_refuses_when_nothing_is_supplied(self):
        with self.assertRaises(SystemExit) as ctx:
            fleet_children.configure()
        self.assertIn("entries", str(ctx.exception))

    def test_configure_accepts_the_full_set_and_binds_it(self):
        world = World()
        fleet_children.configure(**good_deps(entries=world.entries,
                                             sh=world.sh,
                                             owner_tmux_of=world.owner_tmux_of))
        self.assertEqual(fleet_children.entries(), [])
        self.assertEqual(fleet_children.sh(["pi-tmux", "--live-owner", "x"]),
                         (0, "", ""))
        self.assertEqual(fleet_children.owner_tmux_of("x"), "")
        self.assertEqual(fleet_children._base(), "origin/main")
        self.assertEqual(fleet_children.clip("abcdef", 3), "abc")

    def test_every_shim_raises_before_configure(self):
        calls = {"sh": (["pi-tmux", "--live-owner", "x"],), "entries": (),
                 "find": ("someone",), "owner_tmux_of": ("sid",),
                 "clip": ("x", 1), "die": ("boom",), "_base": ()}
        self.assertEqual(set(calls), set(SHIMS))
        for name, call_args in calls.items():
            with self.subTest(shim=name):
                with self.assertRaises(SystemExit) as ctx:
                    getattr(fleet_children, name)(*call_args)
                self.assertIn("configure", str(ctx.exception))

    def test_die_shim_forwards_after_configure(self):
        world = World()
        fleet_children.configure(**good_deps(die=world.die))
        with self.assertRaises(SystemExit):
            fleet_children.die("no such session")
        self.assertEqual(world.died, ["no such session"])


# ── 2. safe_close(): never the neighbour ─────────────────────────────────────
class TestSafeClose(unittest.TestCase):
    TMUX = "dune-fork-1"

    def setUp(self):
        p = mock.patch.object(fleet_children, "_D", {})
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(fleet_children, "_proc_state", lambda pid: "gone")
        p.start()
        self.addCleanup(p.stop)
        self.world = World()
        self.world.answer["pi-tmux --live-owner"] = owner_out(222, self.TMUX)
        self.world.answer["tmux list-panes"] = (0, "0.0 111\n0.1 222\n", "")
        self.world.answer["tmux kill-pane"] = (0, "", "")
        self.world.answer["pi-tmux --close"] = (0, "", "")
        fleet_children.configure(**good_deps(sh=self.world.sh,
                                             owner_tmux_of=self.world.owner_tmux_of))

    def close(self, tmux_session=None, wait_s=0.0):
        with quiet() as out:
            ok, how = fleet_children.safe_close(
                SID, self.TMUX if tmux_session is None else tmux_session,
                wait_s=wait_s)
        return ok, how, out.getvalue()

    def test_multi_pane_kills_the_pane_that_owns_the_pid(self):
        ok, how, _ = self.close()
        self.assertTrue(ok)
        self.assertEqual(how, "kill-pane %s:0.1" % self.TMUX)
        # (a) the pane list is asked for the OWNER session, in the documented shape
        self.assertIn(["tmux", "list-panes", "-t", self.TMUX, "-F", LIST_PANES_FMT],
                      self.world.calls)
        # (b) the exact pane whose pane_pid == 222, not the head of the list
        self.assertEqual(self.world.argv_matching("tmux", "kill-pane"),
                         [["tmux", "kill-pane", "-t", "%s:0.1" % self.TMUX]])

    def test_measured_2026_10_17_bug_pi_tmux_close_is_never_used_multi_pane(self):
        # dune:0.1 / acme-ads:0.1 — pi-tmux --close here resolved to the
        # PARENT pane. This is the regression assertion for the whole module.
        self.close()
        self.assertEqual(self.world.argv_matching("pi-tmux", "--close"), [])

    def test_single_pane_session_uses_pi_tmux_close(self):
        self.world.answer["tmux list-panes"] = (0, "0.0 222\n", "")
        ok, how, _ = self.close()
        self.assertTrue(ok)
        self.assertEqual(how, "pi-tmux --close %s" % self.TMUX)
        self.assertEqual(self.world.argv_matching("pi-tmux", "--close"),
                         [["pi-tmux", "--close", self.TMUX]])
        self.assertEqual(self.world.argv_matching("tmux", "kill-pane"), [])

    def test_single_pane_that_does_not_own_the_pid_is_REFUSED(self):
        """INVERTED 2026-10-17 (F7). This test used to pin the opposite.

        It asserted that one pane plus a stranger's pid still closes by session
        name -- "there is no neighbour to protect". That reasoning was wrong: a
        single pane whose pid disagrees with the owner pid is not a neighbour
        problem, it is the wrong process, and pi-tmux --close resolves whatever
        is there. Liveness got identity (I14); a kill needs the same rigour, so
        the pin now asserts a refusal. Do not "fix" this back to True.
        """
        self.world.answer["pi-tmux --live-owner"] = owner_out(999, self.TMUX)
        self.world.answer["tmux list-panes"] = (0, "0.0 222\n", "")
        ok, how, _ = self.close()
        self.assertFalse(ok, "a pane owned by a stranger was closed")
        self.assertIn("is not the owner pid", how)
        self.assertEqual(self.world.argv_matching("pi-tmux", "--close"), [])
        self.assertEqual(self.world.argv_matching("tmux", "kill-pane"), [])

    def test_owner_session_from_pi_tmux_wins_over_the_argument(self):
        ok, _, _ = self.close(tmux_session="somewhere-else")
        self.assertTrue(ok)
        self.assertIn(["tmux", "list-panes", "-t", self.TMUX, "-F", LIST_PANES_FMT],
                      self.world.calls)
        self.assertEqual(self.world.argv_matching("tmux", "list-panes", "-t",
                                                  "somewhere-else"), [])

    def test_no_owning_pi_closes_nothing(self):
        self.world.answer["pi-tmux --live-owner"] = (1, "", "no owner")
        ok, how, _ = self.close()
        self.assertFalse(ok)
        self.assertIn("no live pi", how)
        self.assertEqual(self.world.argv_matching("tmux"), [])
        self.assertEqual(self.world.argv_matching("pi-tmux", "--close"), [])

    def test_stranded_owner_without_tmux_closes_nothing(self):
        self.world.answer["pi-tmux --live-owner"] = owner_out(222, "")
        ok, how, _ = self.close(tmux_session="")
        self.assertFalse(ok)
        self.assertIn("stranded", how)
        self.assertEqual(self.world.argv_matching("tmux"), [])

    def test_kill_pane_failure_is_reported_not_swallowed(self):
        self.world.answer["tmux kill-pane"] = (1, "", "can't find pane")
        ok, how, _ = self.close()
        self.assertFalse(ok)
        self.assertIn("can't find pane", how)

    def test_pi_tmux_close_failure_is_reported(self):
        self.world.answer["tmux list-panes"] = (0, "0.0 222\n", "")
        self.world.answer["pi-tmux --close"] = (1, "", "session not found")
        ok, how, _ = self.close()
        self.assertFalse(ok)
        self.assertIn("session not found", how)

    def test_list_panes_failure_is_not_treated_as_multi_pane(self):
        # A failed pane listing yields no panes, so there is no target: the
        # single-pane fallback runs rather than guessing at a pane index.
        self.world.answer["tmux list-panes"] = (1, "", "no server running")
        ok, _, _ = self.close()
        self.assertTrue(ok)
        self.assertEqual(self.world.argv_matching("tmux", "kill-pane"), [])

    def test_zombie_pid_counts_as_closed(self):
        self.addCleanup(mock.patch.stopall)
        self._proc_state = mock.patch.object(fleet_children, "_proc_state",
                                             lambda pid: "Z")
        self._proc_state.start()
        ok, how, _ = self.close()
        self.assertTrue(ok)
        self.assertIn("zombie", how)

    def test_pid_still_alive_after_the_wait_is_a_failure(self):
        with mock.patch.object(fleet_children, "_proc_state", lambda pid: "S"):
            ok, how, _ = self.close(wait_s=0.0)      # wait 0 => no sleep at all
        self.assertFalse(ok)
        self.assertIn("still alive", how)

    def test_unknown_pid_in_a_multi_pane_session_refuses(self):
        # The owner pid is in none of a multi-pane session's panes, so the pane
        # cannot be identified. Falling through to `pi-tmux --close` would
        # resolve pane 1 and kill a neighbour — the exact bug this function
        # exists to avoid, reached by a second route. It must refuse instead.
        self.world.answer["pi-tmux --live-owner"] = owner_out(999, self.TMUX)
        ok, how, _ = self.close()
        self.assertFalse(ok)
        self.assertIn("cannot identify", how)
        self.assertIn("2 panes", how)
        self.assertIn("refusing to guess", how)
        self.assertEqual(self.world.argv_matching("pi-tmux", "--close"), [])
        self.assertEqual(self.world.argv_matching("tmux", "kill-pane"), [])
        # it did measure before refusing
        self.assertIn(["tmux", "list-panes", "-t", self.TMUX, "-F", LIST_PANES_FMT],
                      self.world.calls)


# ── 3. _write_digest(): written once, path stable ────────────────────────────
class TestWriteDigest(TempStores):
    def setUp(self):
        super().setUp()
        self.patch_deps(good_deps())

    def test_first_write_creates_one_deterministic_file(self):
        path = fleet_children._write_digest(child(), facts(), {}, NOW)
        self.assertTrue(Path(path).exists())
        self.assertEqual(Path(path).parent, self.digests)
        iso = time.strftime("%Y-%m-%d", time.gmtime(NOW))
        self.assertEqual(Path(path).name, f"{iso}-globex-citations-01a1dead.md")
        md = Path(path).read_text()
        self.assertIn("globex-citations", md)
        self.assertIn(SID, md)
        self.assertIn("audit the citations appendix", md)

    def test_second_call_with_different_inputs_does_not_rewrite(self):
        c = child()
        first = fleet_children._write_digest(c, facts(), {}, NOW)
        before = Path(first).read_bytes()
        c["digest"] = first                       # _cascade records it this way
        second = fleet_children._write_digest(
            c, facts(alive=True, busy=True), {"session": "other"}, NOW + 86400)
        self.assertEqual(first, second)
        self.assertEqual(Path(second).read_bytes(), before)
        self.assertEqual(len(list(self.digests.iterdir())), 1)

    def test_third_call_with_the_empty_entry_still_does_not_rewrite(self):
        c = child()
        c["digest"] = fleet_children._write_digest(c, facts(), {}, NOW)
        before = Path(c["digest"]).read_bytes()
        for _ in range(3):
            again = fleet_children._write_digest(c, facts(**{"last_seen": None}),
                                                 {}, NOW)
            self.assertEqual(again, c["digest"])
        self.assertEqual(Path(c["digest"]).read_bytes(), before)

    def test_a_recorded_but_missing_digest_is_recomputed_not_crashed(self):
        c = child(digest=str(self.root / "gone.md"))
        path = fleet_children._write_digest(c, facts(), {}, NOW)
        self.assertTrue(Path(path).exists())

    def test_name_slashes_cannot_escape_the_digest_dir(self):
        c = child(name="globex/design")
        path = fleet_children._write_digest(c, facts(), {}, NOW)
        self.assertEqual(Path(path).parent, self.digests)
        self.assertNotIn("/", Path(path).name)

    def test_nameless_child_falls_back_to_its_session_prefix(self):
        c = child(name=None)
        path = fleet_children._write_digest(c, facts(), {}, NOW)
        self.assertIn("01a1dead", Path(path).name)


# ── 4. _append_ledger(): deduped, append-only ────────────────────────────────
class TestAppendLedger(TempStores):
    def setUp(self):
        super().setUp()
        self.patch_deps(good_deps())

    def test_identical_line_is_deduped(self):
        line = '{"action": "reap", "sessionId": "x"}'
        self.assertTrue(fleet_children._append_ledger(line))
        self.assertFalse(fleet_children._append_ledger(line))
        self.assertEqual(self.ledger.read_text().splitlines(), [line])

    def test_a_different_line_is_appended(self):
        a, b = '{"action": "reap", "sessionId": "a"}', '{"action": "reap", "sessionId": "b"}'
        fleet_children._append_ledger(a)
        fleet_children._append_ledger(b)
        self.assertEqual(self.ledger.read_text().splitlines(), [a, b])

    def test_parent_directory_is_created(self):
        nested = self.root / "not-yet" / "ledger.jsonl"
        self.assertFalse(nested.parent.exists())
        with mock.patch.object(fleet_children, "LEDGER", nested):
            fleet_children._append_ledger("first")
        self.assertTrue(nested.exists())
        self.assertEqual(nested.read_text(), "first\n")

    def test_ledger_line_from_the_pure_half_roundtrips(self):
        c = dict(child(reaped_at=NOW, why="reported its unit", digest="/x/y.md"))
        line = fleet_lineage.ledger_line(c, "reap", NOW)
        self.assertTrue(fleet_children._append_ledger(line))
        self.assertFalse(fleet_children._append_ledger(line))
        row = json.loads(self.ledger.read_text().splitlines()[0])
        self.assertEqual(row["sessionId"], SID)
        self.assertEqual(row["action"], "reap")
        self.assertEqual(row["digest"], "/x/y.md")


# ── 5. _cascade(): propose, gate, digest, close ──────────────────────────────
class TestCascade(TempStores):
    def setUp(self):
        super().setUp()
        self.patch_proc()          # liveness is identity; no real /proc here
        self.world = World()
        self.world.owner[SID] = "dune-fork-1"
        self.patch_deps(good_deps(entries=self.world.entries, find=self.world.find,
                                  sh=self.world.sh, die=self.world.die,
                                  owner_tmux_of=self.world.owner_tmux_of,
                                  clip=self.world.clip, base=self.world.base))
        self.gate_calls = []
        self.blockers = []
        self.patch_gate()
        self.patch_attr("pi_sources", SimpleNamespace(
            live_records=lambda live_only=False: self.world.records))

    def patch_gate(self):
        def gate(entry, worktree_exists=False, accept_silent=False,
                 base="origin/main"):
            self.gate_calls.append(dict(entry))
            return list(self.blockers)
        self.patch_attr("fleet_gate", SimpleNamespace(gate=gate))

    # -- fixtures ----------------------------------------------------------
    def seed(self, c, alive=False, reported=True, session="dune-fork-1",
             last_seen=NOW - 120):
        """Write a lineage store with one child and matching live records.

        Liveness is now IDENTITY (pid + start time), so a fixture that wants a
        live child must also carry the identity pair -- otherwise the cascade
        correctly refuses to call it alive. `_proc_identity` is patched below so
        these tests never read the real /proc.
        """
        if alive:
            c = dict(c, pid=4242, pid_start=PID_START)
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": {c["sessionId"]: c}}, indent=2)
            + "\n")
        self.world.live_entries = [] if not alive else [{
            "sessionId": c["sessionId"], "name": c.get("name"), "alive": alive,
            "pid": 4242,
            "reported": reported, "session": session, "pi_status": "", "phase": "",
            "pending_ask": None, "context": "58%", "branch": None, "head": None,
            "merged": False, "worktree": None,
        }]
        self.world.records = [{"sessionId": c["sessionId"],
                               "updatedAt": last_seen * 1000}]
        return c

    def patch_proc(self):
        """No real /proc in a unit test: identity is the identity suite's job."""
        self.patch_attr("_proc_identity",
                        lambda pid: ("ok", "S", PID_START) if pid == 4242
                        else ("missing", None, None))

    def reapable(self, alive=True):
        """A reported child with a live pane: the only case that needs a kill."""
        return self.seed(fleet_lineage.mark_reported(child(), "audited 41 citations",
                                                    NOW - 60), alive=alive)

    def _reload(self):
        return json.loads(self.lineage_path.read_text())["children"][SID]

    def _save(self, c):
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": {SID: c}}, indent=2) + "\n")

    def test_reap_children_means_the_children_not_the_lead(self):
        """`fleet reap-children --of X` used to pass X as `only_sid`, so it walked the
        LEAD and gated on the lead's own merge status. Measured 2026-10-26: a
        report-kind lead with three dead throwaways produced "GATED not merged" and its
        children were never reached."""
        parent = fleet_lineage.new_child(SID, "lead-1", None, None, "lead unit", NOW)
        kid = fleet_lineage.new_child(
            "01a1dead-beef-7000-9000-000000000002", "dead-throwaway", SID, "lead-1",
            "rehearsal", NOW)
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": {SID: parent, kid["sessionId"]: kid}},
                       indent=2) + "\n")
        self.world.live_entries = []
        self.world.records = []
        with quiet() as out:
            fleet_children._cascade(args(apply=False), only_parent=SID)
        text = out.getvalue()
        after = json.loads(self.lineage_path.read_text())["children"]
        self.assertIsNone(after[SID].get("verdict"),
                          "the lead must not be judged by a verb that names its children")
        self.assertIn("dead-throwaway", text, "the child must actually be walked")
        self.assertNotIn("lead-1 ", text, "the lead must not be in scope at all")

    # -- dry run -----------------------------------------------------------
    def test_dry_run_closes_nothing_and_writes_no_store(self):
        self.reapable()
        before = self.lineage_path.read_bytes()
        closed = []
        self.patch_attr("safe_close", lambda *a, **k: closed.append(a) or (True, "x"))
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=False))
        self.assertEqual(closed, [])
        self.assertEqual(self.lineage_path.read_bytes(), before)
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.digests.exists())
        self.assertEqual(rc, 0)
        self.assertIn("REAP?", out.getvalue())
        self.assertIn("dry run", out.getvalue())
        self.assertTrue(self.sentinel.exists())

    # -- gate still decides -------------------------------------------------
    def test_gate_blockers_stop_the_close_even_when_applying(self):
        self.reapable()
        self.blockers = ["ownership unverified: pi-tmux reports no live pi "
                         "owning this session"]
        closed = []
        self.patch_attr("safe_close", lambda *a, **k: closed.append(a) or (True, "x"))
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.digests.exists())
        self.assertEqual(rc, 0)
        self.assertIn("GATED", out.getvalue())
        self.assertIn("ownership unverified", out.getvalue())
        saved = json.loads(self.lineage_path.read_text())["children"][SID]
        self.assertIsNone(saved["reaped_at"])
        # ownership is resolved a SECOND time, independently, before the gate
        self.assertEqual(self.world.owner_calls, [SID])
        self.assertEqual(self.gate_calls[0]["owner_tmux"], "dune-fork-1")

    def test_gate_never_runs_for_a_child_the_verdict_holds(self):
        self.seed(fleet_lineage.mark_reported(
            child(keep_until=int(time.time()) + 3600), "done", NOW - 60))
        closed = []
        self.patch_attr("safe_close", lambda *a, **k: closed.append(a) or (True, "x"))
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])
        self.assertEqual(self.gate_calls, [])
        self.assertIn("HOLD", out.getvalue())
        self.assertEqual(rc, 0)

    # -- notify dedupe ------------------------------------------------------
    def test_notify_is_deduped_per_reason(self):
        # A cron tick repeats forever. An undecided child must be announced when
        # the SITUATION changes, not when the clock does.
        self.seed(child(), alive=False, reported=False)          # never reported
        with quiet() as out1:
            fleet_children._cascade(args(apply=True))
        first = self._reload()
        why, ts = first["last_notified"], first["last_notified_at"]
        self.assertTrue(why)
        self.assertGreaterEqual(ts, NOW)                         # recorded
        self.assertEqual(len(self.world.argv_matching("pi-msg", "send")), 1)
        self.assertIn("told globex-main", out1.getvalue())

        # Pin the marker older than any clock this test can reach: a same-second
        # rewrite would be indistinguishable from a no-op, an old value is not.
        first["last_notified_at"] = NOW - 500
        self._save(first)
        with quiet() as out2:
            fleet_children._cascade(args(apply=True))
        again = self._reload()
        self.assertEqual(again["last_notified"], why)
        self.assertEqual(again["last_notified_at"], NOW - 500)   # untouched
        self.assertEqual(len(self.world.argv_matching("pi-msg", "send")), 1)
        self.assertNotIn("told globex-main", out2.getvalue())
        self.assertNotIn("would tell", out2.getvalue())
        # Still surfaced, but now as a running grace rather than a repeat
        # announcement: same situation, no second message.
        self.assertIn("grace running", out2.getvalue())

    def test_a_changed_reason_notifies_again(self):
        # Same child, different situation: alive but idle while its parent runs.
        # A distinct reason string is a distinct message.
        stale = "process gone and never reported — nothing to harvest"
        self._save(child(last_notified=stale, last_notified_at=NOW - 500))
        self.world.live_entries = [
            {"sessionId": SID, "name": "globex-citations", "alive": True, "pid": 4242,
             "reported": False, "session": "dune-fork-1", "pi_status": "",
             "phase": ""},
            {"sessionId": PARENT_SID, "name": "globex-main", "alive": True, "pid": 4242,
             "reported": False, "session": "globex-main", "pi_status": "",
             "phase": ""},
        ]
        self.world.records = [{"sessionId": SID,
                               "updatedAt": (int(time.time()) - 7200) * 1000}]
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        saved = self._reload()
        self.assertNotEqual(saved["last_notified"], stale)
        self.assertIn("idle", saved["last_notified"])
        self.assertGreaterEqual(saved["last_notified_at"], NOW)
        self.assertEqual(len(self.world.argv_matching("pi-msg", "send")), 1)
        self.assertIn("told globex-main", out.getvalue())

    # -- order -------------------------------------------------------------
    def test_reap_order_is_digest_then_ledger_then_close(self):
        # MEASURED ORDER of _cascade: _write_digest -> _append_ledger -> safe_close.
        # Write-ahead on purpose: a crash between the record and the kill leaves a
        # trace of what was attempted, never a closed session with no record.
        self.reapable()
        events = []

        def fake_digest(*a, **k):
            events.append("digest")
            return str(self.digests / "fake.md")

        def fake_ledger(line):
            events.append("ledger")
            self.lines = getattr(self, "lines", []) + [line]
            return True

        def fake_close(sid, tmux_session, wait_s=5.0):
            events.append("close")
            self.close_args = (sid, tmux_session)
            return True, "kill-pane dune-fork-1:0.1"

        self.patch_attr("_write_digest", fake_digest)
        self.patch_attr("_append_ledger", fake_ledger)
        self.patch_attr("safe_close", fake_close)
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(events, ["digest", "ledger", "close"])
        self.assertEqual(self.close_args, (SID, "dune-fork-1"))
        self.assertEqual(rc, 0)
        self.assertIn("REAPED", out.getvalue())
        self.assertEqual(len(self.lines), 1)          # a successful close is one row
        row = json.loads(self.lines[0])
        self.assertEqual(row["action"], "reap")
        self.assertEqual(row["sessionId"], SID)
        self.assertEqual(row["digest"], str(self.digests / "fake.md"))
        saved = json.loads(self.lineage_path.read_text())["children"][SID]
        self.assertIsNotNone(saved["reaped_at"])
        self.assertEqual(saved["digest"], str(self.digests / "fake.md"))
        self.assertEqual(saved["verdict"], "reap")
        self.assertTrue(saved["why"])

    # -- end to end, only the pane state faked ------------------------------
    def test_reap_end_to_end_writes_digest_ledger_and_marks_lineage(self):
        self.reapable()
        self.world.answer["pi-tmux --live-owner"] = owner_out(222, "dune-fork-1")
        self.world.answer["tmux list-panes"] = (0, "0.0 111\n0.1 222\n", "")
        self.world.answer["tmux kill-pane"] = (0, "", "")
        with mock.patch.object(fleet_children, "_proc_state", lambda pid: "gone"):
            with quiet() as out:
                rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(rc, 0)
        self.assertIn("REAPED", out.getvalue())
        # digest on disk
        written = list(self.digests.iterdir())
        self.assertEqual(len(written), 1)
        self.assertIn("audited 41 citations", written[0].read_text())
        # ledger on disk, one reap row naming that digest
        rows = [json.loads(l) for l in self.ledger.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "reap")
        self.assertEqual(rows[0]["digest"], str(written[0]))
        # lineage persisted as reaped, pointing at the digest
        saved = json.loads(self.lineage_path.read_text())["children"][SID]
        self.assertIsNotNone(saved["reaped_at"])
        self.assertEqual(saved["digest"], str(written[0]))
        # the kill was the exact pane, never the neighbour
        self.assertEqual(self.world.argv_matching("tmux", "kill-pane"),
                         [["tmux", "kill-pane", "-t", "dune-fork-1:0.1"]])
        self.assertEqual(self.world.argv_matching("pi-tmux", "--close"), [])

    def test_a_repeated_tick_does_not_change_the_digest_bytes(self):
        self.reapable()
        self.world.answer["pi-tmux --live-owner"] = owner_out(222, "dune-fork-1")
        self.world.answer["tmux list-panes"] = (0, "0.0 111\n0.1 222\n", "")
        with mock.patch.object(fleet_children, "_proc_state", lambda pid: "gone"):
            with quiet():
                fleet_children._cascade(args(apply=True))
        digest_path = Path(json.loads(
            self.lineage_path.read_text())["children"][SID]["digest"])
        before = digest_path.read_bytes()
        with mock.patch.object(fleet_children, "_proc_state", lambda pid: "gone"):
            with quiet() as out:
                rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(digest_path.read_bytes(), before)
        self.assertIn("already reaped", out.getvalue())
        self.assertEqual(len(list(self.digests.iterdir())), 1)
        self.assertEqual(len(self.ledger.read_text().splitlines()), 1)
        self.assertEqual(rc, 0)

    # -- failure paths -----------------------------------------------------
    def test_failed_close_is_reported_and_leaves_the_child_unreaped(self):
        self.seed(fleet_lineage.mark_reported(child(), "audited 41 citations",
                                              NOW - 60), alive=True)
        self.patch_attr("safe_close", lambda *a, **k: (False, "no live pi owns "
                                                              "this sessionId"))
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertIn("FAILED", out.getvalue())
        self.assertIn("no live pi owns", out.getvalue())
        self.assertEqual(rc, 0)
        saved = json.loads(self.lineage_path.read_text())["children"][SID]
        self.assertIsNone(saved["reaped_at"])
        self.assertTrue(saved["digest"])
        # Write-ahead plus a corrective row: the attempt is not erased, and the
        # failure is recorded rather than the row being silently rewritten.
        rows = [json.loads(l) for l in self.ledger.read_text().splitlines()]
        self.assertEqual([r["action"] for r in rows], ["reap", "reap-failed"])
        self.assertEqual([r["sessionId"] for r in rows], [SID, SID])
        self.assertEqual(rows[0]["digest"], rows[1]["digest"])

    def test_nothing_to_report_returns_two(self):
        self.reapable()
        with quiet():
            rc = fleet_children._cascade(args(apply=False), only_sid="not-this-one")
        self.assertEqual(rc, 2)
        self.assertTrue(self.sentinel.exists())

    def test_sentinel_report_records_the_pass(self):
        self.reapable()
        self.patch_attr("safe_close", lambda *a, **k: (True, "kill-pane x:0.1"))
        with quiet():
            fleet_children._cascade(args(apply=True))
        rep = json.loads(self.sentinel.read_text())
        self.assertTrue(rep["applied"])
        self.assertEqual(rep["children"], 1)
        self.assertEqual(rep["reaped"], 1)
        self.assertEqual(len(rep["lines"]), 1)
        self.assertIn("REAPED", rep["lines"][0])

    def test_a_dead_child_is_reaped_without_asking_anyone_to_close(self):
        self.reapable(alive=False)
        called = []
        self.patch_attr("safe_close", lambda *a, **k: called.append(a) or (True, "x"))
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(called, [])                       # nothing to kill
        self.assertIn("nothing to close", out.getvalue())
        self.assertIsNotNone(self._reload()["reaped_at"])

    def test_only_sid_processes_just_that_child(self):
        a = fleet_lineage.mark_reported(child(), "done a", NOW - 60)
        b = dict(fleet_lineage.mark_reported(child(), "done b", NOW - 60),
                 sessionId="01a1beef-beef-7000-9000-000000000002", name="other")
        self.lineage_path.write_text(json.dumps(
            {"version": 1, "children": {a["sessionId"]: a, b["sessionId"]: b}},
            indent=2) + "\n")
        # Alive on purpose: --of is about SCOPE, and a close only happens when
        # there is a pane to close.
        self.world.live_entries = [
            {"sessionId": a["sessionId"], "alive": True, "pid": 4242, "reported": True,
             "session": "dune-fork-1", "pi_status": "", "phase": ""},
            {"sessionId": b["sessionId"], "alive": True, "pid": 4242, "reported": True,
             "session": "other-1", "pi_status": "", "phase": ""},
        ]
        closed = []
        self.patch_attr("safe_close",
                        lambda sid, *a, **k: closed.append(sid) or (True, "k"))
        with quiet():
            fleet_children._cascade(args(apply=True), only_sid=b["sessionId"])
        self.assertEqual(closed, [b["sessionId"]])

    def test_busy_child_is_held_even_with_accept_silent(self):
        # accept_silent is not an override for "mid-tool" — the gate is never
        # even consulted, because verdict() already said hold.
        self.seed(child(), alive=True, reported=False)
        self.world.live_entries[0]["pi_status"] = "tool:bash"
        closed = []
        self.patch_attr("safe_close", lambda *a, **k: closed.append(a) or (True, "k"))
        with quiet() as out:
            fleet_children._cascade(args(apply=True, accept_silent=True))
        self.assertEqual(closed, [])
        self.assertIn("HOLD", out.getvalue())
        self.assertIn("inside", out.getvalue())


if __name__ == "__main__":
    unittest.main()
