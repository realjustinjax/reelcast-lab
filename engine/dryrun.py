#!/usr/bin/env python3
"""Practice run: the real engine on one clip, in a scratch folder, never in your content folder.

  dryrun.py SCRATCH_DIR [CLIP]      (CLIP defaults to sample/sample-clip.mp4)

Builds SCRATCH_DIR/Content/{RAW/Singles,RAW/Multi-part,RAW/Needs-attention,Edited,Lab_Data},
copies the clip into RAW/Singles, then calls tick.py again and again with
REELCAST_LAB_DRY_RUN=1 and a fake clock. What is real: the SDR working copy, the offline
transcription, the renderer and the quality check. What is a stand-in: the editor (a stub that
cuts by sentence and calls the real renderer, so no model is used and nothing leaves the Mac).

It stops at the review hold and prints where the five files and the report are. Every state
change goes to SCRATCH_DIR/practice.log.

Safety: it refuses a scratch folder inside (or around) the configured content root, and any
existing non-empty folder it did not make itself (it leaves a marker file in the ones it makes).
"""
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

CODE = Path(__file__).resolve().parent
sys.path.insert(0, str(CODE))

import common  # noqa: E402

MARKER = ".reelcast-lab-practice-root"
VERSIONS = ["A_control", "B_onscreen_hook", "C_cold_open", "D_tight_cut", "E_visual_open"]


def check_scratch(scratch, content_root):
    """Refuse the configured content root (inside it or around it), and any existing folder
    this script did not create."""
    content_root = Path(content_root).expanduser().resolve()
    if scratch == content_root or content_root in scratch.parents:
        raise SystemExit(f"refusing: {scratch} is inside your content folder {content_root}; "
                         f"use a scratch folder somewhere else")
    if scratch in content_root.parents:
        raise SystemExit(f"refusing: your content folder {content_root} is inside {scratch}; "
                         f"use a scratch folder somewhere else")
    marker = scratch / MARKER
    if scratch.exists() and any(scratch.iterdir()) and not marker.exists():
        raise SystemExit(f"refusing: {scratch} is not empty and has no {MARKER} (it was not made by a practice run)")
    scratch.mkdir(parents=True, exist_ok=True)
    if not marker.exists():
        marker.write_text("made by a ReelCast Lab practice run (engine/dryrun.py); this folder may be wiped by it\n")


def main(scratch, clip):
    scratch = Path(scratch).expanduser().resolve()
    clip = Path(clip).expanduser().resolve()
    if not clip.is_file():
        raise SystemExit(f"no such clip: {clip}")
    env0 = {k: v for k, v in os.environ.items() if k not in ("REELCAST_LAB_ROOT", "REELCAST_LAB_NOW")}
    saved_root = os.environ.pop("REELCAST_LAB_ROOT", None)
    real_cfg = common.load_config()             # the real settings: where the real content folder is
    if saved_root is not None:
        os.environ["REELCAST_LAB_ROOT"] = saved_root
    check_scratch(scratch, real_cfg.root)
    root = scratch / "Content"
    if root.exists():
        shutil.rmtree(root)
    for sub in ("RAW/Singles", "RAW/Multi-part", "RAW/Needs-attention", "Edited", common.LAB_DIR):
        (root / sub).mkdir(parents=True)
    shutil.copy(clip, root / "RAW" / "Singles" / clip.name)
    py = sys.executable
    over = {}
    extra = os.environ.get("REELCAST_LAB_CONFIG")
    if extra:                                   # keep whatever override file the caller already uses
        over = json.loads(Path(extra).read_text())
    over = common._merge(over, {
        "lock_path": str(scratch / "practice.lock"), "log_path": str(scratch / "engine.log"),
        "auto_post_enabled": False, "notify": {"enabled": False},
        "editor": {"command": [py, str(CODE / "tests/stubs/stub_editor.py"), "{run_dir}", "--render"], "cwd": str(scratch)},
        "analyst": {"command": [py, str(CODE / "tests/stubs/stub_analyst.py"), "{run_dir}"], "cwd": str(scratch)},
        "publish": {"publisher": "fake"}, "readings": {"reader": "fake"},
    })
    cfgp = scratch / "practice-config.json"
    cfgp.write_text(json.dumps(over, indent=1))
    env = dict(env0, REELCAST_LAB_ROOT=str(root), REELCAST_LAB_CONFIG=str(cfgp), REELCAST_LAB_DRY_RUN="1")
    logf = open(scratch / "practice.log", "w")

    def say(msg):
        line = f"{datetime.now():%H:%M:%S} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    tz = real_cfg.tz
    t = datetime.now(tz).replace(hour=9, minute=0, second=0, microsecond=0)
    seen = 0
    runs_dir = root / common.LAB_DIR / "runs"
    started = time.time()
    for _ in range(3000):
        r = subprocess.run([py, str(CODE / "tick.py"), "--now", t.isoformat()], env=env, capture_output=True, text=True)
        if r.returncode != 0:
            say(f"tick failed: {r.stderr[-2000:]}")
            return 1
        runs = sorted(runs_dir.iterdir()) if runs_dir.exists() else []
        if not runs:
            t += timedelta(minutes=10)
            continue
        d = json.loads((runs[0] / "run.json").read_text())
        for h in d["history"][seen:]:
            say(f"fake {datetime.fromisoformat(h['time']).astimezone(tz):%Y-%m-%d %H:%M %Z}  {h['step']:<16} {h['result']}"
                + (f"  [{h['detail']}]" if h.get("detail") else ""))
        seen = len(d["history"])
        st = d["state"]
        if st in ("needs_attention", "dropped"):
            say(f"STOPPED in {st}: {d.get('needs_attention')}")
            say(f"Logs: {runs[0] / 'jobs'}")
            return 1
        if st == "awaiting_review":
            ed = Path(d["edited_dir"])
            say(f"REVIEW HOLD reached; wall time {time.time() - started:.0f} s. Nothing was posted.")
            say(f"The five files are in: {ed}")
            for v in VERSIONS:
                f = ed / f"{v}.mp4"
                say(f"  {f.name}  {'ok' if f.exists() else 'MISSING'}")
            say(f"The report: {ed / 'REPORT.md'}")
            say(f"The post caption: {ed / 'caption.txt'}")
            say(f"The quality check results: {runs[0] / 'run.json'} (under \"gate\")")
            return 0
        job = d.get("job")
        if job and not Path(job["exit_file"]).exists():
            time.sleep(5)           # real work running: let real time pass, keep the fake clock still
            continue
        t += timedelta(minutes=10)
    say("gave up: the review hold was never reached")
    return 1


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print(__doc__)
        sys.exit(2)
    clip = sys.argv[2] if len(sys.argv) == 3 else str(CODE.parent / "sample" / "sample-clip.mp4")
    sys.exit(main(sys.argv[1], clip))
