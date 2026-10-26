#!/usr/bin/env python3
"""Invariant A, made measurable.

    A ledger row that is finished must not have a live process.
    A fleet-spawned process (PI_TAXONOMY) must have an open unit.

Reads /proc, never panes. Panes lie: a session can be unreachable, orphaned,
or running under a `sh -c` wrapper whose foreground command is not `pi`.
The process table cannot lie about whether a process exists.

Exit codes: 0 clean, 1 violations found, 2 could not read a source of truth.

Usage:
    fleet_pid_assert.py [--json] [--quiet] [--selftest] [--lineage PATH]
"""
import argparse
import json
import os
import sys
import time

import fleet_paths

LINEAGE = str(fleet_paths.control_dir() / "lineage.json")
TERMINAL_VERDICTS = {"dismissed", "reaped", "done", "closed", "reap-failed"}
PROTECTED_HINTS = ("fork",)


def live_pis():
    """Every real pi process: pid, cwd, taxonomy, tty. Nothing from panes."""
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmd = fh.read().decode("utf-8", "ignore").replace("\0", " ").strip()
        except OSError:
            continue
        if not (cmd == "pi" or cmd.endswith("/pi") or cmd.split(" ")[-1] == "pi"):
            continue
        tax, sid = None, None
        try:
            # Names only, and only the two we own. /proc/<pid>/environ can hold
            # secrets; it is read to extract PI_SESSION_ID / PI_TAXONOMY and nothing
            # else is ever surfaced (guardrail R-B: list names, never values).
            with open(f"/proc/{pid}/environ", "rb") as fh:
                for var in fh.read().decode("utf-8", "ignore").split("\0"):
                    if var.startswith("PI_TAXONOMY="):
                        tax = var.split("=", 1)[1]
                    elif var.startswith("PI_SESSION_ID="):
                        sid = var.split("=", 1)[1]
        except OSError:
            pass
        try:
            cwd = os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            cwd = "?"
        # tty of the process, not of any pane
        tty = "?"
        try:
            stat = open(f"/proc/{pid}/stat").read().split()
            tty_nr = int(stat[6])
            starttime_ticks = int(stat[21])
            if tty_nr:
                major, minor = (tty_nr >> 8) & 0xFFF, (tty_nr & 0xFF) | ((tty_nr >> 12) & 0xFFF00)
                tty = f"pts/{minor}" if major == 136 else f"{major}:{minor}"
            started = proc_start_epoch(starttime_ticks)
        except (OSError, IndexError, ValueError):
            started = None
        out.append({"pid": pid, "cwd": cwd, "taxonomy": tax, "session_id": sid,
                    "tty": tty, "started_at": started})
    return out


def name_candidates(proc):
    """A live process can be named by its taxonomy or by its worktree.

    Worktrees are `<repo>-<session name>`, and the repo is often itself called
    `engrane`, so `engrane-engrane-essay-quality-1` is session
    `engrane-essay-quality-1`. Both strip depths are offered rather than one:
    guessing a single prefix is how the name path silently stopped matching.
    """
    names = set()
    if proc["taxonomy"]:
        names.add(proc["taxonomy"].replace("/", "-"))
    base = os.path.basename(proc["cwd"].rstrip("/"))
    for prefix in ("engrane-", "engrane-engrane-", "design-catalog-site-", "scratch-"):
        if base.startswith(prefix) and len(base) > len(prefix):
            names.add(base[len(prefix):])
    return names


def is_terminal(rec):
    if rec.get("closed_at"):
        return True
    verdict = (rec.get("verdict") or "").lower()
    if verdict in TERMINAL_VERDICTS:
        return True
    state = (rec.get("state") or "").lower()
    return state in {"dead", "closed", "reaped", "dismissed"}


def load_lineage(path):
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"cannot read lineage {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    return data.get("children") or data


def proc_start_epoch(starttime_ticks):
    """Wall-clock start of a process from /proc/<pid>/stat field 22.

    Needed because a pid is not an identity: after a reboot (or heavy churn)
    the same number is a different process, and a stale pid-<n> row would then
    "prove" a violation that does not exist.
    """
    try:
        with open("/proc/uptime") as fh:
            uptime = float(fh.read().split()[0])
        hz = os.sysconf("SC_CLK_TCK")
        with open("/proc/stat") as fh:
            btime = next(int(l.split()[1]) for l in fh if l.startswith("btime"))
        return btime + uptime - (starttime_ticks / hz)
    except (OSError, ValueError, StopIteration, ZeroDivisionError):
        return None


