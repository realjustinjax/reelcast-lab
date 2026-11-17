"""Background jobs: the tick launches long work (transcription, the editor, the analyst,
re-renders) and checks on it later. It never waits.

A job is a small wrapper process (this file run as a script, in its own process group) that runs
the command and writes its exit code to a file when it ends. "Running" means the wrapper is alive
and has not written its exit file. Timeouts use the real wall clock, not the fake clock.
"""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

HEAVY = {"transcribe", "edit", "analyze", "rerender"}   # model or render work: one at a time


def launch(run, kind, cmd, cwd=None, env=None, timeout_s=3600, attempt=1):
    jdir = run.dir / "jobs"
    jdir.mkdir(parents=True, exist_ok=True)
    tag = f"{kind}-{attempt}"
    log = jdir / f"{tag}.log"
    exit_file = jdir / f"{tag}.exit"
    if exit_file.exists():
        exit_file.unlink()
    with open(log, "ab") as lf:
        p = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--exit-file", str(exit_file), "--", *cmd],
                             cwd=str(cwd) if cwd else None, env=env, stdout=lf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    run.d["job"] = dict(kind=kind, attempt=attempt, pid=p.pid, started_real=time.time(),
                        deadline_real=time.time() + timeout_s, log=str(log), exit_file=str(exit_file),
                        cmd=[str(c) for c in cmd][:12])
    run.save()
    return run.d["job"]


def pid_alive(pid):
    try:
        os.waitpid(pid, os.WNOHANG)       # reap if it is our child (tests run in one process)
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def is_running(job):
    """Alive and not finished. This is what the one-model-job-at-a-time rule counts."""
    return bool(job) and not Path(job["exit_file"]).exists() and pid_alive(job["pid"])


def check(job):
    """('done', code), 'running', 'timeout' (still alive past its deadline) or 'lost'."""
    ef = Path(job["exit_file"])
    if ef.exists():
        try:
            return ("done", int(ef.read_text().strip() or "1"))
        except ValueError:
            return ("done", 1)
    if pid_alive(job["pid"]):
        return "timeout" if time.time() > job["deadline_real"] else "running"
    time.sleep(0.2)
    if ef.exists():
        return check(job)
    return "lost"


def _wait_gone(pid, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if not pid_alive(pid):
            return True
        time.sleep(0.1)
    return not pid_alive(pid)


def kill(job, term_wait=10.0, kill_wait=5.0):
    """SIGTERM the job's process group, then SIGKILL, and wait until it is gone. True when gone."""
    pid = job["pid"]
    for sig, wait in ((signal.SIGTERM, term_wait), (signal.SIGKILL, kill_wait)):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            pass
        if _wait_gone(pid, wait):
            return True
    return not pid_alive(pid)


def heavy_running(runs):
    for r in runs:
        j = r.d.get("job")
        if j and j.get("kind") in HEAVY and is_running(j):
            return r
    return None


def recent_activity(paths, seconds=600, skip=("run.json",)):
    """True when any file under paths changed in the last `seconds` (real time)."""
    cutoff = time.time() - seconds
    for base in paths:
        if not base or not Path(base).exists():
            continue
        for root, _, files in os.walk(base):
            for f in files:
                if f in skip or f.endswith(".exit"):
                    continue
                try:
                    if os.path.getmtime(os.path.join(root, f)) > cutoff:
                        return True
                except OSError:
                    pass
    return False


def _wrapper(argv):
    i = argv.index("--exit-file")
    exit_file = argv[i + 1]
    cmd = argv[argv.index("--") + 1:]
    try:
        code = subprocess.call(cmd)
    except OSError as e:
        print(f"could not start {cmd[:1]}: {e}", flush=True)
        code = 127
    tmp = exit_file + ".tmp"
    with open(tmp, "w") as f:
        f.write(str(code))
    os.replace(tmp, exit_file)
    sys.exit(0)


if __name__ == "__main__":
    _wrapper(sys.argv[1:])
