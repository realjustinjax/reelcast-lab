"""Render tests on a small synthetic recording (tests/synth.py): hand-set cut points, rendering
with the whole frame kept (top_crop 0.0), the two line hook check, and the quality check on real
rendered files. They need the Python sandbox; most also need ffmpeg and tesseract. Each is
skipped with a message when its tool is missing."""
import atexit
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from tests import synth
from tests.test_engine import CODE, TOOLS, Base, need_ffmpeg, need_sandbox, need_tesseract

import common  # noqa: E402
import gate  # noqa: E402
from state import VERSIONS, Run  # noqa: E402

DEFAULT_LOOK = dict(preset=CODE.parent / "brand/default/default_pop.json",
                    end_card=CODE.parent / "brand/default/end-card.png", fonts_dir=CODE.parent / "brand/default/fonts")
_FX = {}


def tool_env(root):
    return dict(common.tool_env(TOOLS), REELCAST_LAB_ROOT=str(root))


def render(py, env, *args):
    r = subprocess.run([py, str(CODE / "render_reel.py"), *[str(a) for a in args]], env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError(f"render_reel.py {args[0]} failed:\n{r.stdout[-1500:]}\n{r.stderr[-1500:]}")
    return r


def rendered():
    """All five versions of the synthetic recording, rendered once per test session."""
    if not _FX:
        need_ffmpeg()
        py = need_sandbox()
        d = Path(tempfile.mkdtemp(prefix="reelcast-lab-render-"))
        atexit.register(shutil.rmtree, d, True)
        env = tool_env(d / "Content")
        src = synth.write_source(d / "work", env=env)
        plan = synth.five_version_plan(d / "work", d / "edited", DEFAULT_LOOK, src["sentences"])
        plan_p = d / "work" / "edit_plan.json"
        plan_p.write_text(json.dumps(plan, indent=1))
        render(py, env, "render", plan_p)
        render(py, env, "variants", plan_p, "--source", "synthetic")
        (d / "edited" / "caption.txt").write_text("A clean caption for a test. #one #two #three\n")
        _FX.update(dir=d, work=d / "work", edited=d / "edited", plan=plan)
    return _FX


class TestHandSetCutPoints(Base):
    """Two kept ranges that sit next to each other in the transcript are joined without a cut,
    unless a hand-set cut point names their seam: that always wins."""

    def segments(self, **overrides):
        py = need_sandbox()
        work = self.tmp / "cut"
        if not (work / "audio.wav").exists():
            synth.write_source(work, video=False)
        plan = dict(inputs=dict(video=str(work / "sdr.mp4"), audio=str(work / "audio.wav"),
                                transcript=str(work / "transcript.json")),
                    out_dir=str(work / "out"), work_dir=str(work / "render"),
                    preset=str(DEFAULT_LOOK["preset"]), end_card=str(DEFAULT_LOOK["end_card"]),
                    fonts_dir=str(DEFAULT_LOOK["fonts_dir"]), top_crop=0.0,
                    versions={"A_control": dict(ranges=[[0, 2, 1.0], [3, 5, 1.0]], change="control")}, **overrides)
        (work / "plan.json").write_text(json.dumps(plan))
        render(py, tool_env(self.root), "render", work / "plan.json", "A", "--plan-only")
        return json.loads((work / "render" / "versions" / "A" / "segments.json").read_text())

    def test_adjacent_ranges_join_when_nothing_is_hand_set(self):
        a, b = self.segments()
        self.assertTrue(b["join"])
        self.assertEqual(a["o"], b["i"])

    def test_hand_set_in_point_wins_between_adjacent_ranges(self):
        a, b = self.segments(in_overrides={"3": 1.36})
        self.assertFalse(b["join"])
        self.assertEqual(b["i"], 1.36)
        self.assertLessEqual(a["o"], 1.38)             # A still ends on its own word, before word 3

    def test_hand_set_out_point_wins_between_adjacent_ranges(self):
        a, b = self.segments(out_overrides={"2": 1.28})
        self.assertFalse(b["join"])
        self.assertAlmostEqual(a["o"], 38 / 30, places=4)   # 1.28 s on the 30 fps grid

    def test_both_hand_set_points_win(self):
        a, b = self.segments(in_overrides={"3": 1.4}, out_overrides={"2": 1.3})
        self.assertFalse(b["join"])
        self.assertAlmostEqual(a["o"], 1.3, places=4)
        self.assertEqual(b["i"], 1.4)

    def test_hand_set_point_in_one_version_only(self):
        work = self.tmp / "cut"
        synth.write_source(work, video=False)
        segs = self.segments()
        self.assertTrue(segs[1]["join"])
        plan = json.loads((work / "plan.json").read_text())
        plan["versions"]["A_control"]["in_overrides"] = {"3": 1.37}
        (work / "plan.json").write_text(json.dumps(plan))
        render(need_sandbox(), tool_env(self.root), "render", work / "plan.json", "A", "--plan-only")
        segs = json.loads((work / "render" / "versions" / "A" / "segments.json").read_text())
        self.assertEqual((segs[1]["join"], segs[1]["i"]), (False, 1.37))


class TestRealRender(Base):
    def make_run(self, doctor=False):
        fx = rendered()
        need_tesseract()
        r = Run.create(self.cfg, "g-doctored" if doctor else "g-real")
        if (r.dir / "work").exists():
            (r.dir / "work").rmdir()
        os.symlink(fx["work"], r.dir / "work")
        ed = self.root / "Edited" / r.id
        ed.mkdir()
        for f in fx["edited"].iterdir():
            if f.is_file():
                os.symlink(f, ed / f.name)
        r.d["edited_dir"] = str(ed)
        if doctor:   # paint over the caption band of A: captions gone
            (ed / "A_control.mp4").unlink()
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(fx["edited"] / "A_control.mp4"), "-vf",
                            "drawbox=x=0:y=1180:w=1080:h=400:color=0x808080:t=fill", "-c:v", "libx264", "-preset",
                            "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "copy", str(ed / "A_control.mp4")],
                           check=True, env=common.tool_env(self.cfg))
        return r

    def test_renders_five_files_with_the_whole_frame_kept(self):
        fx = rendered()
        self.assertEqual(fx["plan"]["top_crop"], 0.0)
        for v in VERSIONS:
            m = common.media_summary(self.cfg, fx["edited"] / f"{v}.mp4")
            self.assertEqual((m["width"], m["height"], m["vcodec"], m["acodec"]), (1080, 1920, "h264", "aac"), v)
        variants = json.loads((fx["edited"] / "variants.json").read_text())
        self.assertEqual(variants["top_crop"], 0.0)
        b = next(v for v in variants["versions"] if v["id"] == "B")
        self.assertEqual(b["onscreen_hook_lines"], synth.HOOK_LINES)

    def test_real_render_passes_the_quality_check(self):
        res = gate.run_gate(self.cfg, self.make_run())
        self.assertTrue(res["ok"], res["failures"] + res["font_fail"])

    def test_doctored_render_fails(self):
        res = gate.run_gate(self.cfg, self.make_run(doctor=True))
        self.assertFalse(res["ok"])
        self.assertTrue(any("A_control: burned captions not found" in f for f in res["failures"]))
        self.assertIn("A_control", res["font_fail"])

    def test_two_line_hook_is_read_line_by_line(self):
        """A real two line hook. Read as one strip of text (the old way) it comes back as garbage
        and the check fails; each line read on its own is found."""
        r = self.make_run()
        fx = rendered()
        b = fx["edited"] / "B_onscreen_hook.mp4"
        colors = gate.run_colors(self.cfg, r)
        tmp = self.tmp / "ocr"
        tmp.mkdir()
        variants = json.loads((fx["edited"] / "variants.json").read_text())["versions"]
        spec = gate.hook_spec(r, variants)                      # from the position the renderer recorded
        self.assertEqual([x["text"] for x in spec["lines"]], synth.HOOK_LINES)
        self.assertTrue(all(isinstance(x["y"], int) for x in spec["lines"]))
        ok, missing, reads = gate.check_hook(self.cfg, b, spec, tmp, colors)
        self.assertTrue(ok, reads)
        # no recorded positions (an editor that did not use the renderer): the strip is split by rows
        loose = dict(lines=[dict(text=t, y=None) for t in synth.HOOK_LINES], font_px=None, seconds=3.0)
        buf, hh = gate.caption_crop(self.cfg, b, 1.2, self.cfg["gate"]["hook_y_pct"], self.cfg["gate"]["hook_band_px"])
        _, mask = gate.caption_strokes(buf, hh, colors)
        self.assertEqual(len(gate.mask_row_bands(mask)), 2)
        ok, missing, reads = gate.check_hook(self.cfg, b, loose, tmp, colors)
        self.assertTrue(ok, reads)
        # and the whole check agrees: no hook failure on B
        res = gate.run_gate(self.cfg, r, versions=["B_onscreen_hook"])
        self.assertFalse([f for f in res["failures"] if "hook" in f], res["failures"])
        # every line must be there: one line of the two is not enough, and A has no hook at all
        wrong = dict(spec, lines=[spec["lines"][0], dict(spec["lines"][1], text="never shown")])
        ok, missing, _ = gate.check_hook(self.cfg, b, wrong, tmp, colors)
        self.assertEqual((ok, missing), (False, ["never shown"]))
        ok, missing, _ = gate.check_hook(self.cfg, fx["edited"] / "A_control.mp4", spec, tmp, colors)
        self.assertEqual((ok, missing), (False, synth.HOOK_LINES))
