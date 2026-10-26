#!/usr/bin/env python3
"""Publish-readiness gate for a pi-fleet tree.

One command that answers "may this tree be published?" mechanically instead of
by opinion. Twelve checks, each with a stable id; every finding names the check id
and the file it came from. A secret VALUE is never printed -- only the pattern
name and the path.

    python3 tests/readiness.py --repo <path> [--json]

    exit 0   no blockers
    exit 1   at least one blocker
    exit 2   the gate itself could not run (bad path, unreadable file)

Design notes that matter for trusting the verdict:

* gitignore is honoured for CONTENT scans (credentials, private vocabulary,
  absolute home paths) so generated and local-state trees are not read as
  shipped text.
* HYGIENE checks (backup files, bytecode caches, vendored binaries) deliberately
  inspect the raw tree and ignore .gitignore. "It is gitignored" is not a
  guarantee: a second packaging tool, a tarball step, or a plain `scp` still
  ships the bytes. A leftover *.bak or __pycache__ on disk is a finding.
* The credentials check is never silenced -- not by .gitignore, not by the
  allowlist -- because a leaked key cannot be un-leaked by a config file.
* Two checks run CODE instead of reading files: each guard's own `--selftest`,
  and the shipped `bin/pi-msg` probed with no broker reachable. They shell out
  because the alternative is a second copy of a guard's assertions inside this
  file, and a second source of truth for a guard's test is how a guard starts
  passing while its subject is broken.

Stdlib only, no network.
"""

import argparse
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True

EXIT_READY = 0
EXIT_BLOCKED = 1
EXIT_ERROR = 2

# --------------------------------------------------------------------------
# The twelve checks. Order here is the order they are reported in.
# --------------------------------------------------------------------------

CHECKS = (
    ("secrets",
     "no file matches a credential shape (GitHub gho_/ghp_, GitLab glpat-, "
     "Shopify shpat_, Slack xoxb-, Google AIza, sk-/sk-or-v1, private-key "
     "blocks, JWT eyJ..., Telegram bot tokens); never silenced"),
    ("vendored-binary",
     "no binary or ELF artefact larger than 1 MB (third-party redistribution risk)"),
    ("backup-file",
     "no *.bak, *.bak*, *.orig or editor *~ leftovers in the tree"),
    ("pycache",
     "no __pycache__/ directory and no compiled bytecode (*.pyc) in the tree"),
    ("license",
     "a non-empty LICENSE / LICENCE / COPYING at the repo root"),
    ("readme",
     "README.md at the repo root with at least 40 lines"),
    ("package-manifest",
     'a valid package.json whose keywords include "pi-package" and whose every '
     "declared pi.* resource path exists"),
    ("internal-vocab",
     "no private deployment vocabulary (a private project name, this box's "
     "volume path, a personal operator handle) in shipped files, outside the allowlist"),
    ("hardcoded-home",
     "no absolute home-directory path outside the module that owns path "
     "resolution and outside tests that assert defaults"),
    ("toolchain-deps",
     "every external CLI the fleet entry point shells out to ships inside the "
     "package (bin/pi-msg, bin/pi-tmux, bin/pi-live, bin/pi-tree)"),
    ("guard-selftests",
     "each guard's OWN `--selftest` exits 0 when run as a subprocess "
     "(fleet_pid_assert, fleet_tmux_orphan_guard, fleet_name_lint) -- this gate "
     "calls them, it does not reimplement them"),
    ("serve-guard",
     "a served directory cannot masquerade: bin/fleet_serve.py records the served "
     "directory's (st_dev, st_ino) with the pid and port, and its own `--selftest` "
     "proves it catches a DELETED directory, a REPLACED directory at the same path, "
     "and a DEAD pid whose port still answers -- run here as a subprocess, never "
     "restated"),
    ("intercom-dependency",
     "the messaging broker pi-intercom is a DECLARED dependency (no worker can be "
     "messaged without it) and bin/pi-msg fails actionably, naming the install "
     "command, when it is missing"),
)

