"""Run state: one folder per video at <content root>/Lab_Data/runs/<run_id>/ with run.json.

run.json holds the state and a history of every step (time, step, result). The engine-wide
state file <content root>/Lab_Data/state.json holds the review hold counter and the publish days.
"""
import sys
import shutil
from pathlib import Path

from common import iso, now, read_json, write_json

STATES = [
    "waiting_stable", "ingested", "transcribed", "editing", "edited", "gated",
    "awaiting_review", "queued_publish", "published", "analyzing", "decided", "complete",
]
FAILED = "needs_attention"
DROPPED = "dropped"          # the raw file left RAW before it was stable (the owner took it back)
TERMINAL = {FAILED, DROPPED, "complete"}
VERSIONS = ["A_control", "B_onscreen_hook", "C_cold_open", "D_tight_cut", "E_visual_open"]


class Run:
    def __init__(self, cfg, run_id, data=None):
        self.cfg = cfg
        self.id = run_id
        self.dir = cfg.runs_dir / run_id
        self.d = data if data is not None else read_json(self.dir / "run.json", {})

    # -- persistence
    @classmethod
    def create(cls, cfg, run_id, **fields):
        r = cls(cfg, run_id, {})
        r.dir.mkdir(parents=True, exist_ok=True)
        r.d = dict(id=run_id, state="waiting_stable", created=iso(now()), history=[], attempts={}, **fields)
        r.event("created", "waiting_stable", detail=fields.get("source"))
        return r

    def save(self):
        self.d["updated"] = iso(now())
        write_json(self.dir / "run.json", self.d)

    @property
    def state(self):
        return self.d.get("state")

    @property
    def work(self):
        p = self.dir / "work"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def edited_dir(self):
        e = self.d.get("edited_dir")
        return Path(e) if e else None

    def event(self, step, result, detail=None):
        h = dict(time=iso(now()), step=step, result=result)
        if detail:
            h["detail"] = detail
        self.d.setdefault("history", []).append(h)
        self.save()

    def to(self, state, step=None, detail=None):
        old = self.d.get("state")
        self.d["state"] = state
        self.d["state_since"] = iso(now())
        self.event(step or state, f"{old} -> {state}", detail)

    def bump(self, key):
        a = self.d.setdefault("attempts", {})
        a[key] = a.get(key, 0) + 1
        self.save()
        return a[key]

    def attempts(self, key):
        return self.d.get("attempts", {}).get(key, 0)


def all_runs(cfg):
    out = []
    if not cfg.runs_dir.exists():
        return out
    for p in sorted(cfg.runs_dir.iterdir()):
        if (p / "run.json").exists():
            try:
                out.append(Run(cfg, p.name))
            except Exception as e:   # one corrupt run.json never stops every tick
                print(f"reelcast-lab: skipping unreadable run {p.name}: {e!r}", file=sys.stderr)
    return out


def lab_state(cfg):
    return read_json(cfg.lab / "state.json", {"review_holds_issued": 0, "publish_days": {}})


def save_lab_state(cfg, st):
    write_json(cfg.lab / "state.json", st)


def move_to_needs_attention(cfg, run):
    """Move the run's raw files to RAW/Needs-attention/<run_id>/. Never deletes anything."""
    dest = cfg.root / "RAW" / "Needs-attention" / run.id
    moved = []
    for p in run.d.get("raw_paths", []):
        src = Path(p)
        if src.exists():
            dest.mkdir(parents=True, exist_ok=True)
            target = dest / src.name
            n = 1
            while target.exists():
                target = dest / f"{src.stem}_{n}{src.suffix}"
                n += 1
            shutil.move(str(src), str(target))
            moved.append(str(target))
    if moved:
        run.d["raw_paths"] = moved
        src_dir = run.d.get("source_dir")
        if src_dir and Path(src_dir).is_dir() and not any(Path(src_dir).iterdir()):
            Path(src_dir).rmdir()
    return dest
