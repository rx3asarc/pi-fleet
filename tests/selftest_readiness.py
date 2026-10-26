#!/usr/bin/env python3
"""Self-test for tests/readiness.py -- proof that the gate can actually FAIL.

Each test builds a throwaway tree in a tmpdir, plants exactly one violation, and
runs the real CLI as a subprocess (so the exit codes are the ones CI would see,
not an in-process approximation of them).

A readiness checker that cannot fail is worse than no checker: it launders a
"yes" out of an unshipable tree. This file exists so the gate has to earn its
green.

    python3 tests/selftest_readiness.py
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CHECKER = Path(__file__).resolve().parent / "readiness.py"

# Built, never written as a literal: this file is scanned by the gate when the
# gate runs over the repo it lives in, and a real-looking token in the source
# would be a finding against this repo.
FAKE_GITHUB_TOKEN = "gho_" + "A" * 36
PRIVATE_WORD = "nor" + "disk"
HOME_PATH = "/ro" + "ot" + "/.fleet"

README_TEXT = "\n".join(
    "Line %d: a README long enough that a stranger can actually install this." % n
    for n in range(1, 46)) + "\n"

LICENSE_TEXT = """MIT License

Copyright (c) 2026 Test Author

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.
"""

MANIFEST = {
    "name": "fixture-pkg",
    "version": "0.1.0",
    "description": "a fixture package",
    "license": "MIT",
    "keywords": ["pi-package"],
    "dependencies": {"pi-intercom": "^0.16.1"},
    "pi": {"skills": ["./skills"]},
}

FLEET_CLI = """#!/usr/bin/env python3
# a fixture CLI that shells out to the packaged helpers
import subprocess

HELPERS = ['pi-msg', 'pi-tmux', 'pi-live', 'pi-tree']


def probe():
    return [subprocess.run([h, '--help'], capture_output=True) for h in HELPERS]


if __name__ == '__main__':
    print('fleet fixture', len(probe()))
