#!/usr/bin/env python3
"""Invariant C: every live tmux server must be reachable at a socket path.

Why this exists (2026-10-26): a second tmux server replaced the socket file
/tmp/tmux-0/default. The first server kept running with 22 pane children and no
way to reach them: `tmux attach` saw nothing, connecting through
/proc/<pid>/fd/6 was refused, and the panes were only discovered hours later by
accident. 20 live pi sessions were invisible for ~1.5h and 6 died unattributed
in the gap. Nothing in the fleet noticed, because every existing check asks the
tmux binary or the lineage store -- both of which only see the *reachable*
server.

Detection is exact, not heuristic: a reachable server tells you its own pid.
Any live tmux server pid that no socket path reports back to is, by definition,
holding panes nobody can see.

Exit codes: 0 no orphans, 1 orphaned server(s) found, 2 could not read a source.
"""
import argparse
import json
import os
import subprocess
import sys
import glob

CANDIDATE_SOCKETS = ("/tmp/tmux-*/*", "/run/tmux/*/*", "/root/tmux")


def live_tmux_servers():
    """pid -> argv of every running tmux server, found in /proc only."""
    out = []
    for dirent in os.listdir("/proc"):
        if not dirent.isdigit():
            continue
        pid = int(dirent)
        try:
            raw = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            argv = [a.decode("utf-8", "replace") for a in raw if a]
        except OSError:
            continue
        if not argv or os.path.basename(argv[0]) != "tmux":
            continue
        try:
            comm = open(f"/proc/{pid}/comm").read().strip()
        except OSError:
            comm = ""
        # The kernel renames a server's comm to "tmux: server"; a client stays
        # "tmux: client" and its argv still says `tmux attach ...`. Only a server
        # can hold panes, and only a server can be orphaned by a replaced socket.
        if "server" not in comm:
            continue
        out.append({"pid": pid, "argv": " ".join(argv)[:120], "comm": comm})
    return out


def socket_paths():
    """Every path a tmux server believes it is listening on.

    Globs alone are not enough: a server started with TMUX_TMPDIR binds
    somewhere else and would be reported as an orphan when it is perfectly
    reachable. /proc/net/unix (via ss) lists the path each server bound even
    after the file is unlinked, so the union is what must be tested for
    reachability. Unlinked paths simply fail to answer, which is correct.
    """
    found = set()
    for pattern in CANDIDATE_SOCKETS:
        for path in glob.glob(pattern):
            if os.path.exists(path):
                found.add(path)
    try:
        res = subprocess.run(["ss", "-xlpn"], capture_output=True, text=True, timeout=5)
        for line in res.stdout.splitlines():
            if "tmux: server" not in line:
                continue
            for token in line.split():
                if token.startswith("/") and "tmux" in token:
                    found.add(token)
    except (subprocess.TimeoutExpired, OSError):
        pass
    return sorted(found)


