"""P0/P1/P2/P3 — fleet lineage: orphan scan, dead-parent grace, stale evidence,
revive, verify budget, blocked closes.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_p0 -v
(no pytest on this box — unittest only)

WHAT THIS FILE PINS (one class per behaviour, code first, brief second)

  P1  fleet_lineage.verdict()  — when a child is dead, silent and its PARENT is
      dead too, the outcome is a 600s announced grace instead of the old
      "needs a human decision", which on a dead parent means "waits until
      morning". NOTIFY -> HOLD(grace running) -> REAP(silent: parent gone).
      `keep` still wins over all three; a LIVE parent keeps the old NOTIFY.
  P2  fleet_lineage.claim_status() — claimed files, no verify command, and every
      file older than the spawn = "stale". _cascade must DECIDE (name the
      evidence as predating the unit) and never call safe_close.
  P0  fleet_children.cmd_scan() — live sessions with no lineage record are
      reported, and with --apply enrolled as `protected` orphans. verdict() then
      returns HOLD "orphan: enrol-only..." for them, alive or not.
  P3  cmd_revive() clears the closing fields (keeping report/claims/digest) and
      appends a "revive" ledger row; the verify budget stops a tick from
      spawning checks it cannot afford; a refused close is recorded as
      blocked/blocked_at instead of vanishing.

TWO PLACES THE CODE DISAGREES WITH THE BRIEF (the code wins, and it is pinned
below so nobody "fixes" it back):
  1. `accept_silent` is checked BEFORE the dead-parent grace. A parent that has
     explicitly accepted silence reaps a never-reported dead child immediately;
     the grace is for the default case. test_accept_silent_precedes_the_grace_clock
  2. `--verify-budget-s 0` does not mean "spend nothing": _cascade reads it as
     `int(x or 90)`, so 0 falls back to the 90s default. A negative value is the
     only way to force "budget already spent".
     test_zero_budget_falls_back_to_the_default
  3. The blocked field is `blocked_at`, not the brief's `block_at`.

NO tmux, no /proc, no network, no real sessions, no clock assumptions: every
impure dependency arrives through configure(**deps) and every store is a temp dir.
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
ORPHAN_A = "01a1cafe-0000-7000-9000-00000000000a"
ORPHAN_B = "01a1cafe-0000-7000-9000-00000000000b"
ME = "01a1ffff-eeee-7000-9000-0000000000ee"


# ── fixtures (same shape as test_fleet_children.py) ─────────────────────────
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


def child(**kw):
    c = fleet_lineage.new_child(SID, "globex-citations", PARENT_SID,
                               "globex-main", "audit the citations appendix",
                               NOW, transcript=None)
    c.update(kw)
    return c


def facts(**kw):
    base = {"alive": False, "busy": False, "pending_ask": None,
            "parent_alive": False, "parent_finished": True,
            "last_seen": NOW - 120}
    base.update(kw)
    return base


def args(**kw):
    # Shape of the argparse namespace `fleet` really hands these verbs: every
    # option a command reads must exist, or the test proves nothing about it.
    a = SimpleNamespace(apply=False, accept_silent=False, idle_after_min=30,
                        name=None, unit=None, note=None)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def fcheck(path, exists=True, after=True):
    return {"path": path, "exists": exists, "after_spawn": after}


def claims(files=(), verify=None, unsure=None, c=None):
    """A record_claims() result, reduced to the `claims` dict.

    Goes through the real function so the tests cannot drift from the shape
    claim_status() actually receives.
    """
    return fleet_lineage.record_claims(c or child(), files=files, verify=verify,
                                       unsure=unsure)["claims"]


@contextlib.contextmanager
def quiet():
    """_cascade / cmd_* print their notes; capture them instead of spamming."""
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
        self.answer = {}
        self.base = "origin/main"

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
    """Temp dirs for LINEAGE / DIGESTS / LEDGER / SENTINEL_REPORT."""

    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)          # registered first => runs last
        self.root = Path(td.name)
        self.lineage_path = self.root / "lineage.json"
        self.digests = self.root / "digests"
        self.ledger = self.root / "ledger.jsonl"
        self.sentinel = self.root / "sentinel-report.json"
        self.names_dir = self.root / "names"        # never read the REAL registry
        for name, path in (("LINEAGE", self.lineage_path), ("DIGESTS", self.digests),
                           ("LEDGER", self.ledger), ("NAMES_DIR", self.names_dir),
                           ("SENTINEL_REPORT", self.sentinel)):
            p = mock.patch.object(fleet_children, name, path, create=True)
            p.start()
            self.addCleanup(p.stop)

    def patch_deps(self, deps):
        p = mock.patch.object(fleet_children, "_D", {})
        p.start()
        self.addCleanup(p.stop)
        fleet_children.configure(**deps)

    def patch_attr(self, name, value):
        p = mock.patch.object(fleet_children, name, value)
        p.start()
        self.addCleanup(p.stop)


class WorldStores(TempStores):
    """Temp stores + the injected world + a recording gate. The cascade harness."""

    def setUp(self):
        super().setUp()
        self.world = World()
        self.world.owner[SID] = "dune-fork-1"
        self.patch_deps(good_deps(entries=self.world.entries, find=self.world.find,
                                  sh=self.world.sh, die=self.world.die,
                                  owner_tmux_of=self.world.owner_tmux_of,
                                  clip=self.world.clip, base=self.world.base))
        self.gate_calls = []
        self.blockers = []
        self.patch_attr("fleet_gate", SimpleNamespace(gate=self._gate))
        self.patch_attr("pi_sources", SimpleNamespace(
            live_records=lambda live_only=False: self.world.records))
        self.patch_attr("_self_session_id", lambda: ME)
        self.patch_attr("_proc_identity",
                        lambda pid: ("ok", "S", PID_START) if pid == 4242
                        else ("missing", None, None))

    def _gate(self, entry, worktree_exists=False, accept_silent=False,
              base="origin/main"):
        self.gate_calls.append(dict(entry))
        return list(self.blockers)

    # -- fixtures ----------------------------------------------------------
    def seed(self, c, alive=False, reported=True, session="dune-fork-1",
             last_seen=NOW - 120, parent_alive=False, parent_reported=False):
        if alive:                     # liveness is identity: pid + start time
            c = dict(c, pid=4242, pid_start=PID_START)
        self.write_children({c["sessionId"]: c})
        # A dead CHILD is absent from the live registry (that is what makes it
        # gone); the PARENT is always present, since its liveness is a separate
        # question and the grace path depends on it.
        self.world.live_entries = [] if not alive else [
            {"sessionId": c["sessionId"], "name": c.get("name"), "alive": alive,
             "pid": 4242,
             "reported": reported, "session": session, "pi_status": "",
             "phase": "", "pending_ask": None, "context": "58%", "branch": None,
             "head": None, "merged": False, "worktree": None}]
        self.world.live_entries.append(
            {"sessionId": PARENT_SID, "name": "globex-main",
             "alive": parent_alive, "reported": parent_reported,
             "pid": 5252 if parent_alive else None,
             "session": "globex-main", "pi_status": "", "phase": ""})
        self.world.records = [{"sessionId": c["sessionId"],
                               "updatedAt": last_seen * 1000}]
        return c

    def write_children(self, children):
        # A dead child is absent from entries(); the lineage store is the truth.
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": children}, indent=2) + "\n")

    def reapable(self, **kw):
        return self.seed(fleet_lineage.mark_reported(child(), "audited 41 citations",
                                                    NOW - 60), **kw)

    def _reload(self, sid=SID):
        return json.loads(self.lineage_path.read_text())["children"][sid]

    def _save(self, c, sid=None):
        sid = sid or c["sessionId"]
        kids = (json.loads(self.lineage_path.read_text())["children"]
                if self.lineage_path.exists() else {})
        kids[sid] = c
        self.write_children(kids)

    def _ledger_actions(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l)["action"] for l in self.ledger.read_text().splitlines()]

    def patch_close(self, result=(True, "kill-pane dune-fork-1:0.1")):
        self.closed = []
        self.patch_attr("safe_close",
                        lambda sid, *a, **k: self.closed.append(sid) or result)
        return self.closed

    def patch_deps_extra_spawner(self, tasked):
        """Re-configure with a reviewer spawner that records instead of spawning."""
        deps = good_deps(entries=self.world.entries, find=self.world.find,
                         sh=self.world.sh, die=self.world.die,
                         owner_tmux_of=self.world.owner_tmux_of,
                         clip=self.world.clip, base=self.world.base,
                         spawn_reviewer=lambda c, n: tasked.append(c["sessionId"])
                         or f"fleet-review-{c['sessionId'][:8]}")
        fleet_children.configure(**deps)

    def patch_verify(self, status, detail=None):
        """Record which children were mechanically checked in this tick."""
        self.verified = []

        def fake_verify(rec, timeout=120):
            self.verified.append(rec["sessionId"])
            out = {"files": [], "verify": None, "status": status}
            if detail:
                out["detail"] = detail
            return out

        self.patch_attr("verify_claims", fake_verify)
        return self.verified


# ── P1: the dead-parent grace (pure) ────────────────────────────────────────
class TestDeadParentGrace(unittest.TestCase):
    def v(self, c=None, accept_silent=False, **fkw):
        f = facts(**fkw)
        return fleet_lineage.verdict(c if c is not None else child(), f, NOW,
                                     accept_silent=accept_silent)

    def test_default_grace_is_ten_minutes(self):
        self.assertEqual(fleet_lineage.DEFAULT_GRACE_S, 600)

    def test_dead_parent_without_a_grace_clock_notifies_that_it_is_gone_too(self):
        action, why = self.v(grace_left=None)
        self.assertEqual(action, fleet_lineage.NOTIFY)
        self.assertIn("parent is gone too", why)
        self.assertIn("grace", why)

    def test_dead_parent_never_produces_the_old_needs_a_human_reason(self):
        # The reason string is the dedupe key for notifications, so a new string
        # is a new message. It must not be the one a live parent produces.
        _, why = self.v(grace_left=None)
        self.assertNotIn("needs a human decision", why)
        self.assertNotEqual(why, self.v(parent_alive=True)[1])

    def test_a_running_grace_holds_and_names_the_seconds_left(self):
        for left in (600, 300, 1):
            with self.subTest(left=left):
                action, why = self.v(grace_left=left)
                self.assertEqual(action, fleet_lineage.HOLD)
                self.assertIn("grace running", why)
                self.assertIn(f"{left}s left", why)
                self.assertIn("fleet keep", why)

    def test_an_expired_grace_reaps_as_silent(self):
        for left in (0, -1, -600):
            with self.subTest(left=left):
                action, why = self.v(grace_left=left)
                self.assertEqual(action, fleet_lineage.REAP)
                self.assertIn("silent: parent gone", why)

    def test_a_live_parent_still_needs_a_human_decision(self):
        action, why = self.v(parent_alive=True, grace_left=None)
        self.assertEqual(action, fleet_lineage.NOTIFY)
        self.assertIn("needs a human decision", why)
        self.assertNotIn("parent is gone too", why)

    def test_grace_facts_are_ignored_while_the_parent_lives(self):
        # A grace clock left over from an earlier tick must not reap a child whose
        # parent came back (or was never gone): the dead-parent branch is the only
        # reader of grace_left.
        for left in (None, 300, 0, -600):
            with self.subTest(left=left):
                action, why = self.v(parent_alive=True, grace_left=left)
                self.assertEqual(action, fleet_lineage.NOTIFY)
                self.assertIn("needs a human decision", why)

    def test_keep_wins_over_every_grace_state(self):
        c = child(keep_until=NOW + 3600)
        for left in (None, 300, 0, -600):
            with self.subTest(left=left):
                action, why = self.v(c, grace_left=left)
                self.assertEqual(action, fleet_lineage.HOLD)
                self.assertIn("kept by parent until", why)

    def test_a_reported_dead_child_reaps_at_once_without_a_grace(self):
        # CODE WINS: the grace exists for SILENCE (nothing to harvest). A child
        # that reported is reaped on its report, grace or no grace.
        c = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        for left in (None, 300, -600):
            with self.subTest(left=left):
                action, why = self.v(c, grace_left=left)
                self.assertEqual(action, fleet_lineage.REAP)
                self.assertIn("reported its unit", why)

    def test_accept_silent_precedes_the_grace_clock(self):
        # CODE WINS over the brief's ordering: an explicit accept_silent reaps a
        # never-reported dead child immediately, even mid-grace. The grace is the
        # DEFAULT path, not an override of an explicit decision.
        action, why = self.v(grace_left=300, accept_silent=True)
        self.assertEqual(action, fleet_lineage.REAP)
        self.assertIn("accept_silent", why)

    def test_the_grace_clock_never_touches_a_live_child(self):
        # A live child with a dead parent goes through the warm/idle branch, so a
        # spent grace cannot turn it into "silent: parent gone".
        for left in (None, 0, -600):
            with self.subTest(left=left):
                action, why = self.v(alive=True, parent_alive=False,
                                     parent_finished=False, grace_left=left,
                                     last_seen=NOW - 10)
                self.assertEqual(action, fleet_lineage.HOLD)
                self.assertEqual(why, "parent gone but child still warm")

    def test_the_grace_clock_never_touches_a_busy_or_asking_child(self):
        self.assertEqual(self.v(alive=True, busy=True, grace_left=-600)[0],
                         fleet_lineage.HOLD)
        self.assertIn("inside a tool",
                      self.v(alive=True, busy=True, grace_left=-600)[1])
        self.assertIn("waiting on a human",
                      self.v(alive=True, pending_ask="approve deploy",
                             grace_left=-600)[1])


# ── P2: stale evidence, reduced to one word (pure) ──────────────────────────
class TestStaleEvidence(unittest.TestCase):
    def status(self, cl, ck):
        return fleet_lineage.claim_status(cl, ck)

    def test_every_file_older_than_the_spawn_is_stale(self):
        cl = claims(files=["/a/x.py", "/a/y.py"])
        ck = {"files": [fcheck("/a/x.py", after=False),
                        fcheck("/a/y.py", after=False)]}
        self.assertEqual(self.status(cl, ck), "stale")

    def test_one_fresh_file_turns_stale_into_verified(self):
        cl = claims(files=["/a/x.py", "/a/y.py"])
        ck = {"files": [fcheck("/a/x.py", after=False),
                        fcheck("/a/y.py", after=True)]}
        self.assertEqual(self.status(cl, ck), "verified")

    def test_a_passing_proof_overrides_a_stale_mtime(self):
        # mtime is circumstantial; a re-run proof is real evidence.
        cl = claims(files=["/a/x.py"], verify="python3 -m unittest thing")
        ck = {"files": [fcheck("/a/x.py", after=False)],
              "verify": {"cmd": "python3 -m unittest thing", "rc": 0}}
        self.assertEqual(self.status(cl, ck), "verified")

    def test_a_failing_proof_is_failed_even_with_fresh_files(self):
        cl = claims(files=["/a/x.py"], verify="false")
        ck = {"files": [fcheck("/a/x.py", after=True)],
              "verify": {"cmd": "false", "rc": 1}}
        self.assertEqual(self.status(cl, ck), "failed")

    def test_a_proof_with_no_exit_code_is_partial(self):
        cl = claims(verify="sleep 999")
        ck = {"files": [], "verify": {"cmd": "sleep 999", "rc": None}}
        self.assertEqual(self.status(cl, ck), "partial")

    def test_a_missing_file_is_failed_and_checked_before_the_proof(self):
        cl = claims(files=["/a/x.py"], verify="true")
        ck = {"files": [fcheck("/a/x.py", exists=False)],
              "verify": {"cmd": "true", "rc": 0}}
        self.assertEqual(self.status(cl, ck), "failed")

    def test_no_claim_at_all_is_unclaimed(self):
        self.assertEqual(self.status({}, {}), "unclaimed")
        self.assertEqual(self.status(claims(), {"files": []}), "unclaimed")

    def test_a_claimed_file_with_no_checks_is_not_stale(self):
        # Nothing measured is not the same as measured-and-old: an empty check
        # list must not silently become "stale" (that would turn every dry pass
        # into a DECIDE).
        self.assertEqual(self.status(claims(files=["/a/x.py"]), {}), "unclaimed")


# ── P2 end to end: a stale child is a question, never a kill ────────────────
class TestStaleInCascade(WorldStores):
    def stale_child(self, **kw):
        c = fleet_lineage.mark_reported(
            child(**kw), "wrote the appendix", NOW - 60)
        return fleet_lineage.record_claims(c, files=["/a/x.py"])

    def test_stale_child_is_a_decide_naming_the_predated_unit(self):
        self.seed(self.stale_child())
        self.patch_verify("stale")
        closed = self.patch_close()
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        text = out.getvalue()
        self.assertEqual(closed, [])
        self.assertIn("DECIDE", text)
        self.assertIn("predates the unit", text)
        self.assertNotIn("REAPED", text)
        self.assertEqual(rc, 0)
        saved = self._reload()
        self.assertIsNone(saved["reaped_at"])
        self.assertFalse(self.ledger.exists())
        self.assertFalse(self.digests.exists())
        self.assertNotIn("reap", self._ledger_actions())
        # the parent was told, once, with the same reason
        self.assertEqual(len(self.world.argv_matching("pi-msg", "send")), 1)
        self.assertIn("predates the unit", saved["last_notified"])

    def test_stale_child_is_held_even_with_accept_silent(self):
        # accept_silent silences silence; a contradicted claim is not silence.
        self.seed(self.stale_child())
        self.patch_verify("stale")
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True, accept_silent=True))
        self.assertEqual(closed, [])
        self.assertIn("predates the unit", out.getvalue())

    def test_fresh_evidence_reaps_normally(self):
        c = fleet_lineage.record_claims(
            fleet_lineage.mark_reported(child(), "wrote the appendix", NOW - 60),
            files=["/a/x.py"])
        self.seed(c, alive=True)          # a close needs a pane to close
        self.patch_verify("verified")
        closed = self.patch_close()
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [SID])
        self.assertIn("REAPED", out.getvalue())
        self.assertIn("evidence verified", out.getvalue())
        self.assertEqual(self._ledger_actions(), ["reap"])
        self.assertIsNotNone(self._reload()["reaped_at"])
        self.assertEqual(rc, 0)

    def test_a_failed_claim_is_a_decide_and_never_a_kill(self):
        c = fleet_lineage.record_claims(
            fleet_lineage.mark_reported(child(), "wrote the appendix", NOW - 60),
            files=["/a/x.py"], verify="false")
        self.seed(c)
        self.patch_verify("failed", detail="missing /a/x.py")
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])
        self.assertIn("did not hold", out.getvalue())
        self.assertIn("missing /a/x.py", out.getvalue())
        self.assertIsNone(self._reload()["reaped_at"])

    # -- one end-to-end pass through the REAL verify_claims (offline, instant) --
    def test_real_verify_command_overrides_a_stale_file_mtime(self):
        f = self.root / "appendix.md"
        f.write_text("done\n")
        old = int(f.stat().st_mtime) - 3600
        import os
        os.utime(f, (old, old))                  # written BEFORE the spawn
        c = fleet_lineage.record_claims(
            fleet_lineage.mark_reported(child(), "wrote the appendix", NOW - 60),
            files=[str(f)], verify="true")
        self.seed(c, alive=True)
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [SID])
        self.assertIn("REAPED", out.getvalue())
        self.assertIn("evidence verified", out.getvalue())

    def test_real_failing_verify_command_is_a_decide(self):
        f = self.root / "appendix.md"
        f.write_text("done\n")
        c = fleet_lineage.record_claims(
            fleet_lineage.mark_reported(child(), "wrote the appendix", NOW - 60),
            files=[str(f)], verify="false")
        self.seed(c)
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])
        text = out.getvalue()
        self.assertIn("did not hold", text)
        self.assertIn("exited 1", text)


# ── P0: fleet scan enrols orphans as protected ──────────────────────────────
class TestScanOrphans(WorldStores):
    def setUp(self):
        super().setUp()
        # Never read /root/.pi/agent/sessions from a test.
        self.patch_attr("_transcript_for", lambda sid: None)

    def live(self, sid, name, alive=True, **kw):
        e = {"sessionId": sid, "name": name, "alive": alive, "reported": False,
             "session": name, "pi_status": "", "phase": "", "pending_ask": None,
             "context": "12%", "branch": None, "head": None, "merged": False,
             "worktree": None}
        e.update(kw)
        return e

    def test_dry_run_reports_orphans_and_writes_nothing(self):
        self.world.live_entries = [self.live(ORPHAN_A, "stray-one"),
                                   self.live(ORPHAN_B, "stray-two")]
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=False))
        text = out.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("ORPHAN", text)
        self.assertIn("stray-one", text)
        self.assertIn("stray-two", text)
        self.assertIn("2 orphan(s)", text)
        self.assertIn("--apply", text)
        self.assertFalse(self.lineage_path.exists())
        self.assertEqual(self.world.calls, [])

    def test_apply_enrols_them_as_protected_orphans(self):
        self.world.live_entries = [self.live(ORPHAN_A, "stray-one")]
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=True))
        self.assertEqual(rc, 0)
        self.assertIn("ENROLLED as an orphan", out.getvalue())
        rec = self._reload(ORPHAN_A)
        self.assertIs(rec["protected"], True)
        self.assertIsNone(rec["parent_session_id"])
        self.assertIsNone(rec["parent_name"])
        self.assertIn("orphan", rec["unit"])
        self.assertEqual(rec["name"], "stray-one")
        self.assertIsNone(rec["digest"])
        self.assertIsNone(rec["reaped_at"])

    def test_apply_skips_dead_self_and_already_enrolled_sessions(self):
        self.lineage_path.write_text(json.dumps(
            {"version": 1, "children": {SID: child()}}) + "\n")
        self.world.live_entries = [
            self.live(ORPHAN_A, "dead-one", alive=False),
            self.live(ME, "me-myself"),
            self.live(SID, "already-enrolled"),
            self.live(ORPHAN_B, "real-orphan"),
        ]
        with quiet() as out:
            fleet_children.cmd_scan(args(apply=True))
        kids = json.loads(self.lineage_path.read_text())["children"]
        self.assertEqual(sorted(kids), sorted([SID, ORPHAN_B]))
        self.assertNotIn("protected", kids[SID])       # untouched
        self.assertIs(kids[ORPHAN_B]["protected"], True)
        self.assertNotIn("dead-one", out.getvalue())
        self.assertNotIn("me-myself", out.getvalue())
        self.assertIn("real-orphan", out.getvalue())

    def test_a_second_apply_enrols_nothing_twice(self):
        self.world.live_entries = [self.live(ORPHAN_A, "stray-one")]
        with quiet():
            fleet_children.cmd_scan(args(apply=True))
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=True))
        # Wording follows cmd_scan: it now looks at live sessions AND at ones
        # that ended between two ticks, so "live" alone would be a lie.
        self.assertIn("no unenrolled sessions", out.getvalue())
        self.assertEqual(rc, 0)
        kids = json.loads(self.lineage_path.read_text())["children"]
        self.assertEqual(list(kids), [ORPHAN_A])

    def test_no_live_sessions_at_all_is_a_quiet_no_op(self):
        with quiet() as out:
            rc = fleet_children.cmd_scan(args(apply=True))
        self.assertEqual(rc, 0)
        self.assertIn("no unenrolled sessions", out.getvalue())
        self.assertFalse(self.lineage_path.exists())

    # -- the verdict a protected orphan gets --------------------------------
    def test_a_protected_child_is_held_whatever_its_state(self):
        states = [
            ("alive, fresh", facts(alive=True, parent_alive=False,
                                   parent_finished=False, last_seen=NOW - 5)),
            ("alive, idle", facts(alive=True, last_seen=NOW - 99999)),
            ("reported", facts(alive=False, last_seen=NOW - 60)),
            ("dead and silent, grace spent", facts(alive=False, grace_left=-600)),
            ("busy", facts(alive=True, busy=True, grace_left=-600)),
            ("asking a human", facts(alive=True, pending_ask="approve", )),
        ]
        for label, f in states:
            with self.subTest(state=label):
                c = child(protected=True)
                if label == "reported":
                    c = fleet_lineage.mark_reported(c, "done", NOW - 60)
                action, why = fleet_lineage.verdict(c, f, NOW)
                self.assertEqual(action, fleet_lineage.HOLD)
                self.assertEqual(why, "orphan: enrol-only, never swept "
                                      "automatically")

    def test_protection_beats_keep_and_a_spent_grace(self):
        # `protected` is checked after already-reaped but BEFORE keep: an orphan is
        # never swept out from under a keep, and a keep is not needed to save it.
        action, why = fleet_lineage.verdict(
            child(protected=True, keep_until=NOW + 3600),
            facts(alive=False, grace_left=-600), NOW)
        self.assertEqual(action, fleet_lineage.HOLD)
        self.assertIn("orphan: enrol-only", why)
        self.assertNotIn("kept by parent", why)

    def test_cascade_never_closes_a_protected_orphan(self):
        self.world.live_entries = [self.live(ORPHAN_A, "stray-one")]
        with quiet():
            fleet_children.cmd_scan(args(apply=True))
        self.world.live_entries = [self.live(ORPHAN_A, "stray-one")]   # still alive
        self.world.records = [{"sessionId": ORPHAN_A,
                               "updatedAt": (NOW - 99999) * 1000}]
        closed = self.patch_close()
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True, accept_silent=True))
        self.assertEqual(closed, [])
        self.assertEqual(self.gate_calls, [])          # never even gated
        self.assertFalse(self.ledger.exists())
        self.assertIn("HOLD", out.getvalue())
        self.assertIn("orphan: enrol-only", out.getvalue())
        self.assertEqual(rc, 0)
        self.assertEqual(self._reload(ORPHAN_A)["reaped_at"] is None, True)

    def test_a_protected_orphan_is_never_notified_either(self):
        self.seed(child(protected=True), alive=False, reported=False)
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(self.world.argv_matching("pi-msg", "send"), [])
        self.assertNotIn("would tell", out.getvalue())
        self.assertIn("orphan: enrol-only", out.getvalue())


# ── P1 end to end: the grace clock as _cascade actually runs it ─────────────
class TestGraceInCascade(WorldStores):
    def silent_child(self):
        return fleet_lineage.new_child(SID, "globex-citations", PARENT_SID,
                                       "globex-main", "audit appendix", NOW)

    def test_first_tick_notifies_and_arms_a_600s_grace(self):
        self.seed(self.silent_child(), alive=False, reported=False,
                  parent_alive=False)
        closed = self.patch_close()
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])
        self.assertEqual(rc, 0)
        self.assertIn("parent is gone too", out.getvalue())
        saved = self._reload()
        self.assertIsNotNone(saved["grace_until"])
        self.assertEqual(saved["grace_until"], saved["last_notified_at"] + 600)
        self.assertIsNone(saved["reaped_at"])
        self.assertFalse(self.ledger.exists())

    def test_second_tick_holds_the_running_grace_and_says_nothing_new(self):
        self.seed(self.silent_child(), alive=False, reported=False,
                  parent_alive=False)
        with quiet():
            fleet_children._cascade(args(apply=True))
        sent_before = len(self.world.argv_matching("pi-msg", "send"))
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])
        self.assertIn("grace running", out.getvalue())
        self.assertEqual(len(self.world.argv_matching("pi-msg", "send")),
                         sent_before)          # HOLD is not a notification
        self.assertIsNone(self._reload()["reaped_at"])

    def test_expired_grace_tasks_a_reader_instead_of_sweeping(self):
        # CONTRACT CHANGE (2026-10-17, after review): an expired grace used to
        # sweep immediately. It now tasks a reader first, because a sweep writes
        # a digest from whatever the worker last said and nobody has looked.
        # The reader cannot kill; it only writes the account.
        c = dict(self.silent_child(), grace_until=NOW - 1,
                 last_notified="process gone, never reported")
        self.seed(c, alive=False, reported=False, parent_alive=False)
        tasked = []
        self.patch_deps_extra_spawner(tasked)
        closed = self.patch_close()
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])                  # nothing swept yet
        self.assertEqual(tasked, [SID])               # the reader was tasked
        self.assertIn("REVIEW tasked", out.getvalue())
        saved = self._reload()
        self.assertIsNone(saved["reaped_at"])
        self.assertIsNotNone(saved["review_sid"])
        # Pin the CONTRACT, not a literal: the deadline is a tuned constant (measured
        # 15-19 minute reviews), so a test that duplicates its value fails on every
        # tuning instead of on every regression -- and teaches people to edit the pin.
        self.assertAlmostEqual(saved["review_deadline"],
                               int(time.time()) + fleet_children.REVIEW_DEADLINE_S,
                               delta=120)
        self.assertEqual(rc, 0)

    def test_a_review_that_never_lands_still_sweeps_after_the_deadline(self):
        # A hanging reviewer must not become the new stall: the deadline sweeps
        # anyway, and the digest says so.
        c = dict(self.silent_child(), grace_until=NOW - 1,
                 review_sid="(none)", review_deadline=NOW - 1)
        self.seed(c, alive=False, reported=False, parent_alive=False)
        closed = self.patch_close()
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])                  # dead child: bookkeeping
        self.assertIn("REAPED", out.getvalue())
        self.assertIn("review timed out", out.getvalue())
        saved = self._reload()
        self.assertIsNotNone(saved["reaped_at"])
        self.assertEqual(self._ledger_actions(), ["reap"])
        md = Path(saved["digest"]).read_text()
        self.assertIn("silent: parent gone", md)

    def test_a_reader_that_wrote_the_account_is_attached_then_swept(self):
        # The sidecar exists -> the account is appended to the digest exactly
        # once, and only then is the child swept.
        c = dict(self.silent_child(), grace_until=NOW - 1,
                 review_sid="fleet-review-01a1dead")
        self.seed(c, alive=False, reported=False, parent_alive=False)
        side = fleet_children.REVIEW_DIR / "globex-citations.md"
        side.parent.mkdir(parents=True, exist_ok=True)
        side.write_text("# what happened\n\nthe appendix was half-audited\n")
        self.addCleanup(side.unlink)
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        saved = self._reload()
        self.assertIsNotNone(saved["reaped_at"])
        self.assertIn("reviewed by", saved["why"])
        md = Path(saved["digest"]).read_text()
        self.assertIn("## Reviewed", md)
        self.assertIn("half-audited", md)
        self.assertEqual(md.count("## Reviewed"), 1)   # once, never twice

    def test_an_expired_grace_is_still_gated(self):
        # A dead parent's grace has expired AND the reader's deadline is spent,
        # so verdict() proposes a reap -- and the gate must still refuse it.
        c = dict(self.silent_child(), grace_until=NOW - 1,
                 review_sid="(none)", review_deadline=NOW - 1)
        self.seed(c, alive=False, reported=False, parent_alive=False)
        self.blockers = ["ownership unverified: pi-tmux reports no live pi"]
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [])
        self.assertIn("GATED", out.getvalue())
        self.assertIsNone(self._reload()["reaped_at"])

    def test_a_live_parent_still_notifies_needs_a_human(self):
        self.seed(self.silent_child(), alive=False, reported=False,
                  parent_alive=True)
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        text = out.getvalue()
        self.assertIn("needs a human decision", text)
        self.assertNotIn("parent is gone too", text)
        # no grace is armed for a parent that can still decide
        self.assertIsNone(self._reload().get("grace_until"))

    def test_grace_min_is_read_from_the_args_when_present(self):
        self.seed(self.silent_child(), alive=False, reported=False,
                  parent_alive=False)
        with quiet():
            fleet_children._cascade(args(apply=True, grace_min=1))
        saved = self._reload()
        self.assertEqual(saved["grace_until"], saved["last_notified_at"] + 60)


# ── P3: revive ──────────────────────────────────────────────────────────────
class TestRevive(WorldStores):
    def closed_child(self):
        return dict(
            fleet_lineage.record_claims(
                fleet_lineage.mark_reported(child(), "audited 41 citations",
                                            NOW - 60),
                files=["/a/x.py"], verify="true", unsure="that /a/x.py is final"),
            reaped_at=NOW - 30, verdict="reap", why="reported its unit",
            digest="/tmp/some-digest.md", blocked="cannot identify pane",
            blocked_at=NOW - 30, grace_until=NOW + 600,
            last_notified="process gone and never reported")

    def test_revive_clears_the_closing_fields_and_keeps_the_evidence(self):
        self._save(self.closed_child())
        with quiet() as out:
            rc = fleet_children.cmd_revive(args(name="globex-citations"))
        # cmd_revive prints; it returns nothing (only cmd_scan answers with a code)
        self.assertIsNone(rc)
        self.assertIn("revived", out.getvalue())
        rec = self._reload()
        for field in ("reaped_at", "verdict", "why", "blocked", "blocked_at",
                      "grace_until", "last_notified"):
            with self.subTest(field=field):
                self.assertIsNone(rec[field])
        # history is not rewritten
        self.assertEqual(rec["report"], "audited 41 citations")
        self.assertEqual(rec["claims"]["files"], ["/a/x.py"])
        self.assertEqual(rec["claims"]["verify"], "true")
        self.assertEqual(rec["claims"]["unsure"], "that /a/x.py is final")
        self.assertEqual(rec["digest"], "/tmp/some-digest.md")
        self.assertEqual(rec["reported_at"], NOW - 60)

    def test_revive_appends_a_revive_row_and_never_rewrites_the_reap_row(self):
        self.ledger.write_text(json.dumps(
            {"ts": NOW - 30, "sessionId": SID, "action": "reap"}) + "\n")
        before = self.ledger.read_text()
        self._save(self.closed_child())
        with quiet():
            fleet_children.cmd_revive(args(name="globex-citations"))
        after = self.ledger.read_text()
        self.assertTrue(after.startswith(before))      # append-only
        actions = self._ledger_actions()
        self.assertEqual(actions, ["reap", "revive"])
        self.assertEqual(json.loads(after.splitlines()[-1])["sessionId"], SID)

    def test_a_revived_child_becomes_reapable_again(self):
        self._save(self.closed_child())
        with quiet():
            fleet_children.cmd_revive(args(name="globex-citations"))
        action, why = fleet_lineage.verdict(
            self._reload(), facts(alive=False, reported=True), int(time.time()))
        self.assertEqual(action, fleet_lineage.REAP)
        self.assertIn("reported its unit", why)
        # and the cascade actually closes it again (the /a/x.py claim cannot be
        # inspected here, so the check is stubbed to its passing outcome)
        # Still alive on the later tick: revive restores the record, and the
        # reap of a live child is a real close. A dead one is bookkeeping only.
        self.world.live_entries = [{"sessionId": SID, "name": "globex-citations",
                                   "alive": True, "pid": 4242, "reported": True,
                                   "session": "dune-fork-1", "pi_status": "",
                                   "phase": "", "pending_ask": None,
                                   "context": "", "branch": None, "head": None,
                                   "merged": False, "worktree": None}]
        self.world.records = []
        self.patch_verify("verified")
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [SID])
        self.assertIn("REAPED", out.getvalue())
        self.assertEqual(self._ledger_actions(), ["revive", "reap"])

    def test_a_revived_child_does_not_keep_a_spent_grace(self):
        # A child that never reported and whose grace was already spent must not
        # be swept on the tick after a revive: clearing grace_until means the
        # announcement (and a fresh grace) comes first.
        c = dict(self.silent_child(), grace_until=NOW - 600, reaped_at=NOW - 900,
                 verdict="reap", why="silent: parent gone and it never reported",
                 last_notified="partner gone")
        self._save(c)
        with quiet():
            fleet_children.cmd_revive(args(name="globex-citations"))
        self.assertIsNone(self._reload()["grace_until"])
        action, why = fleet_lineage.verdict(
            self._reload(), facts(alive=False, reported=False,
                                  parent_alive=False, grace_left=None),
            int(time.time()))
        self.assertEqual(action, fleet_lineage.NOTIFY)
        self.assertIn("parent is gone too", why)

    def silent_child(self):
        return fleet_lineage.new_child(SID, "globex-citations", PARENT_SID,
                                      "globex-main", "audit appendix", NOW)

    def test_revive_by_session_id_and_unit_override(self):
        self._save(self.closed_child())
        with quiet():
            fleet_children.cmd_revive(args(name=SID, unit="new unit text"))
        rec = self._reload()
        self.assertEqual(rec["unit"], "new unit text")
        self.assertIsNone(rec["reaped_at"])

    def test_a_prior_checks_block_survives_revive_and_colours_the_ledger_row(self):
        # MEASURED, and flagged to the parent rather than endorsed: the brief's
        # clear-list does not include `checks`, so _cascade's evidence verdict from
        # BEFORE the revive survives it and is copied into the new ledger row's
        # "evidence" field. verdict() never reads checks, so nothing is reaped on
        # it; the row is just coloured by an old measurement. Pinned so a
        # deliberate change is a visible edit here, not a silent drift.
        c = dict(self.closed_child(), checks={"status": "stale", "files": []})
        self._save(c)
        with quiet():
            fleet_children.cmd_revive(args(name="globex-citations"))
        self.assertEqual(self._reload()["checks"], {"status": "stale",
                                                    "files": []})
        row = json.loads(self.ledger.read_text().splitlines()[-1])
        self.assertEqual(row["action"], "revive")
        self.assertEqual(row["evidence"], "stale")

    def test_a_dismissed_child_can_be_revived(self):
        self._save(child())
        with quiet():
            with self.assertRaises(SystemExit) as ctx:
                fleet_children.cmd_revive(args(name="not-a-child"))
        self.assertIn("not in any lineage", str(ctx.exception))
        self.assertEqual(self.world.died, ["not-a-child is not in any lineage"])

    def test_reviving_an_unknown_name_dies(self):
        self._save(self.closed_child())
        with quiet():
            fleet_children.cmd_dismiss(args(name="globex-citations",
                                            note="human says done"))
        self.assertEqual(self._reload()["verdict"], "dismissed")
        with quiet():
            fleet_children.cmd_revive(args(name="globex-citations"))
        rec = self._reload()
        self.assertIsNone(rec["verdict"])
        self.assertIsNone(rec["reaped_at"])


# ── P3: the per-tick verify budget ─────────────────────────────────────────
class TestVerifyBudget(WorldStores):
    def claim_children(self, n=3):
        kids, entries, records = {}, [], []
        for i in range(n):
            sid = f"01a1beef-0000-7000-9000-00000000000{i}"
            c = fleet_lineage.record_claims(
                fleet_lineage.mark_reported(
                    fleet_lineage.new_child(sid, f"worker-{i}", PARENT_SID,
                                            "globex-main", f"unit {i}", NOW),
                    "done", NOW - 60),
                files=[f"/a/{i}.py"])
            kids[sid] = dict(c, pid=4242, pid_start=PID_START)
            entries.append({"sessionId": sid, "name": f"worker-{i}",
                            "alive": True, "pid": 4242, "reported": True,
                            "session": f"worker-{i}", "pi_status": "",
                            "phase": ""})
            records.append({"sessionId": sid, "updatedAt": (NOW - 120) * 1000})
        self.write_children(kids)
        self.world.live_entries = entries
        self.world.records = records
        self.world.owner = {sid: f"worker-{i}" for i, sid in
                            enumerate(sorted(kids))}
        return sorted(kids)

    def test_a_spent_budget_skips_every_claim_bearing_child(self):
        self.claim_children(3)
        verified = self.patch_verify("verified")
        closed = self.patch_close()
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True, verify_budget_s=-1))
        text = out.getvalue()
        self.assertEqual(verified, [])                 # nothing was checked
        self.assertEqual(closed, [])
        self.assertIn("verify budget spent this tick", text)
        self.assertEqual(text.count("HOLD   verify budget spent this tick"), 3)
        self.assertNotIn("REAPED", text)
        self.assertEqual(rc, 0)
        self.assertFalse(self.ledger.exists())
        # NO subprocess at all this tick: not a check, not a kill, not a message
        self.assertEqual(self.world.calls, [])

    def test_a_generous_budget_checks_every_child(self):
        sids = self.claim_children(3)
        verified = self.patch_verify("verified")
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True, verify_budget_s=3600))
        self.assertEqual(verified, sids)
        self.assertEqual(sorted(closed), sids)
        self.assertIn("REAPED", out.getvalue())
        self.assertEqual(self._ledger_actions(), ["reap", "reap", "reap"])

    def test_the_budget_is_measured_from_the_start_of_the_tick(self):
        # A budget smaller than the work is still a budget: with the check itself
        # unable to run in negative time, -1 is the only deterministic "spent".
        self.claim_children(1)
        verified = self.patch_verify("verified")
        self.patch_close()
        with quiet():
            fleet_children._cascade(args(apply=True, verify_budget_s=-1))
        self.assertEqual(verified, [])

    def test_zero_budget_falls_back_to_the_default(self):
        # CODE WINS over the brief: `int(getattr(a, "verify_budget_s", None) or 90)`
        # turns 0 into the 90s default, so 0 means "normal budget", not "spend
        # nothing". Pinned so a future refactor does not silently change it.
        self.claim_children(1)
        verified = self.patch_verify("verified")
        self.patch_close()
        with quiet():
            fleet_children._cascade(args(apply=True, verify_budget_s=0))
        self.assertEqual(len(verified), 1)

    def test_a_child_with_no_claims_is_not_blocked_by_a_spent_budget(self):
        self.claim_children(1)
        free = fleet_lineage.new_child(
            "01a1c0de-0000-7000-9000-0000000000cc", "no-claims", PARENT_SID,
            "globex-main", "no checkable claim", NOW)
        kids = json.loads(self.lineage_path.read_text())["children"]
        free = dict(fleet_lineage.mark_reported(free, "wrote prose only", NOW - 60),
                    pid=4242, pid_start=PID_START)
        free = dict(free, pid=4242, pid_start=PID_START)
        kids[free["sessionId"]] = free
        self.write_children(kids)
        self.world.live_entries.append(
            {"sessionId": free["sessionId"], "name": "no-claims", "alive": True, "pid": 4242,
             "reported": True, "session": "no-claims", "pi_status": "",
             "phase": ""})
        self.patch_verify("verified")
        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True, verify_budget_s=-1))
        self.assertIn(free["sessionId"], closed)
        self.assertNotIn(SID, closed)
        text = out.getvalue()
        self.assertIn("REAPED", text)
        self.assertIn("verify budget spent this tick", text)

    def test_the_budget_is_checked_before_anything_is_spawned(self):
        # Ordering proof: with the budget spent, no verify_claims and no
        # subprocess. read the world's argv log, not the note text.
        self.claim_children(2)
        self.patch_verify("verified")
        self.patch_close()
        with quiet():
            fleet_children._cascade(args(apply=True, verify_budget_s=-1))
        self.assertEqual(self.world.argv_matching("tmux"), [])
        self.assertEqual(self.world.argv_matching("pi-tmux", "--close"), [])
        self.assertEqual(self.world.argv_matching("pi-msg", "send"), [])


# ── P3: a refused close is recorded, not swallowed ─────────────────────────
class TestBlockedClose(WorldStores):
    def test_a_refused_close_records_blocked_and_blocked_at(self):
        # Alive on purpose: refusal is about a pane we cannot identify, and a
        # dead child is reaped without ever asking anyone to close it.
        self.seed(fleet_lineage.mark_reported(child(), "audited", NOW - 60),
                  alive=True)
        reason = ("cannot identify which of 2 panes in dune-fork-1 owns pid 222 "
                  "— refusing to guess")
        closed = self.patch_close((False, reason))
        with quiet() as out:
            rc = fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [SID])
        self.assertIn("FAILED", out.getvalue())
        self.assertIn("refusing to guess", out.getvalue())
        self.assertEqual(rc, 0)
        saved = self._reload()
        self.assertEqual(saved["blocked"], reason)
        self.assertIsNotNone(saved["blocked_at"])          # blocked_at, not block_at
        self.assertNotIn("block_at", saved)
        self.assertIsNone(saved["reaped_at"])
        self.assertEqual(self._ledger_actions(), ["reap", "reap-failed"])

    def test_a_blocked_child_can_be_revived_and_reaped_on_a_later_tick(self):
        # Alive: "blocked" means a close was refused, and a dead child is never
        # asked to close (bookkeeping reap).
        self.seed(fleet_lineage.mark_reported(child(), "audited", NOW - 60),
                  alive=True)
        self.patch_close((False, "no live pi owns this sessionId"))
        with quiet():
            fleet_children._cascade(args(apply=True))
        self.assertIsNotNone(self._reload()["blocked"])

        with quiet() as out:
            rc = fleet_children.cmd_revive(args(name="globex-citations"))
        self.assertIsNone(rc)                    # reviving prints, returns None
        self.assertIsNone(self._reload()["blocked"])
        self.assertIsNone(self._reload()["blocked_at"])

        closed = self.patch_close()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(closed, [SID])
        self.assertIn("REAPED", out.getvalue())
        self.assertIsNotNone(self._reload()["reaped_at"])


if __name__ == "__main__":
    unittest.main()