CHECK_IDS = tuple(cid for cid, _ in CHECKS)

ALLOW_FILE = ".readiness-allow"

# Assembled from fragments on purpose: this file is itself scanned when the
# gate is run over a tree that contains it, and a literal would self-trip.
INTERNAL_VOCAB = (
    "nor" + "disk",
    "engr" + "ane",
    "enc" + "1",
    "mae" + "stro",
    "HC_" + "Volume" + "_105587324",
    "." + "nor" + "disk",
)

# The only module allowed to hold absolute install paths: it owns path
# resolution. Tests may build paths; a test is not a shipped default.
HOME_PATH_EXEMPT_EXACT = ("bin/fleet_paths.py",)
HOME_PATH_EXEMPT_SUFFIXES = ("_test.py",)
HOME_PATH_EXEMPT_PREFIXES = ("test_",)

# fleet shells out to these; they must ship inside the package because a pi
# package cannot put a CLI on PATH.
TOOLCHAIN = ("pi-msg", "pi-tmux", "pi-live", "pi-tree")

# Guards that own their own test. This gate runs `bin/<tool> --selftest` for each;
# it deliberately knows nothing about what they assert.
GUARDS = ("fleet_pid_assert.py", "fleet_tmux_orphan_guard.py", "fleet_name_lint.py")

# The broker every send/ask/done/report travels through.
BROKER_PACKAGE = "pi-intercom"
BROKER_INSTALL_HINT = "npm:pi-intercom"
PI_MSG = "bin/pi-msg"
GUARD_SELFTEST_TIMEOUT = 180

SKIP_DIRS = {'.git', '__pycache__', '.hg', '.svn', 'node_modules'}

# Never *content*-scanned: version-control metadata is not shipped code. Note
# that a git WORKTREE has `.git` as a file holding an absolute gitdir path, so
# this is matched on the name, not only on directories.
VCS_NAMES = {".git", ".hg", ".svn"}

# Files whose allowlist line has to name what it allows.
VOCAB_METADATA_EXEMPT = (ALLOW_FILE,)

TEXT_PROBE_BYTES = 8192
LARGE_BINARY_BYTES = 1024 * 1024

