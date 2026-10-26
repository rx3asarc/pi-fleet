"""U11 (evidence claims) — record_claims, claim_status, verify_claims, digest, cascade.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_claims -v
(unittest only — pytest is NOT installed on this box)

WHAT THIS FILE IS FOR
A worker's report is prose, and prose is cheap. The "claims" feature makes the
checkable half of a report mechanical: which files, which command proves it, and
what the worker could NOT pin down. Three seams are tested separately because
they fail differently:

  * `fleet_lineage.record_claims`  — normalisation only (sort, dedupe, trim).
  * `fleet_lineage.claim_status`   — a pure reduction of (claims, checks) to one
                                     word. Fixtures only, no I/O.
  * `fleet_children.verify_claims` — real files in /tmp, real `sh -c` commands.
                                     No network, no sessions, no tmux.
  * `fleet_children._cascade`      — the refusal: a FAILED claim must turn a
                                     sweep into a DECIDE, never a kill.

MEASURED, NOT ASSUMED (2026-10-17)
1. `verify_claims` reads a MODULE-GLOBAL `time` that `fleet_children` never
   imports (`json`, `os`, `sys`, `Path` only; every other function does a local
   `import time as _t`). A real call therefore raises NameError before any check
   lands — confirmed on the box, see TestTimeImportDrift. The logic is otherwise
   sound, so the classes that exercise it inject the missing global explicitly
   instead of pretending the bug is absent, and one test records which state was
   observed. Both states pass: fixing the import upstream must not break this
   suite.
2. `claim_status` says "partial" ONLY for a declared verify command that never
   ran (rc is None). A missing file is checked FIRST, so
   (missing file + rc None) is "failed", not "partial" — the docstring's
   "partial ... (missing file)" is wrong; the code is right.
3. Declared files with NO checks gathered (checks={}) reduce to "unclaimed", not
   "failed" — `claim_status` will not call a claim disproved when nothing was
   looked at.
4. `_cascade` only refuses on the word "failed". "partial" and "unclaimed" still
   reap, exactly as before, and the gate runs BEFORE the evidence check.
"""
import contextlib
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import fleet_children
import fleet_lineage

PID_START = 777
NOW = 1792242000                       # 2026-10-17T13:00:00Z, fixed
SID = "01a1dead-beef-7000-9000-0000000000c1"
PARENT_SID = "01a10000-aaaa-7000-9000-0000000000aa"
DEPS = ("entries", "find", "sh", "owner_tmux_of", "die", "clip", "base")


# ── fixtures ─────────────────────────────────────────────────────────────────
def claims(files=(), verify=None, unsure=None):
    """A bare claims dict, built the way the module builds it."""
    return fleet_lineage.record_claims({}, files, verify, unsure)["claims"]


def child(**kw):
    c = fleet_lineage.new_child(SID, "fleet-trust-tests-1", PARENT_SID,
                                "tundra", "trust the report or check it", NOW)
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


def good_deps(**over):
    """A full, inert dependency set. Nothing here can touch the machine."""
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


class TimeInjected(unittest.TestCase):
    """Inject the module-global `time` that fleet_children never imports.

    This is a test seam for a real defect (see TestTimeImportDrift), not a claim
    that the defect is fine: without it verify_claims() cannot run at all, and
    the alternative — re-implementing the function in the test — would assert
    nothing about the shipped code.
    """

    def setUp(self):
        super().setUp()
        if not hasattr(fleet_children, "time"):
            p = mock.patch.object(fleet_children, "time", time, create=True)
            p.start()
            self.addCleanup(p.stop)


class TempStores(unittest.TestCase):
    """LINEAGE / DIGESTS / LEDGER / SENTINEL_REPORT redirected into /tmp.

    The module reads those four as module-level Paths (defaults under
    /root/.fleet and /root/handoffs/_reaped). Patching here means no test can
    write to the real stores; create=True keeps it working if a build renames
    them.
    """

    def setUp(self):
        super().setUp()
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

    def patch_attr(self, name, value):
        p = mock.patch.object(fleet_children, name, value)
        p.start()
        self.addCleanup(p.stop)


