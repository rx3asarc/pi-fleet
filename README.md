# pi-fleet

Durable, addressable workers for [pi](https://pi.dev) — spawn a worker into its own tmux
session and git worktree, talk to it by name, and tear it down only when it hands back
evidence that a test **you** froze at spawn time passes.

- Repo: `github.com/rx3asarc/pi-fleet`
- Package: `@rx3asarc/pi-fleet` (npm names are scoped here; the repo name is not — see
  [Why the package is scoped](#why-the-package-is-scoped))

## The problem

A subagent lives inside your context window. When that window ends, so does the work:
the reasoning, the plan, the half-finished branch, and any memory of what was asked are
gone. Long jobs therefore get re-derived rather than continued.

`pi-fleet` gives you workers that outlive the parent. Each one is a real named pi session
in a real tmux pane with a real git worktree, so it can be listed, messaged, asked a
question, and reaped days later without anyone re-reading a transcript.

## The rule that makes it worth using

**A worker reports against the done-test that was frozen when it was spawned, and never
authors its own.**

`fleet` will not spawn a unit that has no checkable test, and at teardown it re-runs the
frozen test itself in a clean tree. So a worker cannot pass by writing a test that suits
it, and it cannot be declared finished because it stopped talking. Teardown is gated on
exit status, not on confidence — which means "done" is a fact about the tree rather than a
claim in a message.

## Install

Two install paths, both from pi's package system:

```bash
# from the repo at a tag (primary path)
pi install git:github.com/rx3asarc/pi-fleet@v0.1.0

# from a local clone
git clone https://github.com/rx3asarc/pi-fleet
pi install ./pi-fleet
```

Either way, pi reads the `pi` manifest in `package.json`, which points at the bundled
skill in `skills/`. **A package cannot put an executable on your shell's PATH**, so the
CLI is not installed as a `fleet` command. The skill tells the model to run it by path
inside the package, and you can do the same:

```bash
cd pi-fleet && ./bin/fleet --help
```

## Requirements

- **python3** — standard library only, nothing to `pip install`. Developed and verified on
  3.12; older 3.x is untested (there is no 3.9 interpreter on the build box to check
  against).
- **tmux** — workers are tmux sessions; `fleet` attaches, drives and closes them.
- **git** — each code worker gets a worktree and a branch.
- **pi** — on `PATH`; the sessions `fleet` manages are pi sessions.

`fleet` shells out to sibling CLIs that ship in the same `bin/`: `pi-msg`, `pi-tmux`,
`pi-live`, `pi-tree`. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Quickstart

```bash
git clone https://github.com/rx3asarc/pi-fleet
cd pi-fleet

# what does the CLI do?
./bin/fleet --help

# a plan: one section per unit, each with a done-test
cat > plan.md <<'PLAN'
## U1 — document the entry point
Add a short section to docs/ARCHITECTURE.md naming the CLI entry point.
done-test: grep -q 'bin/fleet' docs/ARCHITECTURE.md
PLAN

# spawn one worker for unit 1 against a repo you own
./bin/fleet spawn meta-docs-1 --plan plan.md --unit 1 \
    --repo ~/code/my-project --task 'document the CLI entry point'

# talk to it, then watch for its report
./bin/fleet list
./bin/fleet ask meta-docs-1 'which file owns the argparse setup?' --timeout-ms 120000

# when it reports, the frozen test decides; dry-run first
./bin/fleet reap --dry-run
./bin/fleet reap
```

The done-test above fails before the work and passes after it — that is the whole point of
freezing it at spawn time rather than accepting the worker's word.

Workers are named `<domain>-<role>-<n>`; keep at most five running in one domain so you
can still read their reports.

## Reporting

Every worker returns four parts, in this order:

1. **RESULT** — what is now true.
2. **ARTIFACTS** — paths, one per line.
3. **COULD NOT DO** — with the reason.
4. **UNSURE** — verbatim, never smoothed away.

A doubt that is hidden is worse than one that is reported, which is why `UNSURE` is passed
upward word for word instead of being summarised. `fleet rollup` assembles the evidence
deterministically before anyone writes an opinion on top of it.

## State

Runtime state — lineage, registry, ledgers, the verify cache — lives outside the package in
the control dir (`FLEET_CONTROL_DIR`, default `~/.fleet`). None of it is shipped in the npm
tarball, and none of it belongs in git.

## What this is not

- **Not a replacement for subagents.** If the work fits in one context window, use a
  subagent: it is cheaper and needs no tmux, no worktree and no cleanup.
- **Not a scheduler or a cron runner.** It does not decide when work should happen.
- **Not a model harness.** It stats `/proc`, reads JSON and drives tmux; it never calls a
  model itself.
- **Not a sandbox.** A worker runs with your user's permissions, in your repo. Review what
  you hand it.
- **Not a merge tool.** Workers produce branches and evidence; merging is yours.

## Docs

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the modules, the sibling CLIs, the state.
- [docs/PUBLISHING.md](docs/PUBLISHING.md) — how this ships, and what was not tested.
- [skills/fleet/SKILL.md](skills/fleet/SKILL.md) — the model-facing skill.

## Tests

The suite is stdlib `unittest`, no runner to install:

```bash
cd pi-fleet
python3 -m unittest discover -s bin -p 'test_*.py' -v
```

Two suites assert against frozen fixture files that are deliberately not published (they
contain this project's own session state), so a fresh clone can show those two as red; see
`docs/PUBLISHING.md` for the measured counts rather than a promise.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 rx3asarc.
