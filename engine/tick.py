#!/usr/bin/env python3
"""ReelCast Lab tick: the launchd entry point (every 10 minutes). Standard library only.

  tick.py [--now ISO]              advance every video one or more steps (idempotent)
  tick.py approve RUN_ID           approve a video held for review (same as touch <run>/APPROVED)
  tick.py retry RUN_ID             send a Needs-attention video back to the step that failed
  tick.py status                   one line per run
  tick.py dashboard                rebuild Lab_Data/dashboard.html

What works today: a video dropped into RAW is transcribed, edited into five versions, checked,
and held for review. Posting and reading numbers are coming later, so every video stops at
the review hold.

The tick holds a lock, logs, never waits on model work (it launches background jobs and checks
them later), and never runs two model jobs at once. If <content root>/Lab_Data/PAUSED exists it
only logs.
Env: REELCAST_LAB_ROOT (content root), REELCAST_LAB_CONFIG (extra settings JSON),
REELCAST_LAB_DRY_RUN=1 (messages go to the outbox, only fake posting and reading, no sleeping
between trials), REELCAST_LAB_NOW (fake clock).
"""
import fcntl
import json
import os
import shlex
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analysis  # noqa: E402
import dashboard  # noqa: E402
import editing  # noqa: E402
import gate as gate_mod  # noqa: E402
import ingest  # noqa: E402
import jobs  # noqa: E402
import notify  # noqa: E402
import publish  # noqa: E402
import readings as rd  # noqa: E402
import scoring  # noqa: E402
import transcribe  # noqa: E402
from common import CODE_DIR, ensure_content_dirs, iso, load_config, log, now, parse_time, tool_env  # noqa: E402
from state import (FAILED, TERMINAL, VERSIONS, Run, all_runs, lab_state, move_to_needs_attention,  # noqa: E402
                   save_lab_state)

MAX_ERRORS = 3
MIN_WORDS = 30


