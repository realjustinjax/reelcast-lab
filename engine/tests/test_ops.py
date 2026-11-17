"""Guards for how the kit is set up: the headless model commands, the settings merge, the brand
fallback, the built-in text check, the practice run's safety, the job file and the end card."""
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path

from tests.test_engine import CODE, NOWHERE, STUBS, Base, need_ffmpeg

import analysis  # noqa: E402
import common  # noqa: E402
import dryrun  # noqa: E402
import editing  # noqa: E402
import gate  # noqa: E402
import tick  # noqa: E402
from state import VERSIONS, Run  # noqa: E402

KIT = CODE.parent
SHIPPED = json.loads((CODE / "config.json").read_text())


class TestModelCommands(Base):
    """Two lessons from the first live run: a headless run without --strict-mcp-config loads every
    connected tool server and the prompt gets too long, and only Edit(path) deny rules protect
    files (a Write(path) deny rule is ignored)."""

    def built(self, section):
        cfg = common.load_config({"content_root": str(self.root), "editor": {"command": SHIPPED["editor"]["command"]},
                                  "analyst": {"command": SHIPPED["analyst"]["command"]}})
        run = Run.create(self.cfg, "cmd-" + section)
        return editing.model_command(cfg, section, run, run.dir / "brief.md")

    def flag(self, c, name):
        return c[c.index(name) + 1]

    def test_both_commands_pass_strict_mcp_config_and_restricted(self):
        for k in ("editor", "analyst"):
            for c in (SHIPPED[k]["command"], self.built(k)):
                self.assertIn("--strict-mcp-config", c, f"{k} would load every tool server (prompt too long)")
                self.assertIn("--restricted", c, f"{k} would read the user's own permissive settings")
                self.assertEqual(self.flag(c, "--permission-mode"), "acceptEdits")
                self.assertIn("-p", c)

    def test_sandbox_is_on_and_cannot_be_escaped(self):
        for k in ("editor", "analyst"):
            c = self.built(k)
            st = json.loads(self.flag(c, "--settings"))["sandbox"]
            self.assertIs(st["enabled"], True)
            self.assertIs(st["allowUnsandboxedCommands"], False)
            deny = st["filesystem"]["denyRead"]
            self.assertTrue(any(d.endswith("/.ssh") for d in deny), deny)
            self.assertTrue(any(d.endswith("/Library/Keychains") for d in deny), deny)

    def test_file_tools_are_not_blanket_approved(self):
        """Write and Edit must never be in --allowedTools: then acceptEdits only covers the work
        folder and the --add-dir output folder, and every other write is refused."""
        for k in ("editor", "analyst"):
            for c in (SHIPPED[k]["command"], self.built(k)):
                allowed = self.flag(c, "--allowedTools").split(",")
                self.assertNotIn("Write", allowed, f"{k}: Write would be approved everywhere")
                self.assertNotIn("Edit", allowed, f"{k}: Edit would be approved everywhere")
                self.assertFalse([x for x in allowed if x.startswith("Bash(")], "use the sandbox, not Bash patterns")
            c = self.built(k)
            self.assertNotIn("{", self.flag(c, "--add-dir"))
        self.assertEqual(SHIPPED["editor"]["cwd"], "{work}")
        self.assertEqual(SHIPPED["analyst"]["cwd"], "{work}")

    def test_deny_rules_are_edit_rules_for_kit_raw_and_originals(self):
        for k in ("editor", "analyst"):
            raw = SHIPPED[k]["command"]
            self.assertFalse([x for x in raw if x.startswith("Write(")], f"{k}: Write(path) deny rules are ignored")
            for rule in ("Edit(//{kit_root}/**)", "Edit(//{content_root}/RAW/**)", "Edit(//{content_root}/Originals/**)"):
                self.assertIn(rule, raw)
            c = self.built(k)
            self.assertFalse([x for x in c if x.startswith("Write(")])
            self.assertIn("Edit(/" + str(KIT) + "/**)", c)          # two leading slashes: an absolute path
            self.assertIn("Edit(/" + str(self.root) + "/RAW/**)", c)
            self.assertIn("Edit(/" + str(self.root) + "/Originals/**)", c)
            self.assertFalse([x for x in c if not x.startswith("{") and ("{" in x or "}" in x)],
                             "a placeholder was left unfilled")

    def test_editor_has_a_shell_only_inside_the_sandbox_and_model_comes_from_settings(self):
        c = self.built("editor")
        self.assertIn("Bash", self.flag(c, "--allowedTools").split(","))
        self.assertEqual(self.flag(c, "--model"), "opus")
        self.assertEqual(SHIPPED["analyst"]["model"], "opus")
        self.assertNotIn("Bash", self.flag(self.built("analyst"), "--tools"))
        cfg = common.load_config({"content_root": str(self.root), "editor": {"model": "sonnet", "command": SHIPPED["editor"]["command"]},
                                  "claude_bin": "/somewhere/claude"})
        run = Run.create(self.cfg, "cmd-model")
        c = editing.model_command(cfg, "editor", run, run.dir / "b.md")
        self.assertEqual((c[0], c[c.index("--model") + 1]), ("/somewhere/claude", "sonnet"))

    def test_editor_runs_offline(self):
        self.assertEqual(editing.editor_env(self.cfg)["HF_HUB_OFFLINE"], "1")

    def test_unsafe_folder_names_are_refused(self):
        run = Run.create(self.cfg, "cmd-bad")
        run.d["edited_dir"] = str(self.root / "Edited" / "a$b")
        with self.assertRaises(ValueError):
            editing.model_command(self.cfg, "editor", run, run.dir / "b.md")


