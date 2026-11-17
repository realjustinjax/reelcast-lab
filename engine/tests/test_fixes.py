"""Tests for fixes found in review rounds: safety around posting, one model job at a time,
error handling, and the messages the owner gets."""
import os
import subprocess
import sys
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from tests.test_engine import CODE, NOWHERE, Base, lt

import common  # noqa: E402
import gate  # noqa: E402
import jobs  # noqa: E402
import publish  # noqa: E402
import readings  # noqa: E402
import scoring  # noqa: E402
from state import VERSIONS, Run, all_runs  # noqa: E402


# 2 ---------------------------------------------------------------- a practice run forces fakes
class TestDryRunFakes(Base):
    overrides = {"publish": {"publisher": "not_wired"}, "readings": {"reader": "not_wired"}}

    def test_dry_run_gets_fakes_and_refuses_real(self):
        self.assertIsInstance(publish.get_publisher(self.cfg), publish.FakePublisher)
        self.assertIsInstance(readings.get_reader(self.cfg), readings.FakeReader)
        r = Run.create(self.cfg, "x")
        with self.assertRaises(RuntimeError):
            publish.post_set(self.cfg, r, publish.NotWiredPublisher(), [("A_control", "a.mp4")], "c", {})
        os.environ.pop("REELCAST_LAB_DRY_RUN")
        try:
            self.assertIsInstance(publish.get_publisher(self.cfg), publish.NotWiredPublisher)
        finally:
            os.environ["REELCAST_LAB_DRY_RUN"] = "1"


# 3 ---------------------------------------------------------------- analysis failure after posting
class TestAnalysisNeverTerminal(Base):
    analyst_flags = ["--bad-always"]

    def test_decides_anyway_and_keeps_reposts(self):
        self.drop()
        r = self.drive_until("decided", dict(hours=2), 200)
        self.assertTrue(r.d["analysis_missing"])
        self.assertTrue(r.d["reposts"])
        self.assertIn("Tap Share with everyone", self.outbox())
        self.assertIn("analysis failed", self.outbox())
        for _ in range(3):
            self.advance(minutes=10)
            self.tick()
        r = self.run_one()
        self.assertEqual(r.attempts("analyze"), 3)
        self.assertFalse(r.d["analysis_retry"])
        self.assertIn(r.state, ("decided", "complete"))

    def test_fail_after_publish_keeps_state_and_raw(self):
        self.drop()
        r = self.drive_until("published", dict(minutes=10), 40)
        self.engine.fail(r, "something odd")
        r = self.run_one()
        self.assertEqual(r.state, "published")
        self.assertTrue((Path(r.d["originals_dir"]) / "IMG_0001.MOV").exists())
        self.assertIn("already posted", self.outbox())