class Engine:
    def __init__(self, cfg, publisher=None, reader=None, gate_fn=None, sleeper=None):
        self.cfg = cfg
        self._publisher = publisher
        self._reader = reader
        self.gate_fn = gate_fn or gate_mod.run_gate
        self.sleeper = sleeper or publish.default_sleeper
        self.dash_dirty = False

    @property
    def publisher(self):
        if self._publisher is None:
            self._publisher = publish.get_publisher(self.cfg)
        return self._publisher

    @property
    def reader(self):
        if self._reader is None:
            self._reader = rd.get_reader(self.cfg)
        return self._reader

    # ------------------------------------------------------------ helpers
    def text(self, title, body):
        notify.send(self.cfg, title, body)

    def text_once(self, run, key, title, body):
        sent = run.d.setdefault("texts_sent", [])
        if key not in sent:
            sent.append(key)
            run.save()
            self.text(title, body)

    def fail(self, run, reason):
        """Needs-attention. Raw files move to RAW/Needs-attention only while they are still in RAW;
        once ingested they stay in Edited. After posting, nothing moves and the state is kept."""
        j = run.d.get("job")
        if j and jobs.is_running(j):
            jobs.kill(j)
        if run.d.get("trials"):
            run.d["attention"] = reason
            run.event(run.state, "needs attention (state kept, already posted)", detail=reason)
            log(self.cfg, f"[{run.id}] needs attention after posting: {reason}")
            self.text_once(run, "attention:" + reason[:60], "ReelCast Lab needs you",
                           f"{run.d.get('title')}: {reason}. It is already posted, so it stays where it is.")
            return False
        raw_root = self.cfg.root / "RAW"
        in_raw = any(str(p).startswith(str(raw_root) + os.sep) and "Needs-attention" not in str(p)
                     for p in run.d.get("raw_paths", []))
        if in_raw:
            dest = move_to_needs_attention(self.cfg, run)
            where = f"Raw files are in RAW/Needs-attention/{dest.name}."
        else:
            where = f"Your original files are safe in Originals/{run.edited_dir.name}." if run.edited_dir else ""
        run.d["needs_attention"] = reason
        run.to(FAILED, "needs_attention", detail=reason)
        log(self.cfg, f"[{run.id}] needs attention: {reason}")
        self.text("ReelCast Lab needs you", f"{run.d.get('title')}: {reason}. {where}")
        return False

    def launch_if_free(self, run, kind, cmd, timeout_min, cwd=None, env=None):
        """The only way a job starts (first tries and retries alike): one model job at a time."""
        busy = jobs.heavy_running(all_runs(self.cfg))
        mine = run.d.get("job")
        if busy is not None or (mine and jobs.is_running(mine)):
            return False
        attempt = run.bump(kind)
        jobs.launch(run, kind, cmd, cwd=cwd, env=env or tool_env(self.cfg), timeout_s=timeout_min * 60,
                    attempt=attempt)
        run.event(kind, f"job launched (attempt {attempt})", detail=" ".join(str(c) for c in cmd[:3]))
        log(self.cfg, f"[{run.id}] {kind} launched, attempt {attempt}")
        return True

    def job_status(self, run, kind):
        j = run.d.get("job")
        if not j or j.get("kind") != kind:
            return None
        st = jobs.check(j)
        if st == "timeout":
            if not j.get("extended") and jobs.recent_activity([run.dir, run.edited_dir]):
                j["extended"] = True
                j["deadline_real"] += 30 * 60
                run.event(kind, "past its time limit but still writing files: extended once by 30 minutes")
                return "running"
            if not jobs.kill(j):
                run.event(kind, "timed out and could not be stopped yet")
                return "running"
        return st

    def job_failed(self, run, kind, st, retries, relaunch):
        """A job ended badly: relaunch while retries remain, else Needs-attention."""
        why = "timed out" if st == "timeout" else ("stopped unexpectedly" if st == "lost" else f"exited {st[1]}")
        run.event(kind, f"job {why}")
        run.d["job"] = None
        run.save()
        if run.attempts(kind) <= retries:
            relaunch()
            return False
        return self.fail(run, f"{kind} {why} after {run.attempts(kind)} attempts")

    # ------------------------------------------------------------ the tick
    def tick(self):
        cfg = self.cfg
        lock_p = Path(os.path.expanduser(cfg["lock_path"]))
        lock_p.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_p, "w") as lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(cfg, "tick skipped: another tick holds the lock")
                return "locked"
            if (cfg.lab / "PAUSED").exists():
                log(cfg, "tick paused: Lab_Data/PAUSED exists, doing nothing")
                return "paused"
            ensure_content_dirs(cfg)
            if str(cfg["editor"]["command"][0]) == "{claude}":      # a real install, not a test or practice run
                import doctor
                missing = doctor.problems(cfg)
                if missing:
                    log(cfg, "tick skipped, nothing was touched. Missing: " + "; ".join(missing)
                        + ". Run: python3 engine/tick.py doctor")
                    return "missing"
            try:
                for r in ingest.scan(cfg):
                    r.d["stable"] = True
                    r.save()
            except Exception as e:     # one odd file in RAW never stops the tick
                log(cfg, f"scan error: {e!r}")
            for run in all_runs(cfg):
                if run.state in TERMINAL:
                    continue
                for _ in range(15):
                    before = (run.state, len(run.d.get("history", [])))
                    try:
                        moved = self.advance(run)
                        if run.d.get("error_count"):
                            run.d["error_count"] = 0
                            run.save()
                    except Exception as e:  # one bad run never stops the others
                        self.on_error(run, e)
                        break
                    if not moved or run.state in TERMINAL or (run.state, len(run.d.get("history", []))) == before:
                        break
            if self.dash_dirty:
                try:
                    dashboard.build(cfg)
                except Exception as e:
                    log(cfg, f"dashboard error: {e!r}")
            return "ok"

    def on_error(self, run, e):
        n = run.d.get("error_count", 0) + 1
        run.d["error_count"] = n
        log(self.cfg, f"[{run.id}] error {n} in {run.state}: {e!r}")
        run.event(run.state, f"error {n} in a row", detail=repr(e)[:300])
        if n >= MAX_ERRORS:
            self.text_once(run, f"errors:{run.state}", "ReelCast Lab needs you",
                           f"{run.d.get('title')} hit {n} errors in a row in step {run.state}: {repr(e)[:160]}")
            if not run.d.get("trials"):
                try:
                    self.fail(run, f"{n} errors in a row in step {run.state}: {repr(e)[:200]}")
                except Exception as e2:   # a failing fail() must not stop the other runs
                    log(self.cfg, f"[{run.id}] could not move to Needs-attention: {e2!r}")

    def advance(self, run):
        fn = getattr(self, "s_" + run.state, None)
        return fn(run) if fn else False

    # ------------------------------------------------------------ steps
    def s_waiting_stable(self, run):
        if not run.d.get("stable"):
            return False
        return ingest.ingest(self.cfg, run, lambda reason: self.fail(run, reason))

    def s_ingested(self, run):
        cfg, kind = self.cfg, "transcribe"
        tc = cfg.get("transcribe", {})
        st = self.job_status(run, kind)
        go = lambda: self.launch_if_free(run, kind, transcribe.command(cfg, run), tc.get("timeout_minutes", 60))  # noqa: E731
        if st is None:
            go()
            return False
        if st == "running":
            return False
        if st == ("done", 0) and (run.work / "transcript.json").exists():
            run.d["job"] = None
            n, nfix, nflag = transcribe.postprocess(cfg, run)
            if n < MIN_WORDS:
                return self.fail(run, f"the transcript has only {n} words (fewer than {MIN_WORDS}); "
                                      f"nothing to edit")
            run.to("transcribed", "transcribe", detail=f"{n} words, {nfix} glossary fixes, {nflag} flags")
            return True
        if st == ("done", 0):
            st = ("done", "0 but no transcript")
        return self.job_failed(run, kind, st, tc.get("retries", 1), go)

    def _launch_editor(self, run):
        cfg = self.cfg
        done = editing.done_path(run)
        if done.exists():
            done.unlink()
        try:
            brief = editing.write_brief(cfg, run)
            cmd = editing.editor_command(cfg, run, brief)
        except (OSError, ValueError) as e:
            return self.fail(run, f"could not prepare the edit: {e}")
        return self.launch_if_free(run, "edit", cmd, cfg["editor"]["timeout_minutes"],
                                   cwd=editing.model_cwd(cfg, "editor", run), env=editing.editor_env(cfg))

    def s_transcribed(self, run):
        if not self._launch_editor(run):
            return False
        run.to("editing", "edit")
        return True

    def s_editing(self, run):
        st = self.job_status(run, "edit")
        if st is None:      # job record lost (should not happen): relaunch
            self._launch_editor(run)
            return False
        if st == "running":
            return False
        marker = editing.done_marker(run)
        if st[0] == "done" and marker:
            run.d["job"] = None
            run.d["edit_result"] = marker
            run.to("edited", "edit", detail=f"DONE: {marker}")
            return True
        if st[0] == "done":
            st = ("done", f"{st[1]} without a DONE marker")
        return self.job_failed(run, "edit", st, self.cfg["editor"]["retries"] + run.attempts("gate_redo"),
                               lambda: self._launch_editor(run))

    def s_edited(self, run):
        cfg = self.cfg
        st = self.job_status(run, "rerender")
        if st == "running":
            return False
        if st is not None:
            run.d["job"] = None
            run.event("rerender", f"job ended {st}")
        res = self.gate_fn(cfg, run)
        run.d["gate"] = dict(ok=res["ok"], failures=res["failures"], font_fail=res["font_fail"],
                             checks=res.get("checks", {}), at=iso(now()))
        if res["ok"]:
            run.to("gated", "gate", detail="all checks passed")
            return True
        if res["failures"]:
            summary = "; ".join(res["failures"])[:1500]
            run.d["gate"]["summary"] = summary
            if run.attempts("gate_redo") < 1:
                run.bump("gate_redo")
                run.to("transcribed", "gate", detail="back to the editor once: " + summary[:300])
                return True
            return self.fail(run, "quality gate: " + summary[:600])
        plan = run.work / "edit_plan.json"
        if run.attempts("rerender") < cfg["gate"]["font_rerenders"] and plan.exists():
            cmd = [cfg["sandbox_python"], str(CODE_DIR / "render_reel.py"), "render", str(plan), *res["font_fail"]]
            if self.launch_if_free(run, "rerender", cmd, cfg["rerender"]["timeout_minutes"]):
                run.event("gate", "caption font check failed, re-rendering", detail=", ".join(res["font_fail"]))
            return False
        return self.fail(run, "caption font check failed (thin or fallback font) in " + ", ".join(res["font_fail"]))

    def s_gated(self, run):
        cfg = self.cfg
        lab = lab_state(cfg)
        approvals = lab.get("review_approvals", 0)
        if not cfg.get("auto_post_enabled", False) or approvals < cfg["review_required_count"]:
            run.to("awaiting_review", "review",
                   detail=(f"{approvals} of {cfg['review_required_count']} approvals so far"
                           if cfg.get("auto_post_enabled") else "automatic posting is off"))
            self.text("ReelCast Lab", f"5 versions of {run.d.get('title')} are ready in Edited/{run.edited_dir.name}. "
                                      f"Watch them and read REPORT.md there. Posting is coming later; to mark this "
                                      f"video approved: python3 {shlex.quote(str(CODE_DIR / 'tick.py'))} approve {run.id}")
            return True
        run.to("queued_publish", "review", detail="no review needed")
        return True

    def s_awaiting_review(self, run):
        if (run.dir / "APPROVED").exists():
            if not run.d.get("approval_counted"):
                lab = lab_state(self.cfg)
                lab["review_approvals"] = lab.get("review_approvals", 0) + 1
                save_lab_state(self.cfg, lab)
                run.d["approval_counted"] = True
            run.to("queued_publish", "review", detail="approved")
            return True
        return False

    def s_queued_publish(self, run):
        cfg = self.cfg
        t = now()
        if run.d.get("publish_blocked"):
            return False
        queue = sorted((r for r in all_runs(cfg) if r.state == "queued_publish"
                        and r.d.get("error_count", 0) < MAX_ERRORS and not r.d.get("publish_blocked")),
                       key=lambda r: (r.d.get("state_since", ""), r.id))
        if queue and queue[0].id != run.id and not run.d.get("trials"):
            return False
        lab = lab_state(cfg)
        started = bool(run.d.get("trials"))
        if not started and not publish.in_window(cfg, t, publish.set_span(cfg)):
            run.d["next_due"] = iso(publish.next_window_start(cfg, t))
            run.save()
            return False
        if not publish.set_allowed_today(cfg, lab, t, run.id):
            run.d["next_due"] = iso(publish.next_window_start(cfg, t))
            run.save()
            return False
        caption = (run.edited_dir / "caption.txt").read_text().strip()
        files = [(v, run.edited_dir / f"{v}.mp4") for v in VERSIONS]
        try:
            result = publish.post_set(cfg, run, self.publisher, files, caption, lab, self.sleeper)
        except publish.NotWired as e:
            run.event("publish", "not wired", detail=str(e))
            self.text_once(run, "not_wired_publish", "ReelCast Lab", f"{run.d.get('title')} is ready to post, but {e}")
            return False
        except publish.Blocked as e:
            run.d["publish_blocked"] = str(e)
            run.event("publish", "blocked", detail=str(e))
            self.text_once(run, "publish_blocked", "ReelCast Lab needs you", f"{run.d.get('title')}: {e}")
            return False
        finally:
            save_lab_state(cfg, lab)
        run.save()
        if result != "done":
            run.d["next_due"] = iso(publish.next_window_start(cfg, now()))
            run.save()
            return False
        tr = run.d["trials"]
        run.d["set_done_at"] = max(x["posted_at"] for x in tr.values())
        run.to("published", "publish", detail="5 trial reels up")
        links = " ".join(f"{v[0]}: {tr[v]['permalink']}" for v in VERSIONS)
        self.text("ReelCast Lab", f"5 trial reels of {run.d.get('title')} are up. {links}")
        return True

    def _lengths(self, run):
        out = {}
        vp = run.edited_dir / "variants.json"
        if vp.exists():
            for v in json.loads(vp.read_text()).get("versions", []):
                out[v.get("name") or next((n for n in VERSIONS if n[0] == v["id"]), v["id"])] = v.get("length_s")
        return out

    def _read_all(self, run, h):
        lengths = self._lengths(run)
        vals, empty = {}, []
        for v, tr in run.d["trials"].items():
            m = self.reader.read(tr["media_id"], age_hours=h)
            if rd.empty(m):
                empty.append(v)
            vals[v] = rd.derive(m or {}, lengths.get(v))
        return vals, empty

    def s_published(self, run):
        cfg = self.cfg
        t = now()
        hrs = cfg["readings"]["hours"]
        dues = rd.due_times(run.d["set_done_at"], hrs)
        reads = run.d.setdefault("readings", {})
        try:
            for h in hrs:
                if str(h) in reads:
                    continue
                due = dues[h]
                if t < due:
                    run.d["next_due"] = iso(due)
                    run.save()
                    return False
                vals, empty = self._read_all(run, h)
                give_up = t >= due + timedelta(hours=cfg["readings"]["retry_hours"])
                if empty and not give_up:
                    run.d.setdefault("reading_retries", {})[str(h)] = run.d.get("reading_retries", {}).get(str(h), 0) + 1
                    run.event("readings", f"{h}h reading empty for {', '.join(empty)}, will retry")
                    return False
                late = (t - due).total_seconds() > cfg["tick_seconds"]
                reads[str(h)] = dict(taken_at=iso(t), due=iso(due), late=late,
                                     provisional=bool(empty) or h == hrs[0], empty_versions=empty, versions=vals)
                run.event("readings", f"{h}h reading taken" + (" (provisional)" if reads[str(h)]["provisional"] else ""))
                self.dash_dirty = True
            last = str(hrs[-1])
            if reads[last].get("empty_versions") and not reads[last].get("reread"):
                due = rd.due_times(run.d["set_done_at"], [96])[96]
                if t < due:
                    run.d["next_due"] = iso(due)
                    run.save()
                    return False
                vals, empty = self._read_all(run, 96)
                if len(empty) < len(reads[last]["empty_versions"]):
                    reads[last].update(versions=vals, empty_versions=empty, provisional=bool(empty))
                reads[last]["reread"] = iso(t)
                run.event("readings", f"provisional {last}h reading re-read at 96h", detail=f"{len(empty)} still empty")
                self.dash_dirty = True
        except rd.NotWired as e:
            run.event("readings", "not wired", detail=str(e))
            self.text_once(run, "not_wired_reader", "ReelCast Lab", f"{run.d.get('title')}: {e}")
            return False
        run.to("analyzing", "readings", detail="all readings in")
        return True

    def _decision(self, run):
        final = run.d["readings"][str(self.cfg["readings"]["hours"][-1])]
        dec = scoring.decide(final["versions"])
        views = [(m.get("views") or 0) for m in final["versions"].values()]
        thin = len(final.get("empty_versions", [])) == len(final["versions"]) or all(v < 200 for v in views)
        if thin:
            dec["confidence"] = "low"
            dec["thin"] = True
            dec["reasons"].append("results too thin to call")
        return dec

    def _launch_analyst(self, run, errors=None):
        cfg = self.cfg
        if jobs.heavy_running(all_runs(cfg)) is not None:
            return False
        dec = self._decision(run)
        run.d["winner"] = dec
        comments = {}
        try:
            for v, tr in run.d["trials"].items():
                comments[v] = self.reader.comments(tr["media_id"])
        except Exception:
            comments = {}
        for p in (run.edited_dir / "analysis.json", run.work / "ANALYSIS_DONE"):
            if p.exists():
                p.unlink()
        payload = dict(readings=run.d["readings"], decision=dec)
        brief = analysis.write_brief(cfg, run, payload, comments, errors)
        return self.launch_if_free(run, "analyze", analysis.analyst_command(cfg, run, brief),
                                   cfg["analyst"]["timeout_minutes"], cwd=editing.model_cwd(cfg, "analyst", run))

    def _collect_analysis(self, run, st):
        """Returns (analysis or None, errors)."""
        ap = run.edited_dir / "analysis.json"
        if st[0] != "done" or st[1] != 0:
            return None, [f"analyst job ended {st}"]
        if not ap.exists():
            return None, ["analysis.json was not written"]
        try:
            a = json.loads(ap.read_text())
        except ValueError as e:
            return None, [f"analysis.json is not valid JSON ({e})"]
        past = len(analysis.history(self.cfg, run.id))
        errors, notes = analysis.validate(a, past, self.cfg["past_videos_for_ad_verdict"])
        if errors:
            return None, errors
        if notes:
            ap.write_text(json.dumps(a, indent=1))
            run.event("analysis", "; ".join(notes))
        return a, []

    def s_analyzing(self, run):
        cfg = self.cfg
        st = self.job_status(run, "analyze")
        if st is None:
            self._launch_analyst(run)
            return False
        if st == "running":
            return False
        run.d["job"] = None
        a, errors = self._collect_analysis(run, st)
        if a is not None:
            run.d["analysis"] = a
            return self._decide(run)
        run.event("analysis", "invalid: " + "; ".join(errors)[:400])
        if run.attempts("analyze") <= cfg["analyst"]["retries"]:
            self._launch_analyst(run, errors)
            return False
        # already posted: never terminal. Decide anyway and keep the repost schedule.
        run.d["analysis_missing"] = True
        run.d["analysis_retry"] = True
        run.d["analysis_errors"] = errors
        return self._decide(run)

    def _decide(self, run):
        cfg = self.cfg
        dec = self._decision(run)
        run.d["winner"] = dec
        w = dec["winner"]
        link = run.d["trials"][w]["permalink"]
        run.d["decided_at"] = iso(now())
        # a thin result has no real winner, so nothing gets reposted
        run.d["reposts"] = ([] if dec.get("thin") else
                            analysis.repost_schedule(cfg, run.d["decided_at"], cfg["reposts"]["days"]))
        analysis.ledger_append(cfg, analysis.ledger_rows_for_decision(run, dec))
        run.to("decided", "decide", detail=f"winner {w}, confidence {dec['confidence']}"
               + (", analysis missing" if run.d.get("analysis_missing") else ""))
        note = " The analysis failed; it will be retried once." if run.d.get("analysis_missing") else ""
        if dec.get("thin"):
            self.text("ReelCast Lab", f"Results for {run.d.get('title')} are too thin to call "
                                  f"(under 200 views everywhere or no numbers). Nothing to share yet.{note}")
        else:
            why = f": {'; '.join(dec['reasons'])}" if dec["confidence"] == "low" and dec["reasons"] else ""
            self.text("ReelCast Lab", f"Version {w[0]} won ({dec['confidence']} confidence{why}). "
                                  f"Tap Share with everyone: {link}.{note}")
        self.dash_dirty = True
        return True

    def _retry_analysis(self, run):
        """One more analyst try after the decision. Returns True while it is still pending."""
        st = self.job_status(run, "analyze")
        if st is None:
            if run.attempts("analyze") >= self.cfg["analyst"]["retries"] + 2:
                run.d["analysis_retry"] = False
                run.save()
                return False
            self._launch_analyst(run, run.d.get("analysis_errors"))
            return True
        if st == "running":
            return True
        run.d["job"] = None
        a, errors = self._collect_analysis(run, st)
        run.d["analysis_retry"] = False
        if a is not None:
            run.d["analysis"] = a
            run.d["analysis_missing"] = False
            run.event("analysis", "late analysis succeeded")
            self.dash_dirty = True
        else:
            run.d["analysis_errors"] = errors
            run.event("analysis", "late analysis failed again", detail="; ".join(errors)[:300])
            self.text_once(run, "analysis_missing", "ReelCast Lab",
                           f"The analysis of {run.d.get('title')} failed twice. The winner stands; no lesson recorded.")
        run.save()
        return False

    def s_decided(self, run):
        cfg = self.cfg
        t = now()
        if run.d.get("analysis_retry"):
            self._retry_analysis(run)
        a = run.d.get("analysis") or {}
        w = run.d["winner"]["winner"]
        changed = False
        nxt = []
        for k, rp in enumerate(run.d.get("reposts", [])):
            if rp["status"] == "pending":
                due = parse_time(rp["due"])
                if t < due:
                    nxt.append(due)
                    continue
                skip = None
                until = a.get("evergreen_until")
                if a.get("evergreen") is False:
                    skip = "the analysis says it is not evergreen"
                elif until and str(until) < due.astimezone(cfg.tz).strftime("%Y-%m-%d"):
                    skip = f"evergreen only until {until}"
                if skip:
                    rp.update(status="skipped", reason=skip, at=iso(t))
                    run.event("repost", f"{rp['days']} day repost skipped", detail=skip)
                    self.text("ReelCast Lab", f"Skipped the {rp['days']} day repost of {run.d.get('title')}: {skip}.")
                    changed = True
                    continue
                if not publish.in_window(cfg, t) or (cfg.lab / "PAUSED").exists():
                    nxt.append(publish.next_window_start(cfg, t))
                    continue
                caps = a.get("repost_captions") or []
                caption = caps[k % len(caps)] if caps else (run.edited_dir / "caption.txt").read_text().strip()
                if rp.get("intent"):
                    self.text_once(run, f"repost_blocked_{rp['days']}", "ReelCast Lab needs you",
                                   f"The {rp['days']} day repost of {run.d.get('title')} started but its result was "
                                   f"never recorded. Check Instagram before anything is posted again.")
                    continue
                try:
                    publish.guard(self.publisher)
                    rp["intent"] = iso(t)
                    run.save()
                    mid, link = self.publisher.publish_reel(run.edited_dir / f"{w}.mp4", caption)
                except publish.NotWired as e:
                    rp.pop("intent", None)
                    self.text_once(run, f"not_wired_repost_{rp['days']}", "ReelCast Lab",
                                   f"The {rp['days']} day repost of {run.d.get('title')} is due, but {e}")
                    continue
                rp.pop("intent", None)
                rp.update(status="posted", media_id=mid, permalink=link, posted_at=iso(t))
                run.event("repost", f"{rp['days']} day repost posted", detail=link)
                self.text("ReelCast Lab", f"Reposted {run.d.get('title')} ({rp['days']} days): {link}")
                changed = True
            if rp["status"] == "posted" and "reading" not in rp:
                due = parse_time(rp["posted_at"]) + timedelta(hours=cfg["reposts"]["reading_hours"])
                if t < due:
                    nxt.append(due)
                    continue
                try:
                    m = self.reader.read(rp["media_id"], age_hours=cfg["reposts"]["reading_hours"])
                except rd.NotWired:
                    continue
                if rd.empty(m) and t < due + timedelta(hours=cfg["readings"]["retry_hours"]):
                    continue
                rp["reading"] = dict(taken_at=iso(t), provisional=rd.empty(m),
                                     metrics=rd.derive(m or {}, self._lengths(run).get(w)))
                run.event("repost", f"{rp['days']} day repost 72h reading taken")
                self.dash_dirty = True
                changed = True
        run.d["next_due"] = iso(min(nxt)) if nxt else None
        run.save()
        if not run.d.get("analysis_retry") and all(rp["status"] == "skipped" or "reading" in rp
                                                   for rp in run.d.get("reposts", [])):
            run.to("complete", "reposts", detail="all reposts done or skipped")
            return True
        return changed