class TestSettings(Base):
    def setUp(self):
        super().setUp()
        self.saved = {k: os.environ.get(k) for k in ("REELCAST_LAB_CONFIG", "REELCAST_LAB_SETTINGS")}

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def test_shipped_defaults(self):
        os.environ.pop("REELCAST_LAB_CONFIG", None)
        cfg = common.load_config()
        home = Path.home()
        self.assertEqual(cfg.root, home / "ReelCast Lab")
        self.assertEqual(cfg.lab, home / "ReelCast Lab" / "Lab_Data")
        self.assertEqual(cfg["lock_path"], str(home / "Library/Application Support/reelcast-lab/reelcast-lab.lock"))
        self.assertEqual(cfg["log_path"], str(home / "Library/Logs/reelcast-lab.log"))
        self.assertEqual(cfg["sandbox_python"], str(KIT / ".venv/bin/python"))
        self.assertEqual(cfg["transcribe_script"], str(KIT / "vendor/vibe-editing/plugins/vibe-editing/skills/"
                                                       "long-form-ingest/scripts/transcribe_local.py"))
        self.assertEqual(cfg["example_plan"], str(KIT / "examples/edit_plan.example.json"))
        self.assertEqual((SHIPPED["ffmpeg_dir"], SHIPPED["claude_bin"], SHIPPED["timezone"]), ("auto", "auto", "auto"))
        self.assertEqual(cfg["framing"]["top_crop"], 0.0)
        self.assertEqual(common.owner_name(cfg), "the owner")
        self.assertEqual(SHIPPED["offer"], {"_what": SHIPPED["offer"]["_what"], "name": "", "keyword": ""})
        self.assertEqual(cfg["text_rules"]["banned_strings"], ["\u2014", "\u2013"])
        self.assertNotIn(str(home), (CODE / "config.json").read_text())

    def test_merge_order_user_settings_then_override_file_then_root(self):
        user, extra = self.tmp / "settings.json", self.tmp / "extra.json"
        user.write_text(json.dumps({"owner": {"name": "Sam"}, "tick_seconds": 300, "content_root": "~/Somewhere"}))
        extra.write_text(json.dumps({"tick_seconds": 120}))
        os.environ["REELCAST_LAB_SETTINGS"] = str(user)
        os.environ["REELCAST_LAB_CONFIG"] = str(extra)
        cfg = common.load_config()
        self.assertEqual((common.owner_name(cfg), cfg["tick_seconds"]), ("Sam", 120))
        self.assertEqual(cfg.root, Path.home() / "Somewhere")
        self.assertEqual(cfg["brand"]["dir"], str(Path.home() / "Somewhere" / "Lab_Data" / "brand"))
        os.environ["REELCAST_LAB_ROOT"] = str(self.root)
        try:
            self.assertEqual(common.load_config().root, self.root)
        finally:
            os.environ.pop("REELCAST_LAB_ROOT")

    def test_auto_values_resolve(self):
        cfg = common.load_config({"content_root": NOWHERE})
        self.assertTrue(str(cfg.tz))                                   # the Mac's zone, or UTC
        self.assertEqual(str(common.load_config({"content_root": NOWHERE, "timezone": "Europe/London"}).tz), "Europe/London")
        with self.assertRaises(ValueError):
            common.load_config({"content_root": NOWHERE, "timezone": "Not/AZone"})
        self.assertEqual(common.ffmpeg_dir(common.load_config({"content_root": NOWHERE, "ffmpeg_dir": "/x/bin"})), "/x/bin")
        self.assertTrue(common.claude_bin(common.load_config({"content_root": NOWHERE, "claude_bin": "auto"})))
        env = common.tool_env(common.load_config({"content_root": NOWHERE, "ffmpeg_dir": "/x/bin"}))
        self.assertEqual(env["PATH"].split(":")[0], "/x/bin")          # the engine puts ffmpeg first on PATH

    def test_brand_falls_back_to_kit_defaults_file_by_file(self):
        look = common.brand(self.cfg)
        d = KIT / "brand" / "default"
        self.assertEqual(look, dict(preset=d / "default_pop.json", end_card=d / "end-card.png", fonts_dir=d / "fonts",
                                    voice=d / "voice.md", framing=d / "framing.md"))
        for p in look.values():
            self.assertTrue(p.exists(), p)
        preset = json.loads(look["preset"].read_text())
        self.assertEqual(preset["colors"]["speaker"], "FFFFFF")
        self.assertTrue((look["fonts_dir"] / preset["font_file"]).exists())
        mine = self.cfg.lab / "brand"
        mine.mkdir(parents=True)
        (mine / "voice.md").write_text("Short and dry.\n")
        look = common.brand(self.cfg)
        self.assertEqual(look["voice"], mine / "voice.md")
        self.assertEqual(look["preset"], d / "default_pop.json")


