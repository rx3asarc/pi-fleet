#!/usr/bin/env python3
"""test_fleet_paths — the seam is real, the defaults are unchanged, nothing hides.

RUN:  cd bin && python3 test_fleet_paths.py

WHY THIS FILE EXISTS
`fleet_paths` claims to be the one place that decides where fleet keeps things.
A claim like that is only worth anything if two things are checked:

  1. **The defaults still resolve to the legacy locations.** A seam is a
     refactor, and a refactor that quietly moves the store makes every existing
     box look empty: "no fleets" instead of "wrong directory". This test pins the
     legacy paths, so moving them has to be a deliberate edit to a test.
  2. **The override actually redirects.** Pointing FLEET_CONTROL_DIR at a temp
     dir and still writing to the real store is the failure mode that matters:
     the run looks isolated and is not. So the subcommand assertions run against
     a fresh temp control dir AND assert the real store was not touched.

The shell scripts under bin/ cannot import a python module, so they compute the
same defaults from $HOME. That is a second copy of the rule, and a second copy is
a copy that drifts — `test_shell_and_python_agree` runs both spellings and
compares them, so drift is a test failure instead of a mystery.

SKIPS ARE EXPLICIT. The subcommand assertions need `tmux` (fleet enumerates
panes) and `pi`. When those are absent this test SKIPS them with a reason
printed; it never fakes a pass.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fleet_paths as fp  # noqa: E402

# The legacy locations, spelled out on purpose: this is the pin.
LEGACY = {
    "control_dir": "/root/.fleet",
    "work_root": "/root/fleet",
    "agent_home": "/root/.pi/agent",
    "handoff_dir": "/root/handoffs",
}


def env_without_state():
    """A copy of the environment with every fleet override removed."""
    e = dict(os.environ)
    for k in ("FLEET_CONTROL_DIR", "FLEET_WORK_ROOT", "PI_AGENT_HOME",
              "FLEET_HANDOFF_DIR", "FLEET_REPORTS_DIR", "FLEET_BIN_DIR",
              "FLEET_REPO", "FLEET_STATE", "FLEET_LOCK", "FLEET_REGISTRY",
              "FLEET_LINEAGE", "FLEET_DIGESTS", "FLEET_LEDGER", "FLEET_NAMES_DIR",
              "FLEET_VERIFY_CACHE", "FLEET_REPORTS", "FLEET_TRUST"):
        e.pop(k, None)
    return e


def run(args, cwd=None, env=None, timeout=180):
    p = subprocess.run([str(a) for a in args], cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, timeout=timeout,
                       env=env if env is not None else env_without_state())
    return p.returncode, (p.stdout or ""), (p.stderr or "")


class TestDefaultsAreTheLegacyPaths(unittest.TestCase):
    """(a) With no environment set, behaviour on the box that grew up with it is
    unchanged. HOME is what makes this portable, so the expectation is HOME-based
    too — and on this box HOME is /root, which is the pin above."""

    def test_defaults_match_the_legacy_locations(self):
        home = Path.home()
        self.assertEqual(fp.control_dir(), home / ".fleet", "control dir moved")
        self.assertEqual(fp.work_root(), home / "fleet", "work root moved")
        self.assertEqual(fp.agent_home(), home / ".pi" / "agent", "agent home moved")
        self.assertEqual(fp.handoff_dir(), home / "handoffs", "handoff dir moved")
        self.assertEqual(fp.reports_dir(), home / "docs" / "reports", "reports dir moved")

    def test_on_this_box_that_is_literally_the_legacy_path(self):
        if Path.home().as_posix() != "/root":
            self.skipTest(f"HOME is {Path.home()}, not /root — nothing to pin here")
        for name, want in LEGACY.items():
            self.assertEqual(getattr(fp, name)(), Path(want),
                             f"{name}() no longer resolves to its legacy path")

    def test_bin_dir_is_resolved_from_this_file_not_hardcoded(self):
        self.assertEqual(fp.bin_dir().resolve(), HERE,
                         "bin_dir() must be the directory fleet_paths.py lives in")

    def test_bin_dir_follows_a_symlink_into_the_real_package(self):
        """An install that symlinks the CLIs into ~/bin must still find its own
        siblings (fleet shells out to pi-tmux, pi-msg, ... next to itself)."""
        with tempfile.TemporaryDirectory() as tmp:
            link = Path(tmp) / "fleet_paths.py"
            link.symlink_to(HERE / "fleet_paths.py")
            rc, out, err = run([sys.executable, "-c",
                                "import sys;sys.path.insert(0,%r);import fleet_paths;"
                                "print(fleet_paths.bin_dir())" % str(Path(tmp))],
                               env=env_without_state())
            self.assertEqual(rc, 0, err)
            self.assertEqual(Path(out.strip()).resolve(), HERE)

    def test_describe_reports_every_location(self):
        d = fp.describe()
        for k in ("control_dir", "work_root", "agent_home", "handoff_dir",
                  "reports_dir", "bin_dir", "home", "repo"):
            self.assertIn(k, d)


class TestEveryValueIsOverridable(unittest.TestCase):
    """(b) Each location has an environment override, and the override wins."""

    def test_overrides_win_over_the_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = env_without_state()
            env.update({"FLEET_CONTROL_DIR": f"{tmp}/c", "FLEET_WORK_ROOT": f"{tmp}/w",
                        "PI_AGENT_HOME": f"{tmp}/a", "FLEET_HANDOFF_DIR": f"{tmp}/h",
                        "FLEET_REPORTS_DIR": f"{tmp}/r", "FLEET_BIN_DIR": f"{tmp}/b",
                        "FLEET_REPO": f"{tmp}/repo"})
            probe = (
                "import sys;sys.path.insert(0,%r);import fleet_paths as f;"
                "print(f.control_dir());print(f.work_root());print(f.agent_home());"
                "print(f.handoff_dir());print(f.reports_dir());print(f.bin_dir());"
                "print(f.repo())" % str(HERE)
            )
            rc, out, err = run([sys.executable, "-c", probe], env=env)
            self.assertEqual(rc, 0, err)
            got = [ln.strip() for ln in out.strip().splitlines() if ln.strip()]
            want = [f"{tmp}/c", f"{tmp}/w", f"{tmp}/a", f"{tmp}/h", f"{tmp}/r",
                    f"{tmp}/b", f"{tmp}/repo"]
            self.assertEqual(got, want, "an environment override was ignored")

    def test_an_empty_override_is_treated_as_unset(self):
        """A cron line that exports an unset variable (`STATE=$NOPE`) must not
        resolve the store to the current directory."""
        env = env_without_state()
        env["FLEET_CONTROL_DIR"] = ""
        probe = ("import sys;sys.path.insert(0,%r);import fleet_paths as f;"
                 "print(f.control_dir())" % str(HERE))
        rc, out, err = run([sys.executable, "-c", probe], env=env)
        self.assertEqual(rc, 0, err)
        self.assertEqual(Path(out.strip()), Path.home() / ".fleet")


class TestRepoHasNoPrivateDefault(unittest.TestCase):
    """The one default that HAD to change: a public tool must not point at
    somebody else's private project, and must say what to set instead."""

    def test_env_var_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = env_without_state()
            env["FLEET_REPO"] = tmp
            probe = ("import sys;sys.path.insert(0,%r);import fleet_paths as f;"
                     "print(f.repo())" % str(HERE))
            rc, out, err = run([sys.executable, "-c", probe], env=env)
            self.assertEqual(rc, 0, err)
            self.assertEqual(Path(out.strip()), Path(tmp))

    def test_no_repo_is_none_and_not_a_stranger_tree(self):
        """Outside any git repository, with no FLEET_REPO, the answer is None —
        not a path somebody's project happens to sit at."""
        with tempfile.TemporaryDirectory() as tmp:
            env = env_without_state()
            env["FLEET_WORK_ROOT"] = f"{tmp}/work"
            probe = ("import sys;sys.path.insert(0,%r);import fleet_paths as f;"
                     "print(f.repo())" % str(HERE))
            rc, out, err = run([sys.executable, "-c", probe], cwd=tmp, env=env)
            self.assertEqual(rc, 0, err)
            self.assertEqual(out.strip(), "None",
                             "repo() invented a target: " + out.strip())

    def test_require_repo_names_the_variable_to_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = env_without_state()
            env["FLEET_WORK_ROOT"] = f"{tmp}/work"
            probe = ("import sys;sys.path.insert(0,%r);import fleet_paths as f;"
                     "f.require_repo()" % str(HERE))
            rc, out, err = run([sys.executable, "-c", probe], cwd=tmp, env=env)
            self.assertNotEqual(rc, 0, "require_repo() must fail when there is no target")
            self.assertIn("FLEET_REPO", out + err)

    def test_the_cli_needs_no_private_repo(self):
        """NO LITERAL private host volume in any shipped file: a private host path
        is not a default a stranger can inherit."""
        offenders = []
        for p in shipped_files():
            if PRIVATE_VOLUME in p.read_text(errors="replace"):
                offenders.append(p.name)
        self.assertEqual(offenders, [], "private host path still in: " + ", ".join(offenders))