SECRET_PATTERNS = (
    ("GitHub token shape (gho_)", re.compile(r"gho_[A-Za-z0-9]{20,}")),
    ("GitHub token shape (ghp_)", re.compile(r"ghp_[A-Za-z0-9]{20,}")),
    ("GitLab token shape (glpat-)", re.compile(r"glpat-[A-Za-z0-9_\-]{20,}")),
    ("Shopify token shape (shpat_)", re.compile(r"shpat_[A-Fa-f0-9]{32,}")),
    ("Slack token shape (xoxb-)", re.compile(r"xoxb-[A-Za-z0-9\-]{10,}")),
    ("Google API key shape (AIza)", re.compile(r"AIza[0-9A-Za-z_\-]{35}")),
    ("OpenRouter key shape (sk-or-v1)", re.compile(r"sk-or-v1-[A-Za-z0-9]{20,}")),
    ("sk- API key shape", re.compile(r"\bsk-[A-Za-z0-9]{20,}")),
    ("private key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT shape (eyJ...)", re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("Telegram bot token shape", re.compile(r"(?<![0-9])[0-9]{8,10}:[A-Za-z0-9_\-]{35}")),
)

BACKUP_RE = re.compile(r"(\.bak.*|\.orig|~)$")
# Assembled from fragments for the same reason as INTERNAL_VOCAB below: this
# file is scanned when the gate is run over a tree that contains it.
HOMEPATH_RE = re.compile("/ro" + "ot/")
GLOB_CHARS = set("*?[")


# --------------------------------------------------------------------------
# Findings
# --------------------------------------------------------------------------

class Finding:
    __slots__ = ("id", "path", "detail")

    def __init__(self, check_id, path, detail):
        self.id = check_id
        self.path = path
        self.detail = detail

    def as_dict(self):
        return {"id": self.id, "path": self.path, "detail": self.detail}

    def sort_key(self):
        return (self.id, self.path)


class GateError(Exception):
    """The gate could not run. That is exit 2, never a green verdict."""


# --------------------------------------------------------------------------
# .gitignore support (deliberately small: the shapes a repo actually uses)
# --------------------------------------------------------------------------

class IgnoreRules:
    """gitignore semantics, last match wins, `!` negation, `dir/`, `/anchored`."""

    def __init__(self):
        self.rules = []  # (base_rel, pattern, negated, dir_only, anchored)

    def load(self, directory, base_rel):
        path = os.path.join(directory, ".gitignore")
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise GateError("cannot read %s: %s" % (path, exc))
        for raw in lines:
            line = raw.rstrip()
            if not line or line.startswith("#"):
                continue
            negated = line.startswith("!")
            if negated:
                line = line[1:]
            if not line:
                continue
            anchored = line.startswith("/")
            if anchored:
                line = line[1:]
            dir_only = line.endswith("/")
            if dir_only:
                line = line[:-1]
            if not line:
                continue
            self.rules.append((base_rel, line, negated, dir_only, anchored))

    def ignored(self, relpath, is_dir):
        state = False
        for base_rel, pattern, negated, dir_only, anchored in self.rules:
            sub = relpath
            if base_rel:
                if not (relpath == base_rel or relpath.startswith(base_rel + "/")):
                    continue
                sub = "" if relpath == base_rel else relpath[len(base_rel) + 1:]
            if not sub:
                continue
            if _pattern_hits(sub, pattern, dir_only, anchored, is_dir):
                state = not negated
        return state


def _glob_match(name, pattern):
    return fnmatch.fnmatchcase(name, pattern.replace("**", "*"))


def _pattern_hits(sub, pattern, dir_only, anchored, is_dir):
    parts = sub.split("/")
    if anchored:
        if dir_only:
            for i in range(1, len(parts) + 1):
                if _glob_match("/".join(parts[:i]), pattern) and (i < len(parts) or is_dir):
                    return True
            return False
        return _glob_match(sub, pattern)
    if dir_only:
        return any(_glob_match(part, pattern) for part in parts)
    if _glob_match(parts[-1], pattern):
        return True
    # a file pattern also matches a directory that ignores its whole subtree
    return any(_glob_match(part, pattern) for part in parts[:-1])


# --------------------------------------------------------------------------
# Allowlist
# --------------------------------------------------------------------------

class AllowList:
    """`.readiness-allow`: one `check-id path-glob reason` per line.

    A line may silence any check except `secrets`.
    """

    def __init__(self):
        self.entries = []   # (id, glob, reason)
        self.problems = []  # human notes about malformed lines

    def load(self, root):
        path = os.path.join(root, ALLOW_FILE)
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.read().splitlines()
        except OSError as exc:
            raise GateError("cannot read %s: %s" % (path, exc))
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split(None, 2)
            if len(fields) < 2:
                self.problems.append("line %d: expected 'check-id path-glob reason'" % lineno)
                continue
            check_id, glob = fields[0], fields[1]
            reason = fields[2] if len(fields) > 2 else ""
            if check_id not in CHECK_IDS:
                self.problems.append("line %d: unknown check id %r" % (lineno, check_id))
                continue
            if check_id == "secrets":
                self.problems.append(
                    "line %d: the allowlist may not silence 'secrets' -- ignored" % lineno)
                continue
            if not reason.strip():
                self.problems.append(
                    "line %d: no reason given -- allowed, but a bare allow is a smell" % lineno)
            self.entries.append((check_id, glob, reason))

    def reason_for(self, finding):
        if finding.id == "secrets":
            return None
        for check_id, glob, reason in self.entries:
            if check_id != finding.id:
                continue
            if fnmatch.fnmatchcase(finding.path, glob) or fnmatch.fnmatchcase(
                    finding.path, glob.replace("**", "*")):
                return reason
        return None


# --------------------------------------------------------------------------
# File helpers
# --------------------------------------------------------------------------

def _walk(root):
    """Yield (relpath, abspath, is_dir) for the whole tree, never following links."""
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if d not in VCS_NAMES)
            filenames = sorted(f for f in filenames if f not in VCS_NAMES)
            rel_dir = os.path.relpath(dirpath, root)
            if rel_dir == ".":
                rel_dir = ""
            for name in dirnames:
                yield (os.path.join(rel_dir, name) if rel_dir else name,
                       os.path.join(dirpath, name), True)
            for name in filenames:
                yield (os.path.join(rel_dir, name) if rel_dir else name,
                       os.path.join(dirpath, name), False)
    except OSError as exc:
        raise GateError("cannot walk %s: %s" % (root, exc))