class TestTextCheckAndOffer(Base):
    def test_banned_strings_are_built_in(self):
        self.assertEqual(gate.sanity(self.cfg, "A clean caption. #one", "Two lines"), (True, "ok"))
        ok, det = gate.sanity(self.cfg, "Fast \u2014 and wrong", "Two lines")
        self.assertFalse(ok)
        self.assertIn("em dash in caption.txt", det)
        ok, det = gate.sanity(self.cfg, "Fine", "2020\u20132024")
        self.assertIn("en dash in the hook text", det)
        self.cfg["text_rules"]["banned_strings"] = ["guaranteed"]
        self.assertFalse(gate.sanity(self.cfg, "Results guaranteed", "")[0])
        self.assertTrue(gate.sanity(self.cfg, "Fast \u2014 and fine", "")[0])

    def test_offer_comes_from_settings(self):
        self.assertIsNone(analysis.live_keyword(self.cfg))
        self.assertIn("No live offer", analysis.offer_line(self.cfg))
        self.cfg["offer"] = {"name": "The Workshop", "keyword": "START"}
        self.assertEqual(analysis.live_keyword(self.cfg), "START")
        self.assertIn("The Workshop (keyword START)", analysis.offer_line(self.cfg))


class TestBriefs(Base):
    def make_run(self):
        r = Run.create(self.cfg, "brief")
        ed = self.root / "Edited" / "v"
        ed.mkdir(parents=True)
        r.d.update(edited_dir=str(ed), raw_paths=[str(self.root / "Originals" / "v" / "clip.mov")], raw_seconds=12.0, title="clip")
        r.save()
        return r

    def test_edit_brief_is_filled_from_settings_and_brand_files(self):
        self.cfg["owner"] = {"name": "Sam Rivers"}
        self.cfg["glossary"] = [{"display": "Brell", "heard": ["bell"], "action": "flag", "note": "The app."}]
        mine = self.cfg.lab / "brand"
        mine.mkdir(parents=True)
        (mine / "framing.md").write_text("Crop the top so the shelf never shows.\n")
        text = editing.write_brief(self.cfg, self.make_run()).read_text()
        self.assertNotIn("{{", text)
        for want in ("Sam Rivers", "> Crop the top so the shelf never shows.", "**Brell**", "reference/edit_plan.example.json",
                     "render_reel.py", "--model large-v3", "A_control",
                     "B_onscreen_hook", "C_cold_open", "D_tight_cut", "E_visual_open", str(self.root / "Edited" / "v"),
                     "already on your PATH", "partial: <what is missing>"):
            self.assertIn(want, text)
        self.assertNotIn("export PATH", text)

    def test_analyst_brief_is_filled(self):
        need_ffmpeg()
        text = analysis.write_brief(self.cfg, self.make_run(), dict(readings={}, decision={}), {}).read_text()
        self.assertNotIn("{{", text)
        self.assertIn("the owner", text)
        self.assertIn("No live offer", text)

    def test_example_plan_is_valid(self):
        plan = json.loads((KIT / "examples" / "edit_plan.example.json").read_text())
        words = json.loads((KIT / "examples" / "transcript.example.json").read_text())["words"]
        self.assertEqual(sorted(plan["versions"]), sorted(VERSIONS))
        for name, v in plan["versions"].items():
            r = v["ranges"]
            if isinstance(r, str):
                self.assertIn(r, VERSIONS)
                continue
            for a, b, zoom in r:
                self.assertTrue(0 <= a <= b < len(words), (name, a, b))
                self.assertTrue(1.0 <= zoom <= 1.3)
        for k in list(plan["text"]) + list(plan["in_overrides"]) + list(plan["out_overrides"]):
            self.assertLess(int(k), len(words))
        self.assertEqual(len(plan["versions"]["B_onscreen_hook"]["hook"]["lines"]), 2)


