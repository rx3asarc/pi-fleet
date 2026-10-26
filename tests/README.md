# tests/ — the readiness gate

One command decides whether this tree may be published:

```bash
python3 tests/readiness.py --repo .          # human-readable verdict
python3 tests/readiness.py --repo . --json   # machine-readable verdict
```

Exit codes: `0` ready · `1` at least one blocker · `2` the gate itself could not
run (bad path, unreadable file). A gate that cannot run must never look green.

`python3 tests/selftest_readiness.py` proves the gate can fail: it plants one
violation per check in a throwaway tree and demands the right check id back.

## The twelve checks

| id | fails when |
|---|---|
| `secrets` | a file matches a credential shape (GitHub, GitLab, Shopify, Slack, Google, `sk-`/`sk-or-v1`, private-key blocks, JWTs, Telegram bot tokens) |
| `vendored-binary` | a binary/ELF artefact larger than 1 MB |
| `backup-file` | `*.bak`, `*.bak*`, `*.orig`, `*~` |
| `pycache` | a `__pycache__/` directory or `*.pyc` |
| `license` | no non-empty `LICENSE`/`LICENCE`/`COPYING` at the root |
| `readme` | no `README.md` at the root, or fewer than 40 lines |
| `package-manifest` | `package.json` invalid, `keywords` lacks `pi-package`, a declared `pi.*` path missing |
| `internal-vocab` | private deployment vocabulary in a shipped file |
| `hardcoded-home` | an absolute home-directory path outside `bin/fleet_paths.py` and outside tests |
| `toolchain-deps` | `fleet` shells out to `pi-msg`/`pi-tmux`/`pi-live`/`pi-tree` and the CLI is not in `bin/` |
| `guard-selftests` | any of `fleet_pid_assert.py`, `fleet_tmux_orphan_guard.py`, `fleet_name_lint.py` is missing, or its own `--selftest` exits non-zero (the finding carries the tool name and the tail of its output) |
| `intercom-dependency` | `package.json` does not declare `pi-intercom`, or the shipped `bin/pi-msg` exits 0 with no broker reachable / fails without naming `pi install npm:pi-intercom` / prints an absolute home path |

Findings name the check id and the path. A credential *value* is never printed —
only the pattern name and the line number.

Two of these run **code**, not `grep`: `guard-selftests` executes each guard's own
`--selftest` as a subprocess, and `intercom-dependency` runs `bin/pi-msg` with an
empty agent home and no broker on `PATH`. The alternative — copying a guard's
assertions into this file, or grepping for the right words in the failure
message — is a second source of truth, and a second source of truth is how a
guard keeps passing while its subject is broken.

## Two deliberate decisions

**Hygiene beats `.gitignore`.** Content scans (secrets, vocabulary, home paths)
honour `.gitignore`, so generated trees and local state are not read as shipped
text. Hygiene checks (`backup-file`, `pycache`, `vendored-binary`) inspect the
raw tree and ignore `.gitignore` entirely: "it is gitignored" is not "it cannot
ship". A second packager, a `tar` step, or a plain `scp` still sees the bytes.

**The allowlist has a floor.** `.readiness-allow` at the repo root takes one
`check-id path-glob reason` per line. It can silence any check except `secrets`:
a credential that reached a file cannot be un-leaked by a config line. Suppressed
findings are still printed, with their reason, under `suppressed` in the JSON.