def row_start_epoch(rec):
    # A spawner that records kernel ticks (`pid_starttime`, stat field 22) is
    # recording an identity that is no weaker than an epoch, so it must not be
    # ignored here: converting it is cheaper than demanding a second field.
    ticks = rec.get("pid_starttime")
    if isinstance(ticks, int) and ticks > 0:
        epoch = proc_start_epoch(ticks)
        if epoch:
            return float(epoch)
    for field in ("started_at", "spawned_at", "pid_start", "created_at", "ts"):
        val = rec.get(field)
        if isinstance(val, (int, float)) and val > 1_000_000_000:
            return float(val)
    return None


def pid_match_is_current(rec, proc, tolerance=300.0):
    """A pid row only counts if the process really is the one it described."""
    row_start = row_start_epoch(rec)
    proc_start = proc.get("started_at")
    if row_start is None or proc_start is None:
        # No time on either side: a pid alone is not identity, so demand the
        # name agree too before this can become a violation.
        return None
    return abs(row_start - proc_start) <= tolerance


def proc_identity_of(pid):
    """(starttime ticks, epoch) for a live pid, or (None, None)."""
    try:
        ticks = int(open(f"/proc/{int(pid)}/stat").read().split()[21])
    except (OSError, IndexError, ValueError):
        return None, None
    return ticks, proc_start_epoch(ticks)


def live_index():
    """name -> live session entry (pid, sessionId), from `pi-tree --json`.

    Deliberately NOT `import fleet_children`: that module refuses to answer until the
    fleet binary has wired it (`configure()`), and an invariant checker must never
    depend on the program it checks being initialised. If the index cannot be read the
    answer is a loud note, never a silent pass.
    """
    import subprocess
    try:
        out = subprocess.run(["pi-tree", "--json"], capture_output=True, text=True,
                             timeout=60).stdout
        return {e["name"]: e for e in json.loads(out or "[]") if e.get("name")}, None
    except Exception as ex:                                  # noqa: BLE001
        return {}, f"live index unreadable ({type(ex).__name__}: {ex}), rule E skipped"


