#!/usr/bin/env python3
"""Tests for fleet-sentinel-cron.sh — the lock and the kill switch.

RUN:  cd /root/.pi/agent/bin && python3 -m unittest test_sentinel_cron_lock -v

WHY (2026-10-26). Two failures, one measured and one latent:

  1. MEASURED: a tick that took 16+ minutes crossed STALE_AFTER_S, so the next
     cron slot "reclaimed" the lock and started a SECOND sentinel against a store
     the first one was still mutating. Age alone is not evidence of a hang.
  2. LATENT: the script ignored `pi-sentinel pause`, so the documented kill switch
     stopped half the fleet pumps and silently left the other half running.

`FLEET_CRON_EXIT_AFTER_LOCK=1` is the seam: the script exits right after the lock
decision, so these tests never run a tick, never touch the real stores, and never
take 40 seconds. Everything happens under FLEET_STATE in a temp dir.
"""
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path("/root/.pi/agent/bin/fleet-sentinel-cron.sh")


class CronLockCase(unittest.TestCase):
    def setUp(self):
        self.state = Path(tempfile.mkdtemp(prefix="cron-test-"))
        (self.state / "locks").mkdir()
        (self.state / "logs").mkdir()
        self.log = self.state / "logs" / "sentinel-cron.log"
        self.pause = self.state / "PAUSE"
        self.lock = self.state / "locks" / "sentinel.lock"

    def tearDown(self):
        shutil.rmtree(self.state, ignore_errors=True)

    def run_cron(self, **env):
        e = {**os.environ,
             "FLEET_STATE": str(self.state),
             "FLEET_PAUSE_FILE": str(self.pause),
             "FLEET_CRON_EXIT_AFTER_LOCK": "1",
             "FLEET_SWEEPS": env.pop("FLEET_SWEEPS", "off")}
        e.update({k: str(v) for k, v in env.items()})
        p = subprocess.run(["bash", str(SCRIPT)], capture_output=True,
                           text=True, env=e, timeout=120)
        return p, (self.log.read_text() if self.log.exists() else "")

    def hold_lock(self, pid):
        self.lock.mkdir()
        (self.lock / "pid").write_text(f"{pid}\n")

    # -- kill switch --------------------------------------------------------
    def test_pause_file_stops_the_tick(self):
        self.pause.write_text("paused\n")
        p, log = self.run_cron()
        self.assertEqual(p.returncode, 0)
        self.assertIn("kill switch present", log)
        self.assertNotIn("START", log)

    def test_without_pause_the_tick_starts(self):
        p, log = self.run_cron()
        self.assertEqual(p.returncode, 0)
        self.assertIn("START", log)
        self.assertIn("sweeps=off", log)

    # -- lock semantics -----------------------------------------------------
    def test_a_live_holder_is_never_reclaimed_however_old(self):
        """The measured failure: a slow-but-alive tick lost its lock."""
        self.hold_lock(os.getpid())                 # a pid that is certainly alive
        old = time.time() - 99999                   # far beyond STALE_AFTER_S
        os.utime(self.lock, (old, old))
        p, log = self.run_cron()
        self.assertEqual(p.returncode, 0)
        self.assertIn("a live holder is never reclaimed by age", log)
        self.assertNotIn("START", log)
        self.assertTrue(self.lock.exists(), "the live holder keeps its lock")

    def test_a_dead_holder_is_reclaimed(self):
        dead = subprocess.Popen(["sh", "-c", "exit 0"])
        dead.wait()
        self.hold_lock(dead.pid)
        p, log = self.run_cron()
        self.assertEqual(p.returncode, 0)
        self.assertIn("is DEAD", log)
        self.assertIn("START", log)

    def test_a_lock_with_no_pid_is_left_alone_while_young(self):
        self.lock.mkdir()
        p, log = self.run_cron()
        self.assertEqual(p.returncode, 0)
        self.assertIn("no pid recorded", log)
        self.assertNotIn("START", log)

    def test_the_lock_is_released_on_exit(self):
        self.run_cron()
        self.assertFalse(self.lock.exists(), "a clean exit must remove its lock")

    # -- sweep switches -----------------------------------------------------
    def test_sweeps_off_runs_neither_sweep(self):
        p, log = self.run_cron(FLEET_SWEEPS="off")
        self.assertNotIn("sweep:", log)
        self.assertNotIn("lsweep:", log)

    def test_sweeps_subset_is_honoured(self):
        p, log = self.run_cron(FLEET_SWEEPS="records")
        self.assertNotIn("lsweep:", log, "'records' must not run the lineage sweep")


class TestScriptHygiene(unittest.TestCase):
    def test_script_is_syntactically_valid(self):
        p = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_the_sweeps_are_named_as_switches_in_the_header(self):
        text = SCRIPT.read_text()
        for needle in ("FLEET_SWEEPS", "FLEET_PAUSE_FILE", "want records", "want lineage"):
            self.assertIn(needle, text, f"{needle} is part of the kill switch contract")


if __name__ == "__main__":
    unittest.main()
