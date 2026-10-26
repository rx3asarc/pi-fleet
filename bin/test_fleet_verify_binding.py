#!/usr/bin/env python3
"""U0 — the frozen done-test. The difference between verification and self-certification.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_fleet_verify_binding -v

THE BUG THIS FILE PINS (2026-10-26, from the pi-moa review). `fleet done --verify CMD`
accepted any command, so a worker could report a test it wrote itself; reap then re-ran
a green command and recorded `verified`. The evidence chain was self-graded end to end —
the gate proved that *some* command the worker chose passed, not that the unit was done.

The rule: the command that decides "done" is authored once by the plan, frozen in the
record before the worker exists, and reported verbatim. A different command is a
REFUSAL, and the frozen one is what actually runs.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import fleet_done_test as fdt
import fleet_children


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True)


class TempRepo(unittest.TestCase):
    """A real, tiny git repo. The clean-tree check has to be tested against git
    rather than a mock, because the thing being tested IS the checkout."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fdt-test-"))
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        (self.repo / "check.sh").write_text("#!/bin/sh\nexit 0\n")
        (self.repo / "artifact.txt").write_text("hello\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        self.sha = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        # never let a test write the real cache
        self.cache = self.tmp / "cache"
        self._saved = (fdt.CACHE_DIR, os.environ.get("FLEET_VERIFY_CACHE"))
        fdt.CACHE_DIR = self.cache
        os.environ["FLEET_VERIFY_CACHE"] = str(self.cache)

    def tearDown(self):
        fdt.CACHE_DIR = self._saved[0]
        if self._saved[1] is None:
            os.environ.pop("FLEET_VERIFY_CACHE", None)
        else:
            os.environ["FLEET_VERIFY_CACHE"] = self._saved[1]
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rec(self, cmd="sh check.sh", **over):
        rec = {"sessionId": "01a1-0000", "name": "w1", "repo": str(self.repo),
               "base_sha": self.sha, "spawned_at": 1}
        if cmd:
            rec = fdt.bind(rec, "cmd", cmd, authored_by="lead")
        rec.update(over)
        return rec


class TestBinding(TempRepo):
    def test_the_frozen_command_is_accepted(self):
        ok, why = fdt.check_binding(self.rec(), "sh check.sh")
        self.assertTrue(ok, why)

    def test_whitespace_and_quoting_do_not_create_a_false_refusal(self):
        ok, why = fdt.check_binding(self.rec("sh check.sh"), "sh   check.sh ")
        self.assertTrue(ok, why)

    def test_three_kinds_of_drift_are_refused(self):
        rec = self.rec("sh check.sh")
        for claimed in ("sh check.sh --fast",           # extended
                        "pytest tests/i/wrote.py",      # replaced by self
                        "true"):                        # empty pass
            with self.subTest(claimed=claimed):
                ok, why = fdt.check_binding(rec, claimed)
                self.assertFalse(ok, f"{claimed!r} must not satisfy the frozen test")
                self.assertIn("frozen", why)

    def test_an_empty_claim_is_refused_when_a_test_is_frozen(self):
        ok, why = fdt.check_binding(self.rec(), "")
        self.assertFalse(ok)
        self.assertIn("must be reported", why)

    def test_a_unit_with_no_frozen_test_is_legacy_not_verified(self):
        rec = {"sessionId": "x", "name": "w", "spawned_at": 1}
        ok, _ = fdt.check_binding(rec, "anything")
        self.assertTrue(ok, "a legacy record must keep working")
        self.assertTrue(fdt.is_legacy(rec), "but it must be LABELLED as weaker")

    def test_a_test_that_cannot_fail_cannot_be_frozen(self):
        for bad in ("true", ":", "exit 0"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    fdt.bind({}, "cmd", bad)

    def test_a_file_kind_test_binds_a_path_not_a_command(self):
        rec = fdt.bind({}, "file", "artifact.txt")
        ok, why = fdt.check_binding(rec, "artifact.txt", kind="file")
        self.assertTrue(ok, why)
        ok, why = fdt.check_binding(rec, "sh check.sh", kind="cmd")
        self.assertFalse(ok, "a file test must not be satisfied by a command")
        self.assertIn("file", why)


class TestTheFrozenCommandIsTheOneThatRuns(TempRepo):
    def test_verify_claims_runs_the_frozen_test_not_the_claimed_one(self):
        """A drifted claim must not be able to change what is executed."""
        marker = self.tmp / "claimed-ran"
        rec = self.rec("sh check.sh")
        rec["claims"] = {"files": [], "verify": f"touch {marker}", "unsure": None}
        rec["worktree"] = str(self.repo)
        out = fleet_children.verify_claims(rec, timeout=30)
        self.assertIn("cmd", out["verify"])
        self.assertEqual(out["verify"]["cmd"], "sh check.sh",
                         "the worker's command must never replace the frozen one")
        self.assertFalse(marker.exists(), "the claimed command was executed")
        self.assertEqual(out["verify_drift"]["claimed"], f"touch {marker}",
                         "the drift must be recorded, not silently ignored")


class TestCleanTreeRun(TempRepo):
    def test_a_pass_is_recorded_with_where_it_ran(self):
        out = fdt.run_clean(str(self.repo), self.sha, "sh check.sh", use_cache=False)
        self.assertEqual(out["rc"], 0, out)
        self.assertEqual(out["sha"], self.sha)
        self.assertFalse(out["cached"])

    def test_a_failing_command_fails_in_the_clean_tree(self):
        out = fdt.run_clean(str(self.repo), self.sha, "exit 3", use_cache=False)
        self.assertEqual(out["rc"], 3)

    def test_a_file_kind_test_is_checked_in_the_clean_tree(self):
        out = fdt.run_clean(str(self.repo), self.sha, "file:artifact.txt",
                            use_cache=False)
        self.assertEqual(out["rc"], 0, out)
        out = fdt.run_clean(str(self.repo), self.sha, "file:nope.txt", use_cache=False)
        self.assertEqual(out["rc"], 1)

    def test_untracked_worker_files_cannot_make_the_run_pass(self):
        """The whole point: the worker's dirty tree is not consulted."""
        (self.repo / "conftest.py").write_text("# worker's own lie\n")
        (self.repo / "check.sh").write_text("#!/bin/sh\nexit 9\n")   # dirty edit
        out = fdt.run_clean(str(self.repo), self.sha, "sh check.sh", use_cache=False)
        self.assertEqual(out["rc"], 0, "the committed version must be the one run")

    def test_the_cache_is_per_commit_and_says_so(self):
        first = fdt.run_clean(str(self.repo), self.sha, "sh check.sh")
        self.assertFalse(first["cached"])
        second = fdt.run_clean(str(self.repo), self.sha, "sh check.sh")
        self.assertTrue(second["cached"], "second run on the same commit must hit cache")
        # a different command on the same commit is a different key
        other = fdt.run_clean(str(self.repo), self.sha, "file:artifact.txt")
        self.assertFalse(other["cached"])

    def test_a_new_commit_does_not_reuse_an_old_pass(self):
        fdt.run_clean(str(self.repo), self.sha, "sh check.sh")
        (self.repo / "artifact.txt").write_text("changed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "second")
        sha2 = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        out = fdt.run_clean(str(self.repo), sha2, "sh check.sh")
        self.assertFalse(out["cached"], "a pass on an older commit proves nothing")


if __name__ == "__main__":
    unittest.main()
