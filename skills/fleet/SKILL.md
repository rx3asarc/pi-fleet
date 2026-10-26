---
description: Spawn, message, question, and reap durable pi workers that live in their own tmux session and git worktree and outlive this context window. Use when work must survive a context reset, when several units of work should run independently and be assembled later, or when a task needs an addressable worker you can talk to by name hours later.
name: fleet
---

# fleet — durable, evidence-gated pi workers

An in-memory subagent dies with its context. A **fleet-managed worker** is a real named pi
session in its own tmux pane and its own git worktree, so it can be listed, messaged,
asked a question, and reaped — days later — without re-deriving what it was doing.

Use this skill when the work fits one of these shapes:

- the task will not fit in one context window and must be split into units;
- several units should run at the same time, in isolation from each other's edits;
- you want to ask a worker a question long after spawning it;
- the result must be assembled from several independent reports.

Do **not** use it for work that fits in one turn — a subagent is cheaper and simpler.

## The one rule that keeps this honest

**A worker reports against the done-test that was frozen when it was spawned and never
authors its own.** `fleet` refuses to spawn a worker without a checkable test, and it
re-runs the frozen test itself before teardown, so a worker cannot certify itself by
writing a test that passes. When a unit comes from a plan, the test comes from the plan
slice — never pass `--done-test` alongside `--plan`.

Corollary: a worker that stops talking is **not** done. Teardown is gated on evidence,
not on silence.

## Commands

The CLI ships in this package at `bin/fleet`. Run it by path (see *Paths* below).
Every verb below was checked against `./bin/fleet --help`.

### Write a plan first

A lead writes `plan.md` with one section per unit, each ending in a done-test line:

```
## U1 — <title>
<what the unit must achieve>
done-test: <a command that exits 0 only if the unit is done>
```

The done-test must be able to fail; `true` is not a test.

### `fleet spawn` — create a worker

```bash
./bin/fleet spawn meta-audit-1 --plan plan.md --unit 1 \
    --repo /path/to/repo --task '<task, verbatim>'
```

- `--plan plan.md --unit N` renders the brief from that unit's slice and freezes its
  done-test. This is the normal path; the worker cannot be briefed on another unit.
- `--brief FILE --task '...'` is the ad-hoc path, with `--done-test CMD` (or
  `file:PATH`) supplied by you.
- `--kind code` (default) gives worktree + branch; `--kind task` gives a scratch dir and
  no branch, for research.
- `--repo`, `--branch`, `--base`, `--model` override the defaults.
- `--domain <d>` records the domain as a **field** on the lineage row. The **name is the
  subject** (`audit-1`, `theme-dup-create-01`) and does not have to carry a domain at all;
  the domain is data, and the 5-worker cap is counted from that field. Without `--domain`,
  a name that still starts with one of the example domains is read as declaring one, and
  any other name records no domain at all — nothing is invented on your behalf.
- `--over-cap` spawns even when the domain is at the cap. The exception records itself as
  `domain_cap_override` on the row; there is no silent sixth worker.
- The domain set is an example, not a law: `FLEET_DOMAINS=web,data,infra` replaces the
  five defaults for the process that spawns. (A `sk-` credential scanner matches inside a
  domain name that ends in `sk-`; allowlist the domain rather than renaming it.)

### `fleet list` — who exists, and in what state

```bash
./bin/fleet list
./bin/fleet list --json
```

`--json` is the machine-readable form; use it rather than scraping the table.

### `fleet send` and `fleet ask` — talk to a worker

```bash
./bin/fleet send meta-audit-1 'quick note, no reply needed'
./bin/fleet ask meta-audit-1 'does the audit cover secrets?' --timeout-ms 120000
```

`send` is fire-and-forget; `ask` blocks for a reply. Do **not** message a worker to
change its task — re-plan and re-spawn instead, or the frozen test no longer matches
the work.

### `fleet done` — the worker reports

```bash
./bin/fleet done meta-audit-1 --verify '<the frozen done-test>' \
    --file audit-readiness.md --note 'what is now true' \
    --unsure 'what you could not verify, verbatim'
```

- `--verify` re-runs the proof; exit 0 is what makes the claim evidence.
- `--file` (repeatable) names artifacts the reaper checks exist.
- `--unsure` travels upward word for word. Never smooth it away.
- `--claim-file` records typed claims so conflicts between workers become computable.
- `--no-reap` reports without attempting immediate teardown.

Report in four parts, in this order: **RESULT**, **ARTIFACTS** (one path per line),
**COULD NOT DO** (with reason), **UNSURE** (verbatim).

### `fleet rollup` — assemble the evidence

```bash
./bin/fleet rollup <lead-session-id> --report-to <session-id> --stdout
```

Deterministic: it collects every unit's evidence, prints conflicts between typed
claims, and quarantines worker prose. Do not write your own summary *instead* of it —
add interpretation only after the assembled evidence.

### `fleet reap` — tear down, only on evidence

```bash
./bin/fleet reap --dry-run
./bin/fleet reap
```

`--dry-run` shows the proposal without killing anything. Reaping requires the frozen
test to pass; a worker that never reported needs `--accept-silent`, and abandoning a
single one takes `--abandon <name>`. **Prefer `--dry-run` first.**

### Supporting verbs

`fleet attach` (tmux attach), `fleet children` (lineage, heartbeat, the proposal),
`fleet scan` (live sessions with no lineage record), `fleet reader` (adversarial review
brief), `fleet reader-verdict` (PASSES/FAILS), `fleet integration` (cross-unit check in
a clean tree), `fleet reconcile` (rebuild who-is-alive from tmux and `/proc`),
`fleet sentinel` (one autonomous tick), `fleet relabel` (name drift), `fleet revive`,
`fleet dismiss`, `fleet keep`, `fleet adopt`, `fleet reverify`, `fleet plan`, `fleet ack`,
`fleet for`, `fleet sweep-lineage`, `fleet reap-children`.

## Paths — why you must not type `fleet` and expect PATH to find it

A package installed through `pi install` cannot put an executable on your shell's PATH.
So the CLI lives inside the package and you invoke it by path:

```bash
# from a clone
./bin/fleet --help

# from an installed package, the skill directory tells you the package root
<package-root>/bin/fleet --help
```

The `fleet` CLI shells out to sibling CLIs that ship in the same `bin/`
(`pi-msg`, `pi-tmux`, `pi-live`, `pi-tree`), so put that `bin/` on `PATH` for the
worker sessions, or the shell-outs will resolve to whatever else is installed.

Runtime dependencies: **python3** (standard library only — no pip installs),
**tmux**, **git**, and **pi** itself.

## State

Runtime state (lineage, registry, ledgers, verify cache) lives outside the package in
the control dir, `FLEET_CONTROL_DIR` (default `~/.fleet`). It is not part of the repo and
never ships. Never commit it, and never publish a tree that contains it.
