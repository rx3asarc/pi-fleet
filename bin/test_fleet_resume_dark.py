#!/usr/bin/env python3
"""Tests for fleet_resume_dark's verification logic.

These cover the parts that decide whether a resume SAFE and whether it WORKED,
without signalling anything: process identity (pid + start time), the
transcript grew-not-rewritten check, and the mid-turn activity probe.

They were written after the 2026-10-26 rehearsal, which is where each case came
from: pid 1479189 carries two session records, `globex-seo` (live) and
`basalt` (ended, no transcript); `nimbus` had a liveness record saying `idle`
while its transcript was being appended to. Both are cases where a plausible
reader answers wrongly and the answer is unrecoverable.

Run: python3 /root/.pi/agent/bin/test_fleet_resume_dark.py
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fleet_resume_dark as R  # noqa: E402


class ProcessIdentity(unittest.TestCase):
    def test_self_is_alive_and_has_a_start_time(self):
        self.assertTrue(R.alive(os.getpid()))
        start = R.pid_start(os.getpid())
        self.assertIsNotNone(start)
        self.assertEqual(start, R.pid_start(os.getpid()),
                         "start time must be stable for one process")

    def test_nonexistent_pid_is_not_alive(self):
        # pid 0 is never a user process; the highest pid on the box is not ours.
        self.assertFalse(R.alive(0))
        self.assertFalse(R.alive(999999999))

    def test_ppid_chain_starts_with_the_parent(self):
        chain = R.ppid_chain(os.getpid(), limit=4)
        self.assertTrue(chain)
        self.assertEqual(chain[0], os.getppid())

    def test_comm_of_missing_pid_is_empty_not_an_exception(self):
        self.assertEqual(R.comm_of(999999999), "")


class TranscriptChecks(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "t.jsonl")
        with open(self.path, "wb") as fh:
            fh.write(b'{"a":1}\n{"b":2}\n')
        self.before = R.transcript_facts(self.path)

    def test_facts_report_size_lines_and_prefix(self):
        self.assertTrue(self.before["exists"])
        self.assertEqual(self.before["bytes"], 16)
        self.assertEqual(self.before["lines"], 2)
        self.assertEqual(len(self.before["prefix_sha"]), 64)

    def test_grow_keeps_inode_and_prefix(self):
        with open(self.path, "ab") as fh:
            fh.write(b'{"c":3}\n')
        after = R.transcript_facts(self.path)
        self.assertEqual(after["inode"], self.before["inode"])
        self.assertGreater(after["bytes"], self.before["bytes"])
        # The bytes that existed before the append are unchanged. A full-prefix
        # comparison would fail here for a tiny file, which is exactly the bug
        # this test was written for (measured on rehearsal 1, 2026-10-26).
        self.assertEqual(R.prefix_sha(self.path, self.before["prefix_len"]),
                         self.before["prefix_sha"])

    def test_rewrite_is_detected_by_prefix(self):
        # A rewrite with the same size but different content is the failure the
        # resume must never report as success: same pid, same tty, lost history.
        with open(self.path, "wb") as fh:
            fh.write(b'{"z":9}\n{"y":8}\n')
        after = R.transcript_facts(self.path)
        self.assertNotEqual(after["prefix_sha"], self.before["prefix_sha"])

    def test_missing_transcript_is_not_success(self):
        facts = R.transcript_facts(os.path.join(self.dir, "absent.jsonl"))
        self.assertFalse(facts["exists"])

    def test_activity_detects_a_quiet_file(self):
        self.assertTrue(R.activity(self.path, window=0.05)["quiet"])

    def test_activity_detects_a_write_during_the_window(self):
        import threading

        def writer():
            time.sleep(0.1)
            with open(self.path, "ab") as fh:
                fh.write(b'{"late":true}\n')

        t = threading.Thread(target=writer)
        t.start()
        result = R.activity(self.path, window=0.6)
        t.join()
        self.assertFalse(result["quiet"])
        self.assertGreater(result["grew"], 0)


class Refusals(unittest.TestCase):
    def test_dark_set_reads_rule_c_and_never_raises(self):
        rows, err = R.dark_sessions()
        if err:
            self.skipTest(f"rule C unreadable in this environment: {err}")
        self.assertIsInstance(rows, list)
        for row in rows:
            self.assertIn("name", row)
            self.assertIn("pid", row)
            self.assertIn("transcript", row)

    def test_find_dark_is_exact_then_unambiguous(self):
        rows = [{"name": "alpha", "pid": 1, "sessionId": "aaa1", "transcript": ""},
                {"name": "alphabet", "pid": 2, "sessionId": "aaa2", "transcript": ""}]
        row, err = R.find_dark(rows, "alpha")
        self.assertIsNone(err)
        self.assertEqual(row["pid"], 1)
        _, err = R.find_dark(rows, "alph")
        self.assertIn("ambiguous", err)
        _, err = R.find_dark(rows, "nothing-like-this")
        self.assertIn("no dark session", err)

    def test_find_dark_accepts_a_pid(self):
        rows = [{"name": "", "pid": 4321, "sessionId": "bbb1", "transcript": ""}]
        row, err = R.find_dark(rows, "4321")
        self.assertIsNone(err)
        self.assertEqual(row["pid"], 4321)


if __name__ == "__main__":
    unittest.main(verbosity=2)