"""

GITIGNORE = "__pycache__/\n*.pyc\n*.bak*\n"

ALL_CHECK_IDS = {
    "secrets", "vendored-binary", "backup-file", "pycache", "license", "readme",
    "package-manifest", "internal-vocab", "hardcoded-home", "toolchain-deps",
    "guard-selftests", "intercom-dependency",
}

# A fixture guard: it passes its own selftest ONLY when handed --selftest, which is how
# these fixtures prove the gate actually calls the guard instead of restating it.
GUARD_STUB = '''#!/usr/bin/env python3
import sys

if "--selftest" not in sys.argv:
    print("usage: %s --selftest", file=sys.stderr)
    sys.exit(2)
print('{"selftest_ok": true}')
sys.exit(0)
'''

GUARD_NAMES = ("fleet_pid_assert.py", "fleet_tmux_orphan_guard.py", "fleet_name_lint.py")

# A fixture pi-msg that behaves like the real one with no broker reachable: it fails, and
# it says what to run. A stub that exits 0 would make the clean tree a lie.
PI_MSG_BROKERLESS_STUB = '''#!/bin/bash
echo "pi-msg: the pi-intercom broker is not installed - run: pi install npm:pi-intercom" >&2
exit 1
'''


def build_clean_tree(root):
    """A minimal tree that must be reported READY."""
    root = Path(root)
    (root / "bin").mkdir(parents=True, exist_ok=True)
    (root / "skills" / "fleet").mkdir(parents=True, exist_ok=True)
    (root / "LICENSE").write_text(LICENSE_TEXT, encoding="utf-8")
    (root / "README.md").write_text(README_TEXT, encoding="utf-8")
    (root / "package.json").write_text(json.dumps(MANIFEST, indent=2) + "\n",
                                       encoding="utf-8")
    (root / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    (root / "bin" / "fleet").write_text(FLEET_CLI, encoding="utf-8")
    (root / "bin" / "fleet").chmod(0o755)
    for helper in ("pi-msg", "pi-tmux", "pi-live", "pi-tree"):
        path = root / "bin" / helper
        path.write_text("#!/usr/bin/env python3\nprint('%s fixture')\n" % helper,
                        encoding="utf-8")
        path.chmod(0o755)
    # pi-msg is not a plain fixture: with no broker reachable it must fail actionably,
    # because the gate probes exactly that failure.
    (root / "bin" / "pi-msg").write_text(PI_MSG_BROKERLESS_STUB, encoding="utf-8")
    for guard in GUARD_NAMES:
        gpath = root / "bin" / guard
        gpath.write_text(GUARD_STUB, encoding="utf-8")
        gpath.chmod(0o755)
    (root / "skills" / "fleet" / "SKILL.md").write_text(
        "# fixture skill\n\nrun bin/fleet\n", encoding="utf-8")
    return root


def run_gate(repo, json_mode=True):
    cmd = [sys.executable, str(CHECKER), "--repo", str(repo)]
    if json_mode:
        cmd.append("--json")
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env,
                          timeout=120)
    document = None
    if json_mode and proc.returncode in (0, 1) and proc.stdout.strip():
        document = json.loads(proc.stdout)
    return proc.returncode, document, proc.stdout + proc.stderr


def finding_ids(document):
    return {f.get("id") for f in (document or {}).get("findings", [])}


class GateTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="readiness-selftest-")
        self.tree = build_clean_tree(Path(self.tmp) / "tree")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- the baseline: a clean tree is READY, and the gate says how it decided --

    def test_clean_tree_is_ready(self):
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 0, "clean tree should exit 0, got %d:\n%s" % (rc, out))
        self.assertTrue(doc["ok"], "clean tree should report ok=true")
        self.assertEqual(doc["findings"], [])
        declared = {c["id"] for c in doc["checks"]}
        self.assertEqual(declared, ALL_CHECK_IDS,
                         "the gate must declare every check it claims to run")

    def test_human_output_is_readable(self):
        rc, _, out = run_gate(self.tree, json_mode=False)
        self.assertEqual(rc, 0)
        self.assertIn("READY", out)
        for check_id in ALL_CHECK_IDS:
            self.assertIn(check_id, out)

    # -- one planted violation each, and the RIGHT id for it -------------------

    def test_planted_secret_is_caught_and_never_printed(self):
        (self.tree / "bin" / "leak.py").write_text(
            'TOKEN = "%s"\n' % FAKE_GITHUB_TOKEN, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a planted credential must exit 1:\n%s" % out)
        self.assertIn("secrets", finding_ids(doc))
        self.assertNotIn(FAKE_GITHUB_TOKEN, out,
                         "the gate must never print the secret value itself")

    def test_planted_backup_file_is_caught_despite_gitignore(self):
        # .gitignore says "*.bak*"; a stray backup is still a finding, because
        # "gitignored" is not "not shippable" -- another packager still sees it.
        self.assertIn("*.bak*", (self.tree / ".gitignore").read_text())
        (self.tree / "bin" / "fleet.bak").write_text(FLEET_CLI, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a .bak file must exit 1 even when gitignored:\n%s" % out)
        self.assertIn("backup-file", finding_ids(doc))

    def test_planted_pycache_is_caught_despite_gitignore(self):
        cache = self.tree / "bin" / "__pycache__"
        cache.mkdir()
        (cache / "fleet.cpython-311.pyc").write_bytes(b"\x00bytecode")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "bytecode cache must exit 1 even when gitignored:\n%s" % out)
        self.assertIn("pycache", finding_ids(doc))

    def test_missing_license_is_caught(self):
        (self.tree / "LICENSE").unlink()
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "no LICENSE must exit 1:\n%s" % out)
        self.assertIn("license", finding_ids(doc))

    def test_empty_license_is_caught(self):
        (self.tree / "LICENSE").write_text("", encoding="utf-8")
        rc, doc, _ = run_gate(self.tree)
        self.assertEqual(rc, 1)
        self.assertIn("license", finding_ids(doc))

    def test_shallow_readme_is_caught(self):
        (self.tree / "README.md").write_text("# fixture\n", encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a one-line README must exit 1:\n%s" % out)
        self.assertIn("readme", finding_ids(doc))

    def test_manifest_without_pi_package_keyword_is_caught(self):
        manifest = dict(MANIFEST)
        manifest["keywords"] = ["cli", "tmux"]
        (self.tree / "package.json").write_text(json.dumps(manifest),
                                                encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a missing pi-package keyword must exit 1:\n%s" % out)
        self.assertIn("package-manifest", finding_ids(doc))

    def test_manifest_pointing_at_a_missing_pi_path_is_caught(self):
        manifest = json.loads(json.dumps(MANIFEST))
        manifest["pi"] = {"skills": ["./skills"], "extensions": ["./extensions"]}
        (self.tree / "package.json").write_text(json.dumps(manifest),
                                                encoding="utf-8")
        rc, doc, _ = run_gate(self.tree)
        self.assertEqual(rc, 1)
        self.assertIn("package-manifest", finding_ids(doc))

    def test_invalid_manifest_json_is_caught(self):
        (self.tree / "package.json").write_text("{ not json", encoding="utf-8")
        rc, doc, _ = run_gate(self.tree)
        self.assertEqual(rc, 1)
        self.assertIn("package-manifest", finding_ids(doc))

    def test_private_vocabulary_is_caught(self):
        (self.tree / "bin" / "notes.py").write_text(
            "# tuned for the %s storefront\n" % PRIVATE_WORD, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "private vocabulary must exit 1:\n%s" % out)
        self.assertIn("internal-vocab", finding_ids(doc))

    def test_absolute_home_path_is_caught(self):
        (self.tree / "bin" / "state.py").write_text(
            'DEFAULT = "%s"\n' % HOME_PATH, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a hardcoded home path must exit 1:\n%s" % out)
        self.assertIn("hardcoded-home", finding_ids(doc))

    def test_path_module_is_allowed_to_hold_the_defaults(self):
        # the module that OWNS path resolution is the one place a home path may
        # live -- otherwise the gate would forbid the fix it asks for
        (self.tree / "bin" / "fleet_paths.py").write_text(
            'DEFAULT_CONTROL = "%s"\n' % HOME_PATH, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 0, "bin/fleet_paths.py must be exempt:\n%s" % out)

    def test_missing_toolchain_dependency_is_caught(self):
        for helper in ("pi-msg", "pi-tmux", "pi-live", "pi-tree"):
            (self.tree / "bin" / helper).unlink()
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a missing shelled-out CLI must exit 1:\n%s" % out)
        self.assertIn("toolchain-deps", finding_ids(doc))

    # -- the guards: the gate CALLS their own tests, it does not restate them ---

    def test_missing_guard_is_caught(self):
        # a guard that is not shipped is not a guard, and its selftest cannot run
        (self.tree / "bin" / "fleet_name_lint.py").unlink()
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a missing guard must exit 1:\n%s" % out)
        self.assertIn("guard-selftests", finding_ids(doc))

    def test_failing_guard_selftest_is_caught_and_reported_with_its_output(self):
        failing = ('#!/usr/bin/env python3\n'
                   'import sys\n'
                   'print("pageRank: 3 of 4 cases failed")\n'
                   'sys.exit(3)\n')
        (self.tree / "bin" / "fleet_pid_assert.py").write_text(failing, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a red guard selftest must exit 1:\n%s" % out)
        details = " ".join(f.get("detail", "") for f in doc["findings"]
                           if f.get("id") == "guard-selftests")
        self.assertIn("fleet_pid_assert.py", details,
                      "the finding must name the guard that failed")
        self.assertIn("3 of 4 cases failed", details,
                      "the finding must carry the tail of the guard's own output")

    def test_guard_that_ignores_selftest_is_caught(self):
        # the fixture guards exit 2 unless --selftest is passed; a gate that forgot the
        # flag would report them red, so this proves the flag is really passed
        stubborn = '#!/usr/bin/env python3\nimport sys\nsys.exit(0 if "--selftest" in sys.argv else 2)\n'
        for guard in GUARD_NAMES:
            (self.tree / "bin" / guard).write_text(stubborn, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 0, "guards that want --selftest must pass:\n%s" % out)

    # -- the broker: declared, and its absence explained -----------------------

    def test_undeclared_broker_dependency_is_caught(self):
        manifest = json.loads(json.dumps(MANIFEST))
        manifest.pop("dependencies", None)
        (self.tree / "package.json").write_text(json.dumps(manifest), encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "an undeclared broker must exit 1:\n%s" % out)
        self.assertIn("intercom-dependency", finding_ids(doc))

    def test_pi_msg_that_succeeds_without_a_broker_is_caught(self):
        # exit 0 with no broker reachable cannot be a delivery; it is a lie
        (self.tree / "bin" / "pi-msg").write_text("#!/bin/bash\nexit 0\n",
                                                   encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a pi-msg that exits 0 without a broker must exit 1:\n%s" % out)
        self.assertIn("intercom-dependency", finding_ids(doc))

    def test_pi_msg_dead_end_message_is_caught(self):
        # the old failure mode: a path, no action
        (self.tree / "bin" / "pi-msg").write_text(
            '#!/bin/bash\necho "pi-intercom not installed at %s/npm/node_modules" >&2\nexit 1\n'
            % HOME_PATH, encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a dead-end failure message must exit 1:\n%s" % out)
        self.assertIn("intercom-dependency", finding_ids(doc))
        reasons = " ".join(f.get("detail", "") for f in doc["findings"]
                           if f.get("id") == "intercom-dependency")
        self.assertIn("naming the package to install", reasons)

    def test_vendored_large_binary_is_caught(self):
        blob = b"\x7fELF" + b"\x00" * (1200 * 1024)
        (self.tree / "bin" / "fd").write_bytes(blob)
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "a >1MB binary must exit 1:\n%s" % out)
        self.assertIn("vendored-binary", finding_ids(doc))

    # -- the allowlist has a floor: it may never silence a credential ----------

    def test_allowlist_can_silence_an_explained_finding(self):
        (self.tree / "bin" / "fleet.bak").write_text(FLEET_CLI, encoding="utf-8")
        (self.tree / ".readiness-allow").write_text(
            "backup-file bin/fleet.bak leftover from a merge, deleted next commit\n",
            encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 0, "an explained allowlist line should clear it:\n%s" % out)
        self.assertEqual(doc["findings"], [])
        self.assertTrue(doc["suppressed"], "suppressions must be reported, not hidden")

    def test_allowlist_cannot_silence_secrets(self):
        (self.tree / "bin" / "leak.py").write_text(
            'TOKEN = "%s"\n' % FAKE_GITHUB_TOKEN, encoding="utf-8")
        (self.tree / ".readiness-allow").write_text(
            "secrets bin/leak.py it is only a test fixture\n", encoding="utf-8")
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 1, "the allowlist must never silence secrets:\n%s" % out)
        self.assertIn("secrets", finding_ids(doc))
        self.assertNotIn(FAKE_GITHUB_TOKEN, out)

    # -- the gate's own edges --------------------------------------------------

    def test_missing_repo_exits_two_not_zero(self):
        rc, _, out = run_gate(Path(self.tmp) / "does-not-exist")
        self.assertEqual(rc, 2, "a bad --repo must be 'cannot run' (2), not green: %s" % out)

    def test_repo_that_is_a_file_exits_two(self):
        target = Path(self.tmp) / "a-file"
        target.write_text("x", encoding="utf-8")
        rc, _, out = run_gate(target)
        self.assertEqual(rc, 2, "a file as --repo must exit 2: %s" % out)

    def test_gate_does_not_flag_its_own_source(self):
        # the checker holds the secret PATTERNS and the private WORDS in its own
        # source; if it matched itself, every repo shipping it would be red
        shutil.copy(str(CHECKER), str(self.tree / "bin" / "tool.py"))
        rc, doc, out = run_gate(self.tree)
        self.assertEqual(rc, 0, "the gate must not trip on its own source:\n%s" % out)

    def test_text_mode_never_prints_a_secret_value(self):
        (self.tree / "bin" / "leak.py").write_text(
            'TOKEN = "%s"\n' % FAKE_GITHUB_TOKEN, encoding="utf-8")
        rc, _, out = run_gate(self.tree, json_mode=False)
        self.assertEqual(rc, 1)
        self.assertIn("secrets", out)
        self.assertNotIn(FAKE_GITHUB_TOKEN, out)


class RealToolingTest(unittest.TestCase):
    """The shipped tools, not fixtures: the failure a stranger actually meets.

    Every other test here builds a tree. This one runs this repo's own `bin/pi-msg` with
    no broker on PATH and an empty agent home, because "the message is actionable" is a
    claim about the bytes that ship, and a fixture cannot prove it.
    """

    MSG = Path(__file__).resolve().parent.parent / "bin" / "pi-msg"

    def test_shipped_pi_msg_fails_actionably_without_a_broker(self):
        self.assertTrue(self.MSG.is_file(), "bin/pi-msg is missing")
        tmp = tempfile.mkdtemp(prefix="pi-msg-nobroker-")
        try:
            env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": tmp,
                   "AGENT_HOME": tmp, "PI_AGENT_HOME": tmp}
            proc = subprocess.run(
                ["bash", str(self.MSG), "send", "--to", "nobody", "--text", "probe"],
                capture_output=True, text=True, env=env, timeout=180)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        blob = (proc.stdout or "") + (proc.stderr or "")
        self.assertNotEqual(proc.returncode, 0,
                            "pi-msg must not report success with no broker: %s" % blob[:300])
        self.assertIn("install", blob.lower(),
                      "the failure must name the fix, got: %s" % blob[:300])
        self.assertIn("npm:pi-intercom", blob,
                      "the fix must be the package a user can install: %s" % blob[:300])
        self.assertNotIn(HOME_PATH, blob,
                         "a dangling absolute path is not an instruction: %s" % blob[:300])


if __name__ == "__main__":
    if not CHECKER.is_file():
        print("FATAL: %s is missing" % CHECKER)
        sys.exit(2)
    unittest.main(verbosity=2)
