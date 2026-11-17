"""ReelCast Lab engine tests. Run from the engine folder:  python3 -m unittest
Fake clock, fake publisher, fake reader, stub transcriber, editor and analyst. No network, no
notifications, and every file is written under a temp folder.

Tool paths: ffmpeg and the Python sandbox are found through the normal settings. To point the
tests at tools that live somewhere else, set REELCAST_LAB_CONFIG to a JSON file with
"ffmpeg_dir" and "sandbox_python". Tests that need a missing tool are skipped with a message."""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

CODE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(CODE))

# The tests never read the real user settings file, a real content root or a pinned clock.
os.environ["REELCAST_LAB_SETTINGS"] = str(Path(tempfile.gettempdir()) / "reelcast-lab-tests-no-settings.json")
os.environ.pop("REELCAST_LAB_ROOT", None)
os.environ.pop("REELCAST_LAB_NOW", None)

import analysis  # noqa: E402
import common  # noqa: E402
import ingest  # noqa: E402
import notify  # noqa: E402
import publish  # noqa: E402
import readings  # noqa: E402
import scoring  # noqa: E402
import transcribe  # noqa: E402
from state import VERSIONS, Run, all_runs, lab_state  # noqa: E402
from tick import Engine  # noqa: E402

TZ_NAME = "America/New_York"       # a fixed zone with daylight saving, whatever the Mac is set to
TZ = ZoneInfo(TZ_NAME)
STUBS = CODE / "tests" / "stubs"
NOWHERE = str(Path(tempfile.gettempdir()) / "reelcast-lab-tests-no-root")
TOOLS = common.load_config({"content_root": NOWHERE})      # shipped defaults plus any override file
FFMPEG = common.find_tool(TOOLS, "ffmpeg")
TESSERACT = common.find_tool(TOOLS, "tesseract")
SANDBOX_PY = TOOLS["sandbox_python"]
_CLIPS = {}
_SANDBOX_OK = None


def need_ffmpeg():
    if not FFMPEG or not common.find_tool(TOOLS, "ffprobe"):
        raise unittest.SkipTest("needs ffmpeg and ffprobe (install ffmpeg, or set ffmpeg_dir in the "
                                "file named by REELCAST_LAB_CONFIG)")
    return FFMPEG


def need_tesseract():
    if not TESSERACT:
        raise unittest.SkipTest("needs tesseract (the quality check reads the burned captions with it)")
    return TESSERACT


def need_sandbox():
    """The Python sandbox with numpy, Pillow and fontTools (the renderer runs in it)."""
    global _SANDBOX_OK
    if _SANDBOX_OK is None:
        _SANDBOX_OK = False
        if Path(SANDBOX_PY).exists():
            r = subprocess.run([SANDBOX_PY, "-c", "import numpy, PIL, fontTools"], capture_output=True)
            _SANDBOX_OK = r.returncode == 0
    if not _SANDBOX_OK:
        raise unittest.SkipTest("needs the Python sandbox with numpy, Pillow and fontTools (run setup, or set "
                                "sandbox_python in the file named by REELCAST_LAB_CONFIG)")
    return SANDBOX_PY


def clip(kind="upright"):
    """Tiny synthetic clips made once per test session (1 s, 108x192 upright or 192x108)."""
    if kind not in _CLIPS:
        ffmpeg = need_ffmpeg()
        d = Path(tempfile.mkdtemp(prefix="reelcast-lab-clips-"))
        size = "108x192" if kind == "upright" else "192x108"
        p = d / f"{kind}.mov"
        subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration=1",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=1", "-shortest",
                        "-c:v", "libx264", "-c:a", "aac", str(p)], check=True)
        _CLIPS[kind] = p
    return _CLIPS[kind]


def lt(*a):
    """A local wall-clock time in the test zone, as UTC."""
    return datetime(*a, tzinfo=TZ).astimezone(timezone.utc)


