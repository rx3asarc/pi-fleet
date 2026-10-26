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
}


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
                         "the gate must declare all ten checks")

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


if __name__ == "__main__":
    if not CHECKER.is_file():
        print("FATAL: %s is missing" % CHECKER)
        sys.exit(2)
    unittest.main(verbosity=2)