# ── 0. the defect this file works around ─────────────────────────────────────
class TestTimeImportDrift(unittest.TestCase):
    """MEASURED: fleet_children has no module-level `import time`.

    `verify_claims` calls `time.strftime(...)` (existing file) and `time.time()`
    (any verify command) against a global that does not exist, so a real call
    raises NameError before it can check anything — every claim-bearing child
    would crash the sentinel rather than be verified. Written to pass in BOTH
    states so an upstream fix cannot break the suite; it records what it saw.
    """

    def test_verify_claims_state_is_recorded_honestly(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "present.txt"
            f.write_text("payload")
            rec = {"spawned_at": NOW, "claims": {"files": [str(f)], "verify": None}}
            if hasattr(fleet_children, "time"):
                out = fleet_children.verify_claims(rec)
                self.assertTrue(out["files"][0]["exists"])
                self.assertEqual(out["status"], "verified")
            else:
                with self.assertRaises(NameError):
                    fleet_children.verify_claims(rec)
                # ...and for a command, before any subprocess is spawned.
                with self.assertRaises(NameError):
                    fleet_children.verify_claims(
                        {"spawned_at": NOW, "claims": {"verify": "true"}})


# ── 1. record_claims: normalisation only ─────────────────────────────────────
class TestRecordClaims(unittest.TestCase):
    def test_empty_inputs_still_yield_the_three_keys(self):
        c = fleet_lineage.record_claims({})
        self.assertEqual(c["claims"], {"files": [], "verify": None, "unsure": None})

    def test_files_are_sorted_deduped_and_stringified(self):
        c = fleet_lineage.record_claims({}, ["/b", "/a", "/b"], None, None)
        self.assertEqual(c["claims"]["files"], ["/a", "/b"])
        self.assertEqual(fleet_lineage.record_claims({}, ("z",))["claims"]["files"],
                         ["z"])

    def test_non_string_file_values_are_coerced(self):
        p = Path("/tmp/x.py")
        c = fleet_lineage.record_claims({}, [p, 7], None, None)
        self.assertEqual(c["claims"]["files"], sorted({str(p), "7"}))

    def test_files_none_is_the_same_as_empty(self):
        c = fleet_lineage.record_claims({}, None, None, None)
        self.assertEqual(c["claims"]["files"], [])

    def test_verify_and_unsure_are_trimmed_and_blanks_become_none(self):
        c = fleet_lineage.record_claims({}, [], "  python3 -m unittest x  ", "  a, b  ")
        self.assertEqual(c["claims"]["verify"], "python3 -m unittest x")
        self.assertEqual(c["claims"]["unsure"], "a, b")
        blank = fleet_lineage.record_claims({}, [], "   ", "\n\t ")
        self.assertIsNone(blank["claims"]["verify"])
        self.assertIsNone(blank["claims"]["unsure"])

    def test_the_rest_of_the_record_is_preserved_and_not_mutated(self):
        base = fleet_lineage.mark_reported(child(), "did the thing", NOW - 60)
        before = json.dumps(base, sort_keys=True)
        c = fleet_lineage.record_claims(base, ["/a"], "true", None)
        self.assertEqual(json.dumps(base, sort_keys=True), before)   # input pure
        self.assertNotIn("claims", base)
        for k in ("sessionId", "name", "unit", "report", "spawned_at"):
            self.assertEqual(c[k], base[k])
        self.assertIsNot(c, base)

    def test_defaults_allow_a_child_with_no_claims_at_all(self):
        c = fleet_lineage.record_claims(child())
        self.assertEqual(c["claims"]["files"], [])
        self.assertIsNone(c["claims"]["verify"])


# ── 2. claim_status: every branch, fixtures only ─────────────────────────────
class TestClaimStatus(unittest.TestCase):
    def status(self, claims, checks=None):
        return fleet_lineage.claim_status(claims, checks)

    # -- unclaimed ---------------------------------------------------------
    def test_nothing_declared_is_unclaimed(self):
        self.assertEqual(self.status({}, {}), "unclaimed")
        self.assertEqual(self.status(None, None), "unclaimed")
        self.assertEqual(self.status({"files": [], "verify": None}, {}), "unclaimed")

    def test_an_unsure_note_alone_is_not_a_claim(self):
        self.assertEqual(
            self.status({"files": [], "verify": None, "unsure": "could not test"}, {}),
            "unclaimed")

    def test_declared_files_that_were_never_checked_are_unclaimed(self):
        # MEASURED quirk: no checks gathered is not "failed". Nothing was
        # disproved, so the word stays unclaimed.
        self.assertEqual(self.status({"files": ["/a"]}, {}), "unclaimed")
        self.assertEqual(self.status({"files": ["/a"]}, {"files": []}), "unclaimed")

    # -- verified ----------------------------------------------------------
    def test_existing_files_verify(self):
        # A file that exists and was written AFTER enrolment is the only thing
        # mtime alone can prove.
        self.assertEqual(
            self.status({"files": ["/a"]},
                        {"files": [{"path": "/a", "exists": True,
                                    "after_spawn": True}]}),
            "verified")

    def test_a_file_that_predates_the_unit_is_stale_not_verified(self):
        # The cheapest lie: point at an old artifact and call it today's work.
        self.assertEqual(
            self.status({"files": ["/a"]},
                        {"files": [{"path": "/a", "exists": True,
                                    "after_spawn": False}]}),
            "stale")

    def test_a_rerun_proof_overrides_a_stale_mtime(self):
        # mtime is circumstantial; a command that exits 0 is evidence.
        self.assertEqual(
            self.status({"files": ["/a"], "verify": "test -d /tmp"},
                        {"files": [{"path": "/a", "exists": True,
                                    "after_spawn": False}],
                         "verify": {"cmd": "test -d /tmp", "rc": 0}}),
            "verified")

    def test_one_fresh_file_is_enough(self):
        self.assertEqual(
            self.status({"files": ["/a", "/b"]},
                        {"files": [{"path": "/a", "exists": True,
                                    "after_spawn": False},
                                   {"path": "/b", "exists": True,
                                    "after_spawn": True}]}),
            "verified")

    def test_a_command_that_exited_zero_verifies_even_with_no_files(self):
        # The exemplar is `test -d /tmp`, NOT `true`: a no-op cannot fail, so it is
        # not evidence, and the gate now refuses to call it `verified`. These pins
        # used `true` as a convenient zero-exit command and thereby encoded the very
        # loophole they were meant to close (found by a peer, 2026-10-17).
        self.assertEqual(
            self.status({"files": [], "verify": "test -d /tmp"},
                        {"verify": {"cmd": "test -d /tmp", "rc": 0}}),
            "verified")

    def test_files_and_a_zero_exit_together_verify(self):
        self.assertEqual(
            self.status({"files": ["/a"], "verify": "x"},
                        {"files": [{"path": "/a", "exists": True}],
                         "verify": {"cmd": "x", "rc": 0}}),
            "verified")

    # -- failed ------------------------------------------------------------
    def test_a_missing_file_fails(self):
        self.assertEqual(
            self.status({"files": ["/gone"]},
                        {"files": [{"path": "/gone", "exists": False}]}),
            "failed")

    def test_a_nonzero_exit_fails(self):
        self.assertEqual(
            self.status({"verify": "false"}, {"verify": {"cmd": "false", "rc": 1}}),
            "failed")

    def test_a_missing_file_outranks_an_unrun_command(self):
        # Checked first on purpose: a file that is gone is disproof, whether or
        # not the command got to run.
        self.assertEqual(
            self.status({"files": ["/gone"], "verify": "x"},
                        {"files": [{"path": "/gone", "exists": False}],
                         "verify": {"cmd": "x", "rc": None}}),
            "failed")

    # -- partial -----------------------------------------------------------
    def test_a_command_that_never_ran_is_partial(self):
        self.assertEqual(
            self.status({"verify": "sleep 999"},
                        {"verify": {"cmd": "sleep 999", "rc": None}}),
            "partial")

    def test_partial_still_requires_the_files_to_be_present(self):
        self.assertEqual(
            self.status({"files": ["/a"], "verify": "sleep 999"},
                        {"files": [{"path": "/a", "exists": True}],
                         "verify": {"cmd": "sleep 999", "rc": None}}),
            "partial")

    def test_partial_is_never_reported_as_verified(self):
        self.assertNotEqual(
            self.status({"verify": "x"}, {"verify": {"cmd": "x", "rc": None}}),
            "verified")


# ── 3. verify_claims: real files, real commands, real /tmp ───────────────────
class TestVerifyClaimsFiles(TimeInjected):
    def setUp(self):
        super().setUp()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def check(self, files=(), verify=None, spawned_at=NOW, worktree=None, timeout=120):
        rec = {"spawned_at": spawned_at, "claims": claims(files, verify)}
        if worktree is not None:
            rec["worktree"] = worktree
        return fleet_children.verify_claims(rec, timeout=timeout)

    def test_a_present_file_is_measured_not_trusted(self):
        f = self.dir / "payload.txt"
        f.write_bytes(b"known payload")
        out = self.check([str(f)])
        self.assertEqual(len(out["files"]), 1)
        got = out["files"][0]
        self.assertEqual(got["path"], str(f))
        self.assertTrue(got["exists"])
        self.assertEqual(got["bytes"], len(b"known payload"))
        self.assertEqual(got["sha256"], hashlib.sha256(b"known payload").hexdigest())
        self.assertTrue(got["mtime_iso"].endswith("Z"))       # %Y-%m-%dT%H:%M:%SZ
        self.assertRegex(got["mtime_iso"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertTrue(got["after_spawn"])
        self.assertEqual(out["status"], "verified")
        self.assertNotIn("detail", out)

    def test_a_missing_file_is_reported_not_raised(self):
        out = self.check([str(self.dir / "never-existed.txt")])
        got = out["files"][0]
        self.assertFalse(got["exists"])
        self.assertNotIn("bytes", got)
        self.assertEqual(out["status"], "failed")
        self.assertIn("missing", out["detail"])

    def test_a_file_older_than_the_spawn_is_flagged(self):
        f = self.dir / "stale.txt"
        f.write_text("old")
        mtime = int(f.stat().st_mtime)
        fresh = self.check([str(f)], spawned_at=mtime)          # >= counts as after
        self.assertTrue(fresh["files"][0]["after_spawn"])
        stale = self.check([str(f)], spawned_at=mtime + 3600)
        self.assertFalse(stale["files"][0]["after_spawn"])
        # verify_claims reports the fact; the verdict for it lives in
        # claim_status, which calls a unit with no fresh file and no proof stale.
        self.assertEqual(stale["status"], "stale")

    def test_sha256_is_skipped_above_eight_mebibytes(self):
        big = self.dir / "big.bin"
        big.write_bytes(b"\0" * (8 * 1024 * 1024 + 1))
        got = self.check([str(big)])["files"][0]
        self.assertEqual(got["sha256"], "")
        self.assertEqual(got["bytes"], 8 * 1024 * 1024 + 1)
        # ...and exactly at the limit it is still hashed (<= 8 MiB).
        edge = self.dir / "edge.bin"
        edge.write_bytes(b"\0" * (8 * 1024 * 1024))
        self.assertEqual(len(self.check([str(edge)])["files"][0]["sha256"]), 64)

    def test_a_failed_claim_keeps_the_verify_result_even_when_files_pass(self):
        f = self.dir / "ok.txt"
        f.write_text("fine")
        out = self.check([str(f), str(self.dir / "gone.txt")], "false")
        self.assertEqual(out["status"], "failed")
        self.assertEqual(out["verify"]["rc"], 1)
        self.assertIn("gone.txt", out["detail"])
        self.assertIn("exited 1", out["detail"])

    def test_status_is_always_present(self):
        self.assertEqual(self.check()["status"], "unclaimed")
        self.assertIn("status", self.check([]))


class TestVerifyClaimsCommands(TimeInjected):
    def setUp(self):
        super().setUp()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def run_claim(self, cmd, **kw):
        rec = {"spawned_at": NOW, "claims": claims((), cmd)}
        rec.update({k: v for k, v in kw.items() if k == "worktree"})
        return fleet_children.verify_claims(rec, timeout=kw.get("timeout", 120))

    def test_exit_zero_verifies_and_records_cmd_rc_secs(self):
        # A real command that can fail; `true` now reads `unclaimed` (see
        # TestAVerifyThatCannotFail) because it proves nothing.
        out = self.run_claim("test -d /tmp")
        v = out["verify"]
        self.assertEqual(v["cmd"], "test -d /tmp")
        self.assertEqual(v["rc"], 0)
        self.assertGreaterEqual(v["secs"], 0)
        self.assertIsInstance(v["secs"], float)
        self.assertEqual(out["status"], "verified")
        self.assertNotIn("detail", out)

    def test_exit_one_fails_with_a_readable_detail(self):
        out = self.run_claim("echo boom >&2; exit 1")
        self.assertEqual(out["verify"]["rc"], 1)
        self.assertEqual(out["status"], "failed")
        self.assertIn("boom", out["verify"]["tail"])
        self.assertIn("exited 1", out["detail"])

    def test_the_tail_is_the_last_two_lines_joined(self):
        out = self.run_claim("printf 'one\\ntwo\\nthree\\n'")
        self.assertEqual(out["verify"]["tail"], "two | three")

    def test_the_tail_is_capped_at_two_hundred_characters(self):
        out = self.run_claim("printf 'x%.0s' $(seq 1 500)")
        self.assertLessEqual(len(out["verify"]["tail"]), 200)

    def test_a_timeout_gives_rc_none_and_never_raises(self):
        out = self.run_claim("sleep 5", timeout=1)
        v = out["verify"]
        self.assertIsNone(v["rc"])
        self.assertIn("timed out after 1s", v["tail"])
        self.assertGreaterEqual(v["secs"], 0.5)
        self.assertEqual(out["status"], "partial")     # declared, never completed
        self.assertNotIn("detail", out)                # partial does not fail

    def test_the_command_runs_in_the_childs_worktree_when_it_exists(self):
        out = self.run_claim("pwd", worktree=str(self.dir))
        self.assertEqual(os.path.realpath(out["verify"]["tail"].strip()),
                         os.path.realpath(str(self.dir)))

    def test_a_missing_worktree_runs_in_the_cwd_and_says_so(self):
        """The old contract was "run in /root anyway". That produced a FALSE RED on a
        real unit (2026-10-26: a passing suite reported FAILED(rc=2) because
        scripts/build_css.py was looked for under /root). It still runs, but the result
        carries `cwd_unresolved` so the rollup can refuse to call it a failure."""
        out = self.run_claim("pwd", worktree=str(self.dir / "no-such-tree"))
        self.assertTrue(out["verify"]["cwd_unresolved"])
        self.assertEqual(out["verify"]["cwd"], os.getcwd())

    def test_a_missing_file_does_not_stop_the_command_from_running(self):
        # MEASURED: the file loop appends failures and carries on; evidence is
        # gathered completely, then reduced to one word.
        rec = {"spawned_at": NOW,
               "claims": claims([str(self.dir / "gone.txt")], "true")}
        out = fleet_children.verify_claims(rec)
        self.assertEqual(out["verify"]["rc"], 0)
        self.assertEqual(out["status"], "failed")       # the missing file wins


# ── 4. digest_md: Claimed / Checked / UNSURE ─────────────────────────────────
_AUTO = object()          # "compute the checks"; None means "none were taken"


class TestDigestClaims(TimeInjected):
    ISO = "2026-10-17T13:00:00Z"

    def setUp(self):
        super().setUp()
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.dir = Path(td.name)

    def rec(self, files=(), verify=None, unsure=None, spawned_at=NOW):
        # spawned_at is set on the record itself: verify_claims and digest_md
        # both read it to decide "after spawn" vs "BEFORE spawn".
        return fleet_lineage.record_claims(
            fleet_lineage.mark_reported(child(spawned_at=spawned_at),
                                        "wrote the tests", NOW - 60),
            files, verify, unsure)

    def digest(self, rec, checks=_AUTO):
        rec = dict(rec)
        if checks is _AUTO:
            checks = fleet_children.verify_claims(rec)
        return fleet_lineage.digest_md(rec, facts(), NOW, self.ISO, checks=checks)

    def test_a_verified_claim_renders_all_three_sections(self):
        f = self.dir / "test_fleet_claims.py"
        f.write_text("payload")
        md = self.digest(self.rec([str(f)], "true", "nothing unpinned"))
        self.assertIn("## Claimed (the worker's own words)", md)
        self.assertIn("## Checked (mechanical, at reap)", md)
        self.assertIn("## UNSURE (verbatim, never smoothed)", md)
        self.assertIn(f"- file: {f}", md)
        self.assertIn("- proof: `true`", md)
        self.assertIn("nothing unpinned", md)
        self.assertIn("present", md)
        self.assertIn("after enrolment", md)
        self.assertIn("- evidence verdict: **verified**", md)

    def test_a_failed_claim_says_so(self):
        gone = self.dir / "vanished.py"
        md = self.digest(self.rec([str(gone)], "false"))
        self.assertIn(f"- {gone}: MISSING", md)
        self.assertIn("- evidence verdict: **failed**", md)
        self.assertIn("exit 1", md)

    def test_a_file_older_than_the_spawn_is_labelled_before(self):
        f = self.dir / "old.py"
        f.write_text("x")
        md = self.digest(self.rec([str(f)], None, None,
                                  spawned_at=int(f.stat().st_mtime) + 3600))
        self.assertIn("BEFORE enrolment", md)
        self.assertIn("**stale**", md)

    def test_an_unpinned_timeout_reads_as_partial(self):
        md = self.digest(self.rec((), "sleep 5"), checks={"files": [],
                         "verify": {"cmd": "sleep 5", "rc": None, "secs": 1.0,
                                    "tail": "timed out after 1s"}})
        self.assertIn("**partial**", md)
        self.assertIn("timed out after 1s", md)

    def test_the_sha256_line_is_truncated_to_sixteen_characters(self):
        f = self.dir / "p.txt"
        f.write_text("known payload")
        md = self.digest(self.rec([str(f)]))
        self.assertIn(hashlib.sha256(b"known payload").hexdigest()[:16] + "…", md)

    def test_a_child_with_no_claim_gets_no_claimed_section(self):
        md = self.digest(self.rec(), checks=None)
        self.assertNotIn("## Claimed", md)
        self.assertNotIn("## Checked", md)
        # ...the report is still there: absence of a claim is not silence.
        self.assertIn("## What it said it did (its own words)", md)
        self.assertIn("wrote the tests", md)

    def test_checks_without_claims_says_there_was_nothing_checkable(self):
        md = self.digest(self.rec(), checks={"files": [], "verify": None})
        self.assertIn("## Claimed", md)
        self.assertIn("(no checkable claim; see the report below)", md)
        self.assertIn("**unclaimed**", md)

    def test_the_same_claim_renders_the_same_bytes(self):
        f = self.dir / "det.txt"
        f.write_text("deterministic")
        rec = self.rec([str(f)], "true")
        checks = fleet_children.verify_claims(rec)
        checks["verify"]["secs"] = 0.0            # wall clock is the only jitter
        self.assertEqual(self.digest(rec, checks), self.digest(rec, checks))

    def test_unsure_is_passed_through_verbatim(self):
        md = self.digest(self.rec((), "true",
                                  'could NOT test the reap path — "tmux" missing'))
        self.assertIn('could NOT test the reap path — "tmux" missing', md)


# ── 5. _cascade: a failed claim is a DECIDE, never a kill ────────────────────
class TestCascadeRefusal(TempStores, TimeInjected):
    def setUp(self):
        super().setUp()
        self.calls = []
        self.live_entries = []
        self.records = []
        self.owner = {SID: "fleet-trust-deps"}
        self.gate_calls = []
        self.patch_deps()
        self.patch_attr("pi_sources", SimpleNamespace(
            live_records=lambda live_only=False: self.records))
        self.patch_attr("fleet_gate", SimpleNamespace(gate=self.fake_gate))
        self.closed = []
        self.patch_attr("safe_close", self.fake_close)

    # -- injected world ----------------------------------------------------
    def fake_gate(self, entry, worktree_exists=False, accept_silent=False,
                  base=None):
        self.gate_calls.append(dict(entry))
        return []

    def fake_close(self, sid, tmux_session, wait_s=5.0):
        self.closed.append((sid, tmux_session))
        return True, "kill-pane fleet-trust-deps:0.1"

    def fake_sh(self, argv, **kw):
        self.calls.append(list(argv))
        return 0, "", ""

    def patch_deps(self):
        p = mock.patch.object(fleet_children, "_D", {})
        p.start()
        self.addCleanup(p.stop)
        fleet_children.configure(**good_deps(
            entries=lambda: self.live_entries,
            find=lambda name: next((e for e in self.live_entries
                                    if e.get("name") == name), None),
            sh=self.fake_sh, owner_tmux_of=lambda sid: self.owner.get(sid, ""),
            clip=lambda s, n: str(s or "")[:n], base="origin/main"))
        self.patch_attr("_proc_identity",
                        lambda pid: ("ok", "S", PID_START) if pid == 4242
                        else ("missing", None, None))

    # -- fixtures ----------------------------------------------------------
    def seed(self, c, alive=True, reported=True, session="fleet-trust-deps"):
        # Alive by default: these tests are about CLAIMS, and a close only
        # happens when there is a pane to close (a dead child is bookkeeping).
        # Liveness is identity now (pid + start time), so a live fixture carries
        # the pair; _proc_identity is patched in patch_deps. No real /proc.
        if alive:
            c = dict(c, pid=4242, pid_start=PID_START)
        self.lineage_path.write_text(
            json.dumps({"version": 1, "children": {c["sessionId"]: c}}, indent=2)
            + "\n")
        self.live_entries = [{
            "pid": 4242 if alive else None,
            "sessionId": c["sessionId"], "name": c.get("name"), "alive": alive,
            "reported": reported, "session": session, "pi_status": "", "phase": "",
            "pending_ask": None, "worktree": None,
        }]
        self.records = [{"sessionId": c["sessionId"],
                         "updatedAt": (NOW - 120) * 1000}]
        return c

    def reapable(self, keep=()):
        """A dead child that reported. `keep` = extra record updates."""
        c = fleet_lineage.mark_reported(child(), "all tests pass", NOW - 60)
        for k, v in keep:
            c[k] = v
        return self.seed(c)

    def saved(self):
        return json.loads(self.lineage_path.read_text())["children"][SID]

    def msgs(self):
        return [c for c in self.calls if c[:2] == ["pi-msg", "send"]]

    def digests_written(self):
        return sorted(p.name for p in self.digests.glob("*.md")) \
            if self.digests.exists() else []

    # -- the refusal -------------------------------------------------------
    def test_a_missing_claimed_file_is_a_decide_not_a_kill(self):
        with tempfile.TemporaryDirectory() as td:
            gone = str(Path(td) / "test_fleet_claims.py")
            self.reapable([("claims", claims([gone], None, "nothing"))])
            with quiet() as out:
                rc = fleet_children._cascade(args(apply=True))
            text = out.getvalue()
            self.assertEqual(self.closed, [])                 # NOT killed
            self.assertEqual(self.digests_written(), [])      # no digest for a lie
            self.assertFalse(self.ledger.exists())
            self.assertIsNone(self.saved()["reaped_at"])      # still unreaped
            self.assertIn("DECIDE", text)
            self.assertIn("its own evidence did not hold", text)
            self.assertIn("missing", text)
            self.assertIn("missing", self.saved()["last_notified"])
            self.assertEqual(rc, 0)
            # the parent was told, once, with the failure detail
            self.assertEqual(len(self.msgs()), 1)
            self.assertIn("tundra", self.msgs()[0])
            self.assertIn("did not hold", self.msgs()[0][-1])
            # the gate still ran first: evidence never replaces the guard
            self.assertEqual(len(self.gate_calls), 1)

    def test_a_failing_verify_command_is_a_decide_not_a_kill(self):
        self.reapable([("claims", claims((), "exit 1", None))])
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(self.closed, [])
        self.assertIsNone(self.saved()["reaped_at"])
        self.assertIn("exited 1", out.getvalue())
        self.assertEqual(len(self.msgs()), 1)

    def test_a_declared_but_never_run_command_is_not_a_failure(self):
        # partial/unclaimed are NOT disproof; reaping continues as before.
        self.reapable([("claims", claims((), "sleep 5", None))])
        self.patch_attr("verify_claims",
                        lambda rec, timeout=120: {"files": [], "verify": {
                            "cmd": "sleep 5", "rc": None, "secs": 1.0,
                            "tail": "timed out after 1s"}, "status": "partial"})
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(len(self.closed), 1)
        self.assertIn("REAPED", out.getvalue())
        self.assertIn("evidence partial", out.getvalue())

    # -- unchanged behaviour ----------------------------------------------
    def test_a_child_with_no_claims_is_still_reaped(self):
        self.reapable()
        with quiet() as out:
            fleet_children._cascade(args(apply=True))
        self.assertEqual(len(self.closed), 1)
        self.assertIn("unclaimed", out.getvalue())
        self.assertIsNotNone(self.saved()["reaped_at"])

    def test_a_verified_claim_still_reaps_and_records_the_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_fleet_claims.py"
            f.write_text("passing suite")
            self.reapable([("claims", claims([str(f)], "true", "nothing"))])
            with quiet() as out:
                fleet_children._cascade(args(apply=True))
            self.assertEqual(len(self.closed), 1)
            self.assertIn("evidence verified", out.getvalue())
            row = json.loads(self.ledger.read_text().splitlines()[0])
            self.assertEqual(row["evidence"], "verified")
            self.assertEqual(len(self.digests_written()), 1)

    def test_the_digest_of_a_reaped_child_records_what_it_could(self):
        """MEASURED 2026-10-17: `_write_digest` accepts `checks` and never
        forwards it to `digest_md`, so the Checked section of a REAPED child
        says "files: none declared" / **partial** even when the evidence passed
        (digest_md renders it correctly when handed the checks directly — see
        TestDigestClaims). The claim and the UNSURE note survive; the mechanical
        half is lost. This test records which state it saw, so adding the
        forwarding upstream cannot break the suite.
        """
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "test_fleet_claims.py"
            f.write_text("passing suite")
            self.reapable([("claims", claims([str(f)], "true",
                                             "nothing unpinned"))])
            with quiet():
                fleet_children._cascade(args(apply=True))
            md = (self.digests / self.digests_written()[0]).read_text()
            self.assertIn("## Claimed", md)
            self.assertIn("## Checked", md)
            self.assertIn("## UNSURE", md)
            self.assertIn("nothing unpinned", md)
            self.assertIn(f"- file: {f}", md)
            # the evidence itself is still ON the record, whatever the digest says
            self.assertEqual(self.saved()["checks"]["status"], "verified")
            if "files: none declared" in md:
                self.assertIn("**partial**", md)      # the measured loss
                self.assertNotIn("**verified**", md)
            else:
                self.assertIn("**verified**", md)     # forwarding fixed upstream


if __name__ == "__main__":
    unittest.main()


class TestAVerifyThatCannotFail(unittest.TestCase):
    """`--verify "true"` exits 0 always, so it would grant `verified` with no proof.

    This is the same lie as an evidence claim that survives inspection while proving
    nothing, and it is the cheapest forgery available to a worker: one word in argv.
    A command that cannot produce a failure must not produce the word `verified`.
    """

    def test_the_trivial_class_is_recognised(self):
        for cmd in ("true", ":", "exit 0", "/bin/true", "sleep 0", "echo", "echo hi",
                    "printf ok"):
            self.assertFalse(fleet_lineage.verify_can_fail(cmd),
                             f"{cmd!r} would be accepted as proof")

    def test_a_real_check_is_allowed(self):
        for cmd in ("test -s /tmp/x", "grep -q foo /etc/hostname",
                    "python3 -m unittest test_fleet_claims", "true && test -f /tmp/x"):
            self.assertTrue(fleet_lineage.verify_can_fail(cmd), cmd)

    def test_trivial_verify_with_no_files_is_unclaimed_not_verified(self):
        got = fleet_lineage.claim_status(
            {"verify": "true"}, {"verify": {"cmd": "true", "rc": 0}})
        self.assertEqual(got, "unclaimed")

    def test_a_failing_command_still_reads_failed(self):
        got = fleet_lineage.claim_status(
            {"verify": "test -s /nope"}, {"verify": {"cmd": "test -s /nope", "rc": 1}})
        self.assertEqual(got, "failed")
