"""The store lock: the only thing standing between two ticks and a lost kill row.

Read-modify-write of a whole JSON file with no lock means the slower writer wins
and the faster writer's row is gone -- while the pane it described is already
dead. These tests use real processes, because a thread-level fake would prove
nothing: the failure mode is two OS processes, and flock is an OS guarantee.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import fleet_util

PY = sys.executable
BIN = Path(__file__).parent

# A child that increments a counter in a JSON file the unsafe way: read, sleep,
# write. The sleep is what makes the race lose updates without a lock.
WORKER = """
import json, os, sys, time
sys.path.insert(0, %(bin)r)
import fleet_util
path = %(path)r
for _ in range(int(sys.argv[1])):
    with fleet_util.store_lock():
        d = json.loads(open(path).read())
        d["n"] += 1
        time.sleep(0.002)
        open(path, "w").write(json.dumps(d))
""" % {"bin": str(BIN), "path": "__PATH__"}


def _run(n_workers, n_each, tmp, locked=True):
    counter = Path(tmp) / "counter.json"
    counter.write_text(json.dumps({"n": 0}))
    src = (WORKER if locked else WORKER.replace("with fleet_util.store_lock():", "if True:")) \
        .replace("__PATH__", str(counter))
    script = Path(tmp) / ("locked.py" if locked else "naked.py")
    script.write_text(src)
    env = {**os.environ, "FLEET_LOCK": str(Path(tmp) / "lock")}
    procs = [subprocess.Popen([PY, str(script), str(n_each)], env=env,
                              stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
             for _ in range(n_workers)]
    crashed = 0
    for p in procs:
        _, err = p.communicate(timeout=180)
        if p.returncode != 0:
            crashed += 1
            last_err = err.decode()[-400:]
    if crashed and locked:
        raise AssertionError(f"{crashed} locked workers crashed: {last_err}")
    try:
        return json.loads(counter.read_text())["n"]
    except json.JSONDecodeError:
        # A truncated file is the other face of the same race: one worker
        # truncated while another read. Only tolerable when unlocked.
        if locked:
            raise
        return -1


class TestStoreLock(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fleetlock-")
        self._lock = fleet_util.LOCK_PATH
        fleet_util.LOCK_PATH = Path(self.tmp) / "lock"

    def tearDown(self):
        fleet_util.LOCK_PATH = self._lock

    def test_without_the_lock_updates_are_lost(self):
        """The failure the lock exists to prevent -- proven, not asserted.

        Unlocked, the race either loses an increment or reads a half-written
        file (-1). Both are damage; either proves the lock earns its keep.
        """
        got = _run(4, 25, self.tmp, locked=False)
        self.assertLess(got, 100, "the unlocked workers did not lose anything, so "
                                 "this test no longer demonstrates the race")

    def test_with_the_lock_nothing_is_lost(self):
        self.assertEqual(_run(4, 25, self.tmp, locked=True), 100)

    def test_lock_is_inherited_so_a_child_does_not_deadlock(self):
        """A subprocess started under the lock must not queue behind its parent."""
        env = {**os.environ, "FLEET_LOCK": str(Path(self.tmp) / "lock"),
               "FLEET_LOCK_HELD": "1"}
        t0 = time.time()
        with fleet_util.store_lock():
            rc = subprocess.run(
                [PY, "-c", "import sys; sys.path.insert(0, %r); import fleet_util;"
                 "fleet_util.store_lock().gen.close() if False else None" % str(BIN)],
                env=env, capture_output=True, timeout=30).returncode
        self.assertEqual(rc, 0)
        self.assertLess(time.time() - t0, 25)

    def test_a_held_lock_fails_loudly_rather_than_waiting_forever(self):
        import fcntl
        Path(self.tmp).mkdir(exist_ok=True)
        fh = open(fleet_util.LOCK_PATH, "a+")
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            with self.assertRaises(SystemExit) as ctx:
                with fleet_util.store_lock(timeout=0.3):
                    pass
            self.assertIn("refusing to mutate", str(ctx.exception))
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)
            fh.close()


if __name__ == "__main__":
    unittest.main()
