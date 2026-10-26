#!/usr/bin/env python3
"""Resume ONE dark pi session into a visible pane, or refuse and say why.

WHY THIS EXISTS
    2026-10-26: a second tmux server replaced the socket at
    /tmp/tmux-0/default. The first server (pid 3406616) kept running with 13 live
    pi panes and no reachable socket. Rule C (`fleet_tmux_orphan_guard.py`) can
    DETECT that state; it cannot get a session out of it. The only known route
    was written down as prose in a brief:

        SIGTERM -> SIGINT -> SIGKILL the dark pi, recreate the tmux session in
        the reachable server, `pi --session <uuid> --name <name>`

    Prose is not a tool. Thirteen hand-typed sequences of irreversible steps on
    Maestro's own conversations, with no dry run and no verification, is how a
    recovery becomes an outage. This is the same route with the two things prose
    cannot have: a dry run, and a check after every irreversible step.

WHAT IT DOES NOT DO
    * It does not re-implement orphan detection. Detection is rule C
      (`fleet_tmux_orphan_guard.py --json`), called as a subprocess; a second
      detector is how two answers to "which sessions are dark" start existing.
    * It does not re-implement the spawn. `respawn-pi-session` already this year
      solved env forwarding, identity non-inheritance, tmux prefix matching,
      per-cwd session storage and spawn-then-die detection. Re-deriving them
      here would drift within a week.
    * It never touches the orphaned tmux server process itself. It signals one
      pi pid per invocation, the one you named, and nothing else.

REFUSALS (all before any signal is sent)
    * transcript missing / empty        -> nothing to resume; a resume would open
      a blank session and quietly lose the conversation.
    * the pid is reachable              -> that session needs no recovery; a
      resume would create a SECOND writer on the same transcript.
    * another live pid owns the session -> same reason, different process.
    * the tmux name is already live     -> `respawn-pi-session` would refuse this
      anyway; refusing it here means the dry run is honest.
    * can't read the orphan guard       -> unknown state is not "safe".

VERBS
    --list              every live pi whose pane belongs to an orphaned tmux
                        server: name, sessionId, pid, tty, transcript, bytes,
                        last-modified.
    --dry-run <name>    the exact ordered plan: signals and waits, the spawn
                        command, and every check after each step. No side effects.
    --resume <name>     execute the plan, verify, print before/after.

Exit codes: 0 ok / nothing to do · 1 refused or verification failed · 2 usage or
unreadable source.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

import fleet_paths

# Every location below is resolved, once, from the one seam. HOME stays for the
# things that genuinely belong to the user rather than to pi.
HOME = os.path.expanduser("~")
_AGENT = str(fleet_paths.agent_home())
_BIN = str(fleet_paths.bin_dir())
GUARD = os.path.join(_BIN, "fleet_tmux_orphan_guard.py")
RESPAWN = os.path.join(_BIN, "respawn-pi-session")
RECORDS = os.path.join(_AGENT, "session-watch", "records")
NAMES = os.path.join(_AGENT, "session-names")
SESSIONS = os.path.join(_AGENT, "sessions")

PREFIX_HASH_BYTES = 64 * 1024   # rewrite detector: enough to be a fingerprint
# SIGTERM is a REQUEST, and pi's graceful path is slow on this box: measured
# 2026-10-26 on a throwaway, `kill -TERM <pi>` took 9.4s to exit because
# shutdown() awaits runtimeHost.dispose() (extension teardown: intercom, MCP,
# toragents sockets). A shorter wait escalates to SIGKILL on a process that was
# already shutting down cleanly -- i.e. it throws away the graceful bookkeeping
# for no reason. 20s is that measurement with room to spare.
TERM_WAIT = 20.0
KILL_WAIT = 5.0
READY_TIMEOUT = 25.0            # respawn-pi-session's own budget is 20s
# Phases where a kill cannot lose work. Anything else is a session with an
# in-flight turn, and SIGKILL there can truncate the last JSONL line.
SAFE_PHASES = {"", "idle", "ended", "waiting for input", "waiting"}


# ── /proc ────────────────────────────────────────────────────────────────────
def alive(pid):
    """A zombie is not alive: its tty and /proc entry outlive its work."""
    if not pid:
        return False
    try:
        raw = open(f"/proc/{pid}/stat").read()
    except OSError:
        return False
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return False
    fields = tail[1].split()
    if not fields:
        return False
    return fields[0] not in ("Z", "X")


def stat_fields(pid):
    """Fields after comm. comm is parenthesised and may contain spaces --
    a tmux server's comm is literally '(tmux: server)' -- so splitting the whole
    line shifts every later field. Same trap the guard documents."""
    try:
        raw = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return None
    return tail[1].split()


def pid_start(pid):
    """Start time in clock ticks. Together with pid it identifies a process:
    pids are reused on this box (two session records legitimately share one)."""
    fields = stat_fields(pid)
    if not fields or len(fields) < 20:
        return None
    try:
        return fields[19]
    except (IndexError, ValueError):
        return None


def ppid(pid):
    fields = stat_fields(pid)
    if not fields or len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def ppid_chain(pid, limit=12):
    out = []
    seen = set()
    cur = pid
    while cur and cur not in seen and len(out) < limit:
        seen.add(cur)
        cur = ppid(cur)
        if cur:
            out.append(cur)
    return out


def tty_of(pid):
    for fd in (0, 1, 2):
        try:
            return os.readlink(f"/proc/{pid}/fd/{fd}").replace("/dev/", "")
        except OSError:
            continue
    return "?"


def comm_of(pid):
    try:
        return open(f"/proc/{pid}/comm").read().strip()
    except OSError:
        return ""


def _pi_pids():
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if comm_of(pid) != "pi":
            continue
        if alive(pid):
            out.append(pid)
    return out


# ── the record readers ───────────────────────────────────────────────────────
def _load(path):
    try:
        with open(path) as fh:
            rec = json.load(fh)
        return rec if isinstance(rec, dict) else None
    except (OSError, ValueError):
        return None


def records():
    """(source, record) for every liveness record and every name registry entry.

    Both are read because neither is complete on its own: `nordisk-ads` (pid
    885051) has a name-registry entry and NO liveness record, so a reader that
    trusts one source reports a session with no transcript to resume.
    """
    out = []
    for path in sorted(glob.glob(os.path.join(RECORDS, "*.json"))):
        rec = _load(path)
        if rec:
            out.append(("record", rec))
    for path in sorted(glob.glob(os.path.join(NAMES, "*.json"))):
        rec = _load(path)
        if rec:
            out.append(("registry", rec))
    return out


def find_transcript(session_id):
    hits = sorted(glob.glob(os.path.join(SESSIONS, "*", f"*_{session_id}.jsonl")))
    return hits[-1] if hits else ""


def resolve(pid):
    """pid -> the ONE session it is running.

    Matching is pid AND start time, then "not ended" wins. Pid 1479189 carries
    two records with the same pid and the same start time -- `engrane-seo` (live,
    52 turns, 4.7 MB transcript) and `basalt` (ended, 0 turns, transcript
    MISSING). A reader that takes the last record, or the most recently updated,
    gets `basalt` and then refuses to resume a session that is right there.
    """
    start = pid_start(pid)
    cands = []
    for source, rec in records():
        if rec.get("pid") != pid:
            continue
        rec_start = rec.get("pidStart")
        exact = rec_start is not None and start is not None and str(rec_start) == str(start)
        cands.append({
            "source": source,
            "name": rec.get("name") or "",
            "sessionId": rec.get("sessionId") or "",
            "cwd": rec.get("cwd") or "",
            "ended": bool(rec.get("endedAt")),
            "exact_start": exact,
            "updatedAt": rec.get("updatedAt") or 0,
            "transcript": rec.get("transcript") or "",
        })
    if not cands:
        return None
    # live-and-exact > live > exact > anything (report the raw record honestly)
    cands.sort(key=lambda c: (c["ended"] is False, c["exact_start"], c["updatedAt"]),
               reverse=True)
    best = dict(cands[0])
    # phase lives only in the liveness record, never in the name registry, and the
    # two are both in `records()`. Stopping at the first sessionId match picks the
    # registry entry and reports "no phase" for a session that is mid-turn -- the
    # exact case the phase check exists to catch.
    best.setdefault("phase", "")
    for _source, rec in records():
        if rec.get("sessionId") != best["sessionId"]:
            continue
        if rec.get("phase"):
            best["phase"] = rec["phase"]
    if not best["transcript"] and best["sessionId"]:
        best["transcript"] = find_transcript(best["sessionId"])
    best["pid"] = pid
    best["pidStart"] = start
    best["tty"] = tty_of(pid)
    best["candidates"] = cands
    tpath = best["transcript"]
    if tpath and os.path.exists(tpath):
        st = os.stat(tpath)
        best["bytes"] = st.st_size
        best["mtime"] = st.st_mtime
        best["inode"] = st.st_ino
    else:
        best["bytes"] = 0
        best["mtime"] = None
        best["inode"] = None
    return best


# ── rule C: who is dark ──────────────────────────────────────────────────────
def guard_report():
    """Rule C's own answer, as a subprocess. Returns (report, err).

    Exit 1 means "orphans found" and is a SUCCESS here -- it is the state this
    tool exists to fix. Exit 2 (cannot read a source) is the fatal one.
    """
    try:
        res = subprocess.run([sys.executable, GUARD, "--json"],
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"could not run rule C ({GUARD}): {exc}"
    if res.returncode not in (0, 1):
        return None, f"rule C failed (exit {res.returncode}): {res.stderr.strip()[:400]}"
    try:
        return json.loads(res.stdout), None
    except ValueError:
        return None, "rule C printed no JSON"


def reachable_socket(report):
    """The socket path of the REACHABLE server. Verification must not depend on
    the caller's TMUX variable: run this tool from inside a dark pane and the
    default socket is the wrong one."""
    paths = sorted(report.get("reachable", {}).values())
    return paths[0] if paths else "/tmp/tmux-0/default"


def tmux(socket, args, timeout=15):
    env = dict(os.environ)
    for var in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"):
        env.pop(var, None)
    try:
        res = subprocess.run(["tmux", "-S", socket] + args, capture_output=True,
                             text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def dark_sessions():
    """Every live pi trapped in a server no socket path reports, one row per pid."""
    report, err = guard_report()
    if err:
        return None, err
    rows = []
    seen = set()
    for orphan in report.get("orphans", []):
        for trapped in orphan.get("trapped_pi", []):
            pid = trapped.get("pid")
            if not pid or pid in seen:
                continue
            seen.add(pid)
            if not alive(pid):
                continue
            rec = resolve(pid)
            row = {
                "pid": pid,
                "tty": trapped.get("tty") or tty_of(pid),
                "orphan_server": orphan.get("pid"),
                "name": (rec or {}).get("name") or "?",
                "sessionId": (rec or {}).get("sessionId") or "?",
                "transcript": (rec or {}).get("transcript") or "",
                "bytes": (rec or {}).get("bytes") or 0,
                "mtime": (rec or {}).get("mtime"),
                "cwd": (rec or {}).get("cwd") or "",
                "phase": (rec or {}).get("phase") or "",
                "record": rec,
            }
            # Only genuinely DIFFERENT sessions are worth flagging. The same
            # session is recorded twice by design (liveness record + name
            # registry), so a raw candidate count would call every healthy row
            # ambiguous and the warning would be ignored.
            distinct = {}
            for cand in (rec or {}).get("candidates", []):
                key = cand["sessionId"] or f"no-id:{cand['source']}"
                prev = distinct.get(key)
                if prev is None or (prev["ended"] and not cand["ended"]):
                    distinct[key] = cand
            if len(distinct) > 1:
                row["ambiguous"] = [
                    f"{c['name'] or '(unnamed)'}"
                    f"{' ended' if c['ended'] else ''}"
                    f"{'' if c['exact_start'] else ' pid-reused'}"
                    for c in distinct.values()
                ]
            rows.append(row)
    rows.sort(key=lambda r: r["name"])
    return rows, None


def find_dark(rows, name):
    exact = [r for r in rows if r["name"] == name]
    if len(exact) == 1:
        return exact[0], None
    if len(exact) > 1:
        return None, f"{len(exact)} dark sessions are named {name!r}: " + \
            ", ".join(f"pid {r['pid']}" for r in exact)
    partial = [r for r in rows if r["name"].startswith(name)]
    if len(partial) == 1:
        return partial[0], None
    if len(partial) > 1:
        return None, f"{name!r} is ambiguous: " + ", ".join(r["name"] for r in partial)
    # Fall back to pid or sessionId, so `--resume <pid>` works when a name is
    # empty (unnamed sessions are the common case in the dark set).
    by_pid = [r for r in rows if str(r["pid"]) == str(name)]
    if len(by_pid) == 1:
        return by_pid[0], None
    by_sid = [r for r in rows if r["sessionId"].startswith(str(name))]
    if len(by_sid) == 1:
        return by_sid[0], None
    return None, f"no dark session matches {name!r} ({len(rows)} dark rows)"


# ── the checks ───────────────────────────────────────────────────────────────
def reachable_now(pid, report=None):
    """Is this pid's terminal answerable right now? Two independent tests.

    1. its tmux server ancestor must not be one rule C calls orphaned, and
       should be one whose socket answers.
    2. its tty must appear in the REACHABLE server's pane list. A process can
       have a pts with no reachable pane (that is exactly the dark state), so
       the ancestor test alone is not enough -- rule C's server list is read from
       /proc and would still be right if a socket appeared.
    """
    report = report or guard_report()[0] or {}
    reachable = {int(k) for k in report.get("reachable", {})}
    chain = ppid_chain(pid)
    server = None
    for ancestor in chain:
        if "tmux" in comm_of(ancestor) or comm_of(ancestor).startswith("tmux"):
            server = ancestor
            break
    socket = reachable_socket(report)
    panes = tmux(socket, ["list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}\t#{pane_tty}"]) or ""
    ttys = set()
    pane_pids = set()
    for line in panes.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        ttys.add(parts[2].replace("/dev/", ""))
        try:
            pane_pids.add(int(parts[1]))
        except ValueError:
            pass
    tty = tty_of(pid)
    return {
        "server": server,
        "server_reachable": server in reachable if server else False,
        "tty": tty,
        "tty_in_reachable_pane": tty in ttys,
        "pane_pids": pane_pids,
        "reachable": reachable,
        "socket": socket,
    }


def other_writers(session_id, pid):
    """Any OTHER live pid claiming the same session = a second writer already."""
    out = []
    if not session_id:
        return out
    for _source, rec in records():
        if rec.get("sessionId") != session_id:
            continue
        other = rec.get("pid")
        if not other or other == pid or not alive(other):
            continue
        start = pid_start(other)
        if rec.get("pidStart") and start and str(rec["pidStart"]) != str(start):
            continue  # pid reused by an unrelated process
        out.append({"pid": other, "name": rec.get("name") or "",
                    "ended": bool(rec.get("endedAt"))})
    return out


def tmux_has(socket, name):
    res = tmux(socket, ["has-session", "-t", f"={name}"])
    return res is not None


def preflight(row, report=None, force=False):
    """Everything that must be true BEFORE a signal. Returns (ok, problems, ctx)."""
    rec = row.get("record") or {}
    problems = []
    session_id = row.get("sessionId") or ""
    tpath = row.get("transcript") or ""
    if not session_id or session_id == "?":
        problems.append("no sessionId in any record: refusing to resume blind")
    if not tpath:
        problems.append("no transcript path in any record")
    elif not os.path.exists(tpath):
        problems.append(f"transcript is missing: {tpath}")
    elif os.path.getsize(tpath) == 0:
        problems.append(f"transcript is EMPTY: {tpath} -- a resume here would "
                        f"open a blank session and lose the conversation")
    now = reachable_now(row["pid"], report)
    if now["server_reachable"] or now["tty_in_reachable_pane"]:
        problems.append(
            "the old pid is still visible-and-reachable "
            f"(tmux server {now['server']} answers at {now['socket']}, "
            f"tty {now['tty']} is a live pane): this session needs no recovery")
    others = other_writers(session_id, row["pid"])
    if others:
        problems.append("another live pid already owns this session (a second "
                        "writer would corrupt the transcript): " +
                        ", ".join(f"pid {o['pid']} ({o['name'] or 'unnamed'})" for o in others))
    name = row.get("name") or ""
    if name and name.lower() != "basalt":
        if tmux_has(now["socket"], name):
            problems.append(f"tmux session {name!r} already exists in the reachable "
                            f"server: a second writer, not a recovery")
    phase = (row.get("phase") or "").strip().lower()
    if phase and phase not in SAFE_PHASES and not force:
        problems.append(
            f"the recorded phase is {phase!r}, i.e. there is an in-flight turn. A "
            f"signal here loses that turn and can truncate the transcript's last "
            f"line. Wait for it to go idle, or pass --force to accept the loss")
    return (not problems), problems, now

def transcript_facts(tpath):
    try:
        st = os.stat(tpath)
    except OSError:
        return {"exists": False}
    prefix = b""
    try:
        with open(tpath, "rb") as fh:
            prefix = fh.read(PREFIX_HASH_BYTES)
    except OSError:
        pass
    lines = 0
    try:
        with open(tpath, "rb") as fh:
            for _ in fh:
                lines += 1
    except OSError:
        pass
    return {"exists": True, "bytes": st.st_size, "inode": st.st_ino,
            "mtime": st.st_mtime, "lines": lines,
            "prefix_len": len(prefix),
            "prefix_sha": hashlib.sha256(prefix).hexdigest()}


def prefix_sha(tpath, length):
    """sha256 of the first `length` bytes, or None if the file is shorter.

    Comparing full prefix snapshots is wrong for a short transcript: if the whole
    file is 16 bytes then `before.prefix_sha` covers those 16 bytes, and appending
    one line changes it -- so a legitimate grow would look like a rewrite. What
    must stay identical is the bytes that existed BEFORE the resume, so the
    comparison is always over `before['prefix_len']` bytes.
    """
    if length <= 0:
        return hashlib.sha256(b"").hexdigest()
    try:
        with open(tpath, "rb") as fh:
            data = fh.read(length)
    except OSError:
        return None
    if len(data) < length:
        return None
    return hashlib.sha256(data).hexdigest()


def activity(tpath, window=1.5):
    """Is this transcript being written RIGHT NOW?

    The liveness record's phase is not reliable enough on its own: measured
    2026-10-26, `nimbus` (pid 1924628) had a record saying `idle`/waiting since
    18:44 while its transcript was appended to at 19:05. A kill during a write is
    how a transcript loses its last turn, so the file itself is asked instead of
    the record. Read-only: two stats and a size comparison.
    """
    try:
        first = os.stat(tpath).st_size
    except OSError:
        return {"quiet": True, "grew": 0}
    time.sleep(window)
    try:
        second = os.stat(tpath).st_size
    except OSError:
        return {"quiet": True, "grew": 0, "vanished": True}
    return {"quiet": second == first, "grew": second - first, "window": window}


# ── the plan ─────────────────────────────────────────────────────────────────
def plan_steps(row, report=None):
    """The ordered, printed plan. Every irreversible step has a check behind it."""
    sid = row.get("sessionId") or ""
    pid = row["pid"]
    name = row.get("name") or ""
    tpath = row.get("transcript") or ""
    cwd = row.get("cwd") or str(fleet_paths.home())
    socket = reachable_socket(report or {})
    steps = [
        ("0. preflight (read-only)",
         f"transcript {tpath or '(none)'} exists and is non-empty; pid {pid} is not "
         f"reachable; no other live pid owns session {sid}; tmux name {name!r} is free"),
        ("1. SIGTERM", f"kill -TERM {pid}   # pi's own graceful path: session_shutdown "
                        f"hooks run, terminal restored, process exits 0"),
        ("2. wait", f"poll every 0.2s up to {TERM_WAIT:.0f}s. MEASURED 2026-10-26: "
                    f"graceful exit took 9.4s on this box, because shutdown awaits "
                    f"extension teardown (intercom/MCP/toragents sockets). Waiting "
                    f"less than that kills a process that was already exiting cleanly."),
        ("3. SIGKILL (only if SIGTERM did not land)", f"kill -KILL {pid}   # last resort: "
                                                     f"transcript stays valid (it is appended "
                                                     f"per turn) but the liveness record is "
                                                     f"left stale until the resumed pi rewrites it"),
        ("4. check: old pid gone",
         f"/proc/{pid} does not exist or the process is a zombie; if it is still alive "
         f"after SIGKILL the run aborts (nothing is spawned over a live writer)"),
        ("5. spawn in the REACHABLE server",
         f"respawn-pi-session --session {sid} --name {name} --json"
         f"{'' if not cwd else '  (cwd from the record: ' + cwd + ')'}"),
        ("6. check: new pid exists",
         f"pane_pid of tmux session ={name} in {socket} is alive "
         f"(respawn-pi-session refuses a spawn that dies within ~1s)"),
        ("7. check: tty is a pane of the reachable server",
         f"tty of the new pid appears in `tmux -S {socket} list-panes -a`"),
        ("8. check: the name registry points at the NEW pid",
         f"{NAMES}/<id-prefix>.json has pid == new pid and pidStart == its start time"),
        ("9. check: transcript GREW, not rewritten",
         f"{tpath}: same inode, first {PREFIX_HASH_BYTES} bytes unchanged in sha256, "
         f"byte count > before"),
        ("10. check: old pid gone (again)",
         f"no live process for {pid} or {sid}; print the before/after table"),
    ]
    if row.get("phase"):
        steps.insert(1, ("0b. turn state (read-only)",
                         f"recorded phase is {row['phase']!r}; only "
                         f"{sorted(p for p in SAFE_PHASES if p)} can be killed without "
                         f"losing work, so anything else refuses without --force"))
    return steps


def print_plan(row, report=None):
    print(f"PLAN — resume dark session {row['name']!r} (pid {row['pid']}, "
          f"session {row['sessionId']})")
    print(f"  orphan tmux server : {row['orphan_server']}")
    print(f"  tty / transcript   : {row['tty']}  {row['transcript']} "
          f"({row['bytes']} bytes)")
    print(f"  recorded phase     : {row.get('phase') or '(none in any record)'}")
    if row.get("transcript"):
        act = activity(row["transcript"])
        if act["quiet"]:
            print(f"  transcript activity: quiet (unchanged over {act.get('window', 1.5)}s)")
        else:
            print(f"  transcript activity: BEING WRITTEN (+{act['grew']} bytes in "
                  f"{act.get('window', 1.5)}s) -- the session is mid-turn even if its "
                  f"record says idle; SIGTERM will give it the graceful path, SIGKILL "
                  f"would cut the turn")
    ok, problems, now = preflight(row, report)
    if problems:
        print("  preflight          : REFUSED")
        for problem in problems:
            print(f"    - {problem}")
        print("  (a dry run sends no signal; --resume would refuse here)")
    else:
        print("  preflight          : pass")
    print()
    for title, detail in plan_steps(row, report):
        print(f"  {title}")
        print(f"      {detail}")
    print()
    print("  side effects of --resume: one pi pid is signalled (TERM, then KILL only if "
          "TERM did not land); one tmux session appears in the reachable server;")
    print("  the transcript file is appended to, never recreated.")


# ── execution ────────────────────────────────────────────────────────────────
def signal_and_wait(pid, sig, wait, label):
    """Send one signal, then poll. Returns True when the target is gone."""
    try:
        os.kill(pid, sig)
    except ProcessLookupError:
        print(f"    {label}: pid {pid} already gone before the signal")
        return True
    except PermissionError as exc:
        print(f"    {label}: cannot signal pid {pid}: {exc}")
        return False
    deadline = time.time() + wait
    while time.time() < deadline:
        if not alive(pid):
            print(f"    {label}: gone after {wait - (deadline - time.time()):.1f}s")
            return True
        time.sleep(0.2)
    print(f"    {label}: still alive after {wait:.0f}s")
    return False


def wait_for(fn, timeout, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(interval)
    return None


def registry_entry(session_id):
    """The name-registry entry for this session, as written by pi itself."""
    if not session_id:
        return None
    for path in glob.glob(os.path.join(NAMES, "*.json")):
        rec = _load(path)
        if rec and rec.get("sessionId") == session_id:
            return rec
    return None


def resume(row, report, timeout=READY_TIMEOUT, with_sigint=False, force=False):
    pid = row["pid"]
    sid = row.get("sessionId") or ""
    name = row.get("name") or ""
    tpath = row.get("transcript") or ""
    socket = reachable_socket(report)

    ok, problems, now = preflight(row, report, force=force)
    if not ok:
        print("REFUSED — nothing was signalled:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    before = transcript_facts(tpath)
    act = activity(tpath)
    print(f"BEFORE  name={name}  pid={pid}  tty={row['tty']}  session={sid}")
    print(f"        transcript bytes={before['bytes']} lines={before['lines']} "
          f"inode={before['inode']}")
    if not act["quiet"]:
        print(f"        NOTE: the transcript grew {act['grew']} bytes in "
              f"{act.get('window', 1.5)}s before the kill -- the in-flight turn is "
              f"lost, and the last JSONL line may be truncated")
    print()

    print("  1. SIGTERM")
    gone = signal_and_wait(pid, signal.SIGTERM, TERM_WAIT, "SIGTERM")
    if not gone and with_sigint:
        # NOT part of the default ladder. pi registers shutdown handlers for
        # SIGTERM and SIGHUP only (dist/modes/interactive/interactive-mode.js:3581);
        # SIGINT has no shutdown handler, and measured 2026-10-26 it did not end
        # the process -- it landed mid-teardown and left pi stuck past the point
        # where SIGTERM would have finished, so SIGKILL followed anyway. Kept as
        # an explicit opt-in for anyone who wants the old prose recipe verbatim.
        print("  2. SIGINT (--with-sigint)")
        gone = signal_and_wait(pid, signal.SIGINT, KILL_WAIT, "SIGINT")
    if not gone:
        print("  3. SIGKILL")
        gone = signal_and_wait(pid, signal.SIGKILL, KILL_WAIT, "SIGKILL")
    if not gone:
        print("ABORT — pid is still alive after SIGKILL. Nothing was spawned: a "
              "second writer on a live transcript is worse than a dark session.")
        return 1
    print(f"  check: old pid {pid} gone  -> pass")

    # Nothing must be spawned before the old writer is really gone. kill(0) is
    # not enough on its own: /proc can outlive the work for a moment.
    if wait_for(lambda: not alive(pid), 2.0) is None:
        print("ABORT — pid came back or is still alive; refusing to spawn.")
        return 1

    print()
    print(f"  4. spawn: respawn-pi-session --session {sid} --name {name}")
    env = dict(os.environ)
    # The spawn must land in the REACHABLE server. Inside a dark pane the
    # inherited TMUX would point at the orphaned socket; TMUX_TMPDIR could point
    # anywhere. Strip all three and let tmux resolve the default socket, which is
    # the one rule C reports as reachable on this box.
    for var in ("TMUX", "TMUX_PANE", "TMUX_TMPDIR"):
        env.pop(var, None)
    args = [RESPAWN, "--session", sid, "--name", name, "--json"]
    if row.get("cwd"):
        args += ["--cwd", row["cwd"]]
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=90, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"    spawn failed to run: {exc}")
        return 1
    spawn = {}
    try:
        spawn = json.loads(res.stdout or "{}")
    except ValueError:
        spawn = {}
    print(f"    respawn-pi-session exit {res.returncode} status={spawn.get('status', '?')}")
    if res.returncode != 0 or spawn.get("status") not in ("spawned", "already-live"):
        tail = (res.stderr or "").strip()[:600]
        print(f"    spawn failed: {tail or res.stdout.strip()[:600]}")
        print("    The old pid is GONE and nothing replaced it. The transcript is "
              "untouched and resumable — see the report for the manual fallback.")
        return 1
    if spawn.get("status") == "already-live":
        print("    refused: that tmux session name is already live in the reachable "
              "server. Nothing was spawned.")

    tmux_name = spawn.get("tmux") or name

    def _pane_pid():
        out = tmux(socket, ["list-panes", "-t", f"={tmux_name}", "-F", "#{pane_pid}"])
        if not out:
            return None
        try:
            newpid = int(out.split()[0])
        except (ValueError, IndexError):
            return None
        return newpid if alive(newpid) else None

    newpid = wait_for(_pane_pid, timeout)
    if not newpid:
        print(f"  check: new pid exists -> FAIL (no live pane in ={tmux_name} "
              f"within {timeout:.0f}s)")
        return 1
    print(f"  check: new pid exists -> pass (pid {newpid}, comm {comm_of(newpid)!r})")

    out = tmux(socket, ["list-panes", "-a", "-F",
                        "#{session_name}\t#{pane_pid}\t#{pane_tty}"]) or ""
    ttys = {}
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            try:
                ttys[int(parts[1])] = (parts[0], parts[2].replace("/dev/", ""))
            except ValueError:
                pass
    pane = ttys.get(newpid)
    if not pane:
        print(f"  check: tty in reachable server -> FAIL (pid {newpid} is not a pane "
              f"of {socket})")
        return 1
    print(f"  check: tty in reachable server -> pass (tty {pane[1]}, "
          f"session ={pane[0]})")

    def _registry():
        rec = registry_entry(sid)
        if not rec or rec.get("pid") != newpid:
            return None
        return rec

    reg = wait_for(_registry, 20.0)
    if not reg:
        rec = registry_entry(sid)
        print(f"  check: name registry -> FAIL (entry says pid "
              f"{(rec or {}).get('pid')} not {newpid})")
        return 1
    print(f"  check: name registry -> pass (pid {reg.get('pid')}, "
          f"pidStart {reg.get('pidStart')})")

    # The transcript check is the one that decides whether the conversation
    # survived. A rewrite (new inode, different prefix) is a lost history even
    # though the pid, the tty and the registry all look right.
    def _grew():
        after = transcript_facts(tpath)
        if not after.get("exists"):
            return None
        if after["inode"] != before["inode"]:
            return None
        if after["prefix_sha"] != before["prefix_sha"]:
            return None
        # The bytes that existed before the resume must be untouched. Re-read
        # exactly that many, not the (now longer) prefix window.
        if prefix_sha(tpath, before["prefix_len"]) != before["prefix_sha"]:
            return None
        return after if after["bytes"] >= before["bytes"] else None

    after = wait_for(_grew, 20.0)
    if not after:
        now_facts = transcript_facts(tpath)
        print(f"  check: transcript grew -> FAIL (before {before['bytes']}B/"
              f"inode {before['inode']}, after {now_facts.get('bytes')}B/"
              f"inode {now_facts.get('inode')})")
        return 1
    grew = after["bytes"] - before["bytes"]
    print(f"  check: transcript grew -> pass (+{grew} bytes, inode unchanged, "
          f"prefix sha256 unchanged)")
    print(f"  check: old pid gone -> pass ({pid} absent)")

    print()
    print("BEFORE / AFTER")
    print(f"  {'':14} {'BEFORE':>28}   {'AFTER':>28}")
    rows = [
        ("pid", str(pid), str(newpid)),
        ("tty", row["tty"], pane[1]),
        ("tmux server", f"orphan {row['orphan_server']}", f"reachable {socket}"),
        ("tmux session", "(unreachable)", f"={pane[0]}"),
        ("registry pid", str(pid), str(reg.get("pid"))),
        ("transcript bytes", str(before["bytes"]), str(after["bytes"])),
        ("transcript lines", str(before["lines"]), str(after["lines"])),
        ("reachable by name", "no", "yes — pi-peers / intercom see it"),
    ]
    for label, old, new in rows:
        print(f"  {label:<20} {old:>28}   {new:>28}")
    print()
    print(f"RESUMED {name} as pid {newpid} in tmux ={pane[0]} (tty {pane[1]}).")
    print(f"Attach with:  tmux attach -t ={pane[0]}")
    return 0


# ── output ───────────────────────────────────────────────────────────────────
def fmt_age(epoch):
    if not epoch:
        return "?"
    delta = max(0, time.time() - epoch)
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if delta >= size:
            return f"{delta / size:.0f}{unit}"
    return f"{delta:.0f}s"


def print_rows(rows):
    if not rows:
        print("no dark sessions: every live pi's pane is on a reachable tmux server "
              "(rule C clean)")
        return
    print(f"{len(rows)} dark pi session(s) — live, but in a tmux server no socket "
          f"reaches (rule C):")
    print()
    header = f"{'name':<22} {'sessionId':<14} {'pid':>8} {'tty':<8} {'bytes':>9} {'age':>5}  transcript"
    print(header)
    print("-" * len(header))
    for row in rows:
        sid = row["sessionId"]
        print(f"{(row['name'] or '?'):<22} {sid[:12]:<14} {row['pid']:>8} "
              f"{row['tty']:<8} {row['bytes']:>9} {fmt_age(row['mtime']):>5}  "
              f"{row['transcript'] or '(none found)'}")
        if row.get("ambiguous"):
            print(f"{'':<22} ambiguous records: {', '.join(row['ambiguous'])}")


def main():
    ap = argparse.ArgumentParser(description="resume one dark pi session safely")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", metavar="NAME")
    ap.add_argument("--resume", metavar="NAME")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--timeout", type=float, default=READY_TIMEOUT)
    ap.add_argument("--force", action="store_true",
                    help="override ONLY the in-flight-turn refusal")
    ap.add_argument("--with-sigint", action="store_true",
                    help="insert the old prose recipe's SIGINT step after SIGTERM")
    args = ap.parse_args()

    if not (args.list or args.dry_run or args.resume):
        ap.print_help()
        return 2

    rows, err = dark_sessions()
    if err:
        print(f"cannot determine the dark set: {err}", file=sys.stderr)
        return 2

    if args.list:
        if args.json:
            print(json.dumps([{k: v for k, v in r.items() if k != "record"} for r in rows],
                             indent=1))
        else:
            print_rows(rows)
        return 0

    target = args.dry_run or args.resume
    row, err = find_dark(rows, target)
    if err:
        print(f"refused: {err}", file=sys.stderr)
        return 1
    report, err = guard_report()
    if err:
        print(f"cannot read the orphan guard, so state is unknown: {err}",
              file=sys.stderr)
        return 2

    if args.dry_run:
        print_plan(row, report)
        return 0
    return resume(row, report, timeout=args.timeout, with_sigint=args.with_sigint,
                  force=args.force)


if __name__ == "__main__":
    sys.exit(main())
