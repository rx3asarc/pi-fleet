# Architecture

Ten minutes of reading for a new maintainer. Every claim below was taken from the module
docstrings in `bin/` and from running the CLI; where something is still hardcoded on this
box, it says so.

## Shape

One CLI, one pure decision core, one impure world-reader, and a deterministic assembler.

```
bin/fleet                     argparse front end: 26 subcommands, dispatch only
  ├── fleet_door.py           "fleet for <task>" — the one-sentence entry point
  ├── fleet_brief.py          renders the per-worker brief from a plan slice (a gated artifact)
  ├── fleet_ack.py            plan acknowledgement: the gate on the interface itself
  ├── fleet_discovery.py      impure: git worktrees, tmux, registry, index join → ENTRIES
  ├── fleet_entries.py        pure: the entry model (only import is os.path)
  ├── fleet_lineage.py        pure: heartbeat, durable digest, cascade-reap decision
  ├── fleet_children.py       lineage records + the reap proposal
  ├── fleet_gate.py           pure: the teardown gate, split into providers
  ├── fleet_done_test.py      frozen done-tests: verification vs self-certification
  ├── fleet_rollup.py         deterministic evidence assembly, claim conflicts, prose quarantine
  ├── fleet_reader.py         the independent adversarial reader brief + verdict
  ├── fleet_scrub.py          secret redaction over transcripts and logs
  ├── fleet_util.py           sh / die / clip — the three primitives everyone imports
  └── fleet_*.py              audit, bundle, policy, restore, resume_dark, ground_truth,
                              name_drift
```

The split that matters: **`fleet_discovery` is the only module allowed to ask the world
what exists** (git, tmux, the registry). Everything downstream of it takes entries as
arguments, which is why the gate and the lineage decision can be tested without a repo, a
tmux server, or a clock. A second enumerator of "what exists" is the bug this layout
exists to prevent.

## Guards, and why each one owns its own test

Three tools encode a single hard-won rule each. They are not advisory helpers: two of them
stand between a live process and a wrongly-reaped worker, and the third stands between a
fleet and a roster of names that mean nothing.

| guard | the rule it enforces |
|---|---|
| `bin/fleet_pid_assert.py` | a session's pid must still be the pid it claims; a pid that was reused by another process is not evidence that the worker is alive |
| `bin/fleet_tmux_orphan_guard.py` | a tmux server that is alive but unreachable is not a healthy server — do not kill through a socket nobody is listening on |
| `bin/fleet_name_lint.py` | a session name must be its subject: no category prefix (`meta-`, `tmp-`), and a domain prefix only where the domain is the subject |

Each ships a `--selftest` that exits non-zero on its own failure, and each has the same
contract: **exit 2 means it could not read its source**, which is not the same as clean. The
readiness gate (`tests/readiness.py`, check id `guard-selftests`) runs those selftests as
subprocesses rather than restating what they assert — a second copy of a guard's assertions
is how a guard starts passing while its subject is broken.

The name linter is report-only by design: it reports live violations and *counts* historical
ones, and never relabels the record. A linter that rewrites its own past cannot be trusted
to describe it.

## The evidence chain

1. `fleet_brief` writes the brief a worker will read; the brief is a gated artifact, not a
   paraphrase, because no worker ever reads the plan itself.
2. The done-test is **frozen** at spawn (`fleet_done_test`). A worker that supplies its own
   `--verify` command is recorded as self-certified, not verified — the failure this module
   was written to close.
3. `fleet done` records the claim; `fleet gate` decides whether teardown is allowed; the
   decision is pure, so it is testable, and it is the decision that deletes branches.
4. `fleet_rollup` assembles the claims deterministically, prints conflicts between typed
   claims, and quarantines worker prose. No model summarises the evidence: the lead wrote
   the plan, so it would summarise its own work.
5. `fleet_reader` supplies the check nobody's unit owns — a reader with different inputs,
   because a second reader of the same text is a confidence amplifier, not a check.

## External CLIs it shells out to

These ship in the same `bin/` and must be on `PATH` for the worker sessions:

| CLI | what `fleet` uses it for |
|---|---|
| `pi-msg` | the intercom broker: `send`, `ask` between sessions |
| `pi-tmux` | session lifecycle and ownership (`--live-owner`, `--close`) |
| `pi-live` | the live-session store; `pi_sources` reads it |
| `pi-tree` | the session index that entries and drift checks join against |

A package installed through `pi install` cannot register a CLI on the user's `PATH`, which
is why the skill in `skills/fleet/SKILL.md` invokes `bin/fleet` by path and tells the model
to put that `bin/` on `PATH` before spawning workers.

