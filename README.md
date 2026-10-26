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

**The name is the subject; the domain is a field.** Workers are named after what they ARE
(`docs-1`, `audit-1`, `theme-dup-create-01`). A category word in front of the subject
(`meta-docs-1`, `tmp-docs-1`) says nothing the shorter name did not, and
`bin/fleet_name_lint.py` reports it as a violation. The domain is a separate field: pass
`--domain <d>` when you spawn, and `fleet` counts at most five live workers per domain from
that field, refusing the sixth unless you pass `--over-cap` (which records the exception on
the row). A name prefix is read as the domain only for legacy rows that carry no domain
field, and a spawn that gives neither stays unscoped — a worker in no domain at all, which
is legal and is not counted against a bucket nobody chose.

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
- **A brief can be recorded without existing — fixed, and the shape of the bug is worth
  keeping.** MEASURED 2026-10-26: three spawned units carried `brief_path=…/BRIEF.md` with
  `brief_sha: ""` and **no file on disk**; each worked from the lead's `plan.md` instead, and
  one re-rendered its own brief. The cause was narrower than it looked: a brief RENDERED from a
  plan slice was hashed at issue, a brief HANDED to `--brief` was not hashed at all, and the
  row was only patched on the rendered path. So `fleet_brief.drift()` answered "nothing was
  issued" for the commonest way a worker gets briefed, and the check that catches a worker
  rewriting its own contract never ran. Fixed: `fleet_brief.issue()` hashes the file at spawn,
  missing and empty briefs are refused, and the row always carries `brief_path` **and**
  `brief_sha` (verified end to end on a real spawn). Rows created before this fix still carry
  `""` — for those, treat the brief as evidence only when `test -s "$brief_path"` passes too.
- **A correct secret scan flags the scanner.** `bin/fleet_scrub.py` ships the pattern
  definitions, so the `secrets` check matches a file that is a detector rather than a leak —
  and `secrets` can never be allowlisted, deliberately, because a config file must not be able
  to silence a credential. There is no detector-file exemption yet. Until there is, any tree
  containing this repo will show that one finding, and the right response is a design decision
  rather than a new allowlist line.
- **The cap's decision is proven; its wiring is not.** The refuse/allow/override paths were
  exercised against a synthetic lineage and a non-existent repo, so what is tested is the
  decision `fleet_door` makes. No end-to-end spawn (real worktree, real tmux, real cap
  interaction) was run as part of that test, so treat the wiring as unmeasured.
- **The test suite has one non-hermetic test, and it is red on purpose.** `test_fleet_scrub`
  asserts zero false positives over a live external corpus (a directory of operator handoff
  notes, outside this repo). On the box of
  writing that corpus contains a `NAME=value` assignment the rule correctly flags, so the test
  fails there. The failure is the test doing its job; it is not allowlisted and the value is
  not printed. On a clean machine with no such corpus the same test should be green — that is
  what "clean machine" means here.
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

## The shape to watch: a check that cannot apply

Four times in one night, a check was asked a question it could not answer, and answered as
though it had. That is more dangerous than a check that fails, because a failure gets read.

| the check | what it was asked | what it answered |
|---|---|---|
| per-domain cap | how many workers are in *this* domain, for a row that records no domain | "under the limit" — a worker with no domain in its name was counted in no bucket |
| brief drift | is this the brief you issued? for a brief that was never hashed | "nothing was issued" |
| merged | is this branch an ancestor of `origin/main`? asked of the *default* project's repo | "not merged" — while git said `fatal: Not a valid object name` |
| secret corpus | is this a false positive? over a corpus containing a real assignment | "hit" — correctly: the rule was right and the corpus was wrong. This one is loud on purpose |

The first three are fixed, each with a test that fails when the fix is reverted: the cap counts
a `domain` field (name prefix only for legacy rows), `fleet_brief.issue()` hashes a hand-written
brief at spawn and refuses an empty one, and `git_facts` asks the worktree's own repo. The
fourth is not a bug and is not allowlisted.

The rule that came out of it: **a check that skips must say so, and a check that cannot be
answered is not a pass.** Where the question genuinely does not apply, the refusal says which
question it declined to ask and why — `git_provider` returns nothing for a task-kind entry with
no branch rather than refusing it for a merge that could never happen.

A fifth gap is honest and unfixed: **the memory ceiling is live-only.** The deployment this grew
up on refuses a spawn that would cross a RAM+swap ceiling unless the human passes `--over-mem`,
and records the override. That code is not in this package, so a fleet installed from here can
spawn until the box swaps. It is named here rather than quietly left out.

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
python3 tests/readiness.py --repo .            # in a clone
```

**If you installed this package instead of cloning it, run the same gate inside what you
actually received** — the guard is only worth something if you can check it where you are:

```bash
# the path is wherever npm put it, usually:
PKG=./node_modules/@rx3asarc/pi-fleet
python3 "$PKG/tests/readiness.py" --repo "$PKG"
```

The tarball ships `tests/`, so that copy of the gate is the same gate, and the removal proof
travelled with it: delete `$PKG/bin/fleet_serve.py` and the gate goes red under the id
`serve-guard`, in your install, not just in ours. (An installed package cannot verify the
things that live in a git checkout — the full suite under `bin/`, and the fixtures two suites
need — so those stay a clone-only claim; this paragraph is the boundary, deliberately.)

The kill/liveness path is **not** in that suite: its 15 node suites live outside this repo
(`pi-live-tests`), so consider that path unmeasured here rather than covered — see
[Honest limits](#honest-limits).

Two suites assert against frozen fixture files that are deliberately not published (they
contain this project's own session state), so a fresh clone can show those two as red; see
`docs/PUBLISHING.md` for the measured counts rather than a promise.

## Licence

MIT — see [LICENSE](LICENSE). Copyright (c) 2026 rx3asarc.
