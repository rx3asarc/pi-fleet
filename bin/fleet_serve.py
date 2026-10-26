#!/usr/bin/env python3
"""fleet_serve — serve a directory, and be able to prove WHICH directory it is serving.

WHY THIS EXISTS
Measured on this box: four `python3 -m http.server` processes were alive with their
working directories DELETED. One of them answered HTTP 200 on :8792 with stale content
while looking perfectly healthy to `ps`. Nothing about that process looks wrong: the pid
lives, the port answers, the command line is ordinary. The URL works. It is just serving a
directory that no longer exists on disk.

The obvious guard is a path comparison, and it does not work:

    mkdir /tmp/site            # server starts here
    rm -rf /tmp/site; mkdir /tmp/site   # same path, different directory
    # a path check says: healthy. it is serving deleted content.

So this records the served directory's IDENTITY, not its name: `(st_dev, st_ino)` and the
resolved path, alongside the pid, port and bind address. `check` then re-reads
`/proc/<pid>/cwd` and compares. That distinguishes three failures that a port probe and a
`ps` call cannot tell apart:

  1. DELETED     the directory was unlinked — `/proc/<pid>/cwd` ends in " (deleted)".
  2. REPLACED    the path exists but is a different directory (dev:ino differ): the
                 masquerade above. This is the case a path comparison calls healthy.
  3. FOREIGN     the recorded pid is dead and the port STILL answers: somebody else owns
                 the port, and the URL an operator is looking at is not your server.

It is the same distinction the fleet learned about sessions tonight: a name is not an
identity. A path is a name.

USAGE
    fleet_serve serve --dir PATH [--port N] [--bind ADDR] [--state-dir D] [--replace]
    fleet_serve check [--state-dir D] [--json]        exit 0 healthy / 1 problems
    fleet_serve list  [--state-dir D]                 same records, table form
    fleet_serve stop  (--port N | --pid P | --all) [--state-dir D]
    fleet_serve --selftest

STATE
One JSON record per port under the state dir (default `$FLEET_CONTROL_DIR/serve`). A record
is a CLAIM about the world; `check` re-verifies every claim against /proc and the
filesystem and never trusts the record's own word.

stdlib only, no imports outside it, no network beyond loopback.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:  # the seam; a standalone copy still works, it just defaults to ~/.fleet/serve
    import fleet_paths
except Exception:  # pragma: no cover - only when the module is copied alone
    fleet_paths = None

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_USAGE = 2

DEFAULT_BIND = "127.0.0.1"
START_TIMEOUT = 8.0


def default_state_dir() -> Path:
    if fleet_paths is not None:
        try:
            return Path(fleet_paths.control_dir()) / "serve"
        except Exception:
            pass
    return Path(os.path.expanduser("~")) / ".fleet" / "serve"


# --- tiny helpers -----------------------------------------------------------

def _die(msg: str, code: int = EXIT_USAGE) -> "NoReturn":  # noqa: F821
    print("fleet_serve: %s" % msg, file=sys.stderr)
    raise SystemExit(code)


def port_open(bind: str, port: int, timeout: float = 0.4) -> bool:
    """Is ANYTHING listening? Deliberately says nothing about WHAT."""
    try:
        with socket.create_connection((bind, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def http_answers(bind: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen("http://%s:%d/" % (bind, int(port)),
                                    timeout=timeout) as r:
            return 200 <= r.status < 500
    except urllib.error.HTTPError:
        return True          # it answered; 404/403 is still an answer
    except Exception:
        return False


def free_port(bind: str = DEFAULT_BIND) -> int:
    s = socket.socket()
    try:
        s.bind((bind, 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


def pid_alive(pid: int) -> bool:
    """Alive AND able to serve. A Zombie answers `kill(pid, 0)` happily and serves
    nothing, so a liveness check that stops at the signal is a check that believes a
    corpse is on duty."""
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError):
        return False
    try:
        stat = Path("/proc/%d/stat" % int(pid)).read_text()
        state = stat[stat.rindex(")") + 2:].split()[0]
        return state != "Z"
    except (OSError, ValueError, IndexError):
        return True


def proc_cwd(pid: int):
    """The directory a process is actually IN, as the kernel reports it.

    Returns (path_or_None, deleted_flag). A process whose cwd was removed reads
    back as '<path> (deleted)' — that suffix is the kernel's only way of saying
    'this inode has no name any more', and it is the signal this whole tool exists
    to look for.
    """
    try:
        target = os.readlink("/proc/%d/cwd" % int(pid))
    except OSError:
        return None, False
    deleted = target.endswith(" (deleted)")
    if deleted:
        target = target[: -len(" (deleted)")]
    return target, deleted


def dir_identity(path: Path):
    """(dev, ino) of a directory, or None when it is not there any more."""
    try:
        st = os.stat(str(path))
        return int(st.st_dev), int(st.st_ino)
    except OSError:
        return None


# --- records ----------------------------------------------------------------

def state_path(state_dir: Path, port: int) -> Path:
    return Path(state_dir) / ("%d.json" % int(port))


def load_records(state_dir: Path):
    out = []
    d = Path(state_dir)
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            rec = {"port": f.stem, "record_file": str(f), "unreadable": True}
        rec.setdefault("record_file", str(f))
        out.append(rec)
    return out


def save_record(state_dir: Path, rec: dict) -> Path:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    p = state_path(d, rec["port"])
    p.write_text(json.dumps(rec, indent=2, sort_keys=True) + "\n")
    return p


# --- verification: the part that must not trust the record ------------------

def verify(rec: dict) -> dict:
    """Re-derive the truth for one record. Never trusts a field it can measure."""
    problems = []
    port = int(rec.get("port") or 0)
    bind = rec.get("bind") or DEFAULT_BIND
    pid = rec.get("pid")
    path = rec.get("dir_real") or rec.get("dir")
    live_pid = bool(pid) and pid_alive(pid)
    answers = port_open(bind, port) if port else False

    rec_dir, deleted = proc_cwd(pid) if live_pid else (None, False)
    identity_now = dir_identity(Path(path)) if path else None
    identity_rec = (rec.get("dev"), rec.get("ino"))

    if rec.get("unreadable"):
        problems.append("record is unreadable JSON — cannot be verified")
        status = "unverifiable"
    elif not live_pid:
        if answers:
            status = "foreign"
            problems.append(
                "pid %s is DEAD but %s:%d still answers — a DIFFERENT process owns the "
                "port, so whatever the URL shows is not the server this record describes"
                % (pid, bind, port))
        else:
            status = "dead"
    elif identity_rec[0] is None or identity_rec[1] is None:
        status = "unverifiable"
        problems.append("record carries no dev:ino identity — it cannot tell a recreated "
                        "directory from the original")
    elif identity_now is not None and identity_now != identity_rec:
        # The path EXISTS and holds different content: the hardest case, because
        # `ls` looks right and the URL works. The process's cwd may also read as
        # unlinked (it still holds the old inode) — both facts are reported.
        status = "replaced"
        problems.append(
            "pid %d is serving dev:ino %s, but the record says %s — the path %r exists and "
            "holds DIFFERENT content now. The server is stale and still answering."
            % (int(pid), identity_now, identity_rec, path))
        if deleted:
            problems.append(
                "its cwd %r has been UNLINKED — the inode it holds no longer has a name"
                % (rec_dir,))
    elif identity_now is None or deleted:
        status = "deleted"
        problems.append(
            "pid %d is serving %r, which has been UNLINKED — the content it answers with "
            "no longer exists on disk" % (int(pid), rec_dir or path))
    else:
        status = "healthy"

    return {
        "pid": pid, "port": port, "bind": bind, "dir_real": path,
        "dev": identity_rec[0], "ino": identity_rec[1],
        "identity_now": identity_now,
        "proc_cwd": rec_dir, "proc_cwd_deleted": deleted,
        "pid_alive": live_pid, "port_answers": answers,
        "status": status, "problems": problems,
        "started": rec.get("started"), "log": rec.get("log"),
        "record_file": rec.get("record_file"),
    }


def checked(state_dir: Path):
    return [verify(r) for r in load_records(state_dir)]


# --- serve ------------------------------------------------------------------

def cmd_serve(a) -> int:
    target = Path(os.path.expanduser(a.dir))
    if not target.exists():
        _die("no such directory: %s (refusing to record a claim about a directory that is "
             "not there)" % target)
    if not target.is_dir():
        _die("not a directory: %s" % target)
    real = Path(os.path.realpath(str(target)))
    ident = dir_identity(real)
    if ident is None:
        _die("cannot stat %s" % real)

    state_dir = Path(a.state_dir or default_state_dir())
    port = int(a.port or 0)
    if port == 0:
        port = free_port(a.bind)
    existing = state_path(state_dir, port)
    if existing.is_file():
        prev = verify(json.loads(existing.read_text()))
        if prev["status"] in ("healthy", "replaced", "deleted") or prev["port_answers"]:
            if not a.replace:
                _die("%s:%d is already recorded (pid %s, %s) — pass --replace to take it "
                     "over, or pick another port"
                     % (a.bind, port, prev["pid"], prev["status"]))
            _stop_pid(prev["pid"])
            time.sleep(0.3)

    log_path = Path(state_dir) / ("%d.log" % port)
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    log = open(str(log_path), "ab")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", str(a.bind),
             "--directory", str(real)],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True, cwd=str(real))
    finally:
        log.close()

    deadline = time.time() + START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            _die("the server exited immediately (rc=%s); see %s"
                 % (proc.returncode, log_path))
        if port_open(a.bind, port):
            break
        time.sleep(0.05)
    else:
        _stop_pid(proc.pid)
        _die("the server did not answer on %s:%d within %.0fs; see %s"
             % (a.bind, port, START_TIMEOUT, log_path))

    rec = {
        "pid": proc.pid, "port": port, "bind": a.bind,
        "dir_real": str(real), "dev": ident[0], "ino": ident[1],
        "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "cmdline": "python3 -m http.server %d --bind %s --directory %s"
                   % (port, a.bind, real),
        "log": str(log_path),
    }
    save_record(state_dir, rec)
    print("serving %s on %s:%d (pid %d, dev:ino %d:%d)"
          % (real, a.bind, port, proc.pid, ident[0], ident[1]))
    print("record: %s" % state_path(state_dir, port))
    return EXIT_OK


def _stop_pid(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), signal.SIGTERM)
    except OSError:
        return False
    for _ in range(40):
        if not pid_alive(int(pid)):
            return True
        time.sleep(0.05)
    try:
        os.kill(int(pid), signal.SIGKILL)
    except OSError:
        pass
    return True


# --- check / list / stop ----------------------------------------------------

def cmd_check(a) -> int:
    state_dir = Path(a.state_dir or default_state_dir())
    rows = checked(state_dir)
    bad = [r for r in rows if r["problems"]]
    if a.json:
        print(json.dumps({"ok": not bad, "state_dir": str(state_dir),
                          "servers": rows,
                          "problems": [p for r in rows for p in r["problems"]]},
                         indent=2, sort_keys=True))
    else:
        if not rows:
            print("no served directories recorded under %s" % state_dir)
        for r in rows:
            print("%-11s %s:%d  pid %-8s %s" % (r["status"], r["bind"], r["port"],
                                                r["pid"], r["dir_real"]))
            for p in r["problems"]:
                print("            %s" % p)
        if bad:
            print("\n%d of %d recorded server(s) are lying about what they serve"
                  % (len(bad), len(rows)))
        elif rows:
            print("\nall %d recorded server(s) are serving what their record says"
                  % len(rows))
    return EXIT_PROBLEM if bad else EXIT_OK


def cmd_list(a) -> int:
    rows = checked(Path(a.state_dir or default_state_dir()))
    if a.json:
        print(json.dumps({"servers": rows}, indent=2, sort_keys=True))
        return EXIT_OK
    if not rows:
        print("nothing recorded")
        return EXIT_OK
    print("%-11s %-6s %-9s %-9s %s" % ("status", "port", "pid", "alive", "dir"))
    for r in rows:
        print("%-11s %-6d %-9s %-9s %s"
              % (r["status"], r["port"], r["pid"], r["pid_alive"], r["dir_real"]))
    return EXIT_OK


def cmd_stop(a) -> int:
    state_dir = Path(a.state_dir or default_state_dir())
    rows = checked(state_dir)
    if a.all:
        victims = rows
    elif a.port:
        victims = [r for r in rows if r["port"] == int(a.port)]
    elif a.pid:
        victims = [r for r in rows if str(r["pid"]) == str(a.pid)]
    else:
        _die("stop needs --port, --pid or --all")
    if not victims:
        print("nothing matched")
        return EXIT_OK
    for r in victims:
        if r["pid_alive"]:
            _stop_pid(r["pid"])
            print("stopped pid %s (%s:%d)" % (r["pid"], r["bind"], r["port"]))
        elif r["port_answers"]:
            print("pid %s was already dead; %s:%d is answered by something ELSE and was "
                  "left alone" % (r["pid"], r["bind"], r["port"]))
        try:
            os.unlink(r["record_file"])
        except OSError:
            pass
    return EXIT_OK


# --- selftest ---------------------------------------------------------------

def selftest() -> int:
    """Plant all three lies in a temp dir and demand the right diagnosis for each.

    It uses the same `verify()` the check verb uses, on records written the same
    way `serve` writes them. It starts real servers: a guard that has never met a
    real /proc cannot claim to read one.
    """
    problems = []

    def want(label, got, expect):
        if got != expect:
            problems.append("%s: expected %r, got %r" % (label, expect, got))

    tmp = Path(tempfile.mkdtemp(prefix="fleet-serve-selftest-"))
    state = tmp / "state"
    stray = []
    try:
        site = tmp / "site"
        site.mkdir()
        (site / "index.html").write_text("selftest")

        bind = DEFAULT_BIND
        port = free_port(bind)
        real = Path(os.path.realpath(str(site)))
        ident = dir_identity(real)
        log = open(str(tmp / "log"), "ab")
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "http.server", str(port), "--bind", bind,
                 "--directory", str(real)],
                stdout=log, stderr=log, stdin=subprocess.DEVNULL,
                start_new_session=True, cwd=str(real))
        finally:
            log.close()
        stray.append(proc.pid)
        for _ in range(120):
            if port_open(bind, port):
                break
            time.sleep(0.05)
        else:
            return _selftest_fail(problems + ["server never came up on %d" % port], tmp)

        rec = {"pid": proc.pid, "port": port, "bind": bind, "dir_real": str(real),
               "dev": ident[0], "ino": ident[1], "started": "selftest",
               "cmdline": "selftest", "log": "selftest"}
        save_record(state, rec)

        # 0. the honest case must be quiet
        rows = checked(state)
        want("healthy server status", rows[0]["status"], "healthy")
        want("healthy server problems", rows[0]["problems"], [])

        # 1. DELETED — unlink the directory under a live server
        shutil.rmtree(str(site))
        rows = checked(state)
        want("deleted directory status", rows[0]["status"], "deleted")
        if not rows[0]["problems"]:
            problems.append("deleted directory produced no problem text")
        elif "UNLINKED" not in rows[0]["problems"][0]:
            problems.append("deleted directory is not described as unlinked: %r"
                            % rows[0]["problems"][0])

        # 2. REPLACED — same path, different directory. A path check calls this fine.
        site.mkdir()
        (site / "index.html").write_text("impostor")
        rows = checked(state)
        want("replaced directory status", rows[0]["status"], "replaced")
        if rows[0]["identity_now"] == (rec["dev"], rec["ino"]):
            problems.append("a recreated directory reported the SAME dev:ino as the "
                            "original — the identity check is not reading the filesystem")
        if rows[0]["proc_cwd_deleted"] is False and rows[0]["status"] == "healthy":
            problems.append("recreated directory was called healthy")

        # 3. FOREIGN — kill the pid, put someone else on the port
        _stop_pid(proc.pid)
        try:
            proc.wait(timeout=10)   # reap: a zombie is not a live server
        except Exception:
            pass
        for _ in range(80):
            if not port_open(bind, port):
                break
            time.sleep(0.05)
        other = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(port), "--bind", bind,
             "--directory", str(tmp)], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True)
        stray.append(other.pid)
        for _ in range(120):
            if port_open(bind, port):
                break
            time.sleep(0.05)
        rows = checked(state)
        want("foreign listener status", rows[0]["status"], "foreign")
        blob = " ".join(rows[0]["problems"])
        if "DEAD" not in blob:
            problems.append("foreign listener is not reported as a dead pid: %r" % blob)

        # 4. a directory that is not there must be refused, not recorded
        if state_path(state, 1).exists():
            problems.append("selftest wrote a record for a port it never used")
    finally:
        for pid in stray:
            _stop_pid(pid)
        shutil.rmtree(str(tmp), ignore_errors=True)

    return _selftest_fail(problems, None)


def _selftest_fail(problems, tmp) -> int:
    if tmp is not None:
        shutil.rmtree(str(tmp), ignore_errors=True)
    if problems:
        print("fleet_serve --selftest: FAIL")
        for p in problems:
            print("  - %s" % p)
        return EXIT_PROBLEM
    print("fleet_serve --selftest: ok "
          "(healthy quiet; deleted, replaced and foreign each diagnosed)")
    return EXIT_OK


# --- cli --------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fleet_serve",
        description="Serve a directory and be able to prove which directory it is serving.")
    p.add_argument("--selftest", action="store_true",
                   help="prove the detector on synthetic corpses, then exit")
    sub = p.add_subparsers(dest="verb")

    s = sub.add_parser("serve", help="start a server and record its directory identity")
    s.add_argument("--dir", default=".")
    s.add_argument("--port", type=int, default=0, help="0 (default) = pick a free port")
    s.add_argument("--bind", default=DEFAULT_BIND)
    s.add_argument("--state-dir", default=None)
    s.add_argument("--replace", action="store_true",
                   help="take over a port already recorded here")
    s.add_argument("--detach", action="store_true",
                   help="accepted for symmetry: the server is always detached")
    s.set_defaults(fn=cmd_serve)

    c = sub.add_parser("check", help="re-verify every record; exit 1 on any lie")
    c.add_argument("--state-dir", default=None)
    c.add_argument("--json", action="store_true")
    c.set_defaults(fn=cmd_check)

    ls = sub.add_parser("list", help="show the records")
    ls.add_argument("--state-dir", default=None)
    ls.add_argument("--json", action="store_true")
    ls.set_defaults(fn=cmd_list)

    st = sub.add_parser("stop", help="stop a recorded server and drop its record")
    st.add_argument("--port", type=int, default=0)
    st.add_argument("--pid", default=None)
    st.add_argument("--all", action="store_true")
    st.add_argument("--state-dir", default=None)
    st.set_defaults(fn=cmd_stop)
    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if a.selftest:
        return selftest()
    if not getattr(a, "fn", None):
        p.print_help()
        return EXIT_USAGE
    return int(a.fn(a) or 0)


if __name__ == "__main__":
    sys.exit(main())
