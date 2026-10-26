# The config seam — where fleet keeps things, and how to move them

Written by the config unit (U3). Everything below was measured on the branch
`fleet/meta-config-1`, not inferred from reading code.

The program used to answer "where does the store live?" by writing the answer into
34 different files. That means it only worked on the machine it was written on, and
the one default that mattered most pointed at a private project:

```
fleet_discovery.REPO  =  /mnt/<data-volume>/projects/<private-repo>   # shipped as a default
```

A stranger who cloned this package got a fleet that silently listed somebody else's
repository and said nothing about it. That is the single biggest reason the tool was
not fit to be public, and it is fixed below.

## One module decides

`bin/fleet_paths.py` is now the only place that resolves a location. Every other
shipped module asks it.

| location | environment variable | default | what lives there |
|---|---|---|---|
| control dir | `FLEET_CONTROL_DIR` | `~/.fleet` | `lineage.json`, `registry.json`, `reap-ledger.jsonl`, `verify-cache/`, `backups/`, bundles |
| work root | `FLEET_WORK_ROOT` | `~/fleet` | spawn worktrees and task-kind scratch dirs |
| agent home | `PI_AGENT_HOME` | `~/.pi/agent` | `sessions/`, `session-names/`, `trust.json`, the pi-intercom CLI |
| handoff dir | `FLEET_HANDOFF_DIR` | `~/handoffs` | reaped digests (`_reaped/`), reviewer briefs |
| reports dir | `FLEET_REPORTS_DIR` | `~/docs/reports` | `fleet rollup` output |
| bash/cli dir | `FLEET_BIN_DIR` | the directory `fleet_paths.py` lives in (`__file__`, resolved) | `fleet`, `pi-tmux`, `pi-msg`, … |
| repo target | `FLEET_REPO` | **none** — see below | the repository a spawn or a reap acts on |

`~` is resolved from `$HOME`, so the defaults follow the user. On the box this grew
up on that is the root user's home, which is why nothing changed there: the legacy
paths are still `$HOME/.fleet`, `$HOME/fleet`, `$HOME/.pi/agent`, `$HOME/handoffs`,
and a test pins them.

Ask it what it resolved, and where each answer came from:

```
$ python3 bin/fleet_paths.py
home         <the root user's HOME>                   [env HOME]
control_dir  $HOME/.fleet                              [default]
work_root    $HOME/fleet                               [default]
agent_home   $HOME/.pi/agent                           [default]
handoff_dir  $HOME/handoffs                            [default]
reports_dir  $HOME/docs/reports                        [default]
bin_dir      <this package>/bin                        [default]
repo         <the repo you are standing in>            [default]
```

An empty environment variable counts as unset (`FLEET_CONTROL_DIR=""` does not
resolve the store to the current directory — that is the cron-line accident this
guards against).

## The repo target: no default, and it says so

`fleet_paths.repo()` resolves in this order, and `None` is a real answer:

1. `$FLEET_REPO`
2. the git repository containing the current directory (a worktree's `.git` **file**
   counts, not just a directory)
3. `<work root>/repo`, if that is a git repository
4. otherwise `None`

Callers that cannot mean anything without a target refuse and name the variable:

```
$ cd /tmp && python3 bin/fleet spawn meta-example-1 --plan plan.md --unit 1
  error: no repo target: set FLEET_REPO=/path/to/repo, run the command inside the
         repository, or pass --repo
```

`fleet list` still works with no target — session discovery does not need a repo —
but it says once, on stderr, that git worktree discovery is off and what to set.
Silence was the old behaviour and silence was the bug: "no fleets" and "wrong
directory" looked identical.

## Shell scripts cannot import python

`pi-msg`, `pi-tmux`, `fleet-sentinel-cron.sh`, `fleet-pid-assert-cron.sh` and
`fleet-tmux-orphan-cron.sh` spell the same rule from `$HOME` and from
`${BASH_SOURCE[0]}`, because a shell script cannot import a module:

```bash
AGENT_HOME="${PI_AGENT_HOME:-${HOME}/.pi/agent}"
FLEET_BIN="${FLEET_BIN_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
```

