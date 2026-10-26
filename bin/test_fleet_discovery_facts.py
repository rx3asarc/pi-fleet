#!/usr/bin/env python3
"""The `merged` fact must be asked of the worktree's own repo.

WHY THIS FILE EXISTS (MEASURED 2026-10-26). `fleet_gate` refuses a reap with

    not merged: <branch> @ <head> is not an ancestor of origin/main

`merged` comes from `fleet_discovery.git_facts`, which spent a night asking that
question of `fleet_paths.repo()` -- ONE default project's repository -- for every
worktree it discovered. For a worker whose work lived in a different repo the
answer was `fatal: Not a valid object name`, which is not "unmerged": it is "this
repo has never heard of that commit". The gate read it as a refusal.

Observed on `scrub-fixtures`: its tip 423c165 IS an ancestor of origin/main in its
own clone (`git merge-base --is-ancestor` -> 0) and `fleet reap --dry-run` still
said "not merged"; the same command run in the default repo exits 128.

A refusal that cannot be satisfied is not a safety property, it is a stuck queue
(and the tempting workaround -- `--abandon`, which skips the gate -- is strictly
worse). So the fact is now asked in the worktree being described.

These tests use real git repos in a temp dir, because the bug WAS a wrong
directory handed to a real git command. A mock that returns "not an ancestor" for
a foreign repo would pass against the broken code.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fleet_discovery  # noqa: E402


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd)] + list(args),
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), cwd, p.stderr))
    return p.stdout.strip()


def make_repo(root, name, files=("a.txt",)):
    """A standalone repo with one commit and an origin/main ref pointing at it.

    Unrelated histories on purpose: two repos that share a commit cannot tell a
    right-repo answer from a wrong-repo answer.
    """
    d = Path(root) / name
    d.mkdir(parents=True)
    git(d, "init", "-q", "-b", "main")
    git(d, "config", "user.email", "t@example.invalid")
    git(d, "config", "user.name", "t")
    for f in files:
        (d / f).write_text("hello\n")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "initial")
    git(d, "update-ref", "refs/remotes/origin/main", "HEAD")
    return d


class TestMergedIsAskedInTheWorktreesOwnRepo(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleet-facts-")
        self.default_repo = make_repo(self.tmp, "default-repo")
        self.other_repo = make_repo(self.tmp, "other-repo", files=("b.txt",))

    def facts(self, wt):
        # the default repo is passed exactly as the caller passes it: as `repo`
        return fleet_discovery.git_facts(str(wt), "origin/main", self.default_repo)

    def test_a_merge_in_another_repo_is_not_reported_as_unmerged(self):
        f = self.facts(self.other_repo)
        self.assertEqual(f["branch"], "main")
        self.assertIsNotNone(f["head"])
        self.assertIs(True, f["merged"],
                      "the fact was resolved in the wrong repository: %r" % (f,))

    def test_a_commit_the_own_repo_does_not_have_is_not_called_merged(self):
        # now make the worktree's HEAD genuinely absent from origin/main
        (self.other_repo / "b.txt").write_text("changed\n")
        git(self.other_repo, "commit", "-q", "-am", "ahead of origin/main")
        self.assertIs(False, self.facts(self.other_repo)["merged"])

    def test_a_linked_worktree_resolves_origin_main_through_its_shared_repo(self):
        wt = Path(self.tmp) / "linked-wt"
        git(self.default_repo, "worktree", "add", "-q", "-b", "feature",
            str(wt), "origin/main")
        f = self.facts(wt)
        self.assertEqual(f["branch"], "feature")
        self.assertIs(True, f["merged"])

    def test_the_default_repo_is_still_right_for_its_own_worktree(self):
        self.assertIs(True, self.facts(self.default_repo)["merged"])


if __name__ == "__main__":
    unittest.main()