RETRYABLE = {"ingested", "transcribed", "editing", "edited", "gated"}


def retry(cfg, run_id):
    """Send a Needs-attention run back to the step that failed, with fresh attempt counts.
    Only for runs that were already ingested (original files in Originals/<folder>) and never posted.
    Holds the tick lock so it cannot race a running tick."""
    run = Run(cfg, run_id)
    if not run.d:
        raise SystemExit(f"no run {run_id}")
    if run.state != FAILED:
        raise SystemExit(f"{run_id} is at {run.state}, not needs_attention; nothing to retry")
    if run.d.get("trials"):
        raise SystemExit(f"{run_id} already posted trials; retry is only for runs that never posted")
    failed_from = next((h["result"].split(" -> ")[0] for h in reversed(run.d.get("history", []))
                        if str(h.get("result", "")).endswith(" -> " + FAILED)), None)
    if failed_from not in RETRYABLE:
        raise SystemExit(f"{run_id} failed at {failed_from or 'an unknown step'}, before its raw files were "
                         f"ingested. Drag the files from RAW/Needs-attention back into RAW instead.")
    resume = "transcribed" if failed_from == "editing" else failed_from
    lock_p = Path(os.path.expanduser(cfg["lock_path"]))
    lock_p.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_p, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("a tick is running right now; try again in a minute")
        run = Run(cfg, run_id)
        run.d.pop("needs_attention", None)
        run.d["job"] = None
        run.d["error_count"] = 0
        run.d["attempts"] = {}
        run.to(resume, "retry", detail=f"retried by command from {failed_from}")
        run.save()
    log(cfg, f"[{run_id}] retried by command, back to {resume}")
    print(f"{run_id} goes back to {resume}; the next tick picks it up")
    return 0


