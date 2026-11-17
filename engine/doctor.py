"""Check that everything ReelCast Lab needs is on this Mac, in plain words.

  python3 engine/tick.py doctor          the full check
  python3 scripts/setup_env.py check     the same check

It looks for: ffmpeg and ffprobe (with the filters that burn captions and convert HDR footage),
tesseract, the claude command, the kit's own Python sandbox and the packages in it, the
transcription script in vendor/, your content folders, and the caption preset, font and end
card. It exits 0 only when everything needed is there, and it says how to fix what is not.

The tick runs the quick form of the same check before it touches anything. When something is
missing it logs one clear line and leaves every file where it is, so a missing tool can never
send a video to Needs-attention. The quick form looks for the same things without starting the
big Python packages, so it costs well under a second.

Standard library only.
"""
import json
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from common import (KIT_ROOT, brand, claude_bin, content_dirs, find_tool, tool_env,
                    user_settings_path)

VENDOR_DIR = KIT_ROOT / "vendor" / "vibe-editing"
VENDOR_URL = "https://github.com/maddexritter-rgb/vibe-editing.git"
VENDOR_COMMIT = "a342d7173d49b6be43e477a0041aad6014fbf377"

# what the renderer and the transcription script import, and the package each one comes from
SANDBOX_MODULES = [("numpy", "numpy"), ("PIL", "pillow"), ("fontTools", "fonttools"),
                   ("faster_whisper", "faster-whisper"), ("av", "av"), ("ctranslate2", "ctranslate2"),
                   ("tokenizers", "tokenizers"), ("onnxruntime", "onnxruntime")]
FFMPEG_FILTERS = {"ass": "burning captions into the picture",
                  "zscale": "converting HDR footage (most phones film in HDR)",
                  "tonemap": "converting HDR footage (most phones film in HDR)"}
BREW_FFMPEG = "brew install ffmpeg-full"
BREW_TESSERACT = "brew install tesseract"
CLAUDE_FIX = "install Claude Code (https://claude.com/claude-code), then run: claude   and sign in once"

PROBE = (
    "import importlib, importlib.util, json, sys\n"
    "bad = []\n"
    "for n in sys.argv[2:]:\n"
    "    try:\n"
    "        if sys.argv[1] == 'deep':\n"
    "            importlib.import_module(n)\n"
    "        elif importlib.util.find_spec(n) is None:\n"
    "            bad.append(n)\n"
    "    except Exception:\n"
    "        bad.append(n)\n"
    "print(json.dumps({'version': '.'.join(str(x) for x in sys.version_info[:3]), 'bad': bad}))\n"
)


def setup_line():
    return f"python3 {shlex.quote(str(KIT_ROOT / 'scripts' / 'setup_env.py'))} install"


def _run(cmd, env, timeout=120):
    try:
        r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env, timeout=timeout,
                           stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, str(e)


def ffmpeg_filters(ffmpeg, env):
    """The names of the filters this ffmpeg has."""
    code, text = _run([ffmpeg, "-hide_banner", "-filters"], env)
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and "->" in parts[2]:
            names.add(parts[1])
    return names