# 4 ---------------------------------------------------------------- one model job at a time
class TestOneJob(Base):
    editor_mode = "--slow"

    def test_never_two_model_jobs(self):
        self.drop("IMG_A.MOV")
        self.drop("IMG_B.MOV")
        self.engine.tick()
        self.advance(minutes=10)
        peak = 0
        end = time.time() + 40
        while time.time() < end:
            self.engine.tick()
            alive = [r for r in all_runs(self.cfg) if r.d.get("job") and r.d["job"]["kind"] in jobs.HEAVY
                     and jobs.is_running(r.d["job"])]
            peak = max(peak, len(alive))
            if all(r.state not in ("waiting_stable", "ingested", "transcribed", "editing")
                   for r in all_runs(self.cfg)):
                break
            time.sleep(0.2)
        self.assertEqual(peak, 1)
        self.assertTrue(all(r.state in ("queued_publish", "published") for r in all_runs(self.cfg)))

    def test_running_means_alive_and_kill_escalates(self):
        r = Run.create(self.cfg, "k")
        j = jobs.launch(r, "edit", [sys.executable, "-c",
                                    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"],
                        timeout_s=0)
        time.sleep(0.5)
        self.assertEqual(jobs.check(j), "timeout")
        self.assertTrue(jobs.is_running(j))          # past its deadline but alive: still holds the slot
        self.assertTrue(jobs.kill(j, term_wait=0.5))  # SIGTERM ignored, SIGKILL works
        self.assertFalse(jobs.pid_alive(j["pid"]))


# 5 ---------------------------------------------------------------- errors
class TestErrors(Base):
    def test_three_errors_before_publish_go_to_needs_attention(self):
        def boom(cfg, run, versions=None):
            raise ValueError("gate exploded")
        self.engine = self.make_engine(gate_fn=boom)
        self.drop()
        for _ in range(8):
            self.advance(minutes=10)
            self.tick()
        r = self.run_one()
        self.assertEqual(r.state, "needs_attention")
        self.assertEqual(self.outbox().count("errors in a row"), 2)   # the error text plus the needs-attention text

    def test_bad_head_of_queue_does_not_block(self):
        class Picky(publish.FakePublisher):
            def publish_trial(self, file, caption):
                if "img-a" in str(file):
                    raise RuntimeError("upload failed")
                return super().publish_trial(file, caption)
        self.engine = self.make_engine(publisher=Picky(self.cfg))
        self.at(lt(2026, 11, 16, 6, 0))
        self.drop("IMG_A.MOV")
        self.tick()
        self.advance(minutes=1)
        self.drop("IMG_B.MOV")
        for _ in range(8):
            self.advance(minutes=10)
            self.tick()
        self.at(lt(2026, 11, 16, 9, 0))
        for _ in range(5):
            self.tick()
            self.advance(minutes=10)
        states = {r.d["title"]: r.state for r in all_runs(self.cfg)}
        a = next(r for r in all_runs(self.cfg) if r.d["title"] == "IMG_A")
        # the failed upload may or may not be live, so A is blocked (never re-posted) and texted once
        self.assertEqual(states["IMG_A"], "queued_publish")
        self.assertTrue(a.d["publish_blocked"])
        self.assertEqual(self.outbox().count("result was never"), 1)
        self.assertEqual(states["IMG_B"], "published")

    def test_erroring_head_of_queue_leaves_after_three(self):
        self.at(lt(2026, 11, 16, 6, 0))
        self.drop("IMG_A.MOV")
        self.tick()
        self.advance(minutes=1)
        self.drop("IMG_B.MOV")
        for _ in range(8):
            self.advance(minutes=10)
            self.tick()
        a = next(r for r in all_runs(self.cfg) if r.d["title"] == "IMG_A")
        (a.edited_dir / "caption.txt").unlink()        # every publish attempt of A now raises
        self.at(lt(2026, 11, 16, 9, 0))
        for _ in range(5):
            self.tick()
            self.advance(minutes=10)
        states = {r.d["title"]: r.state for r in all_runs(self.cfg)}
        self.assertEqual(states["IMG_A"], "needs_attention")
        self.assertEqual(states["IMG_B"], "published")

    def test_scan_error_does_not_stop_tick(self):
        with mock.patch("ingest.scan", side_effect=OSError("disk hiccup")):
            self.assertEqual(self.engine.tick(), "ok")


# 6 ---------------------------------------------------------------- publishing safety
class TestPublishSafety(Base):
    def setUp(self):
        super().setUp()
        self.ed = self.root / "Edited" / "v"
        self.ed.mkdir()
        self.files = [(v, self.ed / f"{v}.mp4") for v in VERSIONS]
        self.run_ = Run.create(self.cfg, "p")

    def test_paused_mid_set_stops_then_resumes(self):
        lab = self.cfg.lab

        class Pauser(publish.FakePublisher):
            n = 0

            def publish_trial(self, file, caption):
                Pauser.n += 1
                if Pauser.n == 2:
                    (lab / "PAUSED").write_text("")
                return super().publish_trial(file, caption)
        p = Pauser(self.cfg)
        self.assertEqual(publish.post_set(self.cfg, self.run_, p, self.files, "c", {}, lambda s: None), "stopped")
        self.assertEqual(len(self.run_.d["trials"]), 2)
        (lab / "PAUSED").unlink()
        self.assertEqual(publish.post_set(self.cfg, self.run_, p, self.files, "c", {}, lambda s: None), "done")
        self.assertEqual(len(self.run_.d["trials"]), 5)

    def test_window_closes_mid_set(self):
        self.run_.d["trials"] = {"A_control": {}, "B_onscreen_hook": {}}
        self.at(lt(2026, 11, 16, 20, 1))
        p = publish.FakePublisher(self.cfg)
        self.assertEqual(publish.post_set(self.cfg, self.run_, p, self.files, "c", {}, lambda s: None), "stopped")
        self.assertEqual(len(self.run_.d["trials"]), 2)

    def test_intent_without_result_never_reposts(self):
        self.run_.d["trials"] = {"A_control": {}}
        self.run_.d["publish_intent"] = {"version": "B_onscreen_hook", "at": "2026-11-16T15:00:00+00:00"}
        p = publish.FakePublisher(self.cfg)
        with self.assertRaises(publish.Blocked):
            publish.post_set(self.cfg, self.run_, p, self.files, "c", {}, lambda s: None)
        self.assertFalse(p.path.exists())     # nothing posted


# 12 --------------------------------------------------------------- speech start
class TestSpeechStart(unittest.TestCase):
    def test_leading_silence_up_to_point_one(self):
        cfg = common.load_config({"content_root": NOWHERE})
        fake = mock.Mock(stderr="[silencedetect] silence_start: 0.064\n[silencedetect] silence_end: 0.5 | x\n")
        with mock.patch("gate.sh", return_value=fake):
            self.assertEqual(gate.speech_start(cfg, "f.mp4"), 0.5)
        fake.stderr = "[silencedetect] silence_start: 0.3\n[silencedetect] silence_end: 0.9\n"
        with mock.patch("gate.sh", return_value=fake):
            self.assertEqual(gate.speech_start(cfg, "f.mp4"), 0.0)


# 16 --------------------------------------------------------------- texts and re-read
class TestWinnerTexts(Base):
    def test_thin_results_do_not_ask_to_share(self):
        class Thin(readings.FakeReader):
            def read(self, media_id, age_hours=None):
                return dict(views=40, likes=2, comments=0, shares=0, saved=0, reels_skip_rate=50)
        self.engine = self.make_engine(reader=Thin(self.cfg))
        self.drop()
        self.drive_until("complete", dict(hours=2), 200)   # thin: no reposts, so decided -> complete
        self.assertIn("too thin to call", self.outbox())
        self.assertNotIn("Tap Share", self.outbox())
        self.assertEqual(self.run_one().d["winner"]["confidence"], "low")

    def test_provisional_72h_reread_at_96h(self):
        class Late(readings.FakeReader):
            def read(self, media_id, age_hours=None):
                return None if age_hours == 72 else super().read(media_id, age_hours)
        self.engine = self.make_engine(reader=Late(self.cfg))
        self.drop()
        r = self.drive_until("published", dict(minutes=10), 40)
        done = common.parse_time(r.d["set_done_at"])
        self.at(done + timedelta(hours=79))
        self.tick()
        r = self.run_one()
        self.assertTrue(r.d["readings"]["72"]["provisional"])
        self.assertEqual(r.state, "published")
        self.at(done + timedelta(hours=96))
        self.tick()
        r = self.run_one()
        self.assertTrue(r.d["readings"]["72"]["reread"])
        self.assertFalse(r.d["readings"]["72"]["provisional"])
        self.assertIn(r.state, ("analyzing", "decided"))


# 17 --------------------------------------------------------------- tiny metric
class TestTinyMetric(unittest.TestCase):
    def test_lead_on_one_comment_is_low_confidence(self):
        base = dict(views=1000, likes=50, shares=10, saved=10)
        ms = {"A_control": dict(base, comments=1), "B_onscreen_hook": dict(base, comments=0),
              "C_cold_open": dict(base, views=500, comments=0)}
        d = scoring.decide(ms)
        self.assertEqual(d["winner"], "A_control")
        self.assertEqual(d["confidence"], "low")
        self.assertTrue(any("rests on comments" in r for r in d["reasons"]))


if __name__ == "__main__":
    unittest.main()


# final round (checker findings) -----------------------------------------------
class TestFinalRound(Base):
    def test_thin_result_schedules_no_reposts(self):
        class Thin(readings.FakeReader):
            def read(self, media_id, age_hours=None):
                return dict(views=40, likes=2, comments=0, shares=0, saved=0, reels_skip_rate=50)
        self.engine = self.make_engine(reader=Thin(self.cfg))
        self.drop()
        self.drive_until("complete", dict(hours=2), 200)
        self.assertEqual(self.run_one().d["reposts"], [])

    def test_corrupt_run_json_is_skipped(self):
        bad = self.cfg.runs_dir / "zz-bad"
        bad.mkdir(parents=True)
        (bad / "run.json").write_text("{not json")
        Run.create(self.cfg, "good")
        ids = [r.id for r in all_runs(self.cfg)]
        self.assertIn("good", ids)
        self.assertNotIn("zz-bad", ids)

    def test_unblock_command_clears_both_flags(self):
        r = Run.create(self.cfg, "blk")
        r.d["publish_intent"] = {"version": "B_onscreen_hook", "at": "x"}
        r.d["publish_blocked"] = True
        r.save()
        out = subprocess.run([sys.executable, str(CODE / "tick.py"), "unblock", "blk"], env=self.child_env(),
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        r = Run(self.cfg, "blk")
        self.assertNotIn("publish_intent", r.d)
        self.assertNotIn("publish_blocked", r.d)