class TestNoHardcodedHomePaths(unittest.TestCase):
    """The rule the seam exists to enforce: no shipped module spells an absolute
    home directory. fleet_paths is the one file allowed to know a default."""

    def test_no_shipped_file_contains_an_absolute_home_path(self):
        offenders = []
        for p in shipped_files():
            if p.name == "fleet_paths.py":
                continue
            hits = [i + 1 for i, ln in enumerate(p.read_text(errors="replace").splitlines())
                    if "/root/" in ln]
            if hits:
                offenders.append(f"{p.name}:{hits[0]}")
        self.assertEqual(offenders, [],
                         "hardcoded home path in shipped file(s): " + ", ".join(offenders))


class TestShellAndPythonAgree(unittest.TestCase):
    """bin/'s shell scripts compute the same defaults from $HOME because they
    cannot import python. Assert the two spellings agree, or the seam drifts."""

    def _bash(self, expr, home):
        env = env_without_state()
        env["HOME"] = home
        env.pop("PI_AGENT_HOME", None)
        return run(["bash", "-c", expr], env=env)

    def test_agent_home_spellings_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out, err = self._bash('echo "${PI_AGENT_HOME:-${HOME}/.pi/agent}"', tmp)
            self.assertEqual(rc, 0, err)
            old = os.environ.get("HOME")
            os.environ["HOME"] = tmp
            try:
                self.assertEqual(Path(out.strip()), fp.agent_home())
            finally:
                if old is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old

    def test_pi_msg_resolves_the_broker_from_the_seam(self):
        """pi-msg holds the one path that is not under a fleet store: the
        pi-intercom CLI inside the agent home.

        The broker became a declared dependency, so pi-msg now resolves a LADDER (PATH,
        then the agent home, then this package's node_modules, then npx) instead of one
        literal. The assertion is narrower on purpose: the agent home must still come
        from the seam, both sibling entry points must be tried (cli.mjs is the npm bin;
        cli.ts is the TypeScript sibling a release may not ship), and the message when
        every rung fails must name the install command rather than a path.
        """
        src = (HERE / "pi-msg").read_text()
        self.assertRegex(src, r'AGENT_HOME="\$\{PI_AGENT_HOME:-\$\{HOME\}/\.pi/agent\}"',
                         "pi-msg no longer resolves its agent home from the seam")
        self.assertIn('dir="${AGENT_HOME}/npm/node_modules/pi-intercom"', src,
                      "pi-msg no longer looks for the broker in the agent home")
        self.assertIn('"$dir/cli.mjs"', src, "pi-msg does not try the npm bin (cli.mjs)")
        self.assertIn('"$dir/cli.ts"', src, "pi-msg does not try the cli.ts sibling")
        self.assertIn('tsx="${AGENT_HOME}/npm/node_modules/tsx/dist/cli.mjs"', src,
                      "the cli.ts rung needs the tsx runner beside it")
        self.assertIn("pi install npm:pi-intercom", src,
                      "a missing broker must name the command that installs it")
        self.assertNotIn("/ro" + "ot/", src,
                         "pi-msg must not carry an absolute home path")

    def test_cron_wrappers_resolve_their_own_bin_directory(self):
        for name in ("fleet-pid-assert-cron.sh", "fleet-tmux-orphan-cron.sh",
                     "fleet-sentinel-cron.sh"):
            src = (HERE / name).read_text()
            self.assertIn("FLEET_BIN_DIR", src, f"{name} does not honour FLEET_BIN_DIR")
            self.assertIn("FLEET_CONTROL_DIR", src, f"{name} does not honour FLEET_CONTROL_DIR")


