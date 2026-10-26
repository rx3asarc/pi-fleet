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
  └── fleet_*.py              audit, bundle, policy, restore, resume_dark, pid_assert,
                              tmux_orphan_guard, ground_truth, name_drift
```

The split that matters: **`fleet_discovery` is the only module allowed to ask the world
what exists** (git, tmux, the registry). Everything downstream of it takes entries as
arguments, which is why the gate and the lineage decision can be tested without a repo, a
tmux server, or a clock. A second enumerator of "what exists" is the bug this layout
exists to prevent.

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

**Honest status at the packaging commit:** the seam (`bin/fleet_paths.py`) is the config
unit's deliverable and had not landed yet, so the paths above are still literal `/root/…`
strings inside the modules. Documented here because it is the contract; if
`bin/fleet_paths.py` is absent in your checkout, assume the literals are hardcoded and set
nothing — the defaults are this box's.

## Tests

Stdlib `unittest`, 33 files under `bin/test_*.py`:

```bash
python3 -m unittest discover -s bin -p 'test_*.py'
```

Two of them (`test_fleet_entries.py`, `test_fleet_gate.py`) compare against frozen fixture
files that are deliberately not published, because they contain this project's own session
state. They are red in a clean clone for that reason, and `docs/PUBLISHING.md` records the
measured suite state rather than promising a green one.