def audit(children, procs, index=None):
    """Return (violations, notes). One violation per provable contradiction."""
    violations, notes = [], []
    # index lineage by name
    by_name = {}
    for key, rec in children.items():
        if not isinstance(rec, dict):
            continue
        name = rec.get("name") or (None if key.startswith("pid-") else key)
        if not name:
            continue
        by_name.setdefault(name, []).append((key, rec))

    by_pid = {}
    for key, rec in children.items():
        if not isinstance(rec, dict):
            continue
        pid = rec.get("pid")
        if pid is None and key.startswith("pid-") and key[4:].isdigit():
            pid = int(key[4:])
        if isinstance(pid, int):
            by_pid.setdefault(pid, []).append((key, rec))

    for proc in procs:
        cands = name_candidates(proc)
        hit = None
        # pid-keyed rows are real in this store ('pid-<n>' keys), and they are
        # the ones that produced phantom "process gone and never reported"
        # children. A pid match is stronger than a name match, so it is kept:
        # the name loop below must not be allowed to overwrite it.
        for key, rec in by_pid.get(proc["pid"], []):
            if not is_terminal(rec):
                continue
            current = pid_match_is_current(rec, proc)
            if current is False:
                notes.append({
                    "why": "stale pid row ignored (pid reused: start times disagree)",
                    "pid": proc["pid"], "tty": proc["tty"], "cwd": proc["cwd"],
                    "lineage_key": key,
                })
                continue
            if current is None and not (name_candidates(proc) & set(by_name)):
                notes.append({
                    "why": "pid row matched but unconfirmed (no start time on either side)",
                    "pid": proc["pid"], "tty": proc["tty"], "cwd": proc["cwd"],
                    "lineage_key": key,
                })
                continue
            hit = (rec.get("name") or f"pid-{proc['pid']}", key, rec, "pid")
            break
        if hit is None:
            for cand in cands:
                for key, rec in by_name.get(cand, []):
                    if is_terminal(rec):
                        hit = (cand, key, rec, "name")
                        break
                if hit:
                    break
        if hit:
            cand, key, rec, via = hit
            violations.append({
                "rule": "A",
                "why": "ledger row is finished but the process is alive",
                "name": cand,
                "lineage_key": key,
                "matched_by": via,
                "verdict": rec.get("verdict"),
                "pid": proc["pid"],
                "tty": proc["tty"],
                "cwd": proc["cwd"],
            })
            continue
        # ── RULE D (larch, 2026-10-26) ────────────────────────────────────────
        # The row must name THIS process's transcript, not its neighbour's. Rule A
        # only asks "is a finished row's process still alive", so it stayed silent
        # while every row pointed one spawn behind: a row naming the previous
        # worker's session is not a finished row, it is a row about someone else,
        # and the liveness store had nothing to heartbeat. This is the check that
        # would have caught it, and it looks at OPEN rows on purpose.
        for key, rec in by_pid.get(proc["pid"], []):
            if is_terminal(rec):
                continue
            current = pid_match_is_current(rec, proc)
            if current is False:
                continue
            if current is None and not (cands & set(by_name)):
                continue
            sid = rec.get("session_id") or key
            transcript = rec.get("transcript")
            if not transcript:
                continue  # nothing claimed, so nothing contradicted
            base = os.path.basename(str(transcript))
            names_sid = base.endswith(f"_{sid}.jsonl")
            if not names_sid or not os.path.exists(str(transcript)):
                claimed = base.rsplit("_", 1)[-1].replace(".jsonl", "")
                violations.append({
                    "rule": "D",
                    "why": ("row names a different session's transcript" if not names_sid
                            else "row's transcript path does not exist"),
                    "name": rec.get("name") or key,
                    "lineage_key": key,
                    "row_session_id": sid,
                    "transcript_session_id": claimed,
                    "pid": proc["pid"],
                    "tty": proc["tty"],
                    "cwd": proc["cwd"],
                })
                break
        # rule B: a fleet-spawned process must have an open unit behind it
        if proc["taxonomy"]:
            open_units = [
                (key, rec) for key, rec in by_name.get(proc["taxonomy"].replace("/", "-"), [])
                if not is_terminal(rec)
            ]
            if not open_units:
                violations.append({
                    "rule": "B",
                    "why": "fleet-spawned process with no open unit in the ledger",
                    "name": proc["taxonomy"].replace("/", "-"),
                    "pid": proc["pid"],
                    "tty": proc["tty"],
                    "cwd": proc["cwd"],
                })
        elif not any(by_name.get(c) for c in cands):
            notes.append({
                "why": "live pi with no ledger row at all (untracked)",
                "pid": proc["pid"], "tty": proc["tty"], "cwd": proc["cwd"],
            })
    # ── RULE E (meta-fleet-fork-audit-01, 2026-10-26) ─────────────────────
    # A row can be internally consistent (pid + its own start time) and still name a
    # DEAD process for a session that is ALIVE: a session re-attached under a new pid
    # after its original spawner died. Rules A-D cannot see that -- A only looks at
    # finished rows, D only at transcripts -- so a live session reads dead, and dead
    # is the one reading every other rule treats as safe to close.
    # Measured: harbor-fork-1's row said pid 2050350 (absent from /proc) while the
    # session was alive as 2987907 on pts/30.
    # FIRST-CLASS ATTRIBUTION: a spawner that exports PI_SESSION_ID makes /proc alone
    # sufficient — no store, so no store error to inherit. Checked before the index
    # because the index is the thing a probe must be able to contradict.
    _env_reported = set()
    for proc in procs:
        sid = proc.get("session_id")
        if not sid:
            continue
        rec = children.get(sid)
        if not isinstance(rec, dict) or is_terminal(rec):
            continue
        row_pid = rec.get("pid")
        if row_pid is None or int(row_pid) == proc["pid"]:
            continue
        if os.path.isdir(f"/proc/{int(row_pid)}"):
            continue
        violations.append({
            "rule": "E",
            "why": "row names a dead process; /proc/env says this pid runs that session",
            "name": rec.get("name") or sid,
            "lineage_key": sid,
            "row_pid": int(row_pid),
            "live_pid": proc["pid"],
            "session_id": sid,
            "source": "PROC_ENV",
            "tty": proc["tty"],
        })
        _env_reported.add(sid)
    for name, entry in (index or {}).items():
        new_pid = entry.get("pid")
        if not new_pid:
            continue
        try:
            if not os.path.isdir(f"/proc/{int(new_pid)}"):
                continue                      # the index itself is stale
        except (TypeError, ValueError):
            continue
        if entry.get("sessionId") in _env_reported:
            continue                          # already proven from /proc alone
        for key, rec in by_name.get(name, []):
            if is_terminal(rec):
                continue
            row_pid = rec.get("pid")
            if row_pid in (None, new_pid):
                continue
            try:
                row_pid = int(row_pid)
            except (TypeError, ValueError):
                continue
            if os.path.isdir(f"/proc/{row_pid}"):
                continue                      # both alive: ambiguous, not a contradiction
            violations.append({
                "rule": "E",
                "why": "row names a dead process for a session that is alive under another pid",
                "name": name,
                "lineage_key": key,
                "row_pid": row_pid,
                "live_pid": int(new_pid),
                "session_id": entry.get("sessionId"),
                "tty": entry.get("tty"),
            })
    return violations, notes


