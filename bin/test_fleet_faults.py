"""Fault injection: what happens when the reaper itself is interrupted.

The existing suites test the reaper's *decisions*. This one tests its
**durability** — the promises that survive a crash, a corrupt store, two
processes at once, or a pid that used to be somebody else.

The rule for every test in this file: **prove the fault fired**. A test that
claims to kill the process between the digest and the ledger row but never
actually raises there is theatre. Each test asserts its own injection first
(e.g. "the crash happened", "the file is really truncated", "a second process
really was running"), and only then asserts the invariant.

As of 2026-10-17 this suite carries NO `@unittest.expectedFailure` markers: every
fault it names is either handled by the code or documented as a ceiling with a
test that asserts the ceiling exists. If a future invariant does not hold, mark
the test `@unittest.expectedFailure` and name the defect in the test name and the
docstring. That keeps the suite green *and* loud -- fixing the bug turns the
marker into an "unexpected success" error that must be removed deliberately --
and it is the only acceptable alternative to silence. A marker is a claim that
somebody knows what is wrong and has chosen not to fix it yet; keep it true.

Invariants under test
---------------------
F1  digest written, ledger row not      -> restart never marks a child reaped
                                           without its row (write-ahead holds)
F2  ledger row written, kill not         -> no false success; a re-run converges
F3  store truncated mid-write            -> never a silent empty store (a get-out-
                                           of-jail card for the whole fleet)
F4  two real ticks racing                -> one reap, one row, one close
F5  same tick replayed 50x               -> byte-identical stores, one kill,
                                           one notification
F6  recycled pid                         -> not treated as our session
F7  20 records from an older schema      -> no exception, exactly one verdict each

The second half covers the tools built the same night, because they are now
load-bearing: export/import (fleet_bundle) and restore (fleet_restore).

F8  export dies mid-write                 -> a partial bundle must never verify
                                           CLEAN, and a leftover .tmp must never
                                           be mistaken for one
F9  import dies between backup and write   -> store is old or new, never half;
                                           the backup exists; a re-run converges
F10 restore dies after backup, before write-> re-run converges, no duplicated
                                           ledger row
F11 header claims 33, carries 32            -> the COUNT is a claim too: caught
                                           even when the checksum is fixed up
F13 the fault test itself is faulty      -> an injection narrower than the write
                                           path goes silent when the code starts
                                           staging; the harness must prove its own
                                           injection fires, and state its ceiling
F12 a pruned session comes back             -> the tombstone beats the evidence:
                                           prune is durable, and the auditor
                                           still catches a resurrection if it is not
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import fleet_audit
import fleet_bundle
import fleet_children
import fleet_lineage
import fleet_restore

PY = sys.executable
BIN = Path(__file__).parent
NOW = 1_800_000_000
SID = "01a1f000-0000-7000-9000-000000000001"
PARENT = "01a1f000-0000-7000-9000-0000000000aa"


def child(**kw):
    c = fleet_lineage.new_child(SID, "fault-child", PARENT, "fault-parent",
                                "unit under fault injection", NOW,
                                transcript=None)
    c.update(kw)
    return c


def args(**kw):
    a = SimpleNamespace(apply=True, accept_silent=False, idle_after_min=30,
                        grace_min=10, verify_budget_s=90, quiet=True)
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class World:
    """The impure half, injected. Every call is recorded, none is real."""

    def __init__(self):
        self.live = []
        self.records = []
        self.shell = []          # argv lists, in order
        self.closed = []         # (sid, tmux)
        self.spawned = []        # reviewer names
        self.close_fails = False
        self.pid_alive = True
        self.pid_start = 777
        self.panes_out = "0.0 4242\n"
        self.pane_pid = "4242"

    # -- deps ---------------------------------------------------------------
    def entries(self):
        return self.live

    def find(self, name):
        return next((e for e in self.live if e.get("name") == name), None)

    def sh(self, argv, **kw):
        self.shell.append(list(argv))
        joined = " ".join(str(a) for a in argv)
        if joined.startswith("pi-tmux --live-owner"):
            sid = str(argv[-1])
            return (0, f"{self.pane_pid}\t{sid}\tfault-child\tpts/1\tidle\tfault:0.0\n",
                    "")
        if joined.startswith("tmux list-panes"):
            return 0, self.panes_out, ""
        if joined.startswith("tmux kill-pane") or joined.startswith("pi-tmux --close"):
            if self.close_fails:
                return 1, "", "injected close failure"
            self.pid_alive = False          # the kill landed
            return 0, "", ""
        return 0, "", ""

    def owner_tmux_of(self, sid):
        return "fault:0.0"

    def clip(self, s, n):
        return str(s or "")[:n]

    def die(self, msg):
        raise SystemExit(msg)

    def base(self):
        return "origin/main"

    def spawn_reviewer(self, c, now):
        name = "fleet-review-" + c["sessionId"][:8]
        self.spawned.append(name)
        return name


class TempStores(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fleet-faults-"))
        # Each test makes a store, a ledger, digests and bundles; 45 tests twice a
        # minute is a full /tmp. Clean up after ourselves.
        import shutil
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lineage_path = self.tmp / "lineage.json"
        self.ledger = self.tmp / "reap-ledger.jsonl"
        self.digests = self.tmp / "digests"
        self.world = World()
        # The real seam reads the whole pi registry off disk (~2s per tick).
        # Fifty replays of that is 110s of I/O for no extra assurance.
        self.world.records = []
        self._patch("pi_sources", SimpleNamespace(
            live_records=lambda live_only=False: self.world.records))
        self._patch("LINEAGE", self.lineage_path)
        self._patch("LEDGER", self.ledger)
        self._patch("DIGESTS", self.digests)
        self._patch("SENTINEL_REPORT", self.tmp / "report.json")
        # Default world: a kill that lands. `safe_close` waits for the pid to
        # disappear, so a fake that stays "R" makes every close fail.
        self._proc = mock.patch.object(
            fleet_children, "_proc_state",
            lambda pid: "R" if self.world.pid_alive else "gone")
        # Alive means "this child owns the process": the identity probe, not the
        # pid's mere existence.
        self._ident = mock.patch.object(
            fleet_children, "_proc_identity",
            lambda pid: ("ok", "R" if self.world.pid_alive else "gone",
                         self.world.pid_start))
        self._ident.start()
        self.addCleanup(self._ident.stop)
        self._proc.start()
        self.addCleanup(self._proc.stop)
        fleet_children.configure(
            entries=self.world.entries, find=self.world.find, sh=self.world.sh,
            owner_tmux_of=self.world.owner_tmux_of, clip=self.world.clip,
            die=self.world.die, base=self.world.base,
            spawn_reviewer=self.world.spawn_reviewer)

    def _patch(self, name, value):
        p = mock.patch.object(fleet_children, name, value)
        p.start()
        self.addCleanup(p.stop)

    # -- store helpers ------------------------------------------------------
    def save(self, children):
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": children}, indent=2) + "\n")

    def load(self):
        return json.loads(self.lineage_path.read_text())["children"]

    def rows(self):
        if not self.ledger.exists():
            return []
        return [json.loads(l) for l in self.ledger.read_text().splitlines() if l]

    def seed_reported(self, alive=True):
        """A child that reported its unit: the ordinary, sweepable case."""
        c = fleet_lineage.mark_reported(child(pid=4242, pid_start=self.world.pid_start),
                                        "did the thing", NOW - 60)
        self.save({SID: c})
        self.world.live = [self.entry(alive=alive, pid=4242)]
        return c

    def entry(self, alive=True, reported=True, session="fault:0.0", **kw):
        e = {"sessionId": SID, "name": "fault-child", "alive": alive, "pid": 4242,
             "reported": reported, "session": session, "pi_status": "",
             "phase": "", "pending_ask": None, "context": "12%", "branch": None,
             "head": None, "merged": False, "worktree": None}
        e.update(kw)
        return e

    def tick(self, **kw):
        return fleet_children._cascade(args(**kw))


class F1_CrashBetweenDigestAndLedger(TempStores):
    """The digest exists. The ledger row does not. The process is gone."""

    def test_crash_after_digest_leaves_nothing_claiming_success(self):
        """Same reordering, seen from F1's side (regression 16:55, fixed 17:0x).

        A crash between the digest and the ledger row now leaves `reaped_at` set
        with no row at all -- a closed record with no event, the precise shape of
        the fault F1 exists to pin.
        """
        self.seed_reported()
        def boom(line):
            # Fires exactly where the process would die: after the digest is on
            # disk, before the durable row. Assert the digest really landed.
            self.assertTrue(digest_written(self),
                            "the fault fired before the digest was written")
            raise RuntimeError("INJECTED: killed between digest and ledger")

        with mock.patch.object(fleet_children, "_append_ledger", boom):
            with self.assertRaises(RuntimeError) as ctx:
                self.tick()
        self.assertIn("INJECTED", str(ctx.exception))       # the fault fired

        # The digest is on disk -- that is the half that landed.
        files = list(self.digests.glob("*.md"))
        self.assertEqual(len(files), 1)
        first_bytes = files[0].read_bytes()

        # Write-ahead: nothing says "reaped" if no row says it was reaped.
        rec = self.load()[SID]
        self.assertIsNone(rec["reaped_at"])
        self.assertEqual([r for r in self.rows() if r["action"] == "reap"], [])

        # A restart converges and reaps exactly once.
        self.tick()
        rec = self.load()[SID]
        self.assertIsNotNone(rec["reaped_at"])
        self.assertEqual([r["action"] for r in self.rows()].count("reap"), 1)
        self.assertTrue(Path(rec["digest"]).exists())

        # The digest is write-once only while the record remembers its path --
        # and the crashed attempt never stored it, so the retry rewrote the same
        # filename (the name carries the DATE, not the time). One file, and the
        # only bytes that moved are the generated timestamp.
        files = list(self.digests.glob("*.md"))
        self.assertEqual(len(files), 1)          # same filename: date, not time
        self.assertEqual(strip_generated(files[0].read_text()),
                         strip_generated(first_bytes.decode()),
                         "a retry changed the digest's content, not just its clock")


_STAMP = __import__("re").compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")


def strip_generated(text):
    """Drop the only thing allowed to differ between two runs: wall-clock stamps."""
    return "\n".join(_STAMP.sub("<stamp>", l) for l in text.splitlines())


def digest_written(t):
    return bool(list(t.digests.glob("*.md")))


class F2_CrashBetweenLedgerAndKill(TempStores):
    """The row is written (write-ahead). The kill never happened."""

    def test_row_without_kill_is_not_a_false_success_and_rerun_converges(self):
        """A crashed tick must not claim a reap it never did (regression 16:55, fixed 17:0x).

        The write-ahead contract is: the ROW lands before the kill, and
        `reaped_at` means the kill HAPPENED. Saving `reaped_at` before the row
        inverts it -- the store now claims success first and the ledger explains it
        afterwards, which is exactly the direction I1 forbids.
        """
        self.seed_reported()
        calls = []

        def exploding_close(sid, session, wait_s=5.0):
            calls.append(sid)
            raise RuntimeError("INJECTED: killed between ledger row and kill")

        with mock.patch.object(fleet_children, "safe_close", exploding_close):
            with self.assertRaises(RuntimeError) as ctx:
                self.tick()
        self.assertIn("INJECTED", str(ctx.exception))       # the fault fired

        rec = self.load()[SID]
        self.assertIsNone(rec["reaped_at"])                 # no false success
        actions = [r["action"] for r in self.rows()]
        self.assertEqual(actions, ["reap"])                 # the row did land
        self.assertNotIn("reap-failed", actions)            # and did not lie

        # Convergence: the next tick finishes the job. The pane was never killed
        # by the crashed tick, so a second kill attempt is correct, not a double.
        self.tick()
        rec = self.load()[SID]
        self.assertIsNotNone(rec["reaped_at"])
        actions = [r["action"] for r in self.rows()]
        self.assertEqual(actions[-1], "reap")
        self.assertLessEqual(actions.count("reap"), 2)
        self.assertNotIn("reap-failed", actions)

    def test_one_death_leaves_one_reap_row_even_across_a_crash(self):
        """One death leaves one row, even when the retry's clock has moved.

        GREEN since 17:0x, and the history is the point of this docstring -- this
        single invariant was wrong three times in one evening, each time in a way
        the suite could not have caught without a test that proves its own fault
        fired:

        1. exact-line dedupe: a retry five seconds later produced a byte-different
           row for the same death, so two rows. The dedupe could not see it.
        2. event-keyed dedupe (fork-1): right idea, but the key came from
           `reaped_at`, which the write-ahead row does not have yet, so it fell
           back to the attempt clock and the keys still differed.
        3. durable key, optimistic state: `reap_started_at` was persisted together
           with `reaped_at` BEFORE the row and BEFORE the close. The duplicate row
           vanished because THE RETRY vanished -- a record claiming a reap of a
           child whose pane was still running, and a sentinel that never tried
           again. A leaked live session is worse than a duplicate row.

        The repair splits the two: the KEY (`reap_started_at`) is durable before
        the row, the STATE (`reaped_at`) is written only after a close succeeds,
        and `verdict()` never treats the key as terminal. Hence the assertions
        here: one row, TWO append attempts with results [True, False] (deduped,
        not skipped), and event_ts == reap_started_at < reaped_at.

        Original note, still true:
        The event-keyed dedupe needs the event's instant to be known at append
        time. In the write-ahead window it is not: the row is written BEFORE the
        kill, so the record still has no `reaped_at` (and no `closed_at`), and
        `ledger_line` falls back to `now` -- the ATTEMPT's clock. A retry five
        seconds later therefore derives event_ts = now+5, the keys differ, and
        the duplicate lands. Measured, not assumed:

            attempt 1 row: ts=...817  event_ts=...817
            attempt 2 row: ts=...822  event_ts=...822
            store after:   reaped_at=...822

        So the fix covers every retry whose event instant is ALREADY durable (a
        reap-failed corrective row, a reopened record) and misses exactly the
        window this fault injects into. What the fix needs is a durable event
        instant stamped before the row: at the top of the reap path write
        something like `reap_started_at` into the record, and let event_ts prefer
        it over `now`. Then both attempts derive the same key.

        The assertions below are written for the FIXED behaviour, including the
        entry proof: the retry must ATTEMPT the append (spy called twice) and be
        dropped by the dedupe -- not skipped before reaching it, which is how a
        test can go green without exercising anything.
        """
        self.seed_reported()
        attempts = []
        real = fleet_children._append_ledger

        def spy(line):
            landed = real(line)
            attempts.append((json.loads(line).get("event_ts"), landed))
            return landed

        def exploding_close(sid, session, wait_s=5.0):
            raise RuntimeError("INJECTED")

        with mock.patch.object(fleet_children, "_append_ledger", spy):
            with mock.patch.object(fleet_children, "safe_close", exploding_close):
                with self.assertRaises(RuntimeError):
                    self.tick()
            # Retry a few seconds later, as a cron would. Same second would let the
            # exact-line dedupe hide the defect and make this marker flaky.
            later = fleet_children.time.time() + 5
            with mock.patch.object(fleet_children.time, "time", lambda: later):
                self.tick()

        rows = self.rows()
        self.assertEqual([r["action"] for r in rows].count("reap"), 1,
                         f"two rows for one death: {[(r['action'], r.get('event_ts')) for r in rows]}")
        self.assertEqual(len(attempts), 2, f"the retry never reached the ledger: {attempts}")
        self.assertEqual([landed for _, landed in attempts], [True, False],
                         "the second append was not attempted-and-dropped")
        # The contract, not an accident: one key, and it is the EVENT's START.
        # Note what this deliberately does NOT assert: event_ts == reaped_at.
        # Under the correct design those two differ by the retry gap -- the key is
        # when the reap began (durable before the row), reaped_at is when a close
        # finally succeeded (after the kill). Asserting they match was only true
        # while reaped_at was written optimistically, i.e. while the bug was live.
        self.assertEqual(attempts[0][0], attempts[1][0],
                         "the two attempts derived different event keys")
        rec = self.load()[SID]
        self.assertEqual(rows[0]["event_ts"], rec["reap_started_at"],
                         "event_ts is the attempt clock, not the event's instant")
        self.assertIsNotNone(rec["reaped_at"])
        self.assertLess(rows[0]["event_ts"], rec["reaped_at"],
                        "the event key should be the START of the reap, stamped "
                        "before the row; reaped_at is the completion")

    def test_the_started_key_is_never_terminal(self):
        """The repair's guard: `reap_started_at` is a KEY, never a STATE.

        A record that carries the key but not `reaped_at` has NOT been reaped --
        the row may be on disk, the kill certainly is not. If `verdict()` ever
        treats the key as terminal, the retry stops retrying and a live pane is
        leaked while the store says done. (The regression this pins was live for
        twenty minutes on 2026-10-17.)
        """
        rec = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        self.assertIsNone(rec.get("reaped_at"))
        rec["reap_started_at"] = NOW - 5
        action, why = fleet_lineage.verdict(rec, {"alive": True}, NOW)
        self.assertEqual(action, fleet_lineage.REAP,
                         f"a started-but-unreaped record was treated as done: {why}")

    def test_the_event_key_comes_from_the_started_stamp_not_the_attempt_clock(self):
        """What makes the dedupe fire in the write-ahead window.

        No `reaped_at`, no `closed_at` -- exactly the state a crashed tick leaves.
        Two attempts five seconds apart must derive the SAME key, or no ledger
        dedupe can ever see the duplicate.
        """
        rec = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        rec["reap_started_at"] = NOW
        first = json.loads(fleet_lineage.ledger_line(rec, "reap", NOW))
        retry = json.loads(fleet_lineage.ledger_line(rec, "reap", NOW + 5))
        self.assertEqual(first["event_ts"], NOW)
        self.assertEqual(first["event_ts"], retry["event_ts"],
                         "the retry's key moved with its own clock")
        self.assertNotEqual(first["ts"], retry["ts"])   # the attempt clock DID move

    def test_a_genuinely_different_event_still_lands(self):
        """The over-correction guard: dedupe on the EVENT, never on (sid, action).

        If the next fix dedupes coarsely, one death would swallow every later
        event for that session and the ledger would lose real history.
        """
        a = {"sessionId": "01a1f000-0000-7000-9000-0000000000a1", "name": "a",
             "reaped_at": 1000}
        b = dict(a, reaped_at=2000)                      # reopened, then reaped again
        self.assertTrue(fleet_children._append_ledger(
            fleet_lineage.ledger_line(a, "reap", 1000)))
        self.assertTrue(fleet_children._append_ledger(
            fleet_lineage.ledger_line(b, "reap", 2000)),
            "a second, genuinely different reap was swallowed as a duplicate")

    def test_reap_failed_is_never_swallowed_by_its_own_reap_row(self):
        """A corrective row is a DIFFERENT event, at the same instant, same action
        family. Deduping without the action would hide the only trace of a kill
        that did not happen."""
        rec = {"sessionId": "01a1f000-0000-7000-9000-0000000000a2", "name": "a",
               "reaped_at": 1000}
        self.assertTrue(fleet_children._append_ledger(
            fleet_lineage.ledger_line(rec, "reap", 1000)))
        self.assertTrue(fleet_children._append_ledger(
            fleet_lineage.ledger_line(rec, "reap-failed", 1000)),
            "the corrective row was swallowed by the reap row it corrects")

    def test_a_second_kill_attempt_happens_because_the_first_never_landed(self):
        """A retry must still attempt the kill (REGRESSION caught 16:55, fixed 17:0x).

        `reaped_at` is saved before the close, so after a crashed tick the record
        reads as finished. The child's pane is never closed by anyone: the sentinel
        sees "already reaped" and moves on. Found by removing F2's marker -- the
        assertions were already written for the fixed behaviour, so the flip was
        one line, and the three other pins below went red on their own.
        """
        self.seed_reported()
        attempts = []

        def exploding_close(sid, session, wait_s=5.0):
            attempts.append(sid)
            if len(attempts) == 1:
                raise RuntimeError("INJECTED: first kill dies")
            return True, "kill-pane fault:0.0"

        with mock.patch.object(fleet_children, "safe_close", exploding_close):
            with self.assertRaises(RuntimeError):
                self.tick()
            self.tick()
        self.assertEqual(attempts, [SID, SID])              # retried, once
        self.assertIsNotNone(self.load()[SID]["reaped_at"])


class F3_CorruptStore(TempStores):
    """A half-written lineage.json. The worst failure mode on this box.

    Fixed 2026-10-17 (fork-1): the store layer refuses to LOAD a broken store, and
    refuses to SAVE a shrinking one. The fault is kept -- what changed is that it
    is now handled instead of silently swallowed.
    """

    def truncate(self):
        c = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        other = dict(fleet_lineage.new_child(
            "01a1f000-0000-7000-9000-0000000000bb", "bystander", PARENT,
            "fault-parent", "an unrelated live unit", NOW, transcript=None))
        self.save({SID: c, other["sessionId"]: other})
        full = self.lineage_path.read_text()
        self.lineage_path.write_text(full[: len(full) // 2])
        self.assertRaises(json.JSONDecodeError, json.loads,
                          self.lineage_path.read_text())      # the fault fired

    def test_a_truncated_store_is_refused_loudly(self):
        self.truncate()
        with self.assertRaises(SystemExit) as ctx:
            fleet_children.load_lineage()
        msg = str(ctx.exception)
        self.assertIn("not valid JSON", msg)
        self.assertIn("refusing", msg)

    def test_an_empty_store_is_refused_unless_it_is_declared_intentional(self):
        self.lineage_path.write_text("")
        with self.assertRaises(SystemExit) as ctx:
            fleet_children.load_lineage()
        self.assertIn("EMPTY", str(ctx.exception))
        # ... and the escape hatch is explicit, not implicit.
        self.assertEqual(fleet_children.load_lineage(allow_empty=True)["children"], {})

    def test_a_store_that_parses_to_zero_children_is_refused(self):
        self.lineage_path.write_text(json.dumps({"version": 1, "children": {}}))
        with self.assertRaises(SystemExit) as ctx:
            fleet_children.load_lineage()
        self.assertIn("no children", str(ctx.exception))

    def test_a_missing_store_is_still_a_new_fleet(self):
        self.assertFalse(self.lineage_path.exists())
        self.assertEqual(fleet_children.load_lineage(), {"version": 1, "children": {}})

    def test_a_truncated_store_can_no_longer_erase_the_fleet(self):
        """The severity test, inverted: the erase path is unreachable by design.

        It used to read {} and write it back, losing both records without a word.
        Now the read raises, so the routine adopt that would have saved the empty
        dict never gets to run. The fault stays in the suite: it now proves the
        damage cannot happen.
        """
        self.truncate()
        with self.assertRaises(SystemExit):
            fleet_children.load_lineage()
        on_disk = self.lineage_path.read_text()
        self.assertIn("01a1f000", on_disk)   # still broken, but not emptied
        self.assertNotEqual(json.loads(on_disk) if on_disk.strip().endswith("}")
                          else None, {"version": 1, "children": {}})

    def test_a_save_that_would_shrink_the_store_is_refused(self):
        c = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        self.save({SID: c})
        with self.assertRaises(SystemExit) as ctx:
            fleet_children.save_lineage({"version": 1, "children": {}})
        self.assertIn("shrinks", str(ctx.exception))
        self.assertIn(SID, self.load())            # nothing was written
        # An intended removal says so.
        fleet_children.save_lineage({"version": 1, "children": {}}, allow_shrink=True)

    def test_skipping_never_turns_a_truncated_store_into_a_quiet_no_op(self):
        """The layer trap: F3's skip must not become F1's silence.

        fork-1 pins this on the code side (`test_skipping_never_turns_a_truncated_
        store_into_a_quiet_no_op`). This is the fault-suite side of the same
        agreement: a store that parses to nothing is NOT five skippable records.
        """
        self.truncate()
        with self.assertRaises(SystemExit):
            self.tick(apply=True)                  # the tick refuses, it does not skip
        self.assertEqual([r for r in self.rows()], [])


class F4_TwoRealTicksRacing(TempStores):
    """Two OS processes, the same stores, the same child. Real, not threaded.

    The race is the one test_fleet_lock.py describes for the lock itself: a
    read-modify-write of a whole JSON file. Here the question is whether
    `_cascade` -- the thing the cron actually runs -- is inside that lock.
    """

    TICK = textwrap.dedent("""
        import json, sys, time
        sys.path.insert(0, {bin!r})
        from types import SimpleNamespace
        import fleet_children

        tmp = {tmp!r}
        from pathlib import Path
        fleet_children.LINEAGE = Path(tmp) / "lineage.json"
        fleet_children.LEDGER = Path(tmp) / "reap-ledger.jsonl"
        fleet_children.DIGESTS = Path(tmp) / "digests"
        fleet_children.SENTINEL_REPORT = Path(tmp) / "report.json"
        fleet_children._proc_state = lambda pid: "gone"
        fleet_children._proc_identity = lambda pid: ("ok", "R", 777)

        class W:
            def entries(self):
                return [{{
                    "sessionId": {sid!r}, "name": "fault-child", "alive": True,
                    "reported": True, "session": "fault:0.0", "pi_status": "",
                    "phase": "", "pending_ask": None, "context": "1%",
                    "branch": None, "head": None, "merged": False,
                    "worktree": None}}]
            def find(self, name):
                return None
            def sh(self, argv, **kw):
                joined = " ".join(str(a) for a in argv)
                if joined.startswith("pi-tmux --live-owner"):
                    return 0, "4242\\t" + {sid!r} + "\\tfault-child\\tpts/1\\tidle\\tfault:0.0\\n", ""
                if joined.startswith("tmux list-panes"):
                    return 0, "0.0 4242\\n", ""
                if joined.startswith("tmux kill-pane") or joined.startswith("pi-tmux --close"):
                    with open(tmp + "/closes.log", "a") as fh:
                        fh.write(joined + "\\n")
                    time.sleep(0.15)          # widen the window on purpose
                    return 0, "", ""
                return 0, "", ""
            def owner_tmux_of(self, sid):
                return "fault:0.0"
            def clip(self, s, n):
                return str(s or "")[:n]
            def die(self, msg):
                raise SystemExit(msg)
            def base(self):
                return "origin/main"
            def spawn_reviewer(self, c, now):
                with open(tmp + "/reviewers.log", "a") as fh:
                    fh.write(c["sessionId"] + "\\n")
                return "fleet-review-x"

        fleet_children.configure(
            entries=W().entries, find=W().find, sh=W().sh,
            owner_tmux_of=W().owner_tmux_of, clip=W().clip, die=W().die,
            base=W().base, spawn_reviewer=W().spawn_reviewer)

        time.sleep(float(sys.argv[1]))          # line them up at the gate
        a = SimpleNamespace(apply=True, accept_silent=False, idle_after_min=30,
                            grace_min=10, verify_budget_s=90, quiet=True)
        fleet_children._cascade(a)
        """)

    def test_two_ticks_at_once_produce_one_reap_one_row_one_close(self):
        self.world.live = [self.entry(alive=True, pid=4242)]
        c = fleet_lineage.mark_reported(child(pid=4242, pid_start=777),
                                        "did the thing", NOW - 60)
        self.save({SID: c})
        script = self.tmp / "tick.py"
        script.write_text(self.TICK.format(bin=str(BIN), tmp=str(self.tmp),
                                           sid=SID))
        env = {**os.environ, "FLEET_LOCK": str(self.tmp / "lock"),
               "FLEET_LINEAGE": str(self.lineage_path),
               "FLEET_LEDGER": str(self.ledger)}
        procs = [subprocess.Popen([PY, str(script), str(offset)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for offset in (0.0, 0.05)]
        errs = []
        for p in procs:
            _, err = p.communicate(timeout=120)
            if p.returncode != 0:
                errs.append(err.decode()[-400:])
        self.assertEqual(errs, [])                     # both really ran
        closes = (self.tmp / "closes.log")
        self.assertTrue(closes.exists(), "neither process reached the kill")
        n_close = len(closes.read_text().splitlines())
        n_rows = [r["action"] for r in self.rows()].count("reap")

        # Both processes were genuinely racing; now the invariant.
        self.assertEqual(n_close, 1, f"two ticks killed the pane {n_close} times")
        self.assertEqual(n_rows, 1, f"{n_rows} reap rows for one death")
        self.assertIsNotNone(self.load()[SID]["reaped_at"])


class F5_Replay50x(TempStores):
    """The same tick, fifty times. Nothing may drift."""

    def test_fifty_replays_are_byte_identical_and_silent(self):
        self.seed_reported()
        self.tick()
        state1 = self.lineage_path.read_bytes()
        digest1 = next(self.digests.glob("*.md")).read_bytes()
        rows1 = self.rows()
        shell1 = len(self.world.shell)
        self.assertEqual(len(rows1), 1)                # the first tick did the work

        for _ in range(49):
            self.tick()
        self.assertEqual(self.lineage_path.read_bytes(), state1)
        self.assertEqual(next(self.digests.glob("*.md")).read_bytes(), digest1)
        self.assertEqual(self.rows(), rows1)           # no second row
        self.assertEqual(len(self.world.shell), shell1,
                         "a replayed tick re-checked the world: "
                         f"{self.world.shell[shell1:]}")

    def test_fifty_replays_send_one_notification(self):
        """The dead-parent case announces once, then holds. Fifty ticks, one message.

        A plain HOLD (busy child) is deliberately silent -- it is not an event --
        so the notification path is exercised here with the state that NOTIFYs.
        """
        c = child()                       # never reported
        self.save({SID: c})
        self.world.live = []              # child gone, parent gone
        for _ in range(50):
            self.tick()
        msgs = [s for s in self.world.shell if s and s[0] == "pi-msg"]
        self.assertEqual(len(msgs), 1, f"{len(msgs)} notifications for one state")
        self.assertIn("parent is gone", msgs[0][-1])


class F6_RecycledPid(TempStores):
    """The pid in the owner table now belongs to an unrelated process.

    A recycled pid is the quiet version of killing the wrong pane: nothing
    looks wrong, and the close lands on a stranger.
    """

    def test_a_recycled_pid_in_a_multi_pane_session_is_refused(self):
        """The owner pid is not one of the session's panes: refuse to guess."""
        self.seed_reported()
        self.world.pane_pid = "4242"
        self.world.panes_out = "0.0 9999\n0.1 8888\n"   # neither is 4242
        ok, how = fleet_children.safe_close(SID, "fault:0.0")
        self.assertFalse(ok, "closed a pane it could not prove was ours")
        self.assertIn("refusing to guess", how)
        self.assertEqual([x for x in self.world.shell
                          if x[:2] == ["tmux", "kill-pane"]], [])

    def test_a_stranger_pid_is_never_closed_by_session_name(self):
        """INVERTED 2026-10-17. This test used to assert the opposite.

        With exactly one pane and a pid that was not ours, `safe_close` fell back
        to `pi-tmux --close <session>` and killed whatever the session name
        resolved to. The green pin and the expectedFailure were the same finding
        from two sides; when the kill path got identity, the pin had to be
        inverted, NOT repaired by restoring the old behaviour.
        """
        self.seed_reported()
        self.world.pane_pid = "4242"
        self.world.panes_out = "0.0 9999\n"       # the pane is not ours
        ok, how = fleet_children.safe_close(SID, "fault:0.0")
        self.assertFalse(ok, "closed a pane whose pid it could not match")
        self.assertIn("not the owner pid", how)
        self.assertEqual([x for x in self.world.shell
                          if x[:2] == ["tmux", "kill-pane"]], [])
        self.assertEqual([x for x in self.world.shell
                          if x[:2] == ["pi-tmux", "--close"]], [])

    def test_an_int_owner_pid_and_a_string_pane_pid_still_match(self):
        """The over-correction that would have broken every legitimate kill.

        `_owner_pid` parses text, so it returns a string; `tmux list-panes` also
        returns text. A fix that compared them as values rather than as strings
        would refuse every real close -- safe, and useless.
        """
        self.seed_reported()
        self.world.panes_out = "0.0 4242\n"
        with mock.patch.object(fleet_children, "_owner_pid",
                               lambda sid: (4242, "fault:0.0")):   # int
            ok, how = fleet_children.safe_close(SID, "fault:0.0")  # pane: "4242"
        self.assertTrue(ok, f"a legitimate kill was refused: {how}")
        # One pane, pid verified: closing by session name is correct here.
        self.assertTrue(how.startswith(("kill-pane", "pi-tmux --close")), how)

    def test_a_zombie_is_treated_as_dead_not_as_alive(self):
        with mock.patch.object(fleet_children, "_proc_state", lambda pid: "Z"):
            self.seed_reported()
            self.world.panes_out = "0.0 4242\n"
            ok, how = fleet_children.safe_close(SID, "fault:0.0")
        self.assertTrue(ok)
        self.assertIn("zombie", how)

    def test_a_pid_that_survives_the_kill_is_reported_as_a_failure(self):
        self.seed_reported()
        self.world.panes_out = "0.0 4242\n"
        with mock.patch.object(fleet_children, "_proc_state", lambda pid: "R"), \
                mock.patch.object(fleet_children.time, "sleep", lambda s: None):
            ok, how = fleet_children.safe_close(SID, "fault:0.0", wait_s=0.01)
        self.assertFalse(ok, "claimed a kill that did not happen")
        self.assertIn("still alive", how)