A second copy of a rule is a copy that drifts, so `test_fleet_paths.py` runs both
spellings and compares them. Drift becomes a failing test instead of a mystery.

The node CLIs (`pi-live`, `pi-peers`) honour `PI_AGENT_HOME` the same way, and
`pi-live` no longer falls back to a hardcoded home directory.

## Verification

```
$ python3 bin/fleet_paths.py              # what it resolved, and from where
$ python3 bin/test_fleet_paths.py         # 18 tests, no box state required
$ python3 -m unittest test_fleet_gate     # skips with a reason if fixtures/ is absent
$ python3 <the lead's checks dir>/check_u3.py         # the lead's frozen done-test
```

`test_fleet_paths.py` proves four things that matter:

1. **The defaults are the legacy paths.** A seam is a refactor; a refactor that
   quietly moves the store makes every existing box look empty.
2. **Every value is overridable**, including the empty-string case.
3. **A fresh temp control dir really works**: `FLEET_CONTROL_DIR=$tmp
   FLEET_WORK_ROOT=$tmp/w python3 bin/fleet list --json` exits 0 and emits a JSON
   list; and `save_lineage` writes its record into `$tmp`, provably *not* into the
   real store (asserted by content marker, so a busy live fleet cannot make the test
   flake).
4. **No shipped file spells an absolute home path**, and no shipped file contains a
   private host path. The rule the seam exists to enforce is checked, not assumed.

## Test suite: before and after, in this same worktree

Measured by checking the base commit `f77873e` out into `bin/`, running the whole
suite, and then restoring the branch. Same path, same box, same Python — so the
difference is the change, not the location.

| | files | tests | skipped | files failing |
|---|---:|---:|---:|---:|
| base commit `f77873e` | 33 | 694 | 0 | 3 |
| this branch | 34 | 712 | 19 | 1 |

The three failing files at the base commit, and what happened to each:

**`test_fleet_gate.py` — 3 errors, pre-existing, now skips with a reason.**
Cause: it reads its baseline fixture under `bin/fixtures/`, and `fixtures/`
is excluded from this repo on purpose (U2's scope: it is a private project's 7
entries — its session names, branches and refusal texts). The guard is real and it
should stay in the tree; what it needs is the data. It now skips **per test** with
the reason, the number of unarmed guards is visible in the run summary
(`skipped=11`), and `FLEET_GATE_FIXTURES=/path/to/fixtures` re-arms it for whoever
has the data. Skipped, not passed.

**`test_fleet_entries.py` — 6 errors, pre-existing, same cause, same fix**
(`skipped=8`).

**`test_fleet_scrub.py` — 1 failure, pre-existing, NOT fixed here, and it is a real
finding.** `test_the_real_digest_corpus_yields_no_false_positives` scrubs this box's
live handoff corpus. Two matches today, both in
`$HOME/handoffs/2026-10-20-<project>-profile-subject.md`, both the same string: a
`JWT_TOKEN=<value>` assignment inside a shell command recorded in that digest. The
redactor's dotenv-assignment rule fires on it, which is the rule doing its job — the
test's "zero false positives" pin is older than the digest. Deciding whether a
`JWT_TOKEN=` placeholder should be redacted is a change to the redactor, not to the
seam, so it is reported rather than touched. The test now resolves the corpus
through `fleet_paths.handoff_dir()` and skips only when the corpus directory does not
exist at all (a fresh install); a corpus that exists but is empty is still a finding.

Note on suite side effects, measured with `strace -f` over the whole suite: no test
opens the real `lineage.json` or `registry.json` for writing. The one live-store
touch is the advisory lock file `$HOME/.fleet/.lineage.lock`, which tests that never
set `FLEET_CONTROL_DIR` open in order to take the store lock. Harmless, but it is the
reason the seam exists: with `FLEET_CONTROL_DIR` set, even that lands in the temp
dir.

## What did not change

Behaviour on this box with no environment set, which is the whole point of a seam
rather than a rewrite: same store locations, same defaults, same verbs. The only
intentional difference is the repo target, described above.