class TestPracticeRunSafety(Base):
    def test_refuses_the_content_root_and_unmarked_folders(self):
        root = self.root.resolve()
        for bad in (root, root / "practice", root.parent):
            with self.assertRaises(SystemExit) as cm:
                dryrun.check_scratch(bad, root)
            self.assertIn("refusing", str(cm.exception))
        other = (self.tmp / "other").resolve()
        other.mkdir()
        (other / "keep.txt").write_text("mine")
        with self.assertRaises(SystemExit) as cm:
            dryrun.check_scratch(other, root)
        self.assertIn("not empty", str(cm.exception))
        fresh = (self.tmp / "fresh").resolve()
        dryrun.check_scratch(fresh, root)
        self.assertTrue((fresh / dryrun.MARKER).exists())
        (fresh / "left-over.txt").write_text("x")
        dryrun.check_scratch(fresh, root)          # its own marker: allowed again


class TestJobFile(Base):
    def run_script(self, *args):
        return subprocess.run([sys.executable, str(KIT / "scripts" / "install_job.py"), *args], env=self.child_env(),
                              capture_output=True, text=True)

    def test_writes_the_job_file_and_only_prints_launchctl(self):
        dest = self.tmp / "agents"
        out = self.run_script("--dest", str(dest), "--python", "/usr/bin/python3")
        self.assertEqual(out.returncode, 0, out.stderr)
        plist = dest / "com.reelcast-lab.tick.plist"
        d = plistlib.loads(plist.read_bytes())
        self.assertEqual(d["Label"], "com.reelcast-lab.tick")
        self.assertEqual(d["ProgramArguments"], ["/usr/bin/python3", str(CODE / "tick.py")])
        self.assertEqual(d["StartInterval"], 600)
        self.assertEqual(d["StandardOutPath"], str(self.tmp / "test.log"))
        self.assertIn(f"launchctl bootstrap gui/{os.getuid()} ", out.stdout)
        self.assertIn(str(plist), out.stdout)
        src = (KIT / "scripts" / "install_job.py").read_text()
        self.assertNotIn("subprocess", src)        # it prints the commands, it never runs them
        self.assertNotIn("os.system", src)
        self.assertNotIn("{{", plist.read_text())

    def test_uninstall_prints_the_reverse_and_changes_nothing(self):
        dest = self.tmp / "agents"
        out = self.run_script("--uninstall", "--dest", str(dest))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(f"launchctl bootout gui/{os.getuid()}/com.reelcast-lab.tick", out.stdout)
        self.assertIn("rm ", out.stdout)
        self.assertFalse(dest.exists())


