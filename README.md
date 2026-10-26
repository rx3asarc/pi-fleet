# pi-fleet

Durable, addressable workers for [pi](https://pi.dev) — spawn a worker into its
own tmux session and git worktree, talk to it by name, and only tear it down
when it hands back *evidence* that its own frozen test passes.

> **Status: draft.** This README is a working draft written during the repo
> scaffold. Packaging, install paths and the publishing story are still being
> written in later units; do not treat any install line here as tested yet.

## What it is

`fleet` is a small python CLI plus a set of sibling CLIs. It gives you workers
that survive the parent's context window: each one is a real named session in a
real tmux pane with a real worktree, so it can be listed, messaged, asked a
question, and reaped — days later — without re-deriving what it was doing.

The rule that keeps the fleet honest: **a worker reports against the done-test
that was frozen when it was spawned, and never authors its own.** A worker with
no checkable test cannot be spawned; a worker that passes a test it wrote itself
is not evidence.

## Requirements

- python3 (standard library only — no pip installs)
- tmux
- git
- pi (the sessions fleet manages are pi sessions)

## Quickstart (draft)

```bash
git clone <this repo> pi-fleet
cd pi-fleet
./bin/fleet --help          # the CLI entry point lives in bin/
```

`fleet` shells out to its sibling CLIs (`pi-msg`, `pi-tmux`, `pi-tree`,
`pi-live`), which ship in the same `bin/` directory. Until the packaging unit
wires that directory onto your PATH, invoke the CLI by path and/or add
`bin/` to PATH yourself.

## The four-part contract

Every worker's return is reported in four parts, in this order:

1. **RESULT** — what is now true.
2. **ARTIFACTS** — paths, one per line.
3. **COULD NOT DO** — with the reason.
4. **UNSURE** — verbatim, never smoothed away.

A doubt that is hidden is worse than one that is reported, which is why
`UNSURE` is passed upward word for word instead of being summarised.

## What this is not

- Not a replacement for in-memory subagents: if the work fits one context
  window, use a subagent. Fleet is for work that must outlive a context.
- Not a scheduler or a cron runner.
- Not a model harness: it stats `/proc` and reads JSON, it does not call models.

## Licence

MIT — see `LICENSE`.
