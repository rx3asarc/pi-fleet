# Publishing pi-fleet

Everything here was run on the build box on 2026-10-26 unless it is explicitly marked
**UNTESTED**. The command that produced each measured number is quoted so it can be re-run.

Two names, deliberately different:

| thing | value | why |
|---|---|---|
| GitHub repo | `rx3asarc/pi-fleet` | the repo name is free |
| npm package | `@rx3asarc/pi-fleet` | the unscoped name `pi-fleet` is **taken** on npm |

Measured:

```bash
$ npm view pi-fleet version              # → 0.2.0   (someone else owns the name)
$ npm view @rx3asarc/pi-fleet version    # → E404 Not found   (free at time of writing)
```

So every install line must distinguish them: the *package* is scoped, the *repo* is not.

## 1. The package is publish-ready — measured with `npm pack`

There is **no npm auth on this box**, so publishing was not attempted. Publish-*readiness*
was verified by packing the tarball into a temp dir and reading its file list:

```bash
TMP=$(mktemp -d)
npm pack --pack-destination "$TMP"
tar tzf "$TMP"/rx3asarc-pi-fleet-0.1.0.tgz | sort
```

Measured result of that command (npm 10.9.7, node v22.22.2):

- **43 files** in the tarball, `182 kB` packed, `569 kB` unpacked as measured with this
  document in it (packed/unpacked sizes move by a few hundred bytes whenever a doc is
  edited; the **file count is the stable fact**).
- `bin/` (37 files, all executables keep their `0755` mode inside the tarball — checked
  with `tar tvzf`), `skills/fleet/SKILL.md`, `docs/ARCHITECTURE.md`, `docs/PUBLISHING.md`,
  `README.md`, `LICENSE`, `package.json`.

Excluded on purpose, by the `files` allowlist in `package.json`:

| excluded | count at pack time | reason |
|---|---:|---|
| `bin/test_*.py` | 33 files | the suite is for maintainers; it also needs fixtures that are not published |
| `bin/__pycache__/` | 57 `.pyc` files | build residue, never source |
| `docs/SCAFFOLD-NOTES.md` | 1 file | internal scaffolding record: this box's paths, session names and fixture inventory |
| `fixtures/` | absent in this tree | real session state; never published |
| `fd` (vendored ELF) | absent in this tree | third-party binary, not ours to redistribute |
| `*.bak`, `*.orig`, `*~` | 0 found | nothing to exclude; the patterns are there so it stays that way |

`package.json` carries `keywords: ["pi-package", …]` (required for the gallery), `license:
MIT`, no `private` flag, no lifecycle scripts (an install must be inert), and the `pi`
manifest:

```json
"pi": { "skills": ["./skills"] }
```

## 2. Create the repo and tag it

```bash
gh repo create rx3asarc/pi-fleet --public \
    --description "Durable, addressable pi workers in tmux + git worktrees, torn down only on evidence" \
    --source . --remote origin --push

git tag -a v0.1.0 -m "pi-fleet 0.1.0"
git push origin v0.1.0
```

`gh` is **not authenticated for this** on the build box, so the two commands above are the
path, not a record of something that happened — the publish step belongs to whoever holds
the credentials. Verify afterwards with:

```bash
git ls-remote --tags https://github.com/rx3asarc/pi-fleet
```

## 3. Install paths — tested or untested, with the reason

| install path | status | reason / how it was checked |
|---|---|---|
| `pi install ./pi-fleet` (local clone) | **TESTED** | ran `pi install /abs/path/to/pi-fleet -l` in a throwaway git repo: exit 0, and `.pi/settings.json` then contained the resolved path |
| `pi install git:github.com/rx3asarc/pi-fleet@v0.1.0` | **UNTESTED** | the repo does not exist yet; the *form* is verified against `pi install --help` (`pi install git:github.com/user/repo`) and pi's `docs/packages.md` (`git:github.com/example/pi-tools@v1`) |
| `pi install https://github.com/rx3asarc/pi-fleet` | **UNTESTED** | same reason; a repo URL is documented as a git source |
| `pi install npm:@rx3asarc/pi-fleet@0.1.0` | **UNTESTED** | nothing is on the npm registry yet; `npm publish` cannot run here (next section) |
| skill actually loading inside a session | **UNTESTED** | the install was accepted and the declared `pi.skills` path exists, but no non-interactive command lists discovered skills (`pi config` is a TUI), so "the model is told about it" is not something I verified |

The primary path is the git one, because it needs no npm account:

```bash
pi install git:github.com/rx3asarc/pi-fleet@v0.1.0
```