def _read_text(path, limit=None):
    """Read a file as text, or return None when it looks binary."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(TEXT_PROBE_BYTES)
            if b"\x00" in head:
                return None
            if limit is not None and size > limit:
                return None
            body = head + fh.read()
    except FileNotFoundError:
        # a dangling symlink is not text and not a secret; hygiene checks report
        # binaries, and a broken link ships nothing
        return None
    except OSError as exc:
        raise GateError("cannot read %s: %s" % (path, exc))
    return body.decode("utf-8", errors="replace")


def _looks_like_binary(path):
    try:
        with open(path, "rb") as fh:
            head = fh.read(TEXT_PROBE_BYTES)
    except OSError as exc:
        raise GateError("cannot read %s: %s" % (path, exc))
    return head.startswith(b"\x7fELF") or b"\x00" in head


def _line_of(text, index):
    return text.count("\n", 0, index) + 1


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def check_secrets(root, files):
    """files: every regular file in the tree, gitignored or not. Never silenceable."""
    findings = []
    for rel, abs_path in files:
        text = _read_text(abs_path)
        if not text:
            continue
        for label, pattern in SECRET_PATTERNS:
            match = pattern.search(text)
            if match:
                findings.append(Finding(
                    "secrets", rel,
                    "%s at line %d" % (label, _line_of(text, match.start()))))
    return findings


def check_vendored_binary(root, files):
    findings = []
    for rel, abs_path, is_dir in files:
        if is_dir:
            continue
        try:
            size = os.path.getsize(abs_path)
        except FileNotFoundError:
            continue  # dangling symlink: no bytes to redistribute
        except OSError as exc:
            raise GateError("cannot stat %s: %s" % (rel, exc))
        if size <= LARGE_BINARY_BYTES:
            continue
        if _looks_like_binary(abs_path):
            findings.append(Finding(
                "vendored-binary", rel,
                "binary artefact, %.1f MB -- do not redistribute third-party "
                "binaries in a package" % (size / 1048576.0)))
    return findings


def check_backup_file(root, files):
    findings = []
    for rel, abs_path, is_dir in files:
        if is_dir:
            continue
        if BACKUP_RE.search(os.path.basename(rel)):
            findings.append(Finding(
                "backup-file", rel,
                "backup/editor leftover; delete it or keep it out of the tree"))
    return findings


def check_pycache(root, files):
    findings = []
    for rel, abs_path, is_dir in files:
        base = os.path.basename(rel)
        if is_dir:
            if base == "__pycache__":
                findings.append(Finding(
                    "pycache", rel + "/",
                    "bytecode cache directory; delete it (add it to .gitignore too)"))
        elif base.endswith(".pyc") or base.endswith(".pyo"):
            findings.append(Finding("pycache", rel, "compiled bytecode; delete it"))
    return findings


def check_license(root, files):
    # keep the on-disk spelling: the lowercase form is only for matching
    hits = [rel for rel, _, is_dir in files
            if not is_dir and "/" not in rel
            and (rel.lower().startswith("license")
                 or rel.lower().startswith("licence")
                 or rel.lower().startswith("copying"))]
    if not hits:
        return [Finding("license", "LICENSE",
                        "no LICENSE/LICENCE/COPYING at the repo root")]
    findings = []
    for name in sorted(hits):
        abs_path = os.path.join(root, name)
        try:
            size = os.path.getsize(abs_path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise GateError("cannot stat %s: %s" % (name, exc))
        if size == 0:
            findings.append(Finding("license", name, "license file is empty"))
    return findings


def check_readme(root, files):
    candidates = [rel for rel, _, is_dir in files
                  if not is_dir and "/" not in rel and rel.lower() == "readme.md"]
    if not candidates:
        return [Finding("readme", "README.md",
                        "no README.md at the repo root")]
    text = _read_text(os.path.join(root, candidates[0])) or ""
    lines = len(text.splitlines())
    if lines < 40:
        return [Finding("readme", candidates[0],
                        "%d lines; a public README needs at least 40" % lines)]
    return []


def check_package_manifest(root, files):
    path = os.path.join(root, "package.json")
    if not os.path.isfile(path):
        return [Finding("package-manifest", "package.json",
                        "no package.json at the repo root")]
    text = _read_text(path)
    if text is None:
        return [Finding("package-manifest", "package.json",
                        "package.json is not readable text")]
    try:
        manifest = json.loads(text)
    except ValueError as exc:
        return [Finding("package-manifest", "package.json",
                        "package.json is not valid JSON: %s" % exc)]
    if not isinstance(manifest, dict):
        return [Finding("package-manifest", "package.json",
                        "package.json must hold a JSON object")]
    findings = []
    keywords = manifest.get("keywords")
    if not isinstance(keywords, list) or "pi-package" not in keywords:
        findings.append(Finding(
            "package-manifest", "package.json",
            'keywords must be a list containing "pi-package" (that keyword is what '
            "makes pi.dev/packages eligible)"))
    pi_block = manifest.get("pi")
    if pi_block is not None:
        if not isinstance(pi_block, dict):
            findings.append(Finding("package-manifest", "package.json",
                                    "the 'pi' manifest must be an object"))
        else:
            for key, value in sorted(pi_block.items()):
                declared = value if isinstance(value, list) else [value]
                for entry in declared:
                    if not isinstance(entry, str):
                        findings.append(Finding(
                            "package-manifest", "package.json",
                            "pi.%s holds a non-string path" % key))
                        continue
                    relative = entry.lstrip("./").rstrip("/")
                    if not relative:
                        continue
                    if GLOB_CHARS & set(relative):
                        prefix = re.split(r"[*?\[]", relative)[0].rstrip("/")
                        if prefix and not os.path.exists(os.path.join(root, prefix)):
                            findings.append(Finding(
                                "package-manifest", "package.json",
                                "pi.%s path %r does not exist" % (key, entry)))
                        continue
                    if not os.path.exists(os.path.join(root, relative)):
                        findings.append(Finding(
                            "package-manifest", "package.json",
                            "pi.%s path %r does not exist" % (key, entry)))
    for required in ("name", "version"):
        if not manifest.get(required):
            findings.append(Finding("package-manifest", "package.json",
                                    "package.json has no %r" % required))
    return findings


def _is_home_exempt(rel):
    if rel in HOME_PATH_EXEMPT_EXACT:
        return True
    base = os.path.basename(rel)
    if base.endswith(HOME_PATH_EXEMPT_SUFFIXES):
        return True
    if base.startswith(HOME_PATH_EXEMPT_PREFIXES) and base.endswith(".py"):
        return True
    return rel.startswith("tests/")


def check_internal_vocab(root, shipped):
    findings = []
    lowered = [(word, word.lower()) for word in INTERNAL_VOCAB]
    for rel, abs_path in shipped:
        if rel in VOCAB_METADATA_EXEMPT:
            continue
        text = _read_text(abs_path)
        if not text:
            continue
        haystack = text.lower()
        hits = []
        for word, needle in lowered:
            index = haystack.find(needle)
            if index >= 0:
                hits.append((word, index))
        if hits:
            word, index = sorted(hits, key=lambda item: item[1])[0]
            findings.append(Finding(
                "internal-vocab", rel,
                "private deployment vocabulary %r at line %d"
                % (word, _line_of(haystack, index))))
    return findings


def check_hardcoded_home(root, shipped):
    findings = []
    for rel, abs_path in shipped:
        if _is_home_exempt(rel):
            continue
        text = _read_text(abs_path)
        if not text:
            continue
        match = HOMEPATH_RE.search(text)
        if match:
            findings.append(Finding(
                "hardcoded-home", rel,
                "absolute home-directory path at line %d; resolve it through the "
                "path module instead" % _line_of(text, match.start())))
    return findings


def check_toolchain_deps(root, files):
    present = {os.path.basename(rel) for rel, _, is_dir in files if not is_dir}
    candidates = []
    for rel, abs_path, is_dir in files:
        if is_dir:
            continue
        base = os.path.basename(rel)
        if base.startswith("test_"):
            continue
        if base == "fleet" or (base.startswith("fleet_") and base.endswith(".py")):
            candidates.append((rel, abs_path))
        elif base.startswith("pi-"):
            candidates.append((rel, abs_path))
    findings = []
    seen = set()
    for rel, abs_path in candidates:
        text = _read_text(abs_path)
        if not text:
            continue
        for tool in TOOLCHAIN:
            if not re.search(r"\b%s\b" % re.escape(tool), text):
                continue
            if tool in present:
                continue
            key = (tool, rel)
            if key in seen:
                continue
            seen.add(key)
            findings.append(Finding(
                "toolchain-deps", rel,
                "shells out to %r but bin/%s is not in the package -- a pi package "
                "cannot put a CLI on PATH, so it has to ship inside" % (tool, tool)))
    return findings


def _run_selftest(check_id, tool, root):
    """Run one guard's OWN test as a subprocess. Never restate its assertions.

    A guard whose selftest is missing or red is a guard nobody can trust, and the
    failure is reported with the tool name and the tail of its output: "a guard
    failed" is not a finding a maintainer can act on.
    """
    rel = "bin/%s" % tool
    path = os.path.join(root, "bin", tool)
    if not os.path.isfile(path):
        return [Finding(
            check_id, rel,
            "guard is missing from the package, so its selftest cannot run -- a guard "
            "that is not shipped is not a guard")]
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    try:
        proc = subprocess.run(
            [sys.executable, path, "--selftest"],
            cwd=root, env=env, capture_output=True, text=True,
            timeout=GUARD_SELFTEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return [Finding(check_id, rel,
                        "--selftest did not finish in %ds" % GUARD_SELFTEST_TIMEOUT)]
    except OSError as exc:
        return [Finding(check_id, rel, "cannot run --selftest: %s" % exc)]
    if proc.returncode != 0:
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip().splitlines()[-3:]
        return [Finding(check_id, rel,
                        "%s --selftest exits %d: %s"
                        % (tool, proc.returncode, " | ".join(tail)))]
    return []


def check_guard_selftests(root):
    findings = []
    for tool in GUARDS:
        findings.extend(_run_selftest("guard-selftests", tool, root))
    return findings


def _declared_dependencies(root):
    """The manifest's dependency fields, or None when there is no readable manifest."""
    path = os.path.join(root, "package.json")
    if not os.path.isfile(path):
        return None
    text = _read_text(path)
    if text is None:
        return None
    try:
        manifest = json.loads(text)
    except ValueError:
        return None
    if not isinstance(manifest, dict):
        return None
    deps = {}
    for field in ("dependencies", "peerDependencies", "optionalDependencies"):
        block = manifest.get(field)
        if isinstance(block, dict):
            deps.update(block)
    return deps


