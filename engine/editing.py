"""Edit step: write the per video brief from prompts/edit_template.md and launch the editor
(a headless claude -p run by default; the command is in the settings so tests can swap in a stub).
The tick only launches and later checks the DONE marker; it never waits.

Where the editor may write: its working folder is <run>/work, and the video's Edited folder is
added with --add-dir. Everything it needs to read is copied into work/reference first, the brief
and the DONE marker live in work/, and the approval marker stays one level up where it cannot
write. config.json explains the flags that enforce this."""
import json
import os
import shlex
import shutil
from pathlib import Path

from common import CODE_DIR, brand, claude_bin, ffmpeg_dir, owner_name, tool_env, unsafe_path_chars

TEMPLATE = CODE_DIR / "prompts" / "edit_template.md"
NOTES_MAX_CHARS = 6000


def read_notes(path):
    """The user's own notes (voice or framing), as a quoted block for the brief."""
    try:
        text = Path(path).read_text().strip()
    except OSError:
        return "> (none)"
    if len(text) > NOTES_MAX_CHARS:
        text = text[:NOTES_MAX_CHARS] + "\n(cut short here; the full notes are in the file)"
    return "\n".join("> " + ln if ln.strip() else ">" for ln in text.splitlines())


def glossary_notes(glossary):
    """One line per glossary entry, so the editor knows the spellings and what to do with flags."""
    if not glossary:
        return "The glossary in the settings is empty, so there are no special spellings to watch for."
    out = ["Spellings from the glossary in the settings:", ""]
    for g in glossary:
        heard = ", ".join(f'"{h}"' for h in g.get("heard", []))
        what = "flagged for you to decide" if g.get("action") == "flag" else "already fixed on screen"
        note = f" {g['note']}" if g.get("note") else ""
        out.append(f"- **{g.get('display')}** (the transcriber may write {heard}): {what}.{note}")
    return "\n".join(out)


def reference_dir(run):
    d = run.work / "reference"
    d.mkdir(parents=True, exist_ok=True)
    return d


def copy_reference(ref, src, name=None):
    """Copy a file the editor must read into work/reference (its file tools cannot leave the work
    folder). Returns the copy's path."""
    dst = ref / (name or Path(src).name)
    shutil.copyfile(src, dst)
    return dst


def playbook_line(cfg, ref):
    books = [Path(p) for p in (cfg.get("editor", {}).get("playbooks") or []) if Path(p).exists()]
    if not books:
        return ""
    books = [copy_reference(ref, p, f"playbook-{n + 1}-{p.name}") for n, p in enumerate(books)]
    return ("- Playbooks for judgment (what to cut, pacing): " + ", ".join(f"`{p}`" for p in books)
            + ". Use them for editing judgment only; ignore any setup, upload or publish steps in them.\n")


