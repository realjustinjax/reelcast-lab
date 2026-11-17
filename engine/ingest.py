"""Find new footage in RAW, wait until it is stable, then ingest it.

Stability: size and mtime are recorded on every tick; a video is ready once they are unchanged
across two ticks at least stability.min_gap_seconds apart (10 minutes). Multi-part: the whole
subfolder must be stable; parts join in filmed order (ffprobe creation time, file name as the
tiebreak).
"""
import os
import re
import shutil
import stat
from pathlib import Path

from common import iso, log, media_summary, now, parse_time, slugify
from state import DROPPED, Run, all_runs

SKIP_NAMES = {"README.md", ".DS_Store"}


def is_video(cfg, p):
    return p.is_file() and not p.name.startswith(".") and p.suffix.lower() in cfg["video_extensions"]


def candidates(cfg):
    """{source_key: (kind, path, [video files])} for everything waiting in RAW."""
    raw = cfg.root / "RAW"
    out = {}
    singles = raw / "Singles"
    if singles.is_dir():
        for p in sorted(singles.iterdir()):
            if is_video(cfg, p):
                out[f"single:{p.name}"] = ("single", p, [p])
    multi = raw / "Multi-part"
    if multi.is_dir():
        for d in sorted(multi.iterdir()):
            if d.is_dir() and not d.name.startswith("."):
                files = sorted(f for f in d.iterdir() if is_video(cfg, f))
                if files:
                    out[f"multi:{d.name}"] = ("multi", d, files)
    return out


def signature(path, files):
    sig = []
    for f in files:
        st = f.stat()
        sig.append([f.name, st.st_size, int(st.st_mtime)])
    if path.is_dir():   # a part still being copied in counts too, even before it has a video suffix
        others = sorted(x.name for x in path.iterdir() if not x.name.startswith(".") and x.name not in SKIP_NAMES)
        sig.append(["__names__", ",".join(others)])
    return sig


def stability_check(run, sig, t, min_gap_s):
    """Record this tick's observation; True once unchanged for at least min_gap_s."""
    st = run.d.get("stability")
    if not st or st.get("sig") != sig:
        run.d["stability"] = dict(sig=sig, since=iso(t), checks=1)
        return False
    st["checks"] = st.get("checks", 1) + 1
    return (t - parse_time(st["since"])).total_seconds() >= min_gap_s


def unique_run_id(cfg, base):
    rid, n = base, 2
    while (cfg.runs_dir / rid).exists():
        rid = f"{base}-{n}"
        n += 1
    return rid


def scan(cfg):
    """Create runs for new drops and update stability on waiting ones. Returns runs made ready."""
    t = now()
    found = candidates(cfg)
    waiting = {r.d.get("source_key"): r for r in all_runs(cfg) if r.state == "waiting_stable"}
    ready = []
    for key, (kind, path, files) in found.items():
        run = waiting.pop(key, None)
        sig = signature(path, files)
        if run is None:
            stem = path.stem if kind == "single" else path.name
            local = t.astimezone(cfg.tz)
            rid = unique_run_id(cfg, f"{local:%Y%m%d-%H%M}_{slugify(stem, 40)}")
            run = Run.create(cfg, rid, source_key=key, kind=kind, source=str(path), title=stem,
                             raw_paths=[str(f) for f in files],
                             source_dir=str(path) if kind == "multi" else None)
            stability_check(run, sig, t, cfg["stability"]["min_gap_seconds"])
            run.event("stability", "first seen", detail=f"{len(files)} file(s)")
            log(cfg, f"[{rid}] new {kind} drop: {path.name}")
            continue
        run.d["raw_paths"] = [str(f) for f in files]
        if stability_check(run, sig, t, cfg["stability"]["min_gap_seconds"]):
            run.event("stability", "stable")
            ready.append(run)
        else:
            run.save()
    for key, run in waiting.items():    # file left RAW before it was stable
        run.to(DROPPED, "stability", detail="the file left RAW before it was picked up")
    return ready


def parse_created(c):
    """ffprobe creation tags: '2026-11-13T22:22:26-0600' or '2026-11-14T04:22:26.000000Z'."""
    c = c.strip().replace("Z", "+00:00")
    c = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", c)
    c = re.sub(r"\.(\d{6})\d*", r".\1", c)
    return parse_time(c)


def _created_key(part):
    try:
        return (0, parse_created(part.get("created")).timestamp(), part["name"])
    except Exception:
        return (1, 0.0, part["name"])


def order_parts(parts):
    return sorted(parts, key=_created_key)


def edited_folder(cfg, title, t):
    local = t.astimezone(cfg.tz)
    base = cfg.root / "Edited" / f"{local:%Y-%m-%d}_{slugify(title)}"
    p, n = base, 2
    while p.exists():
        p = Path(f"{base}-{n}")
        n += 1
    return p


def ingest(cfg, run, fail):
    """Probe, check limits, create Edited/<date>_<title>/ and move the raws to Originals/<same name>/.
    fail(reason) sends the run to Needs-attention."""
    parts = []
    for p in run.d["raw_paths"]:
        try:
            s = media_summary(cfg, p)
        except Exception as e:
            return fail(f"could not read {Path(p).name}: {e}")
        if not s.get("width"):
            return fail(f"{Path(p).name} has no video track")
        s["name"] = Path(p).name
        s["path"] = p
        parts.append(s)
    parts = order_parts(parts)
    total = sum(x["duration"] for x in parts)
    if total > cfg["limits"]["raw_max_seconds"]:
        return fail(f"raw footage runs {total / 60:.1f} minutes, over the {cfg['limits']['raw_max_seconds'] // 60} minute limit")
    sideways = [x["name"] for x in parts if x["width"] > x["height"]]
    if sideways:
        return fail(f"filmed sideways ({', '.join(sideways)}); version 1 cuts upright video only")
    dest = edited_folder(cfg, run.d["title"], now())
    dest.mkdir(parents=True)
    # The originals go to Originals/<same name>, outside every folder the editor may write to.
    orig = cfg.root / "Originals" / dest.name
    orig.mkdir(parents=True, exist_ok=True)
    moved = []
    for x in parts:
        if Path(x["path"]).is_symlink():
            return fail(f"{x['name']} is a shortcut (symlink), not a video file; drop the file itself into RAW")
    for x in parts:
        target = orig / x["name"]
        shutil.move(x["path"], str(target))
        x["path"] = str(target)
        moved.append(str(target))
    for m in moved:      # read only from here on
        os.chmod(m, os.stat(m).st_mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    src_dir = run.d.get("source_dir")
    if src_dir and Path(src_dir).is_dir():
        ds = Path(src_dir) / ".DS_Store"
        try:
            if ds.exists():
                ds.unlink()
            os.rmdir(src_dir)    # fails safely when anything else is still inside
        except OSError:
            pass
    run.d.update(raw_paths=moved, parts=parts, edited_dir=str(dest), originals_dir=str(orig),
                 raw_seconds=round(total, 2),
                 hdr=any(x.get("hdr") for x in parts))
    run.to("ingested", "ingest", detail=f"{len(parts)} part(s), {total:.0f}s raw -> {dest.name}")
    return True