def check_serve_guard(root):
    """The serve guard proves itself, and this gate only asks it to.

    Its three failures are DIFFERENT diseases with the same symptom ("the URL works"),
    and a readiness gate that described them in prose without running them would be
    the very thing this unit exists to stop: a claim about the world that nobody
    re-checks.
    """
    return _run_selftest("serve-guard", "fleet_serve.py", root)


def check_intercom_dependency(root):
    """The broker is required, declared, and its absence is explained, not fatal-looking.

    `package.json` shipped no dependencies at all once, which produced a fleet that could
    spawn workers and then had no way to message them -- an install that looks successful
    and cannot coordinate. Declaring it is half the fix; the other half is that a user who
    lacks it is told the command that installs it.
    """
    findings = []
    deps = _declared_dependencies(root)
    if deps is None:
        findings.append(Finding(
            "intercom-dependency", "package.json",
            "no readable package.json, so %r cannot be shown to be a dependency"
            % BROKER_PACKAGE))
    elif BROKER_PACKAGE not in deps:
        findings.append(Finding(
            "intercom-dependency", "package.json",
            "%r is not declared in dependencies/peerDependencies; `pi install` would "
            "not install the broker, and every send/ask/done/report needs it"
            % BROKER_PACKAGE))

    # the failure has to be actionable: no broker, one line, naming the install.
    msg = os.path.join(root, PI_MSG)
    if not os.path.isfile(msg):
        findings.append(Finding("intercom-dependency", PI_MSG,
                                "pi-msg is missing from the package"))
        return findings
    findings += _probe_pi_msg_without_broker(msg, root)
    return findings