def model_cache_dir(model):
    """Where the downloaded speech model sits once it has been used (the Hugging Face cache)."""
    hub = os.environ.get("HF_HUB_CACHE")
    if not hub:
        home = os.environ.get("HF_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "huggingface")
        hub = os.path.join(home, "hub")
    return Path(hub) / f"models--Systran--faster-whisper-{model}"


def speech_model_note(cfg):
    """None when the speech model is already on this Mac, else one plain sentence."""
    model = str(cfg.get("whisper_model") or "")
    if "/" in model or os.sep in model or model_cache_dir(model).exists():
        return None
    return (f"the speech model ({model}) is not on this Mac yet. The first video downloads it once "
            f"(a few GB for the large models), so that first transcript needs the internet and takes longer")


def vendor_head():
    code, text = _run(["git", "-C", VENDOR_DIR, "rev-parse", "HEAD"], dict(os.environ, GIT_TERMINAL_PROMPT="0"))
    return text.strip().splitlines()[-1] if code == 0 and text.strip() else None


def checks(cfg, deep=True):
    """[{name, ok, detail, fix}] for everything the engine needs. deep=False is the quick form."""
    env = tool_env(cfg)
    out = []

    def add(name, ok, detail, fix=""):
        out.append(dict(name=name, ok=bool(ok), detail=detail, fix="" if ok else fix))

    # ffmpeg and ffprobe
    ffmpeg = find_tool(cfg, "ffmpeg")
    if not ffmpeg:
        add("ffmpeg", False, "not found. It cuts the video, burns the captions and sets the loudness", BREW_FFMPEG)
    else:
        have = ffmpeg_filters(ffmpeg, env)
        gone = [f for f in FFMPEG_FILTERS if f not in have]
        if gone:
            why = sorted({FFMPEG_FILTERS[f] for f in gone})
            add("ffmpeg", False, f"{ffmpeg} is a small build without {', '.join(gone)} (needed for "
                                 f"{' and '.join(why)})", BREW_FFMPEG)
        else:
            add("ffmpeg", True, f"{ffmpeg} (can burn captions and convert HDR footage)")
    ffprobe = find_tool(cfg, "ffprobe")
    add("ffprobe", ffprobe, ffprobe or "not found. It reads the length and shape of every video", BREW_FFMPEG)

    # tesseract
    tess = find_tool(cfg, "tesseract")
    if not tess:
        add("tesseract", False, "not found. The quality check reads the burned captions with it", BREW_TESSERACT)
    elif deep:
        code, text = _run([tess, "--list-langs"], env)
        ok = code == 0 and "eng" in text.split()
        add("tesseract", ok, tess if ok else f"{tess} is there but its English language data is not", BREW_TESSERACT)
    else:
        add("tesseract", True, tess)

    # the claude command
    claude = claude_bin(cfg)
    found = shutil.which(claude, path=env["PATH"]) or (claude if os.access(claude, os.X_OK) else None)
    if not found:
        add("claude", False, "the claude command was not found. It is the editor", CLAUDE_FIX)
    elif deep:
        code, text = _run([found, "--version"], env, timeout=60)
        add("claude", code == 0, f"{found} ({text.strip().splitlines()[0] if text.strip() else 'no version'}). "
            f"This check cannot tell whether it is signed in" if code == 0 else f"{found} did not start", CLAUDE_FIX)
    else:
        add("claude", True, found)

    # the Python sandbox and what it must be able to import
    py = str(cfg.get("sandbox_python") or "")
    if not py or not os.access(py, os.X_OK):
        add("Python sandbox", False, f"not found at {py}. The renderer and the transcription run in it", setup_line())
    else:
        code, text = _run([py, "-c", PROBE, "deep" if deep else "quick", *[m for m, _ in SANDBOX_MODULES]], env)
        try:
            res = json.loads(text.strip().splitlines()[-1])
        except (ValueError, IndexError):
            res = None
        if code != 0 or res is None:
            add("Python sandbox", False, f"{py} did not start", setup_line())
        else:
            bad = [pkg for mod, pkg in SANDBOX_MODULES if mod in res["bad"]]
            add("Python sandbox", not bad,
                f"{py} (Python {res['version']}) " + (f"is missing or cannot load: {', '.join(bad)}" if bad
                                                      else "with every package it needs"), setup_line())

    # the transcription script from the kit in vendor/
    script = Path(str(cfg.get("transcribe_script") or ""))
    custom = bool((cfg.get("transcribe") or {}).get("command"))
    if custom:
        add("transcription", True, "your own transcribe command from the settings is used")
    elif not script.is_file():
        add("transcription script", False, f"not found at {script}", setup_line())
    else:
        detail, ok = str(script), True
        inside = VENDOR_DIR in script.resolve().parents
        if deep and inside and (VENDOR_DIR / ".git").exists():
            head = vendor_head()
            if head and head != VENDOR_COMMIT:
                ok = False
                detail = (f"the kit in {VENDOR_DIR} is at version {head[:7]}, not the pinned {VENDOR_COMMIT[:7]}")
        add("transcription script", ok, detail, setup_line())

    # the content folders
    gone = [str(d) for d in content_dirs(cfg) if not d.is_dir()]
    locked = [str(d) for d in content_dirs(cfg) if d.is_dir() and not os.access(d, os.W_OK)]
    if gone:
        add("content folders", False, f"missing: {', '.join(gone)}", setup_line())
    elif locked:
        add("content folders", False, f"cannot be written to: {', '.join(locked)}",
            "give yourself write permission on those folders")
    else:
        add("content folders", True, f"{cfg.root} (RAW, Edited, Originals)")

    # the look: caption preset, font, end card
    look = brand(cfg)
    try:
        preset = json.loads(Path(look["preset"]).read_text())
        need = [k for k in ("font_file", "font_size_px", "layout", "colors", "shadow", "weights", "animation",
                            "timing", "video") if k not in preset]
        font = Path(look["fonts_dir"]) / str(preset.get("font_file") or "")
        if need:
            add("caption preset", False, f"{look['preset']} has no {', '.join(need)}",
                f"copy the missing parts from {KIT_ROOT / 'brand' / 'default' / 'default_pop.json'}")
        elif not font.is_file():
            add("caption font", False, f"{font} is not there (the preset names it)",
                f"put the font file in {look['fonts_dir']} or fix \"font_file\" in {look['preset']}")
        else:
            add("caption preset and font", True, f"{look['preset']} with {font.name}")
    except (OSError, ValueError) as e:
        add("caption preset", False, f"{look['preset']} cannot be read ({e})",
            f"fix that file, or remove it to use the kit's own look")
    add("end card", Path(look["end_card"]).is_file(), str(look["end_card"]),
        "put an end-card.png in your brand folder, or remove the broken one to use the kit's own")
    return out


def notes(cfg):
    """Things worth knowing that do not stop any work."""
    out = []
    try:
        out.append(f"time zone: {cfg.tz.key}"
                   + (" (could not read this Mac's zone, so UTC is used)" if cfg.tz.key == "UTC" else ""))
    except ValueError as e:
        out.append(f"time zone: {e}")
    m = speech_model_note(cfg)
    if m:
        out.append(m)
    s = user_settings_path()
    out.append(f"your settings file: {s}" + ("" if s.exists() else " (not there yet, so the shipped defaults are in use)"))
    return out


def problems(cfg, deep=False):
    """One plain phrase per missing thing. Empty when the engine can run."""
    return [f"{c['name']}: {c['detail']}" for c in checks(cfg, deep=deep) if not c["ok"]]


def main(cfg, deep=True, out=print):
    """Print the check and return 0 when everything needed is there, else 1."""
    res = checks(cfg, deep=deep)
    out("ReelCast Lab check")
    for c in res:
        out(f"  {'ok     ' if c['ok'] else 'MISSING'}  {c['name']}: {c['detail']}")
        if c["fix"]:
            out(f"           To fix it: {c['fix']}")
    for n in notes(cfg):
        out(f"  note     {n}")
    bad = [c for c in res if not c["ok"]]
    if bad:
        out(f"\n{len(bad)} thing{'s are' if len(bad) > 1 else ' is'} missing: {', '.join(c['name'] for c in bad)}. "
            f"Fix {'them' if len(bad) > 1 else 'it'}, then run this check again.")
        return 1
    out("\nEverything needed is here.")
    return 0