class TestAFreshStoreActuallyWorks(unittest.TestCase):
    """(c) A fleet subcommand against a fresh temp control dir and work root.

    The point is not "the command ran". It is "the command ran against the temp
    store": so the store must be written where the override says, and the real
    store must be untouched afterwards.
    """

    def setUp(self):
        missing = [b for b in ("tmux", "pi") if not shutil.which(b)]
        if missing:
            self.skipTest("subcommand assertions need " + ", ".join(missing)
                          + " on PATH — skipped rather than faked")
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.env = env_without_state()
        self.env.update({"FLEET_CONTROL_DIR": str(self.tmp / "c"),
                         "FLEET_WORK_ROOT": str(self.tmp / "w"),
                         "PI_AGENT_HOME": str(self.tmp / "a"),
                         "FLEET_HANDOFF_DIR": str(self.tmp / "h"),
                         "FLEET_REPO": str(HERE.parent)})

    def tearDown(self):
        self._tmp.cleanup()

    def _real_store(self) -> Path:
        return Path.home() / ".fleet" / "lineage.json"

    def test_list_json_works_against_a_temp_control_dir(self):
        rc, out, err = run([sys.executable, HERE / "fleet", "list", "--json"],
                           cwd=self.tmp, env=self.env)
        self.assertEqual(rc, 0, f"fleet list --json failed: {err[-400:]}")
        start = out.find("[")
        self.assertGreaterEqual(start, 0, f"no JSON in output: {out[:200]}")
        data = json.loads(out[start:])
        self.assertIsInstance(data, list)
        # It must enumerate the repo it was TOLD about (FLEET_REPO=this package's
        # repo), not whatever happens to be lying around, and it must not have
        # created the registry it would write on a mutating verb.
        self.assertFalse((self.tmp / "c" / "registry.json").exists(),
                         "a read-only verb wrote the registry")

    def test_the_store_is_created_where_the_override_says(self):
        """save_lineage is the writer every mutating verb goes through, lock and
        all. Point the control dir at a temp dir and the store must appear there —
        and the probe must not be in the real store.

        The assertion is by CONTENT MARKER, not by hash: this box's live store is
        written by a running fleet, so "the file did not change" would be a flaky
        accusation. "My record is not in it" is exact.
        """
        marker = "seam-probe-not-in-the-real-store"
        probe = (
            "import sys;sys.path.insert(0,%r);"
            "import fleet_children as fc;"
            "fc.save_lineage({'children': {%r: {'name': %r}}});"
            "print(fc.LINEAGE)" % (str(HERE), marker, marker)
        )
        rc, out, err = run([sys.executable, "-c", probe], cwd=self.tmp, env=self.env)
        self.assertEqual(rc, 0, f"save_lineage failed: {err[-400:]}")
        written = Path(out.strip())
        self.assertEqual(written, self.tmp / "c" / "lineage.json",
                         "the store was not written under FLEET_CONTROL_DIR")
        self.assertTrue(written.is_file(), "no store file was created")
        self.assertIn(marker, written.read_text(),
                      "the record did not reach the temp store")
        real = self._real_store()
        if real.is_file():
            self.assertNotIn(marker, real.read_text(errors="replace"),
                             "a write aimed at a temp control dir landed in the REAL store")

    def test_spawn_without_a_repo_says_what_to_set(self):
        """The public-facing half of the changed default: refuse, and name the
        variable. It must refuse BEFORE creating anything."""
        env = dict(self.env)
        env.pop("FLEET_REPO", None)
        plan = self.tmp / "plan.md"
        plan.write_text("## U1 — probe\nNothing here.\ndone-test: true\n")
        rc, out, err = run([sys.executable, HERE / "fleet", "spawn", "meta-pathprobe-1",
                            "--plan", plan, "--unit", "1"], cwd=self.tmp, env=env)
        self.assertNotEqual(rc, 0, "spawn without a repo target must fail")
        self.assertIn("FLEET_REPO", out + err,
                      "the failure must name FLEET_REPO: " + (out + err)[-300:])
        self.assertFalse((self.tmp / "w" / "meta-pathprobe-1").exists(),
                         "spawn created a worktree before resolving its repo")


# Assembled from fragments, exactly as tests/readiness.py assembles its own
# vocabulary list: this test file IS scanned by that gate, and a literal would
# report itself. The fragments say the same thing the joined string says.
PRIVATE_VOLUME = "/mnt/" + "HC" + "_Volume" + "_105587324"


def shipped_files():
    """Every file this package ships that can hold a path: python, shell, node."""
    out = []
    for p in sorted(HERE.iterdir()):
        if not p.is_file() or p.name.startswith("test_") or p.name.startswith("."):
            continue
        if p.suffix in (".py", ".sh", ".md", ".json"):
            out.append(p)
        elif p.suffix == "" or p.name.startswith("pi-") or p.name == "fleet":
            out.append(p)
    return out


if __name__ == "__main__":
    unittest.main(verbosity=2)
