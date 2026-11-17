#!/usr/bin/env python3
"""Stub editor for tests and the practice run. Reads the run's run.json and transcript.

  stub_editor.py RUN_DIR --fake     writes 5 small different files (unit tests; the gate is faked)
  stub_editor.py RUN_DIR --render   writes edit_plan.json from the transcript and calls the REAL
                                    render_reel.py, so real video files come out (practice run)
  stub_editor.py RUN_DIR --fail     exits 1 without a DONE marker
"""
import json, subprocess, sys
from pathlib import Path

CODE = Path(__file__).resolve().parents[2]
run_dir = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else "--fake"
if mode == "--fail":
    sys.exit(1)
if mode == "--slow":           # holds the model slot for a while (one-job-at-a-time test)
    import time
    time.sleep(float(sys.argv[3]) if len(sys.argv) > 3 else 2)
    mode = "--fake"
run = json.loads((run_dir / "run.json").read_text())
ed = Path(run["edited_dir"])
work = run_dir / "work"
VERS = ["A_control", "B_onscreen_hook", "C_cold_open", "D_tight_cut", "E_visual_open"]
caption = "A stand-in caption from the stub editor. Save this for later. #practice #reels #editing"

if mode == "--fake":
    for k, v in enumerate(VERS):
        (ed / f"{v}.mp4").write_bytes(b"fake video " + v.encode() * (k + 1))
    (ed / "variants.json").write_text(json.dumps(dict(versions=[dict(id=v[0], name=v, file=f"{v}.mp4", length_s=60.0 - k,
        change_vs_A="control" if k == 0 else f"change {v}", onscreen_hook_text="Hook line" if v.startswith("B") else None)
        for k, v in enumerate(VERS)])))
else:
    sys.path.insert(0, str(CODE))
    import common
    cfg = common.load_config()
    look = common.brand(cfg)
    words = json.loads((work / "transcript.json").read_text())["words"]
    fixes = json.loads((work / "transcript_fixes.json").read_text())
    # sentences as [first, last] word index ranges
    sents, s0 = [], 0
    for i, w in enumerate(words):
        if w["word"].strip().endswith((".", "?", "!")) or i == len(words) - 1:
            sents.append([s0, i]); s0 = i + 1
    sents = [s for s in sents if s[1] - s[0] >= 3]
    a, total = [], 0.0
    for s in sents:
        if total > 50:
            break
        a.append([s[0], s[1], 1.10 if len(a) % 3 == 2 else 1.0])
        total += words[s[1]]["end"] - words[s[0]]["start"]
    c = [a[-1]] + a[:-1] if len(a) > 1 else a
    d = a[::2] if len(a) > 2 else a
    first = " ".join(words[i]["word"].strip() for i in range(a[0][0], min(a[0][1], a[0][0] + 7) + 1)).rstrip(".,")
    hw = first.split()
    hook = [" ".join(hw[:len(hw) // 2 + 1]), " ".join(hw[len(hw) // 2 + 1:]) or "watch this"]
    plan = dict(inputs=dict(video=str(work / "sdr.mp4"), audio=str(work / "audio.wav"), transcript=str(work / "transcript.json")),
                out_dir=str(ed), work_dir=str(work / "render"), preset=str(look["preset"]),
                end_card=str(look["end_card"]), fonts_dir=str(look["fonts_dir"]),
                top_crop=cfg["framing"]["top_crop"], eye_line=cfg["framing"]["eye_line"],
                text=fixes["text"], key_words=[r[0] + 1 for r in a], versions={
                    "A_control": dict(ranges=a, change="Control: the story as told."),
                    "B_onscreen_hook": dict(ranges="A_control", change="Same cut as A plus an on-screen hook for 3 seconds.",
                                            hook=dict(lines=hook, key_words=[], seconds=3.0)),
                    "C_cold_open": dict(ranges=c, change="Same cut as A with the last line moved to the front."),
                    "D_tight_cut": dict(ranges=d, change="Every other sentence of A, faster and shorter."),
                    "E_visual_open": dict(ranges="A_control", change="Same cut as A, opens on a 1.2x punch-in easing out over 3 s.",
                                          visual_open=dict(start_zoom=1.2, ramp_s=3.0))})
    (work / "edit_plan.json").write_text(json.dumps(plan, indent=1))
    py = cfg["sandbox_python"]
    subprocess.run([py, str(CODE / "render_reel.py"), "render", str(work / "edit_plan.json")], check=True)
    subprocess.run([py, str(CODE / "render_reel.py"), "variants", str(work / "edit_plan.json"),
                    "--source", ", ".join(Path(p).name for p in run["raw_paths"])], check=True)
(ed / "caption.txt").write_text(caption + "\n")
(ed / "REPORT.md").write_text("# Stub edit\n\nMade by the stub editor for a test or the practice run. It cut by sentence, "
                              "with no judgment: a real edit is made by the editor model.\n")
(run_dir / "work" / "DONE").write_text("ok\n")