class TestEndCardBuilder(Base):
    def test_page_is_built_from_arguments_only(self):
        out = self.tmp / "card" / "end-card.png"
        r = subprocess.run([sys.executable, str(KIT / "brand" / "end-card" / "build_end_card.py"), "--name", "Sam <Rivers>",
                            "--title", "Gardener & writer", "--platforms", "instagram,tiktok", "--bg", "#102030",
                            "--out", str(out), "--html-only"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        page = out.with_suffix(".html").read_text()
        self.assertNotIn("{{", page)
        self.assertIn("Sam &lt;Rivers&gt;", page)
        self.assertIn("Gardener &amp; writer", page)
        self.assertIn("#102030", page)
        self.assertEqual(page.count('class="ic"'), 2)
        self.assertFalse(out.exists())
        bad = subprocess.run([sys.executable, str(KIT / "brand" / "end-card" / "build_end_card.py"), "--name", "x",
                              "--bg", "red;}", "--out", str(out), "--html-only"], capture_output=True, text=True)
        self.assertNotEqual(bad.returncode, 0)


class TestRetry(Base):
    editor_mode = "--fail"

    def test_retry_after_editor_failure(self):
        self.drop()
        r = self.drive_until("needs_attention", dict(minutes=10), 60)
        self.assertIn("edit", r.d["needs_attention"])
        raw = Path(r.d["originals_dir"]) / "IMG_0001.MOV"
        self.assertTrue(raw.exists())
        self.cfg["editor"]["command"] = [sys.executable, str(STUBS / "stub_editor.py"), "{run_dir}", "--fake"]
        self.assertEqual(tick.retry(self.cfg, r.id), 0)
        r = self.run_one()
        self.assertEqual(r.state, "transcribed")
        self.assertEqual(r.d["attempts"], {})
        self.assertNotIn("needs_attention", r.d)
        r = self.drive_until("published", dict(minutes=10), 200)
        self.assertTrue(raw.exists())

    def test_retry_refusals(self):
        self.drop()
        self.tick()
        r = self.run_one()
        with self.assertRaises(SystemExit):        # not in Needs-attention
            tick.retry(self.cfg, r.id)
        with self.assertRaises(SystemExit):
            tick.retry(self.cfg, "no-such-run")


class TestRetrySideways(Base):
    def test_pre_ingest_failure_refused(self):
        self.drop(kind="sideways")
        r = self.drive_until("needs_attention", dict(minutes=10), 10)
        with self.assertRaises(SystemExit) as cm:
            tick.retry(self.cfg, r.id)
        self.assertIn("Drag the files", str(cm.exception))