class Base(unittest.TestCase):
    editor_mode = "--fake"
    analyst_flags = []
    overrides = {}

    def setUp(self):
        os.environ.pop("REELCAST_LAB_ROOT", None)
        os.environ.pop("REELCAST_LAB_NOW", None)
        os.environ["REELCAST_LAB_DRY_RUN"] = "1"
        self.tmp = Path(tempfile.mkdtemp(prefix="reelcast-lab-test-"))
        self.root = self.tmp / "Content"
        for sub in ("RAW/Singles", "RAW/Multi-part", "RAW/Needs-attention", "Edited", common.LAB_DIR):
            (self.root / sub).mkdir(parents=True)
        py = sys.executable
        ov = {
            "content_root": str(self.root), "lock_path": str(self.tmp / "test.lock"),
            "log_path": str(self.tmp / "test.log"), "timezone": TZ_NAME,
            "owner": {"name": "the owner"}, "notify": {"enabled": False}, "glossary": [],
            "offer": {"name": "", "keyword": ""},
            "transcribe": {"command": [py, str(STUBS / "stub_transcribe.py"), "{run_dir}"]},
            "editor": {"command": [py, str(STUBS / "stub_editor.py"), "{run_dir}", self.editor_mode], "cwd": str(self.tmp)},
            "analyst": {"command": [py, str(STUBS / "stub_analyst.py"), "{run_dir}", *self.analyst_flags], "cwd": str(self.tmp)},
            "publish": {"publisher": "fake"}, "readings": {"reader": "fake"},
            "review_required_count": 0, "auto_post_enabled": True, "reposts": {"days": [14]},
        }
        ov.update(self.overrides)
        self.cfg = common.load_config(ov)
        self.gate_calls = 0
        self.engine = self.make_engine()
        self.t = lt(2026, 11, 16, 9, 0)
        common.set_now(self.t)

    def tearDown(self):
        common.set_now(None)
        os.environ["REELCAST_LAB_DRY_RUN"] = "1"
        shutil.rmtree(self.tmp, ignore_errors=True)

    def child_env(self, **extra):
        """Environment for a tick.py child process: this test's content root, lock and log in the
        temp folder, on top of whatever override file the test session was started with."""
        over = {}
        base = os.environ.get("REELCAST_LAB_CONFIG")
        if base:
            over = json.loads(Path(base).read_text())
        over = common._merge(over, {"lock_path": str(self.tmp / "test.lock"), "log_path": str(self.tmp / "test.log"),
                                    "timezone": TZ_NAME, "notify": {"enabled": False}})
        over = common._merge(over, extra)
        cfgp = self.tmp / "child-config.json"
        cfgp.write_text(json.dumps(over))
        return dict(os.environ, REELCAST_LAB_ROOT=str(self.root), REELCAST_LAB_CONFIG=str(cfgp),
                    REELCAST_LAB_DRY_RUN="1")

    def make_engine(self, **kw):
        def fake_gate(cfg, run, versions=None):
            self.gate_calls += 1
            return dict(ok=True, font_fail=[], failures=[], checks={})
        kw.setdefault("gate_fn", fake_gate)
        kw.setdefault("sleeper", lambda s: None)
        return Engine(self.cfg, **kw)

    def at(self, t):
        self.t = t
        common.set_now(t)

    def advance(self, **delta):
        self.at(self.t + timedelta(**delta))

    def drop(self, name="IMG_0001.MOV", kind="upright"):
        p = self.root / "RAW" / "Singles" / name
        shutil.copy(clip(kind), p)
        return p

    def wait_jobs(self, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            busy = [r for r in all_runs(self.cfg) if r.d.get("job") and
                    not Path(r.d["job"]["exit_file"]).exists()]
            if not busy:
                return
            time.sleep(0.05)
        self.fail("job did not finish")

    def tick(self, n=1):
        for _ in range(n):
            self.engine.tick()
            self.wait_jobs()

    def run_one(self):
        runs = all_runs(self.cfg)
        self.assertEqual(len(runs), 1)
        return runs[0]

    def drive_until(self, state, step=dict(minutes=10), limit=400):
        if not all_runs(self.cfg):
            self.tick()
        for _ in range(limit):
            r = self.run_one()
            if r.state == state:
                return r
            self.advance(**step)
            self.tick()
        self.fail(f"never reached {state}; at {self.run_one().state}")

    def outbox(self):
        p = self.cfg.lab / "notify-outbox.log"
        return p.read_text() if p.exists() else ""


class TestHappyPath(Base):
    def test_walks_every_state_to_complete(self):
        self.drop()
        self.tick()
        r = self.run_one()
        self.assertEqual(r.state, "waiting_stable")
        self.advance(minutes=10)
        self.tick()           # stable -> ingested -> transcribe launched
        r = self.run_one()
        self.assertIn(r.state, ("ingested", "transcribed", "editing"))
        r = self.drive_until("published", dict(minutes=10), 50)
        self.assertEqual(len(r.d["trials"]), 5)
        times = [datetime.fromisoformat(r.d["trials"][v]["posted_at"]) for v in VERSIONS]
        self.assertEqual([int((b - a).total_seconds()) for a, b in zip(times, times[1:])], [120] * 4)
        r = self.drive_until("decided", dict(hours=1), 100)
        self.assertEqual(set(r.d["readings"]), {"24", "48", "72"})
        self.assertTrue(r.d["readings"]["24"]["provisional"])
        self.assertEqual(r.d["analysis"]["ad_verdict"]["verdict"], "too_early")   # coerced: no history yet
        self.assertIn("confidence). Tap Share with everyone: https://www.instagram.com/reel/FAKE", self.outbox())
        r = self.drive_until("complete", dict(hours=6), 200)
        self.assertEqual(r.d["reposts"][0]["status"], "posted")
        self.assertIn("reading", r.d["reposts"][0])
        self.assertTrue((self.cfg.lab / "dashboard.html").exists())
        steps = [h["result"] for h in r.d["history"]]
        for s in ("waiting_stable -> ingested", "ingested -> transcribed",
                  "transcribed -> editing", "editing -> edited", "edited -> gated", "gated -> queued_publish",
                  "queued_publish -> published", "published -> analyzing", "analyzing -> decided",
                  "decided -> complete"):
            self.assertIn(s, steps)
        self.assertFalse(list((self.root / "RAW" / "Singles").iterdir()))
        self.assertTrue((Path(r.d["originals_dir"]) / "IMG_0001.MOV").exists())


class TestStability(Base):
    def test_needs_two_ticks_ten_minutes_apart(self):
        p = self.drop()
        self.tick()
        self.advance(minutes=5)
        self.tick()
        self.assertEqual(self.run_one().state, "waiting_stable")
        with open(p, "ab") as f:      # still copying: grows
            f.write(b"\0" * 10)
        self.advance(minutes=6)
        self.tick()
        self.assertEqual(self.run_one().state, "waiting_stable")
        self.advance(minutes=9)
        self.tick()
        self.assertEqual(self.run_one().state, "waiting_stable")
        self.advance(minutes=1)
        self.tick()
        self.assertNotEqual(self.run_one().state, "waiting_stable")

    def test_removed_before_stable_is_dropped(self):
        p = self.drop()
        self.tick()
        p.unlink()
        self.advance(minutes=10)
        self.tick()
        self.assertEqual(self.run_one().state, "dropped")


class TestMultiPart(Base):
    def test_order_by_creation_time_then_name(self):
        parts = [dict(name="b.mov", created="2026-11-13T22:30:00-0600"),
                 dict(name="a.mov", created="2026-11-14T04:40:00.000000Z"),
                 dict(name="c.mov", created="2026-11-13T22:30:00-0600"),
                 dict(name="z.mov", created=None)]
        self.assertEqual([p["name"] for p in ingest.order_parts(parts)], ["b.mov", "c.mov", "a.mov", "z.mov"])

    def test_whole_folder_ingested_in_order(self):
        d = self.root / "RAW" / "Multi-part" / "my story"
        d.mkdir()
        for n in ("part2.mov", "part1.mov"):
            shutil.copy(clip(), d / n)
        self.tick()
        self.advance(minutes=4)
        shutil.copy(clip(), d / "part3.mov")     # a new part resets stability
        self.tick()
        self.advance(minutes=8)
        self.tick()
        self.assertEqual(self.run_one().state, "waiting_stable")
        self.advance(minutes=3)
        self.tick()
        r = self.run_one()
        self.assertNotEqual(r.state, "waiting_stable")
        self.assertEqual([p["name"] for p in r.d["parts"]], ["part1.mov", "part2.mov", "part3.mov"])
        self.assertFalse(d.exists())


class TestLimits(Base):
    def test_raw_over_limit_goes_to_needs_attention(self):
        self.cfg["limits"]["raw_max_seconds"] = 0.5
        self.drop()
        self.tick()
        self.advance(minutes=10)
        self.tick()
        r = self.run_one()
        self.assertEqual(r.state, "needs_attention")
        self.assertIn("over the", r.d["needs_attention"])
        self.assertTrue((self.root / "RAW" / "Needs-attention" / r.id / "IMG_0001.MOV").exists())
        self.assertIn("ReelCast Lab needs you", self.outbox())

    def test_six_minute_raw_is_fine_limit_is_on_finished_cut(self):
        self.assertGreaterEqual(self.cfg["limits"]["raw_max_seconds"], 6 * 60)
        self.assertEqual(self.cfg["limits"]["finished_max_seconds"], 240)

    def test_sideways_goes_to_needs_attention(self):
        self.drop(kind="sideways")
        self.tick()
        self.advance(minutes=10)
        self.tick()
        self.assertIn("sideways", self.run_one().d["needs_attention"])

    def test_gate_rejects_finished_cut_over_limit(self):
        import gate
        r = Run.create(self.cfg, "gate-run")
        ed = self.root / "Edited" / "x"
        ed.mkdir()
        r.d["edited_dir"] = str(ed)
        for k, v in enumerate(VERSIONS):
            subprocess.run([need_ffmpeg(), "-v", "error", "-y", "-f", "lavfi", "-i",
                            f"color=c=0x{k}0{k}0{k}0:size=1080x1920:rate=30:duration=3", "-f", "lavfi", "-i",
                            "sine=frequency=300:duration=3", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                            "-c:a", "aac", str(ed / f"{v}.mp4")], check=True)
        (ed / "caption.txt").write_text("A clean caption #AI\n")
        self.cfg["limits"]["finished_max_seconds"] = 1
        res = gate.run_gate(self.cfg, r)
        self.assertFalse(res["ok"])
        self.assertTrue(any("A_control runs" in f for f in res["failures"]))
        self.assertTrue(any("burned captions not found" in f for f in res["failures"]))
        self.assertIn("A_control", res["font_fail"])       # no caption strokes at all


class TestEditorRetry(Base):
    editor_mode = "--fail"

    def test_retry_once_then_needs_attention(self):
        self.drop()
        self.tick()
        self.advance(minutes=10)
        for _ in range(6):
            self.tick()
            self.advance(minutes=10)
        r = self.run_one()
        self.assertEqual(r.state, "needs_attention")
        self.assertEqual(r.attempts("edit"), 2)
        self.assertIn("edit", r.d["needs_attention"])
        # already ingested: the raw stays in Edited, nothing goes back to RAW
        self.assertTrue((Path(r.d["originals_dir"]) / "IMG_0001.MOV").exists())
        self.assertFalse((self.root / "RAW" / "Needs-attention" / r.id).exists())


class TestAnalystRetry(Base):
    analyst_flags = ["--bad-first"]

    def test_invalid_analysis_retried_once(self):
        self.drop()
        r = self.drive_until("decided", dict(hours=2), 200)
        self.assertEqual(r.attempts("analyze"), 2)


class TestGlossary(unittest.TestCase):
    # invented names: a two word product, a name the transcriber mishears, and a word that is only
    # fixed next to certain neighbours
    GLOSSARY = [
        {"display": "Zentro 5.5", "heard": ["zentro 5.5", "zentro 5 5", "zentro five point five", "zentro 55"],
         "action": "replace"},
        {"display": "Lumo 5.2", "heard": ["lumo 5.2", "lumo 5 2", "lumo five point two", "lumo 52"], "action": "replace"},
        {"display": "Brell", "heard": ["bell"], "action": "flag", "note": "Only when the app is meant, not a real bell."},
        {"display": "to-do", "heard": ["to do"], "action": "replace",
         "when_prev": ["a", "the", "my", "your", "each", "every", "one", "this", "that"],
         "when_next": ["list", "lists", "item", "items"]},
    ]

    def test_display_fixes_and_flags(self):
        toks = "I switched from Zentro 5 .5 to Lumo 5 .2. Bell made a to do item. What to do now".split()
        words = [dict(word=w, start=i, end=i + 0.5) for i, w in enumerate(toks)]
        text, flags = transcribe.apply_glossary(words, self.GLOSSARY)
        self.assertEqual((text[4], text[5]), ("5.5", ""))
        self.assertEqual((text[8], text[9]), ("5.2.", ""))
        self.assertEqual((text[13], text[14]), ("to-do", ""))
        self.assertNotIn(10, text)      # the misheard name is only flagged
        self.assertIn(10, [f["index"] for f in flags])
        self.assertIn(17, [f["index"] for f in flags])   # "what to do" flagged, not replaced

    def test_shipped_glossary_is_empty_and_documented(self):
        cfg = json.loads((CODE / "config.json").read_text())
        self.assertEqual(cfg["glossary"], [])
        for word in ("display", "heard", "action", "when_prev"):
            self.assertIn(word, cfg["_glossary"])
        words = [dict(word=w, start=i, end=i + 0.5) for i, w in enumerate("nothing to fix here".split())]
        self.assertEqual(transcribe.apply_glossary(words, cfg["glossary"]), ({}, []))


class TestScoring(unittest.TestCase):
    def m(self, views, comments=0, shares=0, saved=0, likes=0, hold=None):
        return dict(views=views, comments=comments, shares=shares, saved=saved, likes=likes, hold_3s=hold)

    def test_weights_and_winner(self):
        ms = {"A_control": self.m(1000, 10, 5, 5, 50), "B_onscreen_hook": self.m(500, 5, 1, 1, 20),
              "C_cold_open": self.m(400, 2, 0, 0, 10), "D_tight_cut": self.m(300), "E_visual_open": self.m(250)}
        d = scoring.decide(ms)
        self.assertEqual(d["winner"], "A_control")
        self.assertEqual(d["scores"]["A_control"], 100.0)
        self.assertEqual(d["confidence"], "normal")

    def test_tie_goes_to_better_hold(self):
        ms = {v: self.m(1000, 1, 1, 1, 1, hold=50) for v in VERSIONS}
        ms["D_tight_cut"]["hold_3s"] = 70
        d = scoring.decide(ms)
        self.assertEqual(d["winner"], "D_tight_cut")
        self.assertTrue(d["tie"])

    def test_low_confidence_close_or_few_views(self):
        ms = {"A_control": self.m(1000, 10, 5, 5, 50), "B_onscreen_hook": self.m(950, 10, 5, 5, 48)}
        self.assertEqual(scoring.decide(ms)["confidence"], "low")
        ms = {"A_control": self.m(1000, 10, 5, 5, 50), "B_onscreen_hook": self.m(150)}
        d = scoring.decide(ms)
        self.assertEqual(d["confidence"], "low")
        self.assertTrue(any("under 200" in r for r in d["reasons"]))


class TestDueTimes(unittest.TestCase):
    def test_readings_are_real_hours_across_dst(self):
        done = common.iso(lt(2027, 3, 13, 12, 0))          # day before DST starts (Mar 14 2027)
        due = readings.due_times(done, [24, 48, 72])
        self.assertEqual(due[24].astimezone(TZ).hour, 13)    # 24 real hours = 1 PM local summer time
        self.assertEqual((due[72] - due[24]).total_seconds(), 48 * 3600)

    def test_reposts_keep_wall_clock_across_dst(self):
        cfg = common.load_config({"content_root": NOWHERE, "timezone": TZ_NAME})
        sched = analysis.repost_schedule(cfg, common.iso(lt(2027, 3, 1, 10, 0)), [14, 30, 90, 180])
        locals_ = [common.parse_time(s["due"]).astimezone(TZ) for s in sched]
        self.assertEqual([x.strftime("%Y-%m-%d %H:%M") for x in locals_],
                         ["2027-03-15 10:00", "2027-03-31 10:00", "2027-05-30 10:00", "2027-08-28 10:00"])
        fall = analysis.repost_schedule(cfg, common.iso(lt(2026, 10, 20, 9, 30)), [14])
        self.assertEqual(common.parse_time(fall[0]["due"]).astimezone(TZ).strftime("%H:%M"), "09:30")


class TestPublishWindow(Base):
    overrides = {"review_required_count": 0}

    def test_window(self):
        span = publish.set_span(self.cfg)
        self.assertFalse(publish.in_window(self.cfg, lt(2026, 11, 16, 7, 59), span))
        self.assertTrue(publish.in_window(self.cfg, lt(2026, 11, 16, 8, 0), span))
        self.assertTrue(publish.in_window(self.cfg, lt(2026, 11, 16, 19, 52), span))
        self.assertFalse(publish.in_window(self.cfg, lt(2026, 11, 16, 19, 55), span))   # set would end past 8 PM
        self.assertEqual(publish.next_window_start(self.cfg, lt(2026, 11, 16, 21, 0)).strftime("%Y-%m-%d %H:%M"),
                         "2026-11-17 08:00")

    def test_one_set_per_day_queue_in_order(self):
        self.at(lt(2026, 11, 16, 5, 0))
        self.drop("IMG_A.MOV")
        self.tick()
        self.advance(minutes=1)
        self.drop("IMG_B.MOV")
        self.tick()
        for _ in range(12):
            self.advance(minutes=10)
            self.tick()
        states = {r.d["title"]: r.state for r in all_runs(self.cfg)}
        self.assertEqual(states, {"IMG_A": "queued_publish", "IMG_B": "queued_publish"})   # before 8 AM
        self.at(lt(2026, 11, 16, 8, 5))
        self.tick()
        self.advance(hours=3)
        self.tick()
        states = {r.d["title"]: r.state for r in all_runs(self.cfg)}
        self.assertEqual(states, {"IMG_A": "published", "IMG_B": "queued_publish"})
        self.at(lt(2026, 11, 17, 8, 0))
        self.tick()
        states = {r.d["title"]: r.state for r in all_runs(self.cfg)}
        self.assertEqual(states["IMG_B"], "published")
        days = lab_state(self.cfg)["publish_days"]
        self.assertEqual(len(days["2026-11-16"]), 1)
        self.assertEqual(len(days["2026-11-17"]), 1)


class TestNotWired(Base):
    overrides = {"publish": {"publisher": "not_wired"}}

    def test_stays_queued_and_texts_once(self):
        os.environ.pop("REELCAST_LAB_DRY_RUN")     # a practice run would force the fake publisher; notify stays off
        self.engine = self.make_engine()
        self.drop()
        self.drive_until("queued_publish", dict(minutes=10), 20)
        for _ in range(3):
            self.advance(minutes=10)
            self.tick()
        self.assertEqual(self.run_one().state, "queued_publish")
        self.assertEqual(self.outbox().count("reading numbers are coming later"), 1)


class TestPauseAndReview(Base):
    overrides = {"review_required_count": 1}

    def test_pause_flag(self):
        (self.cfg.lab / "PAUSED").write_text("")
        self.drop()
        self.assertEqual(self.engine.tick(), "paused")
        self.assertEqual(all_runs(self.cfg), [])

    def test_review_counts_approvals_not_holds(self):
        self.drop("IMG_A.MOV")
        self.drop("IMG_B.MOV")
        self.tick()
        for _ in range(8):
            self.advance(minutes=10)
            self.tick()
        states = sorted(r.state for r in all_runs(self.cfg))
        self.assertEqual(states, ["awaiting_review", "awaiting_review"])   # no approvals yet: both held
        held = sorted(all_runs(self.cfg), key=lambda r: r.id)[0]
        self.assertRegex(self.outbox(), r"tick\.py'? approve " + held.id)
        subprocess.run([sys.executable, str(CODE / "tick.py"), "approve", held.id], check=True,
                       env=self.child_env(), capture_output=True)
        self.tick()
        self.assertEqual(Run(self.cfg, held.id).state, "published")
        self.assertEqual(lab_state(self.cfg)["review_approvals"], 1)
        self.tick()
        self.assertEqual(lab_state(self.cfg)["review_approvals"], 1)    # counted once
        self.drop("IMG_C.MOV")                                          # 1 approval reached: no hold
        for _ in range(4):
            self.advance(minutes=10)
            self.tick()
        c = next(r for r in all_runs(self.cfg) if r.d["title"] == "IMG_C")
        self.assertEqual(c.state, "queued_publish")

    def test_auto_post_off_holds_every_video(self):
        self.cfg["auto_post_enabled"] = False
        self.cfg["review_required_count"] = 0
        self.drop()
        for _ in range(5):
            self.tick()
            self.advance(minutes=10)
        self.assertEqual(self.run_one().state, "awaiting_review")


class TestNotify(Base):
    def test_dry_run_never_shells_out(self):
        self.cfg["notify"]["enabled"] = True
        with mock.patch("subprocess.run", side_effect=AssertionError("shelled out")) as m:
            notify.send(self.cfg, "T", "body")
            m.assert_not_called()
        self.assertIn("T\tbody", self.outbox())

    def test_disabled_never_shells_out_even_live(self):
        os.environ.pop("REELCAST_LAB_DRY_RUN")
        try:
            with mock.patch("subprocess.run", side_effect=AssertionError("shelled out")):
                notify.send(self.cfg, "T2", "body")
        finally:
            os.environ["REELCAST_LAB_DRY_RUN"] = "1"
        self.assertIn("T2", self.outbox())

    def test_enabled_passes_text_as_arguments_never_as_script(self):
        nasty_title, nasty_body = 'Ready" & (do shell script "id") & "', "it's `here` $(now)\nline two"
        self.cfg["notify"]["enabled"] = True
        os.environ.pop("REELCAST_LAB_DRY_RUN")
        try:
            with mock.patch("subprocess.run", return_value=mock.Mock(returncode=0)) as m:    # nothing is shown
                self.assertTrue(notify.send(self.cfg, nasty_title, nasty_body))
        finally:
            os.environ["REELCAST_LAB_DRY_RUN"] = "1"
        cmd = m.call_args[0][0]
        self.assertEqual(cmd[0], "/usr/bin/osascript")
        self.assertEqual(cmd[-2:], [nasty_title, nasty_body])
        self.assertEqual(cmd[-3], "--")
        script = [cmd[i + 1] for i, x in enumerate(cmd[:-3]) if x == "-e"]
        self.assertTrue(script)
        for line in script:
            self.assertNotIn("Ready", line)
            self.assertNotIn("here", line)
        self.assertFalse(m.call_args[1].get("shell"))
        self.assertEqual(self.outbox(), "")

    def test_shipped_config_is_off(self):
        cfg = json.loads((CODE / "config.json").read_text())
        self.assertFalse(cfg["notify"]["enabled"])
        self.assertFalse(cfg["auto_post_enabled"])
        self.assertEqual(cfg["publish"]["publisher"], "not_wired")
        self.assertEqual(cfg["readings"]["reader"], "not_wired")


class TestLedger(Base):
    def test_append_only_latest_wins(self):
        analysis.ledger_append(self.cfg, [dict(id="v1", row_type="FILMED", title="one")])
        p = analysis.ledger_path(self.cfg)
        first = p.read_text()
        analysis.ledger_append(self.cfg, [dict(id="v1", row_type="FILMED", title="one again"),
                                          dict(id="v2", row_type="POSTED", title="two")])
        self.assertTrue(p.read_text().startswith(first))
        rows = list(csv.DictReader(open(p)))
        self.assertEqual(len(rows), 3)
        self.assertEqual(analysis.ledger_latest(self.cfg)["v1"]["title"], "one again")


class TestAnalysisValidation(unittest.TestCase):
    def good(self):
        return {"summary": "s", "why_stayed_past_3s": "a", "why_stayed_or_left": "b",
                "claims": [{"version": "C", "claim": "x", "label": "supported"}],
                "learning": {"lesson": "l"}, "evergreen": True, "evergreen_until": None,
                "repost_captions": ["1", "2", "3", "4"], "ad_verdict": {"verdict": "no", "reason": "default"}}

    def test_valid_and_errors(self):
        self.assertEqual(analysis.validate(self.good(), 0)[0], [])
        bad = self.good()
        bad["claims"][0]["label"] = "maybe"
        bad["repost_captions"] = ["1"]
        bad["summary"] = "a \u2014 b"
        errs, _ = analysis.validate(bad, 0)
        self.assertEqual(len(errs), 3)

    def test_yes_needs_eight_past_videos(self):
        a = self.good()
        a["ad_verdict"] = {"verdict": "yes", "reason": "r", "primary_text": "p", "headline": "h", "button": "b", "audience": "a"}
        errs, notes = analysis.validate(a, 3)
        self.assertEqual(errs, [])
        self.assertEqual(a["ad_verdict"]["verdict"], "too_early")
        a["ad_verdict"] = {"verdict": "yes", "reason": "r"}
        self.assertTrue(analysis.validate(a, 9)[0])


class TestReadingsRetry(Base):
    def test_empty_answers_retried_then_provisional(self):
        class EmptyReader(readings.FakeReader):
            def read(self, media_id, age_hours=None):
                return None if age_hours == 48 else super().read(media_id, age_hours)
        self.engine = self.make_engine(reader=EmptyReader(self.cfg))
        self.drop()
        r = self.drive_until("published", dict(minutes=10), 40)
        done = datetime.fromisoformat(r.d["set_done_at"])
        self.at(done + timedelta(hours=48))
        self.tick()
        r = self.run_one()
        self.assertNotIn("48", r.d.get("readings", {}))
        self.at(done + timedelta(hours=54, minutes=1))
        self.tick()
        r = self.run_one()
        self.assertTrue(r.d["readings"]["48"]["provisional"])
        self.assertEqual(len(r.d["readings"]["48"]["empty_versions"]), 5)

    def test_derived_numbers(self):
        d = readings.derive(dict(reels_skip_rate=35.5, ig_reels_avg_watch_time=30000), 60.0)
        self.assertEqual((d["hold_3s"], d["avg_pct_watched"]), (64.5, 50.0))


if __name__ == "__main__":
    unittest.main()