class F7_OldSchema(TempStores):
    """Twenty records from before the fields existed. None may raise."""

    def sparse_records(self):
        out = {}
        for i in range(20):
            sid = f"01a1f000-0000-7000-9000-{i:012d}"
            rec = {"sessionId": sid, "name": f"old-{i}"}
            if i % 3 == 1:
                rec.update({"parent_session_id": None, "reported_at": None,
                            "claims": None, "digest": None, "keep_until": None,
                            "protected": None, "role": None})
            if i % 5 == 0:
                rec = {"name": f"old-{i}"}       # not even a sessionId
            if i % 7 == 0:
                rec.update({"sessionId": sid, "reaped_at": None,
                            "spawned_at": None, "transcript": None})
            out[sid if "sessionId" in rec else f"bogus-{i}"] = rec
        return out

    def test_a_record_without_a_session_id_does_not_abort_the_whole_tick(self):
        """Fixed 2026-10-17 at the RECORD layer, not the store layer.

        The record is skipped and named on every tick; it is not dropped, and it
        still counts when `load_lineage` decides whether the store looks whole.
        """
        self.save(self.sparse_records())
        self.world.live = []
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.tick(apply=False)        # must not raise
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertIn("not processed, not removed", out)
        skipped = [l for l in out.splitlines() if "SKIP" in l]
        self.assertGreaterEqual(len(skipped), 1)
        # Not removed: the store is untouched by a dry run of a broken record.
        after = self.load()
        self.assertEqual(len(after), len(self.sparse_records()))

    def test_a_malformed_record_is_skipped_while_the_good_ones_are_processed(self):
        """The point of skipping: the tick keeps working for everyone else."""
        good = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        bad = {"name": "no-session-id"}                 # the poison pill
        self.save({SID: good, "bogus-key": bad})
        self.world.live = [self.entry(alive=True, pid=4242)]
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.tick(apply=True)
        out = buf.getvalue()
        self.assertIn("SKIP", out)
        self.assertIn(SID, self.load())
        self.assertIn("bogus-key", self.load(),
                      "the malformed record was DROPPED, not skipped")

    def test_twenty_legacy_records_raise_nothing_and_get_one_verdict_each(self):
        # Records WITH a sessionId: the store always keys by one. The nastier
        # case (a record missing its key) is pinned above, by
        # test_a_record_without_a_session_id_does_not_abort_the_whole_tick.
        recs = {k: v for k, v in self.sparse_records().items()
                if k.startswith("01a1f000")}
        self.save(recs)
        self.world.live = []
        out = []
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = self.tick(apply=False)          # dry run: propose only
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        lines = [l for l in out.splitlines() if l.startswith("  ")
                 and any(v in l for v in ("HOLD", "REAP", "DECIDE", "FAILED",
                                          "NOTIFY", "GATED"))]
        self.assertEqual(len(lines), len(recs),
                         f"expected one verdict per record, got {len(lines)}:\n{out}")
        # And nothing was mutated by a dry run.
        self.assertEqual(self.load(), recs)

    def test_twenty_legacy_records_on_the_apply_path_do_not_raise(self):
        self.save(self.sparse_records())
        self.world.live = []
        n = 0
        for sid, rec in self.sparse_records().items():
            facts = {"alive": False, "busy": False, "pending_ask": None,
                     "parent_alive": False, "parent_finished": False,
                     "last_seen": None}
            action, why = fleet_lineage.verdict(rec, facts, NOW,
                                                accept_silent=True)
            self.assertIn(action, (fleet_lineage.REAP, fleet_lineage.HOLD,
                                   fleet_lineage.NOTIFY))
            self.assertTrue(why)
            n += 1
        self.assertEqual(n, 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ═══════════════════════════════════════════════════════════════════════════
# The bundle tools: export / import / restore under interruption
# ═══════════════════════════════════════════════════════════════════════════
class BundleWorld(TempStores):
    """A small but real store + ledger + digest, so bundles are real bundles."""

    def setUp(self):
        super().setUp()
        self.bundles = self.tmp / "bundles"
        self.backups = self.tmp / "backups"
        self.bundles.mkdir()
        c = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        self.save({SID: c})
        self.digests.mkdir(parents=True, exist_ok=True)
        dpath = self.digests / "2026-10-17-fault-child-01a1f000.md"
        dpath.write_text("# receipt\nthe account body\n")
        kids = self.load()
        kids[SID]["digest"] = str(dpath)
        self.save(kids)
        self.ledger.write_text(json.dumps({"ts": NOW - 30, "action": "reap",
                                           "sessionId": SID, "why": "reported"}) + "\n")

    def make_bundle(self, name="bundle.txt"):
        out = self.bundles / name
        # Pass the stores explicitly: build_bundle's defaults are the REAL
        # /root/.fleet, and a fault suite must never read (or imply) live data.
        lines, header = fleet_bundle.export(out=out, lineage=self.lineage_path,
                                            ledger=self.ledger,
                                            digests_dir=self.digests)
        return out, lines, header

    def verify(self, path):
        return fleet_bundle.verify(path, lineage=self.lineage_path,
                                   ledger=self.ledger, digests_dir=self.digests)

    def import_bundle(self, path, **kw):
        return fleet_bundle.import_bundle(path, lineage=self.lineage_path,
                                          ledger=self.ledger,
                                          backups_dir=self.backups, **kw)


class F8_ExportInterrupted(BundleWorld):
    """A bundle is a receipt for the whole fleet. A partial one is a false receipt."""

    def test_a_bundle_cut_mid_line_never_verifies_clean(self):
        out, lines, _ = self.make_bundle()
        good = out.read_bytes()
        self.assertEqual(self.verify(out)["verdict"], "CLEAN")   # the fault is real
        out.write_bytes(good[: len(good) // 2])                  # killed mid-write
        res = self.verify(out)
        self.assertNotEqual(res["verdict"], "CLEAN")
        self.assertNotEqual(res["exit_code"], 0)
        self.assertTrue(res["problems"])

    def test_a_bundle_missing_its_tail_is_not_clean(self):
        out, lines, _ = self.make_bundle()
        self.assertGreater(len(lines), 3)
        out.write_text("\n".join(lines[:-2]) + "\n")   # whole lines dropped
        res = self.verify(out)
        self.assertNotEqual(res["verdict"], "CLEAN")
        self.assertTrue(any("section" in p for p in res["problems"]), res["problems"])

    def test_an_export_killed_mid_write_leaves_the_partial_STAGED_not_in_place(self):
        """F8 FIXED 2026-10-17 (was: the partial landed at the real path).

        A reader that trusts the path without verifying reads a half fleet. After
        the fix the half-write can only exist as `*.tmp`, which `verify` refuses
        by name -- so a killed export degrades to "no bundle", never to "a wrong
        one".
        """
        out = self.bundles / "bundle.txt"
        boom = _HalfWriter(out, fraction=0.5)
        with mock.patch.object(fleet_bundle, "Path", boom.wrap):
            with self.assertRaises(OSError) as ctx:
                fleet_bundle.export(out=out)
        self.assertIn("INJECTED", str(ctx.exception))
        self.assertFalse(out.exists(), "a half bundle landed at the real path")
        staged = list(self.bundles.glob("*.tmp"))
        self.assertEqual(len(staged), 1, "nothing was staged, so nothing was fsynced")
        self.assertTrue(staged[0].read_text(), "an empty stage is a different fault")
        self.assertEqual(self.verify(out)["verdict"], "UNREADABLE")
        self.assertEqual(self.verify(staged[0])["verdict"], "UNREADABLE",
                         "a leftover stage must never be taken for a bundle")

    def test_export_must_be_atomic_so_a_crash_cannot_leave_a_half_bundle(self):
        """The pin that used to be an expectedFailure, now the contract."""
        out = self.bundles / "bundle.txt"
        boom = _HalfWriter(out, fraction=0.5)
        with mock.patch.object(fleet_bundle, "Path", boom.wrap):
            with self.assertRaises(OSError):
                fleet_bundle.export(out=out)
        self.assertFalse(out.exists(),
                         "a crashed export left a half bundle at the real path; "
                         "it must stage to .tmp and rename")

    def test_a_completed_export_leaves_no_stage_behind(self):
        out, _, _ = self.make_bundle("clean.txt")
        self.assertEqual(out.read_text(), out.read_text())
        self.assertEqual(list(self.bundles.glob("clean.txt.tmp")), [],
                         "os.replace must consume the stage")

    def test_a_leftover_tmp_is_never_taken_for_a_bundle(self):
        out, lines, _ = self.make_bundle()
        tmp = self.bundles / "bundle.txt.tmp"
        tmp.write_text("\n".join(lines[:2]) + "\n")     # half a bundle, staged
        self.assertEqual(self.verify(out)["verdict"], "CLEAN")     # the real one
        self.assertNotEqual(self.verify(tmp)["verdict"], "CLEAN")  # the leftover
        # And the exporter does not glob: it writes the path it was given.
        out2, _, _ = self.make_bundle("second.txt")
        self.assertEqual(sorted(p.name for p in self.bundles.iterdir()),
                         # digest bodies live elsewhere; no .tmp was adopted
                         ["bundle.txt", "bundle.txt.tmp", "second.txt"])


class _HalfWriter:
    """A Path double whose write_text dies after `fraction` of the bytes.

    The point is not to break Python: `Path.write_text` really does write in
    place, so a process killed mid-call leaves a prefix on disk. This reproduces
    that shape deterministically.
    """

    def __init__(self, only, fraction=0.5, killed=None):
        self.only = Path(only)
        self.fraction = fraction
        self.killed = killed if killed is not None else []   # paths it fired on

    def __call__(self, *a, **k):
        real = Path(*a, **k)
        return _Shim(real, self.only, self.fraction, self.killed)

    wrap = property(lambda self: self)


class _Shim:
    def __init__(self, real, only, fraction, killed=None, exact=False):
        self._p, self._only, self._fraction = real, only, fraction
        self._killed = killed if killed is not None else []
        self._exact = exact          # True: only the requested path (the old hole)

    def __getattr__(self, name):
        return getattr(self._p, name)

    def __truediv__(self, other):
        return _Shim(self._p / other, self._only, self._fraction, self._killed,
                     self._exact)

    def _matches(self):
        return self._p == self._only if self._exact else self._p.parent == self._only.parent

    def __fspath__(self):
        return str(self._p)

    def write_text(self, text, **kw):
        # F8 FIXED (tundra-fork-3, 2026-10-17): export stages to <out>.tmp and
        # os.replace()s it, so the injected death must fire on the STAGED write.
        # Any file in the target's directory counts; the old exact-path rule would
        # have let the crash pass silently and made this suite lie.
        if self._matches():
            blob = text[: max(1, int(len(text) * self._fraction))]
            self._p.write_text(blob)
            self._killed.append(str(self._p))
            raise OSError("INJECTED: killed mid-write")
        return self._p.write_text(text, **kw)


class F9_ImportInterrupted(BundleWorld):
    """Import: backup first, then the store. A crash between them must be safe."""

    def test_crash_between_backup_and_store_write_leaves_the_store_old(self):
        out, lines, _ = self.make_bundle()
        before = self.lineage_path.read_bytes()
        self.die_on_store_payload()                 # raise just before the write
        with self.assertRaises(OSError) as ctx:
            self.import_bundle(out, apply=True)
        self.assertIn("INJECTED", str(ctx.exception))
        self.assertEqual(self.lineage_path.read_bytes(), before)
        json.loads(self.lineage_path.read_text())   # old, and still parseable

    def die_on_store_payload(self):
        real = fleet_bundle.json.dumps

        def dumps(obj, *a, **k):
            if isinstance(obj, dict) and "children" in obj:
                raise OSError("INJECTED: killed between backup and write")
            return real(obj, *a, **k)
        self._dumps = mock.patch.object(fleet_bundle.json, "dumps", dumps)
        self._dumps.start()
        self.addCleanup(self._dumps.stop)

    def test_a_crashed_import_leaves_a_backup_and_reruns_to_convergence(self):
        out, lines, _ = self.make_bundle()
        before = self.lineage_path.read_text()
        self.die_on_store_payload()
        with self.assertRaises(OSError) as ctx:
            self.import_bundle(out, apply=True)
        self.assertIn("INJECTED", str(ctx.exception))
        made = list(self.backups.glob("lineage-bundle-*.json"))
        self.assertEqual(len(made), 1, "no backup exists, so nothing is recoverable")
        self.assertEqual(made[0].read_text(), before)   # the backup is the old store
        self._dumps.stop()                              # the crash is over
        self.assertEqual([r for r in self.rows() if r["action"] == "restore"], [])

        res = self.import_bundle(out, apply=True)       # the re-run
        self.assertTrue(res["written"])
        self.assertEqual([r["action"] for r in self.rows()].count("restore"), 1)
        self.assertEqual(self.load(), self.load())      # parses, nothing half

    def test_the_backup_is_taken_before_anything_is_written(self):
        """Order, observed rather than asserted from the source."""
        out, lines, _ = self.make_bundle()
        seen = []
        real_text, real_bytes = Path.write_text, Path.write_bytes

        def spy_text(self_path, *a, **k):
            seen.append(str(self_path))
            return real_text(self_path, *a, **k)

        def spy_bytes(self_path, *a, **k):
            seen.append(str(self_path))     # the backup is written with write_bytes
            return real_bytes(self_path, *a, **k)

        with mock.patch.object(Path, "write_text", spy_text), \
                mock.patch.object(Path, "write_bytes", spy_bytes):
            self.import_bundle(out, apply=True)
        backup_i = next(i for i, s in enumerate(seen) if "lineage-bundle-" in s)
        store_i = next(i for i, s in enumerate(seen) if s.endswith("lineage.json"))
        self.assertLess(backup_i, store_i, "the store was written before the backup")


class F10_RestoreInterrupted(BundleWorld):
    """restore --apply: backup, then store, then the ledger row."""

    def proposal(self):
        kids = self.load()
        kids[SID] = dict(kids[SID], why="restored from evidence")
        return {"version": 1, "children": kids}

    def test_crash_after_backup_before_the_store_write_converges_on_rerun(self):
        prop = self.proposal()
        before = self.lineage_path.read_text()
        real = fleet_restore.json.dumps

        def dumps(obj, *a, **k):
            if isinstance(obj, dict) and "children" in obj:
                raise OSError("INJECTED: killed after the backup")
            return real(obj, *a, **k)

        with mock.patch.object(fleet_restore.json, "dumps", dumps):
            with self.assertRaises(OSError) as ctx:
                fleet_restore.apply_proposal(prop, lineage=self.lineage_path,
                                             backups_dir=self.backups,
                                             ledger=self.ledger, now=NOW)
        self.assertIn("INJECTED", str(ctx.exception))
        self.assertEqual(self.lineage_path.read_text(), before)     # old, whole
        self.assertEqual(len(list(self.backups.glob("lineage-restore-*.json"))), 1)
        self.assertEqual([r for r in self.rows() if r["action"] == "restore"], [])

        res = fleet_restore.apply_proposal(prop, lineage=self.lineage_path,
                                           backups_dir=self.backups,
                                           ledger=self.ledger, now=NOW + 5)
        self.assertTrue(res["written"])
        self.assertEqual([r["action"] for r in self.rows()].count("restore"), 1,
                         "the re-run duplicated the ledger row")
        self.assertEqual(self.load()[SID]["why"], "restored from evidence")

    def test_a_restore_that_would_shrink_the_store_refuses_after_the_backup(self):
        """The backup is taken before the shrink check, so nothing is written."""
        prop = {"version": 1, "children": {}}       # everything would be lost
        res = fleet_restore.apply_proposal(prop, lineage=self.lineage_path,
                                           backups_dir=self.backups,
                                           ledger=self.ledger, now=NOW)
        self.assertFalse(res["written"])
        self.assertIn("shrink", res["reason"])
        self.assertIn(SID, self.load())              # still there
        self.assertEqual([r for r in self.rows() if r["action"] == "restore"], [])


class F11_HeaderIsAClaimToo(BundleWorld):
    """A bundle header says how many records it carries. Believe it, and verify it."""

    def forge(self, out, drop=1, fix_checksum=False):
        """Drop lineage lines; optionally recompute the section checksum."""
        lines = out.read_text().splitlines()
        header = json.loads(lines[0])
        body = [l for l in lines[1:] if json.loads(l)["kind"] != "lineage"]
        lineage = [l for l in lines[1:] if json.loads(l)["kind"] == "lineage"]
        self.assertGreater(len(lineage), drop)
        kept = lineage[:-drop]
        if fix_checksum:
            header["sections"]["lineage"]["sha256"] = fleet_bundle._sha(kept)
            # count deliberately left at the original, larger number
        out.write_text("\n".join([fleet_bundle._canon(header)] + kept + body) + "\n")
        return header

    def test_a_bundle_missing_a_record_is_caught_by_the_checksum(self):
        out, lines, header = self.make_bundle()
        self.assertEqual(self.verify(out)["verdict"], "CLEAN")
        # A second record, so dropping one still leaves a plausible bundle.
        kids = self.load()
        kids["01a1f000-0000-7000-9000-0000000000dd"] = dict(
            kids[SID], sessionId="01a1f000-0000-7000-9000-0000000000dd",
            name="second")
        self.save(kids)
        out2, _, _ = self.make_bundle("two.txt")
        self.assertEqual(self.verify(out2)["verdict"], "CLEAN")
        self.forge(out2, drop=1)
        res = self.verify(out2)
        self.assertNotEqual(res["verdict"], "CLEAN")
        self.assertTrue(any("checksum" in p for p in res["problems"]), res["problems"])

    def test_a_header_that_lies_about_the_count_is_caught_even_with_a_fixed_checksum(self):
        kids = self.load()
        kids["01a1f000-0000-7000-9000-0000000000dd"] = dict(
            kids[SID], sessionId="01a1f000-0000-7000-9000-0000000000dd",
            name="second")
        self.save(kids)
        out, _, header = self.make_bundle()
        want = header["sections"]["lineage"]["count"]
        self.assertEqual(want, 2)
        h = self.forge(out, drop=1, fix_checksum=True)
        self.assertEqual(h["sections"]["lineage"]["count"], want)   # still says 2
        res = self.verify(out)
        self.assertNotEqual(res["verdict"], "CLEAN",
                            "a fixed-up checksum hid a short bundle: the count is "
                            "a claim too, and it was not checked independently")
        self.assertEqual(res["sections"]["lineage"]["count"], 1)
        self.assertEqual(res["sections"]["lineage"]["expected_count"], 2)
        self.assertTrue(any("expected 2" in p for p in res["problems"]), res["problems"])

    def test_a_header_count_that_matches_a_truncated_body_still_fails_the_store_crosscheck(self):
        kids = self.load()
        kids["01a1f000-0000-7000-9000-0000000000dd"] = dict(
            kids[SID], sessionId="01a1f000-0000-7000-9000-0000000000dd",
            name="second")
        self.save(kids)
        out, _, header = self.make_bundle()
        # Forge consistently: drop a record AND fix both count and checksum.
        lines = out.read_text().splitlines()
        h = json.loads(lines[0])
        lineage = [l for l in lines[1:] if json.loads(l)["kind"] == "lineage"]
        body = [l for l in lines[1:] if json.loads(l)["kind"] != "lineage"]
        kept = lineage[:-1]
        h["sections"]["lineage"] = {"count": len(kept),
                                    "sha256": fleet_bundle._sha(kept)}
        out.write_text("\n".join([fleet_bundle._canon(h)] + kept + body) + "\n")
        res = self.verify(out)
        self.assertNotEqual(res["verdict"], "CLEAN",
                            "an internally consistent bundle that lost a record "
                            "must not verify against the live store")
        self.assertTrue(any("only in the live store" in p for p in res["problems"]),
                        res["problems"])


class F12_PruneIsATombstone(BundleWorld):
    """Pruning deletes a record. The ledger row is the only witness left.

    Reported by fork-3 (2026-10-17): four pruned `fleet-dp-*` records reappeared
    in the store three minutes after their prune rows, because the posthumous
    scan re-derived them from evidence. A prune that a later automated pass can
    undo is the dismiss bug one layer out: an automated pass reversing a human's
    decision. The tombstone (`pruned_sids()`) now answers first.
    """

    VICTIM = "01a1f000-0000-7000-9000-0000000000ee"

    def setUp(self):
        super().setUp()
        # A bystander keeps the store non-empty: load_lineage refuses a store
        # that parses to zero children, and an empty store would make this test
        # pass for the wrong reason.
        self.save({SID: fleet_lineage.mark_reported(
            child(), "did the thing", NOW - 60)})
        self.world.live = [self.entry(alive=True, pid=4242)]
        self._patch("_ended_recently", lambda: [])

    def prune_row(self, sid, raw=None):
        row = raw if raw is not None else json.dumps(
            {"ts": NOW - 30, "action": "prune", "sessionId": sid,
             "why": "reaped fixture, removed with --prune"})
        with self.ledger.open("a") as fh:
            fh.write(row + "\n")

    def live_victim(self):
        return {"sessionId": self.VICTIM, "name": "fleet-dp-143359",
                "alive": True, "pid": 4242, "reported": False,
                "session": "fault:0.1", "pi_status": "", "phase": "",
                "pending_ask": None, "context": "1%", "branch": None,
                "head": None, "merged": False, "worktree": None}

    def scan(self):
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = fleet_children.cmd_scan(SimpleNamespace(apply=True))
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_a_pruned_session_is_not_resurrected_by_a_later_scan(self):
        self.world.live.append(self.live_victim())
        self.prune_row(self.VICTIM)
        out = self.scan()
        self.assertNotIn(self.VICTIM, self.load(),
                         f"a pruned session came back:\n{out}")

    def test_scan_still_enrols_a_session_that_was_never_pruned(self):
        """The control. Without it, the test above could pass because scan is broken."""
        self.world.live.append(self.live_victim())
        out = self.scan()
        self.assertIn(self.VICTIM, self.load(), out)
        self.assertTrue(self.load()[self.VICTIM]["protected"])

    def test_a_corrupt_ledger_line_does_not_unbury_a_pruned_session(self):
        """A half-written ledger must not turn every tombstone into permission."""
        self.world.live.append(self.live_victim())
        self.prune_row(None, raw='{"action": "prune", "sessionId": ')   # truncated
        self.prune_row(self.VICTIM)                                     # the real one
        self.scan()
        self.assertNotIn(self.VICTIM, self.load())

    def test_the_auditor_still_calls_a_resurrected_record_a_phantom(self):
        """The net under the tombstone: if scan regresses, this must scream."""
        self.save({SID: self.load()[SID],
                   self.VICTIM: fleet_lineage.new_child(
                       self.VICTIM, "fleet-dp-143359", None, None,
                       "resurrected by a scan", NOW, transcript=None)})
        rows = [{"ts": NOW - 30, "action": "prune", "sessionId": self.VICTIM,
                 "_line": 1}]
        verdict = fleet_audit.check_write_ahead(self.load(), rows)
        self.assertEqual(verdict.status, fleet_audit.FAIL)
        self.assertIn("deletion that never happened", verdict.detail)
        self.assertTrue(any(self.VICTIM in i for i in (verdict.ids or [])),
                        verdict.ids)


class F13_TheFaultTestItself(BundleWorld):
    """The lesson from F8, pinned: an injection must be as wide as the write path.

    Fork-3's finding was not a product bug. `_Shim` fired only when the write path
    equalled the requested bundle path; the moment export started staging to
    `<out>.tmp`, the injection stopped firing and the fault test went GREEN —
    certifying an export that had never been interrupted. A fault that does not
    fire is worse than no fault: it produces a green suite about nothing.

    So this class attacks the harness instead of the product, and states the
    ceiling out loud rather than leaving it as a silent gap.
    """

    def test_the_injection_fires_on_the_staged_write_not_the_requested_path(self):
        out = self.bundles / "bundle.txt"
        killed = []
        shim = _HalfWriter(out, fraction=0.5, killed=killed)
        with mock.patch.object(fleet_bundle, "Path", shim.wrap):
            with self.assertRaises(OSError) as ctx:
                fleet_bundle.export(out=out)
        self.assertIn("INJECTED", str(ctx.exception))     # it fired at all
        self.assertEqual(len(killed), 1, f"nothing was killed: {killed}")
        self.assertNotEqual(killed[0], str(out),
                            "the injection fired on the requested path — good luck "
                            "if the code later stages, renames or chunks its write")
        self.assertTrue(killed[0].endswith(".tmp"), killed[0])
        self.assertFalse(out.exists(), "the real path must never carry the half")

    def test_a_narrow_injection_certifies_a_bundle_that_was_never_interrupted(self):
        """The bug, demonstrated rather than described.

        With the OLD rule (fire only on the exact path) the export completes:
        no exception, a complete bundle, `verify` says CLEAN. The suite would have
        reported the fault as handled while the code under it had changed shape.
        """
        out = self.bundles / "bundle.txt"
        narrow = _NarrowShim(out)
        with mock.patch.object(fleet_bundle, "Path", narrow.wrap):
            # Stores passed explicitly: the defaults are the LIVE /root/.fleet.
            # (This test failed on exactly that the first time it ran -- the scar
            # from §17, catching itself.)
            fleet_bundle.export(out=out, lineage=self.lineage_path,
                                ledger=self.ledger, digests_dir=self.digests)
        self.assertTrue(out.exists())
        self.assertEqual(self.verify(out)["verdict"], "CLEAN")
        self.assertEqual(narrow.fired, 0,
                         "the narrow rule was supposed to miss — if it fired, this "
                         "demonstration no longer shows the hole")

    def test_the_injection_does_not_sabotage_unrelated_writes(self):
        """Wide is not the same as lawless: a sibling READ must be untouched."""
        out = self.bundles / "bundle.txt"
        out.write_text("existing bundle\n")
        shim = _HalfWriter(out)
        probe = shim(out)                                  # same dir, no write
        self.assertEqual(probe.read_text(), "existing bundle\n")

    def test_the_ceiling_staging_outside_the_watched_directory_goes_silent(self):
        """THE CEILING, stated as a test so nobody discovers it the hard way.

        The shim watches the requested path's DIRECTORY. If product code ever
        stages somewhere else — a temp dir, /var/tmp — the injection goes silent
        again and this suite would lie in exactly the way fork-3 caught. Watching
        the whole filesystem is not possible from a Path double, so this is a
        documented limit, not a bug: when a write path changes, the injection
        point has to be widened with it.
        """
        out = self.bundles / "bundle.txt"
        elsewhere = self.tmp / "other-dir"
        elsewhere.mkdir()
        shim = _HalfWriter(out)
        target = shim(elsewhere / "staged.txt")
        target.write_text("half")                          # no raise, by design
        self.assertTrue((elsewhere / "staged.txt").exists())
        self.assertEqual(shim.killed, [], "the shim claimed a kill it cannot see")
        self.assertFalse(out.exists())


class _NarrowShim(_HalfWriter):
    """The rule that was there before fork-3's fix: fire on the exact path only.

    Counts KILLS, not constructions: an injection that merely wraps a path without
    firing is exactly the silent hole this class demonstrates.
    """

    def __call__(self, *a, **k):
        return _Shim(Path(*a, **k), self.only, self.fraction, self.killed,
                     exact=True)

    @property
    def fired(self):
        return len(self.killed)


class F14_ReviewerNamesCollide(BundleWorld):
    """Sibling sessions share an 8-character prefix. Reviewer names are 8 chars.

    Found 2026-10-17 19:5x, not by a test: a remote peer spent two hours unable to
    tell two sessions apart because their ids share the first EIGHT characters --
    `01a14ace-18d2` and `01a14ace-2916` are different sessions, same `01a14ace`.
    That is not rare, it is the norm for concurrent spawns: the three tundra forks
    are `01a14a7b-3a74`, `-7785`, `-c178`.

    `_review_name(sid)` = "fleet-review-" + sid[:8], and `_reviewer_state` looks a
    reviewer up BY NAME in the live registry. So for three sibling forks:

        fleet-review-01a14a7b   <- all three

    Consequences, in order of how quietly they fail:
      1. the brief path REVIEW_DIR/<name>.brief.md is shared, so a second spawn
         OVERWRITES the brief of a reviewer that is still reading it;
      2. _reviewer_state cannot tell whose review is whose, so child B can be told
         it is "under review" by the session reviewing child A;
      3. _append_review writes "## Reviewed -- by <name>" into a CHILD's digest, so
         A's account can land in B's digest. Wrong meaning, no error.

    Eight characters is a display convenience, never an identity -- the same rule
    this suite enforces everywhere else. `sid[:13]` is enough to separate siblings
    (it carries the first UUID group) and costs nothing.
    """

    A = "01a14a7b-3a74-aaaa-8000-000000000001"
    B = "01a14a7b-7785-bbbb-8000-000000000002"
    C = "01a14a7b-c178-cccc-8000-000000000003"

    def test_sibling_sessions_get_distinct_reviewer_names(self):
        names = [fleet_children._review_name(sid) for sid in (self.A, self.B, self.C)]
        self.assertEqual(len(set(names)), 3,
                         f"three sessions share a reviewer name: {names}")

    def test_sibling_briefs_do_not_overwrite_each_other(self):
        paths = [fleet_children.REVIEW_DIR / (fleet_children._review_name(sid) + ".brief.md")
                 for sid in (self.A, self.B, self.C)]
        self.assertEqual(len({str(p) for p in paths}), 3,
                         f"a second reviewer would overwrite the first's brief: "
                         f"{[p.name for p in paths]}")