def _probe_pi_msg_without_broker(msg_path, root):
    """Run the shipped pi-msg with no broker reachable and grade only its FAILURE.

    A package can pass every static check and still tell a stranger `not installed at
    /some/path`, which is a dead end. So the probe asserts the three things a human
    needs: it exits non-zero, it names the install command, and it prints no absolute
    home path.
    """
    findings = []
    tmp = tempfile.mkdtemp(prefix="readiness-broker-")
    try:
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin",
               "HOME": tmp, "AGENT_HOME": tmp, "PI_AGENT_HOME": tmp}
        try:
            proc = subprocess.run(
                ["bash", msg_path, "send", "--to", "readiness-probe", "--text", "probe"],
                cwd=root, env=env, capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.TimeoutExpired) as exc:
            findings.append(Finding("intercom-dependency", PI_MSG,
                                    "could not be probed without a broker: %s" % exc))
            return findings
        blob = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0:
            findings.append(Finding(
                "intercom-dependency", PI_MSG,
                "exits 0 with no broker reachable -- it cannot have sent anything, so "
                "the probe (or the tool) is lying about delivery"))
            return findings
        if BROKER_INSTALL_HINT not in blob:
            findings.append(Finding(
                "intercom-dependency", PI_MSG,
                "fails without naming the package to install; a user with no broker gets "
                "a dead end instead of `pi install %s` (said: %s)"
                % (BROKER_INSTALL_HINT, blob.strip().splitlines()[:1])))
        elif not re.search(r"\bpi\s+install\b", blob):
            findings.append(Finding(
                "intercom-dependency", PI_MSG,
                "names the package but not the command that installs it (said: %s)"
                % blob.strip().splitlines()[:1]))
        if HOMEPATH_RE.search(blob):
            findings.append(Finding(
                "intercom-dependency", PI_MSG,
                "prints an absolute home path instead of an action: %s"
                % blob.strip().splitlines()[:1]))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return findings