`pi-msg` is a thin client, not the broker itself: it drives the `pi-intercom` broker, which
is therefore a **declared dependency** (`package.json` → `dependencies`) rather than an
assumption about the host. It resolves the broker in a ladder — `pi-intercom` on `PATH`,
then `$PI_AGENT_HOME/npm/node_modules/pi-intercom/cli.mjs` (the npm bin; `cli.ts` is the
TypeScript sibling a release may not ship), then this package's own `node_modules`, then
`npx --no-install pi-intercom` — and when every rung fails it prints the one command that
fixes it, `pi install npm:pi-intercom`. Nothing in `send`/`ask`/`done`/`rollup` works
without that broker, which is why the gate's `intercom-dependency` check probes the failure
path and not just the manifest.

## State it keeps

Runtime state lives **outside** the package, in the control dir `FLEET_CONTROL_DIR`
(default `~/.fleet`). Nothing there is shipped and nothing there belongs in git:

| path | contents |
|---|---|
| `$FLEET_CONTROL_DIR/lineage.json` | every spawn record: parent, unit, frozen done-test, heartbeat, report, claims. Timestamped pre-write copies sit beside it (`lineage.json.pre-reconcile-*`) |
| `.../registry.json` | worktree/registry keys |
| `.../verify-cache/` | one JSON per verified done-test, keyed by digest |
| `.../backups/`, `.../snapshots/`, `.../bundles/` | restore points and exported bundles |
| `.../locks/`, `.../management/`, `.../logs/` | lock files, human decision records, logs |

Related locations, each with its own environment override:

| variable | default on the build box | meaning |
|---|---|---|
| `FLEET_CONTROL_DIR` | `~/.fleet` | state above |
| `FLEET_WORK_ROOT` | `~/fleet` | task scratch dirs and worker worktrees |
| `PI_AGENT_HOME` | `~/.pi/agent` | pi sessions, session names, settings |
| `FLEET_HANDOFF_DIR` | `~/handoffs` | handoff documents |
| `FLEET_BIN_DIR` | the directory the CLI lives in | siblings above are resolved from here |

**The seam is real.** `bin/fleet_paths.py` resolves every location, and each one has an
environment override; the table above is a description of its defaults, not a list of
literals scattered through the modules. If a module still holds an absolute path, that is a
bug in that module and the readiness gate's `hardcoded-home` check is expected to name it.

**What this architecture does not cover is written down in the README's "Honest limits"
section** ([../README.md](../README.md#honest-limits)): hand-started pi sessions are invisible
to the pid/orphan rules, worker-spawned processes are unsupervised until the box-doctor
`leaks` domain lands, the per-domain cap counts a recorded field rather than a prefix, and
the name is a label while the sessionId is the identity. Read that before trusting a green
test run to mean "nothing can go wrong".

## Tests

Stdlib `unittest`, 33 files under `bin/test_*.py`:

```bash
python3 -m unittest discover -s bin -p 'test_*.py'
```

Two of them (`test_fleet_entries.py`, `test_fleet_gate.py`) compare against frozen fixture
files that are deliberately not published, because they contain this project's own session
state. They are red in a clean clone for that reason, and `docs/PUBLISHING.md` records the
measured suite state rather than promising a green one.

What the suites here do **not** cover: the kill/liveness path is exercised by 15 node suites
that live outside this repo (`pi-live-tests`). That coverage is UNMEASURED from inside
`pi-fleet`, not green — see [../README.md](../README.md#honest-limits).

## fleet_serve.py — a served directory cannot masquerade

`fleet serve`'s cousin for anything a worker starts and a human then looks at. It records
the served directory's IDENTITY — `(st_dev, st_ino)` plus the resolved path — with the pid,
port and bind address, and `check` re-verifies every claim against `/proc/<pid>/cwd` and the
filesystem.

It exists because a path comparison is defeated by substitution:

```
mkdir /tmp/site                    # server starts here
rm -rf /tmp/site && mkdir /tmp/site   # same path, DIFFERENT directory
# "does the path exist?" says healthy. The server is answering from a deleted inode.
```

Measured on this box: four `python3 -m http.server` processes were alive with deleted cwds,
one answering HTTP 200 on :8792 with stale content. `ps` was happy, the port answered, and
the content no longer existed anywhere.

Three failures it can tell apart, because they have one symptom ("the URL works") and three
different causes: **deleted** (cwd unlinked), **replaced** (path exists, different
`dev:ino` — the masquerade above), and **foreign** (recorded pid dead, port still answering
— someone else owns the port). `--selftest` plants all three and is run by the readiness
gate under the id `serve-guard`; the gate never restates the assertions.

    bin/fleet_serve.py serve --dir /tmp/site --port 8792
    bin/fleet_serve.py check --json        # exit 1 if any record is lying
    bin/fleet_serve.py list | stop --all