def save_lineage(children, path=LINEAGE):
    """Write the store atomically (tmp + replace). Same writer rule as the fleet CLI."""
    import tempfile
    data = json.load(open(path)) if os.path.exists(path) else {"children": {}}
    data["children"] = children
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path))
    with os.fdopen(fd, "w") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--lineage", default=LINEAGE)
    ap.add_argument("--fix-reattach", action="store_true",
                    help="refresh rows that rule E proved were re-attached (opt-in writer; "
                         "cron stays read-only)")
    ap.add_argument("--selftest", action="store_true",
                    help="prove the detector fires: synthesise one terminal row for a live pid")
    args = ap.parse_args()

    children = load_lineage(args.lineage)
    procs = live_pis()
    index, index_err = live_index()

    if args.selftest:
        if not procs:
            print("selftest needs at least one live pi", file=sys.stderr)
            return 2
        victim = next((p for p in procs if name_candidates(p)), procs[0])
        named = sorted(name_candidates(victim))
        fake_name = named[0] if named else f"pid-{victim['pid']}"
        now = victim.get("started_at") or 1_700_000_000
        cases = {}
        # (a) pid row that really is this process (start time agrees) -> must fire
        probe_a = dict(children)
        probe_a[f"pid-{victim['pid']}"] = {"name": fake_name, "verdict": "reaped",
                                          "closed_at": 1, "pid": victim["pid"],
                                          "spawned_at": now}
        cases["pid_row_current"] = any(v["pid"] == victim["pid"] for v in audit(probe_a, procs)[0])
        # (b) same pid number, wrong process (2020) -> must NOT fire
        probe_b = dict(children)
        probe_b[f"pid-{victim['pid']}"] = {"name": "pid-reuse-probe", "verdict": "reaped",
                                          "closed_at": 1, "pid": victim["pid"],
                                          "spawned_at": 1_600_000_000}
        cases["pid_row_reused_ignored"] = not any(v["pid"] == victim["pid"] for v in audit(probe_b, procs)[0])
        # (c) name-only terminal row -> must fire. Needs a worktree-shaped cwd to
        # derive a name from; when no live session has one, say so instead of
        # pretending the case passed.
        if named:
            probe_c = dict(children)
            probe_c["selftest-name"] = {"name": fake_name, "verdict": "reaped", "closed_at": 1}
            cases["name_row"] = any(v["pid"] == victim["pid"] for v in audit(probe_c, procs)[0])
        else:
            cases["name_row"] = "skipped: no live process has a worktree-shaped cwd"
        # (d) THE ROW NAMES THE WRONG TRANSCRIPT (larch, 2026-10-26). The off-by-one
        # that made every row name its predecessor's session was invisible to rule A:
        # the row was open and its process alive, so nothing was "finished". Case (d)
        # takes the victim's own open row and swaps in another session's transcript;
        # the detector must call that a contradiction and name both ids.
        live_rows = [(k, r) for k, r in children.items()
                     if isinstance(r, dict) and r.get("pid") == victim["pid"]
                     and not is_terminal(r) and r.get("transcript")]
        if live_rows:
            key, rec = live_rows[0]
            own = str(rec.get("transcript"))
            others = [str(r.get("transcript")) for k, r in children.items()
                      if isinstance(r, dict) and k != key and r.get("transcript")
                      and str(r.get("transcript")) != own]
            if others:
                probe_d = dict(children)
                probe_d[key] = {**rec, "transcript": others[0]}
                caught = [v for v in audit(probe_d, procs)[0]
                          if v["rule"] == "D" and v["pid"] == victim["pid"]]
                cases["wrong_transcript"] = bool(caught)
                if caught:
                    cases["wrong_transcript_names_both"] = bool(
                        caught[0].get("row_session_id") and caught[0].get("transcript_session_id"))
            else:
                cases["wrong_transcript"] = "skipped: no second transcript on disk to swap in"
        else:
            cases["wrong_transcript"] = "skipped: no open row with a transcript for this pid"
        # (e) ROW NAMES A DEAD PID FOR A LIVE SESSION (rule E). Synthesise the
        # re-attach: the row claims a pid that is not running while the index says the
        # session lives at the victim's pid. Must fire, and must name both pids.
        if named:
            probe_e = dict(children)
            probe_e["selftest-reattach"] = {"name": fake_name, "pid": 9999999,
                                            "session_id": f"selftest-{victim['pid']}"}
            fake_index = {fake_name: {"pid": victim["pid"],
                                      "sessionId": f"selftest-{victim['pid']}",
                                      "tty": victim["tty"]}}
            caught_e = [v for v in audit(probe_e, procs, fake_index)[0]
                        if v["rule"] == "E"]
            cases["dead_pid_for_live_session"] = bool(caught_e)
            cases["dead_pid_names_both"] = bool(
                caught_e and caught_e[0].get("row_pid") and caught_e[0].get("live_pid"))
        else:
            cases["dead_pid_for_live_session"] = "skipped: no nameable live process"
        verdict = "pass" if all(c is True or isinstance(c, str) for c in cases.values()) else "FAIL"
        verdict = "pass" if all(c is True or str(c).startswith("skipped") for c in cases.values()) and any(c is True for c in cases.values()) else "FAIL"
        print(json.dumps({
            "selftest": verdict,
            "victim_pid": victim["pid"], "name": fake_name, "cases": cases,
            "violations_without": len(audit(children, procs, index)[0]),
        }, indent=2))
        return 0 if verdict == "pass" else 1

    if args.fix_reattach:
        fixed = []
        for v in audit(children, procs, index)[0]:
            if v["rule"] != "E":
                continue
            rec = children.get(v["lineage_key"])
            if not isinstance(rec, dict):
                continue
            live = v["live_pid"]
            ticks, started = proc_identity_of(live)
            children[v["lineage_key"]] = {
                **rec,
                "pid": live,
                "pid_starttime": ticks,
                "started_at": started,
                "spawn_pid": rec.get("spawn_pid") or rec.get("pid"),
                # Idempotent: repeated refreshes must not accrete "+reattached" tags.
                "identity_source": "spawn+reattached",
                "reattached_at": int(time.time()),
            }
            fixed.append((v["name"], v.get("row_pid"), live))
        if fixed:
            save_lineage(children)
        print(json.dumps({"reattach_fixed": fixed}, indent=2))
        return 0

    violations, notes = audit(children, procs, index)
    if index_err:
        notes.append({"why": index_err, "pid": 0, "tty": "-", "cwd": "-"})
    if args.json:
        print(json.dumps({"live_pis": len(procs), "violations": violations, "notes": notes}, indent=2))
    elif not args.quiet:
        print(f"live pi processes: {len(procs)}   violations: {len(violations)}   untracked: {len(notes)}")
        for v in violations:
            pid_field = v.get("pid") if v.get("pid") is not None else f"{v.get('row_pid')}->{v.get('live_pid')}"
            print(f"  RULE {v['rule']}  {v['name']:28} pid={str(pid_field):7} {str(v.get('tty') or '-'):7} {v['why']}"
                  f"  verdict={v.get('verdict')}")
        for n in notes:
            print(f"  note   {'(untracked)':28} pid={n['pid']:7} {n['tty']:7} {n['why']}")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