def server_pid_at(path, timeout=4):
    """Ask the server behind `path` for its own pid. None if nobody answers."""
    env = dict(os.environ)
    env.pop("TMUX", None)
    try:
        res = subprocess.run(
            ["tmux", "-S", path, "display-message", "-p", "#{pid}"],
            capture_output=True, text=True, timeout=timeout, env=env,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if res.returncode != 0:
        return None
    try:
        return int(res.stdout.strip())
    except ValueError:
        return None


def stat_ppid(pid):
    """ppid from /proc/<pid>/stat, parsed after the comm field.

    comm is parenthesised and may itself contain spaces and parentheses
    (a tmux server is literally '(tmux: server)'), so splitting the whole line
    shifts every later field and silently reads state instead of ppid.
    """
    try:
        raw = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    tail = raw.rsplit(")", 1)
    if len(tail) != 2:
        return None
    fields = tail[1].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def panes_of(pid):
    """Child processes of a tmux server = its panes. Read /proc, not tmux."""
    out = []
    for dirent in os.listdir("/proc"):
        if not dirent.isdigit():
            continue
        if stat_ppid(int(dirent)) != pid:
            continue
        try:
            argv = open(f"/proc/{dirent}/cmdline", "rb").read().split(b"\0")
            argv = [a.decode("utf-8", "replace") for a in argv if a]
        except OSError:
            argv = []
        out.append({"pid": int(dirent), "argv": " ".join(argv)[:90]})
    return out


def _is_pi(pid):
    try:
        return open(f"/proc/{pid}/comm").read().strip() == "pi"
    except OSError:
        return False


def _tty_of(pid):
    for fd in (0, 1, 2):
        try:
            return os.readlink(f"/proc/{pid}/fd/{fd}").replace("/dev/", "")
        except OSError:
            continue
    return "?"


def pi_descendants(pid):
    """pi sessions trapped in this server: a pane may BE a pi, or wrap one."""
    found = []
    for pane in panes_of(pid):
        if _is_pi(pane["pid"]):
            found.append({"pid": pane["pid"], "tty": _tty_of(pane["pid"])})
            continue
        for dirent in os.listdir("/proc"):
            if not dirent.isdigit():
                continue
            if stat_ppid(int(dirent)) != pane["pid"]:
                continue
            if _is_pi(int(dirent)):
                found.append({"pid": int(dirent), "tty": _tty_of(int(dirent))})
    return found


def audit():
    servers = live_tmux_servers()
    paths = socket_paths()
    reachable = {}
    for path in paths:
        pid = server_pid_at(path)
        if pid is not None:
            reachable[pid] = path
    orphans = []
    known = {s["pid"] for s in servers}
    for pid in sorted(reachable):
        if pid not in known:
            # reachable but not a live server anymore: transient, ignore
            pass
    for server in servers:
        if server["pid"] in reachable:
            continue
        if server["comm"] and "tmux" not in server["comm"] and "tmux" not in server["argv"]:
            continue
        orphans.append({
            "pid": server["pid"],
            "argv": server["argv"],
            "panes": panes_of(server["pid"]),
            "trapped_pi": pi_descendants(server["pid"]),
        })
    return {"servers": servers, "sockets": paths, "reachable": reachable,
            "orphans": orphans}


def selftest():
    """Prove the comparison, not the environment: same servers, two truths.

    When no socket path reports a server, it must be a violation. When every
    server is reported, there must be none. This fails if the orphan logic is
    ever reverted to "trust tmux list-sessions", which is what missed the
    original incident.
    """
    global server_pid_at
    real = server_pid_at
    servers = live_tmux_servers()
    if not servers:
        print("selftest needs at least one live tmux server", file=sys.stderr)
        return 2
    victim = servers[0]["pid"]

    server_pid_at = lambda path, timeout=4: None
    blind = audit()["orphans"]
    server_pid_at = lambda path, timeout=4: victim
    sighted = [o for o in audit()["orphans"] if o["pid"] == victim]
    server_pid_at = real

    cases = {
        "unreachable_server_flagged": any(o["pid"] == victim for o in blind),
        "reachable_server_not_flagged": not sighted,
    }
    print(json.dumps({"selftest": "pass" if all(cases.values()) else "fail",
                      "victim_pid": victim, **cases}, indent=1))
    return 0 if all(cases.values()) else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    report = audit()
    if args.json:
        print(json.dumps(report, indent=1))
        return 1 if report["orphans"] else 0

    if not report["orphans"]:
        if not args.quiet:
            print(f"ok: {len(report['servers'])} tmux server(s), "
                  f"{len(report['reachable'])} reachable, 0 orphaned")
        return 0

    for orphan in report["orphans"]:
        trapped = orphan["trapped_pi"]
        print(f"VIOLATION rule C: tmux server pid {orphan['pid']} is live but "
              f"unreachable — no socket path reports it")
        print(f"  argv           : {orphan['argv']}")
        print(f"  panes          : {len(orphan['panes'])}")
        print(f"  trapped pi     : {len(trapped)}")
        for p in trapped:
            print(f"    pid {p['pid']:>8} {p['tty']}")
        print(f"  reachable pids : {sorted(report['reachable'])}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