def write_brief(cfg, run):
    w = run.work
    fixes_p = w / "transcript_fixes.json"
    flags = json.loads(fixes_p.read_text()).get("flags", []) if fixes_p.exists() else []
    flag_note = (f"There are {len(flags)} flags to decide." if flags else "There are no flags.")
    attempt = run.attempts("edit")
    note = ""
    if attempt >= 1 and not run.d.get("gate", {}).get("summary"):
        note = ("\n**This is a retry.** The first attempt did not finish (see `" + str(run.dir / "jobs") +
                "` for its log). Reuse anything in the work folder that is already right.\n")
    gate_note = run.d.get("gate", {}).get("summary")
    raws = ["".join(c for c in Path(p).name if c not in '"$`\\') for p in run.d.get("raw_paths", [])]
    look = brand(cfg)
    framing = cfg.get("framing") or {}
    q = shlex.quote
    ref = reference_dir(run)
    example = copy_reference(ref, cfg["example_plan"])
    words = Path(cfg["example_plan"]).with_name("transcript.example.json")
    if words.exists():
        copy_reference(ref, words)
    py, render = str(cfg["sandbox_python"]), str(CODE_DIR / "render_reel.py")
    plan = str(w / "edit_plan.json")
    vals = {
        "TITLE": run.d.get("title"), "OWNER": owner_name(cfg), "CONTENT_ROOT": cfg.root,
        "RUN_ID": run.id, "RUN_DIR": run.dir, "WORK": w, "EDITED_DIR": run.edited_dir,
        "RAW_LIST": ", ".join(f"`{p}`" for p in run.d.get("raw_paths", [])),
        "Q_RAW_NAMES": q(", ".join(raws)),
        "RAW_SECONDS": run.d.get("raw_seconds"),
        "HDR_NOTE": ("It was filmed in HDR; the working copy below is already tone mapped to SDR BT.709."
                     if run.d.get("hdr") else "It was filmed in SDR."),
        "SDR": w / "sdr.mp4", "AUDIO": w / "audio.wav", "TRANSCRIPT": w / "transcript.json",
        "TRANSCRIPT_TXT": w / "transcript.txt", "FIXES": fixes_p, "FLAGS": flag_note,
        "GLOSSARY_NOTES": glossary_notes(cfg.get("glossary") or []),
        "RENDER": render, "PLAN": plan, "REFERENCE": ref,
        "Q_SANDBOX_PY": q(py), "Q_RENDER": q(render), "Q_PLAN": q(plan),
        "Q_TRANSCRIBE_SCRIPT": q(str(cfg["transcribe_script"])),
        "FFMPEG_NOTE": (f"ffmpeg and ffprobe are already on your PATH (from `{ffmpeg_dir(cfg)}`)" if ffmpeg_dir(cfg)
                        else "ffmpeg and ffprobe are already on your PATH"),
        "EXAMPLE_PLAN": example, "PLAYBOOKS": playbook_line(cfg, ref),
        "WHISPER_MODEL": cfg["whisper_model"],
        "PRESET": look["preset"], "END_CARD": look["end_card"], "FONTS": look["fonts_dir"],
        "VOICE_FILE": look["voice"], "VOICE_NOTES": read_notes(look["voice"]),
        "FRAMING_FILE": look["framing"], "FRAMING_NOTES": read_notes(look["framing"]),
        "TOP_CROP": framing.get("top_crop", 0.0), "EYE_LINE": framing.get("eye_line", 0.43),
        "MAX_SECONDS": cfg["limits"]["finished_max_seconds"], "DONE": done_path(run),
        "ATTEMPT_NOTE": note + (f"\nThe quality check said: {gate_note}\n" if gate_note else ""),
    }
    text = TEMPLATE.read_text()
    for k, v in vals.items():
        text = text.replace("{{" + k + "}}", str(v))
    brief = run.work / "edit_brief.md"
    brief.write_text(text)
    return brief


def sandbox_settings(cfg):
    """Claude Code settings for a model run, as one JSON string: the sandbox is on for every shell
    command, a command can never run outside it, and the listed secrets folders cannot be read.
    The network is closed by the sandbox itself (no domain is allowed)."""
    return json.dumps({"sandbox": {
        "enabled": True, "autoAllowBashIfSandboxed": True, "allowUnsandboxedCommands": False,
        "filesystem": {"denyRead": list(cfg.get("sandbox_deny_read") or [])}}})


def model_command(cfg, section, run, brief):
    """The headless command for the editor or the analyst, with every placeholder filled.
    {sandbox_settings} is put in whole, after the other placeholders, so nothing in the JSON is
    ever read as a placeholder."""
    for what, p in (("this video's work folder", run.work), ("this video's Edited folder", run.edited_dir)):
        bad = unsafe_path_chars(p)
        if bad:
            raise ValueError(f"{what} ({p}) holds a character the safety rules cannot carry: {' '.join(bad)}. "
                             "Move your ReelCast Lab folder to a path without it.")
    sec = cfg[section]
    out = []
    for c in sec["command"]:
        if c == "{sandbox_settings}":
            out.append(sandbox_settings(cfg))
        else:
            out.append(cfg.fill(c, brief=brief, run_dir=run.dir, work=run.work, edited_dir=run.edited_dir,
                                claude=claude_bin(cfg), model=sec.get("model") or "opus"))
    return out


def model_cwd(cfg, section, run):
    return cfg.fill(cfg[section].get("cwd") or "{work}", run_dir=run.dir, work=run.work,
                    edited_dir=run.edited_dir)


def editor_command(cfg, run, brief):
    return model_command(cfg, "editor", run, brief)


def editor_env(cfg):
    """The engine, not the brief, puts ffmpeg on the editor's PATH. The sandbox has no network,
    so the read-back transcription must use the speech model the engine already downloaded."""
    env = tool_env(cfg)
    env["HF_HUB_OFFLINE"] = "1"
    return env


def done_path(run):
    return run.work / "DONE"


def done_marker(run):
    p = done_path(run)
    if not p.exists():
        return None
    return p.read_text().strip() or "ok"
