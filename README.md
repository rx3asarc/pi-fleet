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

Nothing here is optional, and the last one is the one people miss.

- **python3** — standard library only, nothing to `pip install`. Developed and verified on
  3.12; older 3.x is untested (there is no 3.9 interpreter on the build box to check
  against).
- **tmux** — workers are tmux sessions; `fleet` attaches, drives and closes them.
- **git** — each code worker gets a worktree and a branch.
- **pi** — on `PATH`; the sessions `fleet` manages are pi sessions.
- **the pi-intercom broker** — see [Communication](#communication). Without it a fleet can
  spawn workers and then has no way to talk to them.

`fleet` shells out to sibling CLIs that ship in the same `bin/`: `pi-msg`, `pi-tmux`,
`pi-live`, `pi-tree`. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Communication

**The broker is a hard requirement, not a nicety.** `fleet send`, `fleet ask`, every
`fleet done` report upward, the rollup and the `[wake]` pokes all travel through
`pi-intercom`'s local broker. Install it alongside the package:

```bash
pi install npm:pi-intercom
```

It is declared in this package's `dependencies`, so `pi install` of `pi-fleet` should pull
it in; install it explicitly if you use a source pi does not resolve dependencies for. If
it is missing, `bin/pi-msg` says so in one line and names that command — it never prints a
path and leaves you to guess. The readiness gate (`python3 tests/readiness.py --repo .`)
checks both the declaration and that failure message (check id `intercom-dependency`).

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

# spawn one worker for unit 1 of that plan against a repo you own
./bin/fleet spawn docs-1 --plan plan.md --unit 1 \
    --repo ~/code/my-project --task 'document the CLI entry point'

# talk to it, then watch for its report
./bin/fleet list
./bin/fleet ask docs-1 'which file owns the argparse setup?' --timeout-ms 120000

# when it reports, the frozen test decides; dry-run first
./bin/fleet reap --dry-run
./bin/fleet reap
```

The done-test above fails before the work and passes after it — that is the whole point of
freezing it at spawn time rather than accepting the worker's word.

**The name is the subject; the domain is a field.** `docs-1` is a fine name — it says what
the worker is for. A category word in front of it (`meta-docs-1`, `tmp-docs-1`) says nothing
the shorter name did not, and `bin/fleet_name_lint.py` reports it as a violation. The domain
is passed as `--domain docs` (or read from the name prefix when you do not pass one) and is
what the per-domain cap counts, so keep at most five running in one domain and you can
still read their reports.

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

## Honest limits

A green gate means "nothing on the checklist is wrong". It does not mean the fleet
supervises everything, and these are the places where it does not. They are published, not
fixed, because hiding an unmeasured edge is worse than admitting it.

- **A pi started BY HAND inside a worktree is invisible to the pid and orphan rules.**
  Those rules (A–E) key on the marker, the lineage row and the transcript claim that
  `fleet spawn` writes at spawn time. Start `pi` yourself in a worktree and there is no
  marker, no row and no claim for it: it is a perfectly legitimate process that the guards
  cannot see and will never assert about. The guards cover spawned workers, not every
  process on the box.
- **The fleet does not supervise what a worker starts.** Dev servers, watchers and stray
  loops launched by a worker were invisible entirely on the day of writing (111 processes,
  3.15 GB of swap, 0 of them accounted for) — that gap is what the box-doctor `leaks`
  domain is meant to close, and until it lands, a worker that leaks a process leaks it past
  every check in this repo. Reaping a worker will not clean up after it.
- **The 5-per-domain cap is a number about a FIELD, so pass `--domain` explicitly.** The
  cap is enforced in `fleet spawn` and in the door from a recorded `domain` field, counted
  field-first with the name prefix only as a fallback for old records. A door call with no
  domain passed *infers* one from the task's first non-stopword, and an inferred domain is
  labelled as inferred — the cap then counts a domain you did not choose. Pass `--domain`
  and the cap counts what you chose.
- **The name is a LABEL; the `sessionId` is the identity.** The in-process namer rewrites a
  session's name from user-message text, so the label can drift or repeat. The process's
  own `/name` lands; an external `pi-rename` send-keys can be silently dropped mid-turn.
  When you need to know which session you are talking to, trust the sessionId, not the
  roster label.
- **The naming lint reports live violations and counts historical ones, and never relabels
  history.** Removing a prefix from a live record is a deliberate act by a human on an idle
  session; a linter that rewrites its own past cannot be trusted to describe it. Historical
  violations stay in the output, counted, on purpose.
- **The kill/liveness path is UNMEASURED here, not green.** It is covered by 15 node suites
  that live outside this repo (`pi-live-tests`), so nothing in this README's test counts
  says anything about the most dangerous code path. Treat that part as unproven.
- **The broker's own dependencies are outside our control.** `pi-intercom` declares none of
  its own, yet its CLI imports `tsx`; on the box we measured, `tsx` resolves because pi's
  npm tree hoists it. Whether that holds in a clean global install is unmeasured — if
  `bin/pi-msg` fails with a module-resolution error rather than the install hint, that is
  the reason, and installing `tsx` beside the broker is the workaround.

One bookkeeping note for anyone reading the ship-readiness audit that accompanies this
release: it was written before the last night of changes, and two of its findings expired
rather than being fixed. Protection is now **attributed** in the live store — the record
carries `protected_set_by`, e.g. `scan:orphan-enrolment (no human signature)` for protection
added by a scan, or `pre-2026-10-26:unattributed` for records opened before that change, and
no human signature is ever invented for either — and `fleet relabel` now exists as a verb.
Where this README and that report disagree, this README describes the code that ships.

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

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — the modules, the guards, the sibling CLIs,
  the state, and the seams. It cross-links [Honest limits](#honest-limits).
- [docs/PUBLISHING.md](docs/PUBLISHING.md) — how this ships, and what was not tested.
- [skills/fleet/SKILL.md](skills/fleet/SKILL.md) — the model-facing skill.

## Tests

The suite is stdlib `unittest`, no runner to install:

```bash
cd pi-fleet
python3 -m unittest discover -s bin -p 'test_*.py' -v
```

And the one command that answers "may this tree be published?" — it scans for
credentials, vendored binaries, leftover backups, bytecode, a licence, the manifest,
private vocabulary and absolute home paths, then runs each guard's own `--selftest` and
probes `bin/pi-msg` with no broker reachable. Exit 0 is the only green:

```bash
python3 tests/readiness.py --repo .
```

The kill/liveness path is **not** in that suite: its 15 node suites live outside this repo
(`pi-live-tests`), so consider that path unmeasured here rather than covered — see
[Honest limits](#honest-limits).

Two suites assert against frozen fixture files that are deliberately not published (they
contain this project's own session state), so a fresh clone can show those two as red; see
`docs/PUBLISHING.md` for the measured counts rather than a promise.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 rx3asarc.