**A package cannot put an executable on the user's `PATH`.** That is the central packaging
decision: the python CLI ships inside the package at `bin/fleet`, and `skills/fleet/SKILL.md`
tells the model to invoke it by path and to put that `bin/` on `PATH` so the sibling CLIs
(`pi-msg`, `pi-tmux`, `pi-live`, `pi-tree`) resolve.

## 4. The pi.dev/packages gallery — npm-only, and therefore blocked here

Measured: `https://pi.dev/packages` returns HTTP 200 and describes itself as the *"Package
Catalog"* — a catalog of extensions, skills, prompt templates and themes that live on npm.
Every entry
on the page links npm + repo and every install line is of the form
`pi install npm:<package>`. pi's own `docs/packages.md` states that the `pi-package`
keyword is what makes an npm package eligible for gallery discovery.

Consequence, stated plainly: **a git-only install does not get the package into the
gallery.** The gallery follows from an npm publish, so on a box with no npm auth the
gallery route is blocked, not merely untested.

- Keyword in place: `pi-package` ✅ (see `package.json`)
- Gallery submission procedure: **UNTESTED** — no submission form or moderation step was
  found on the catalog page during a single fetch on 2026-10-26, and pi's docs describe
  eligibility rather than a review process. What is unknown: whether listing is purely
  crawler-driven (publish → appears) or requires a manual step; whether the `pi.image` /
  `pi.video` gallery previews are required for a polished entry (they are documented as
  optional and none are supplied here).

## 5. What is NOT done here, and why

- **`npm publish` has not been run.** Measured: `npm whoami` → `need auth`. This is an
  environment fact, not a packaging failure, and it is not a readiness blocker for the git
  install path.
- **No tag pushed, no repo created.** No credentials for `gh` were available to this unit,
  and creating the repo is explicitly the publishing unit's job, not packaging's.
- **Gallery listing not submitted**, because it depends on the npm publish above.
- **No npm lifecycle scripts**, by design: `preinstall`/`install`/`postinstall` would make a
  package install execute code, and this package needs none.

### The two commands for whoever holds the npm token

```bash
npm login                        # UNTESTED here: interactive, requires credentials
npm publish --access public      # UNTESTED here: no auth on this box
```

Run both from the repo root, where `name` is `@rx3asarc/pi-fleet`. `--access public` is
required for a scoped package to be installable by anyone. Then re-verify with
`npm view @rx3asarc/pi-fleet version` and `npm pack --dry-run` before announcing the
`npm:` install line.

## 6. Measured state of the test suite at this commit

```bash
# on a clean clone (no bin/fixtures/, no external corpus)
python3 -m unittest discover -s bin -p 'test_*.py'
# Ran 809 tests in 17.762s
# FAILED (failures=1, skipped=19)

# with the frozen baseline armed (FLEET_GATE_FIXTURES, or bin/fixtures/)
python3 -m unittest discover -s bin -p 'test_*.py'
# Ran 809 tests in 16.906s
# FAILED (failures=1)
```

**The count, once, with the commit it belongs to: 809 test methods at `51c1142`**
(805 before `51c1142`, 801 before the brief-issue fix, which added four and four again).
Subtests are not a denominator — 309 subtest passes ride inside those methods — and an earlier
figure of 829 in the ship-readiness audit could not be reproduced from any tree on this box, so
it is withdrawn rather than explained. If you read a different number elsewhere, that number
belongs to a different commit; this line is the one to compare against.

One red is expected and is not a defect in this tree: `test_fleet_scrub.py` asserts zero false
positives over a corpus *outside* the repo (operator handoff notes). On a machine without that
corpus it passes; here it fails, correctly, because the corpus contains an assignment the rule
is built to catch. It is not allowlisted.

The 19 skips are `test_fleet_entries.py` and `test_fleet_gate.py` declining to run because
`bin/fixtures/*.json` is deliberately unpublished (it is a copy of a private deployment's real
state, not an example). They **skip loudly**, naming the path they looked for, and say in the
message that nothing has been verified — a guard that passes vacuously is the failure mode
this arrangement exists to avoid. To arm them, see `tests/README.md`.

## 7. Pre-flight checklist before the first public push

1. `python3 tests/readiness.py --repo .` exits 0 (the readiness checker is the mechanical
   gate; do not publish on an opinion).
2. `npm pack --dry-run` file list matches section 1 — no secrets, no `fd`, no `.bak`, no
   `__pycache__`, no state.
3. `grep -rlE '/(root|Users|home)/' bin/` returns nothing except the module that owns
   path resolution.
4. No internal project vocabulary in shipped files — the term list is in
   `docs/SCAFFOLD-NOTES.md`, which is deliberately not shipped.
5. `LICENSE` present, `README.md` renders, `version` is `0.1.0` in `package.json`.
