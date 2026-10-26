# Scaffold notes — what was copied out of `/root/.pi/agent/bin`, and what was not

Written by the scaffold unit (U2). The source directory is the LIVE tooling on the
build box and was treated as read-only: every file below was copied, never moved or
edited. Nothing in this repo was 'fixed' while copying — repairs belong to later units.

Source inventory: 87 regular files (`ls -p | grep -v /`), plus two directories: `fixtures/` and `__pycache__/`. The table covers **all 87 files**, plus the one file
brought in from outside that directory (`pi-tmux`) — every include/exclude decision is
auditable, not asserted.

## Source inventory and decisions

| file | bytes | decision | reason |
|---|---:|---|---|
| `fd` | 3544856 | **exclude** | vendored third-party ELF binary (3.4 MB, owner 1001:1001) — redistribution risk, not ours to publish |
| `fleet` | 59810 | **include** | the CLI itself — the thing being shipped |
| `fleet-pid-assert-cron.sh` | 385 | **include** | cron wrapper for the shipped fleet_pid_assert.py guard |
| `fleet-sentinel-cron.sh` | 8795 | **include** | cron wrapper for the sentinel tick — ships because the unit's ship list includes its test (test_sentinel_cron_lock.py) |
| `fleet-tmux-orphan-cron.sh` | 394 | **include** | cron wrapper for the shipped fleet_tmux_orphan_guard.py guard |
| `fleet_ack.py` | 4198 | **include** | `fleet ack` — parent acknowledges a worker's report |
| `fleet_audit.py` | 25875 | **include** | `fleet audit` — audits registry/lineage consistency |
| `fleet_brief.py` | 7150 | **include** | renders the per-unit brief slice and freezes the done-test from the plan |
| `fleet_bundle.py` | 18015 | **include** | `fleet bundle` — gathers a child's evidence into one bundle |
| `fleet_children.py` | 95354 | **include** | children: spawn/list/park/reap, the teardown gate and safe_close |
| `fleet_discovery.py` | 9332 | **include** | registry + git-fact discovery shared by `fleet list`/`spawn` |
| `fleet_done_test.py` | 9024 | **include** | done-test freeze/verify: the evidence gate's core |
| `fleet_door.py` | 8870 | **include** | the door — refuses a spawn whose unit has no failing-capable done-test |
| `fleet_entries.py` | 8396 | **include** | joins rows+git facts into the entry shape the CLIs print |
| `fleet_gate.py` | 8307 | **include** | teardown gate — refuses to kill a worker that is live or owned |
| `fleet_ground_truth.py` | 8510 | **include** | ground-truth probe used by the verified claims path |
| `fleet_lineage.py` | 18098 | **include** | per-session lineage store (identity, not worktree intent) |
| `fleet_name_drift.py` | 4579 | **include** | detects tmux/pi name drift before it causes a wrong kill |
| `fleet_pid_assert.py` | 24199 | **include** | `fleet pid-assert` guard: asserts a pid still belongs to a session |
| `fleet_policy.py` | 3882 | **include** | policy table: who may spawn/reap/report what |
| `fleet_reader.py` | 5191 | **include** | `fleet reader` — reads a child's transcript tail for a verdict |
| `fleet_restore.py` | 20208 | **include** | restore/rollback of fleet stores from a snapshot |
| `fleet_resume_dark.py` | 39193 | **include** | `fleet resume-dark` — revive a dead session as a named session |
| `fleet_rollup.py` | 8847 | **include** | `fleet rollup` — deterministic assembly of every unit's evidence |
| `fleet_scrub.py` | 7677 | **include** | transcript scrubbing (secret redaction) helpers |
| `fleet_tmux_orphan_guard.py` | 9146 | **include** | orphan tmux guard: detects panes with no owning session |
| `fleet_util.py` | 4400 | **include** | shared helpers (sh, die, store_lock) imported by the CLI |
| `mvp_check` | 2322 | **exclude** | box-local one-off MVP checker for this machine's pipelines; not part of the fleet contract |
| `pi-drill` | 1905 | **exclude** | box-local QA drill harness (runs the live tooling against this box); not shipped |
| `pi-live` | 15018 | **include** | live-session store; `pi_sources` shells out to `pi-live --json` |
| `pi-live-tests` | 7863 | **exclude** | box-local dev harness for pi-live (like pi-live-tests, it drives live state), not a test_*.py suite |
| `pi-live.bak-20261003` | 41594 | **exclude** | backup of an older pi-live — never shipped |
| `pi-live.bak-20261007-110514` | 44299 | **exclude** | backup of an older pi-live — never shipped |
| `pi-msg` | 5480 | **include** | intercom broker client; `fleet send`/`fleet ask` shell out to it |
| `pi-names-sync` | 8234 | **exclude** | box-local session-naming hygiene utility; `fleet` does not shell out to it |
| `pi-peers` | 3426 | **include** | peer listing CLI, documented alongside the fleet commands |
| `pi-pkg-search` | 2555 | **exclude** | unrelated helper (package search); no fleet dependency |
| `pi-rename` | 9706 | **exclude** | box-local session rename utility; `fleet` does not shell out to it |
| `pi-sentinel` | 364 | **exclude** | entry script of the separate sentinel sweep subsystem — no shipped module imports it (grep-verified) |
| `pi-tab-titles` | 26379 | **exclude** | box-local tmux tab-title cosmetic tool; `fleet` does not shell out to it |
| `pi-tree` | 3990 | **include** | index CLI; `fleet_pid_assert` shells out to `pi-tree --json` |
| `pi_identity.py` | 13765 | **include** | canonical session-name identity + repair planning |
| `pi_index.py` | 6556 | **include** | the join index that both `pi-tree` and `fleet` consume |
| `pi_rows.py` | 3842 | **include** | prompt/row rendering used by the tree view |
| `pi_sentinel.py` | 15282 | **exclude** | sentinel sweep implementation; no shipped module imports it (grep-verified) |
| `pi_sentinel_cli.py` | 27186 | **exclude** | sentinel CLI; shipped modules do not import it (grep-verified) |
| `pi_sources.py` | 9143 | **include** | the three live sources (panes, pi-live store, registry) |
| `pi_taxonomy.py` | 14591 | **include** | session classification vocabulary shared by pi-tree/pi_view |
| `pi_view.py` | 10574 | **include** | state/group/render for the tree view — REQUIRED BY `fleet` at import time; the unit's ship list omits it and `bin/fleet --help` fails without it (measured) |
| `probe-dead-parent.sh` | 7686 | **exclude** | one-off investigation probe (dead parent pids), not a product command |
| `probe-spawn-lineage.sh` | 4335 | **exclude** | one-off investigation probe (spawn lineage), not a product command |
| `respawn-pi-session` | 16770 | **exclude** | spawn helper hardcoded to this box's $HOME/.pi/agent/bin; excluded from the public snapshot — see Known breakage |
| `spawn-parked-session.sh` | 5180 | **exclude** | detached-spawn helper hardcoded to /root/.pi/agent/bin; excluded — see Known breakage |
| `test_fleet_ack.py` | 3332 | **include** | suite for the shipped fleet_ack.py |
| `test_fleet_audit.py` | 24250 | **include** | suite for the shipped fleet_audit.py |
| `test_fleet_brief.py` | 4848 | **include** | suite for the shipped fleet_brief.py |
| `test_fleet_bundle.py` | 16064 | **include** | suite for the shipped fleet_bundle.py |
| `test_fleet_children.py` | 39581 | **include** | suite for the shipped fleet_children.py |
| `test_fleet_claims.py` | 37290 | **include** | suite for the shipped fleet_claims.py |
| `test_fleet_dismiss_semantics.py` | 16345 | **include** | suite for the shipped fleet_dismiss_semantics.py |
| `test_fleet_door.py` | 5368 | **include** | suite for the shipped fleet_door.py |
| `test_fleet_entries.py` | 14277 | **include** | suite for the shipped fleet_entries.py |
| `test_fleet_faults.py` | 71851 | **include** | suite for the shipped fleet_faults.py |
| `test_fleet_gate.py` | 10556 | **include** | suite for the shipped fleet_gate.py |
| `test_fleet_identity.py` | 10514 | **include** | suite for the shipped fleet_identity.py |
| `test_fleet_integrity.py` | 14335 | **include** | suite for the shipped fleet_integrity.py |
| `test_fleet_lineage.py` | 7832 | **include** | suite for the shipped fleet_lineage.py |
| `test_fleet_lock.py` | 4530 | **include** | suite for the shipped fleet_lock.py |
| `test_fleet_p0.py` | 53786 | **include** | suite for the shipped fleet_p0.py |
| `test_fleet_policy.py` | 3694 | **include** | suite for the shipped fleet_policy.py |
| `test_fleet_reader.py` | 4040 | **include** | suite for the shipped fleet_reader.py |
| `test_fleet_reconcile.py` | 5652 | **include** | suite for the shipped fleet_reconcile.py |
| `test_fleet_restore.py` | 16950 | **include** | suite for the shipped fleet_restore.py |
| `test_fleet_resume_dark.py` | 5400 | **include** | suite for the shipped fleet_resume_dark.py |
| `test_fleet_rollup.py` | 8437 | **include** | suite for the shipped fleet_rollup.py |
| `test_fleet_scan.py` | 10450 | **include** | suite for the shipped fleet_scan.py |
| `test_fleet_scrub.py` | 17639 | **include** | suite for the shipped fleet_scrub.py |
| `test_fleet_sweep_lineage.py` | 7429 | **include** | suite for the shipped fleet_sweep_lineage.py |
| `test_fleet_verify_binding.py` | 8047 | **include** | suite for the shipped fleet_verify_binding.py |
| `test_name_contract.py` | 12027 | **include** | suite: the session-name contract across spawn/list/tree |
| `test_pi_identity.py` | 8609 | **include** | suite for the shipped pi_identity.py |
| `test_pi_index.py` | 15372 | **include** | suite for the shipped pi_index.py |
| `test_pi_sentinel.py` | 29354 | **exclude** | suite for the excluded pi_sentinel subsystem — cannot pass without it |
| `test_pi_sources.py` | 3769 | **include** | suite for the shipped pi_sources.py |
| `test_pi_taxonomy.py` | 10444 | **include** | suite for the shipped pi_taxonomy.py |
| `test_pi_view.py` | 10331 | **include** | suite for the shipped pi_view.py (added: pi_view must ship, so its test ships — the unit's list omits it) |
| `test_sentinel_cron_lock.py` | 5057 | **include** | suite: lock + kill switch of fleet-sentinel-cron.sh (shipped alongside it) |
| `pi-tmux (source: /root/bin/pi-tmux -> /usr/local/bin/pi-tmux)` | 36895 | **include** | tmux launcher; `fleet` shells out to `pi-tmux --live-owner`/`--close` (copied from /root/bin/pi-tmux -> /usr/local/bin/pi-tmux) |

Totals: **69 included** (+1 from `/root/bin`), **18 excluded**, 87 files classified.

## Directories excluded wholesale

| dir | decision | reason |
|---|---|---|
| `fixtures/` | **exclude** | real session state from this box (`engrane_baseline_20261011.json`, `engrane_gate_baseline_20261011.json`) — internal data, and it is what two suites read; see Known breakage |
| `__pycache__/` | **exclude** | build artefact (`.gitignore`d) |

## Measured facts about the included files

Measured on the copy with `grep -c '/root/'` and a vocabulary scan (`nordisk|engrane|enc1|maestro|HC_Volume_105587324|.nordisk`). These are the raw
numbers a readiness checker will trip over; recording them here so no later unit has
to guess.

| included file | literal `/root/` hits | internal-vocab hits |
|---|---:|---|
| `fleet` | 2 | engrane |
| `fleet-pid-assert-cron.sh` | 3 | - |
| `fleet-sentinel-cron.sh` | 7 | - |
| `fleet-tmux-orphan-cron.sh` | 3 | - |
| `fleet_ack.py` | 0 | nordisk |
| `fleet_audit.py` | 2 | - |
| `fleet_brief.py` | 0 | - |
| `fleet_bundle.py` | 5 | - |
| `fleet_children.py` | 11 | engrane,nordisk |
| `fleet_discovery.py` | 2 | engrane,hc_volume_105587324 |
| `fleet_done_test.py` | 1 | - |
| `fleet_door.py` | 0 | engrane |
| `fleet_entries.py` | 0 | engrane |
| `fleet_gate.py` | 0 | engrane |
| `fleet_ground_truth.py` | 0 | nordisk |
| `fleet_lineage.py` | 0 | - |
| `fleet_name_drift.py` | 1 | - |
| `fleet_pid_assert.py` | 1 | engrane |
| `fleet_policy.py` | 0 | - |
| `fleet_reader.py` | 0 | - |
| `fleet_restore.py` | 11 | - |
| `fleet_resume_dark.py` | 0 | engrane,maestro,nordisk |
| `fleet_rollup.py` | 0 | - |
| `fleet_scrub.py` | 2 | - |
| `fleet_tmux_orphan_guard.py` | 1 | - |
| `fleet_util.py` | 2 | - |
| `pi-live` | 0 | maestro |
| `pi-msg` | 2 | nordisk |
| `pi-peers` | 0 | - |
| `pi-tmux` (from /root/bin) | 2 | none |
| `pi-tree` | 0 | nordisk |
| `pi_identity.py` | 0 | engrane |
| `pi_index.py` | 0 | engrane |
| `pi_rows.py` | 0 | - |
| `pi_sources.py` | 1 | - |
| `pi_taxonomy.py` | 0 | enc1,engrane,maestro,nordisk |
| `pi_view.py` | 0 | maestro |
| `test_fleet_ack.py` | 1 | nordisk |
| `test_fleet_audit.py` | 0 | engrane |
| `test_fleet_brief.py` | 1 | - |
| `test_fleet_bundle.py` | 1 | engrane |
| `test_fleet_children.py` | 3 | engrane,nordisk |
| `test_fleet_claims.py` | 3 | - |
| `test_fleet_dismiss_semantics.py` | 2 | engrane |
| `test_fleet_door.py` | 1 | engrane |
| `test_fleet_entries.py` | 0 | engrane,hc_volume_105587324,nordisk |
| `test_fleet_faults.py` | 2 | - |
| `test_fleet_gate.py` | 0 | engrane |
| `test_fleet_identity.py` | 1 | engrane |
| `test_fleet_integrity.py` | 1 | engrane |
| `test_fleet_lineage.py` | 1 | engrane |
| `test_fleet_lock.py` | 0 | - |
| `test_fleet_p0.py` | 2 | engrane |
| `test_fleet_policy.py` | 1 | maestro |
| `test_fleet_reader.py` | 1 | - |
| `test_fleet_reconcile.py` | 1 | nordisk |
| `test_fleet_restore.py` | 3 | engrane |
| `test_fleet_resume_dark.py` | 1 | engrane |
| `test_fleet_rollup.py` | 1 | - |
| `test_fleet_scan.py` | 1 | - |
| `test_fleet_scrub.py` | 11 | engrane,nordisk |
| `test_fleet_sweep_lineage.py` | 1 | engrane |
| `test_fleet_verify_binding.py` | 1 | - |
| `test_name_contract.py` | 1 | engrane,nordisk |
| `test_pi_identity.py` | 1 | engrane,nordisk |
| `test_pi_index.py` | 0 | engrane,hc_volume_105587324,nordisk |
| `test_pi_sources.py` | 0 | - |
| `test_pi_taxonomy.py` | 1 | engrane,hc_volume_105587324,nordisk |
| `test_pi_view.py` | 0 | engrane,nordisk |
| `test_sentinel_cron_lock.py` | 2 | - |

## Known breakage after copy

Everything here was run **in `/root/fleet/meta-lead/staging/bin/`** in this session,
not guessed. `rc` is the process exit code.

### 1. What still works

| command (run inside `staging/bin/`) | result |
|---|---|
| `./fleet --help` | rc=0 — usage printed, all 26 subcommands listed |
| `python3 test_fleet_brief.py` | rc=0, `Ran 15 tests … OK` |
| `python3 test_fleet_door.py` | rc=0, 20 tests OK |
| `python3 test_fleet_policy.py` | rc=0, 12 tests OK |
| `python3 test_fleet_reader.py` | rc=0, 14 tests OK |
| `python3 test_pi_view.py` | rc=0, 24 tests OK |
| `python3 test_name_contract.py` | rc=0, 21 tests OK |
| `python3 test_sentinel_cron_lock.py` | rc=0, 10 tests OK — **but it ran the LIVE script**, see 3(c) |
| `python3 -c 'importlib.import_module(...)' for all 28 non-test modules | all 28 import cleanly |

Full suite in the copy: 33 files, **30 green / 3 red** (records in the unit report).
Only two of the three red are caused by the copy (they read the excluded `fixtures/`);
the third fails identically in the source directory, i.e. pre-existing.

| suite (run in `staging/bin/`) | source dir result | copy result | verdict |
|---|---|---|---|
| `test_fleet_entries.py` | rc=0, 21 tests OK | rc=1, 21 tests, `errors=6` — `FileNotFoundError: …/bin/fixtures/engrane_baseline_20261011.json` | **copy-induced** |
| `test_fleet_gate.py` | rc=0, 26 tests OK | rc=1, 26 tests, `errors=3` — `FileNotFoundError: …/bin/fixtures/engrane_gate_baseline_20261011.json` | **copy-induced** |
| `test_fleet_scrub.py` | rc=1, 16 tests, `failures=1` (`test_the_real_digest_corpus_yields_no_false_positives`, path `2026-10-20-engrane-profile-subject.md`) | rc=1, same test, same assertion | **pre-existing** |

### 2. Breaks because the excluded `fixtures/` is not shipped

`test_fleet_entries.py` and `test_fleet_gate.py` assert byte-identity against frozen
baselines stored in `bin/fixtures/`. The unit's exclude rule removes `fixtures/`, so 9
assertions in those two files cannot pass in the copy. The fixtures are two ~1–3 KB JSON
files of this box's real worktree/session data: they would have to be re-derived and
scrubbed before they can ship. **Not fixed here** (U2 does not repair while copying).

### 3. Breaks because paths are absolute

Path resolution is still `sed`-free but hardcoded; the numbers are in the table above.
Three distinct failure modes, and they are not the same failure:

(a) **Hardcoded `/root/…` in shipped modules.** After the copy, 43 of the 70 included files
    still contain the literal string `/root/` (see the table). They keep working *on this
    box*, which is exactly why the copy is a faithful snapshot and also exactly why it is
    not portable. This is the single biggest 'not ready' item for a public release.

(b) **PATH-dependent shell-outs.** `fleet` calls its siblings by bare name (`pi-msg`,
    `pi-tmux`, `pi-tree`; `pi_sources` calls `pi-live`). In the copy they resolve from
    `PATH`, i.e. to *this box's* installed binaries, not to the copied files. The copy
    therefore runs on this box for the wrong reason. Nothing here puts `bin/` on PATH.

(c) **A shipped test that silently exercises the live box.**
    `test_sentinel_cron_lock.py` does not locate its subject relatively — it sets
    `SCRIPT = Path("/root/.pi/agent/bin/fleet-sentinel-cron.sh")`. In the copy it passed by
    running the LIVE script. That is a false green for anyone who is not this box: the
    copy's own `bin/fleet-sentinel-cron.sh` was never the file under test. Re-pointing it
    is a one-line fix and is deliberately left to a later unit.

(d) **Spawn helpers are referenced by absolute path and are not shipped.**
    `fleet_resume_dark.py` computes `RESPAWN = $HOME/.pi/agent/bin/respawn-pi-session` and
    `fleet_children.py` calls `/root/.pi/agent/bin/spawn-parked-session.sh`. Both helpers
    were excluded as box-local spawn glue, so `fleet resume-dark` and the park path keep
    working *on this box only*, by reaching back into the live directory. Anywhere else
    they fail at spawn time with a missing file.

### 4. Not measured here

- The two cron scripts other than the sentinel one have no test runner in the copy; they
  were copied, not executed (running them would take real locks on this box).
- `pi-tmux` came from `/root/bin/pi-tmux` (a symlink to `/usr/local/bin/pi-tmux`); the
  file content was copied, and its own absolute paths were not audited in this unit.
- No command was run against a public install path — packaging and the pi.dev route are
  later units, so nothing above should be read as an install test.

### 5. Secret-shape scan of the copy — nothing real found

Ran before the first commit, over every shipped file (`.git` skipped), matching the
shapes a public-release checker looks for: `gho_`, `ghp_`, `glpat-`, `shpat_`, `xoxb-`,
`AIza`, `sk-`/`sk-or-v1`, `-----BEGIN … PRIVATE KEY`, JWTs (`eyJ…`), Telegram bot tokens.

Result: **one file matched, and it is a false positive.** `bin/test_fleet_scrub.py` builds
a PEM header from concatenated strings at runtime (its own comment says it does so to avoid
containing a PEM header) — the words `BEGIN RSA PRIVATE KEY` appear there *without* the
leading dashes and with no key body, inside assertions that the scrubber removes the header
and keeps the body. No secret value, no credential, no live token was found anywhere in the
copy.

Consequence for a readiness checker: a naive `BEGIN … PRIVATE KEY` pattern — or any pattern
that does not require the surrounding dashes — will flag this line. It is a test *about*
redaction, not a leak. Any allowlist entry for it must say so in its reason column.