def main(argv):
    if "--now" in argv:
        i = argv.index("--now")
        os.environ["REELCAST_LAB_NOW"] = argv[i + 1]
        del argv[i:i + 2]
    cfg = load_config()
    if argv and argv[0] == "approve":
        run = Run(cfg, argv[1])
        if not run.d:
            raise SystemExit(f"no run {argv[1]}")
        (run.dir / "APPROVED").write_text(iso(now()) + "\n")
        run.event("review", "approved by command")
        print(f"approved {run.id}")
        return 0
    if argv and argv[0] == "unblock":
        # after the owner checked Instagram: clears the stuck post so the rest of the set can go out
        run = Run(cfg, argv[1])
        if not run.d:
            raise SystemExit(f"no run {argv[1]}")
        run.d.pop("publish_intent", None)
        run.d.pop("publish_blocked", None)
        run.d["error_count"] = 0
        run.save()
        run.event("publish", "unblocked by command")
        print(f"unblocked {run.id}")
        return 0
    if argv and argv[0] == "retry":
        return retry(cfg, argv[1])
    if argv and argv[0] == "doctor":
        import doctor
        return doctor.main(cfg)
    if argv and argv[0] == "status":
        for r in all_runs(cfg):
            print(f"{r.id}\t{r.state}\t{r.d.get('title')}\t"
                  f"{r.d.get('needs_attention') or r.d.get('attention') or r.d.get('next_due') or ''}")
        return 0
    if argv and argv[0] == "dashboard":
        print(dashboard.build(cfg))
        return 0
    Engine(cfg).tick()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