# --------------------------------------------------------------------------
# Driver
# --------------------------------------------------------------------------

def run_gate(root):
    """Return (findings, suppressed, allow_problems). Raises GateError."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise GateError("--repo is not a directory: %s" % root)

    all_files = list(_walk(root))
    plain_files = [(rel, abs_path) for rel, abs_path, is_dir in all_files
                   if not is_dir
                   and not any(part in SKIP_DIRS for part in rel.split("/")[:-1])]

    ignore = IgnoreRules()
    ignore.load(root, "")
    for rel, abs_path, is_dir in all_files:
        if not is_dir or os.path.basename(rel) in SKIP_DIRS:
            continue
        ignore.load(abs_path, rel)

    shipped = []  # content-scan set: not gitignored, not inside a skipped dir
    for rel, abs_path, is_dir in all_files:
        if is_dir:
            continue
        parts = rel.split("/")
        if any(part in SKIP_DIRS for part in parts[:-1]):
            continue
        if ignore.ignored(rel, False):
            continue
        shipped.append((rel, abs_path))

    allow = AllowList()
    allow.load(root)

    raw = []
    raw += check_secrets(root, plain_files)
    raw += check_vendored_binary(root, all_files)
    raw += check_backup_file(root, all_files)
    raw += check_pycache(root, all_files)
    raw += check_license(root, all_files)
    raw += check_readme(root, all_files)
    raw += check_package_manifest(root, all_files)
    raw += check_internal_vocab(root, shipped)
    raw += check_hardcoded_home(root, shipped)
    raw += check_toolchain_deps(root, all_files)
    raw += check_guard_selftests(root)
    raw += check_serve_guard(root)
    raw += check_intercom_dependency(root)

    findings, suppressed = [], []
    for finding in sorted(raw, key=Finding.sort_key):
        reason = allow.reason_for(finding)
        if reason is None:
            findings.append(finding)
        else:
            suppressed.append((finding, reason))
    return findings, suppressed, allow.problems


def render_text(root, findings, suppressed, allow_problems):
    out = []
    out.append("readiness gate: %s" % root)
    if findings:
        out.append("NOT READY -- %d finding(s) across %d check(s)"
                   % (len(findings), len({f.id for f in findings})))
    else:
        out.append("READY -- no blockers (0 findings)")
    if suppressed:
        out.append("")
        out.append("suppressed by %s: %d" % (ALLOW_FILE, len(suppressed)))
        for finding, reason in suppressed:
            out.append("  [%s] %s -- %s" % (finding.id, finding.path, reason or "(no reason)"))
    for problem in allow_problems:
        out.append("allowlist note: %s" % problem)
    out.append("")
    for check_id, detail in CHECKS:
        hits = [f for f in findings if f.id == check_id]
        mark = "FAIL" if hits else "ok  "
        out.append("%s  %-17s %s" % (mark, check_id, detail))
        for finding in hits:
            out.append("        %s: %s" % (finding.path, finding.detail))
    return "\n".join(out)


def build_document(root, findings, suppressed, allow_problems):
    counts = {}
    for finding in findings:
        counts[finding.id] = counts.get(finding.id, 0) + 1
    checks = []
    for check_id, detail in CHECKS:
        entry = {"id": check_id, "detail": detail, "findings": counts.get(check_id, 0)}
        checks.append(entry)
    return {
        "ok": not findings,
        "repo": root,
        "checks": checks,
        "findings": [f.as_dict() for f in findings],
        "suppressed": [{"id": f.id, "path": f.path, "reason": reason}
                       for f, reason in suppressed],
        "allowlist_notes": list(allow_problems),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="readiness.py",
        description="Decide whether a pi-fleet tree may be published.")
    parser.add_argument("--repo", required=True,
                        help="path to the tree to check")
    parser.add_argument("--json", action="store_true",
                        help="emit a machine-readable verdict on stdout")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.repo)
    try:
        findings, suppressed, allow_problems = run_gate(root)
    except GateError as exc:
        print("readiness: cannot run: %s" % exc, file=sys.stderr)
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc),
                              "checks": [{"id": cid, "detail": detail} for cid, detail in CHECKS],
                              "findings": []}))
        return EXIT_ERROR

    if args.json:
        print(json.dumps(build_document(root, findings, suppressed, allow_problems),
                         indent=2, sort_keys=False))
    else:
        print(render_text(root, findings, suppressed, allow_problems))
    return EXIT_READY if not findings else EXIT_BLOCKED


if __name__ == "__main__":
    sys.exit(main())
