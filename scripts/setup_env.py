#!/usr/bin/env python3
"""Set up what ReelCast Lab needs on this Mac, or check that it is all there.

  python3 scripts/setup_env.py check
  python3 scripts/setup_env.py install [--settings-json '{"owner": {"name": "Sam"}}']

check     the same check as  python3 engine/tick.py doctor  (exit 0 only when nothing is missing)

install   1. saves the keys in --settings-json (when given) into your settings file. Keys that
             are already there and not named are kept.
          2. finds Python 3.12 and makes the kit's own Python sandbox in .venv
          3. installs the pinned packages from requirements.lock.txt into that sandbox
          4. downloads the Vibe Editing kit into vendor/vibe-editing and pins it to one exact
             version. Its own setup and onboarding are never run.
          5. creates your content folders
          6. runs the check

It never runs brew and never asks for your password. When Python 3.12, ffmpeg or tesseract is
missing it prints the one line to run yourself. Running install again is safe: it only fills in
what is not there yet.

Your settings file is ~/.config/reelcast-lab/settings.json, or the file named by
REELCAST_LAB_SETTINGS. Standard library only, so the python3 that comes with macOS can run it.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "engine"))

import common  # noqa: E402
import doctor  # noqa: E402

VENV = KIT_ROOT / ".venv"
VENV_PY = VENV / "bin" / "python"
LOCK = KIT_ROOT / "requirements.lock.txt"
PY_WANTED = (3, 12)
PY_PLACES = ["/opt/homebrew/bin/python3.12", "/usr/local/bin/python3.12",
             "/opt/homebrew/opt/python@3.12/bin/python3.12", "/usr/local/opt/python@3.12/bin/python3.12",
             "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3.12"]
BREW_PYTHON = "brew install python@3.12"


class Stop(Exception):
    """A setup step could not finish. The message says what to do."""


def say(msg=""):
    print(msg, flush=True)


def py_version(python):
    """(major, minor) of a Python program, or None when it does not run."""
    try:
        r = subprocess.run([str(python), "-c", "import sys; print(sys.version_info[0], sys.version_info[1])"],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        a, b = r.stdout.split()
        return int(a), int(b)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def find_python312(places=None, on_path=True):
    """The full path of a Python 3.12, from PATH first and then the usual Homebrew places."""
    seen = []
    if on_path:
        seen.append(shutil.which("python3.12"))
    seen += list(PY_PLACES if places is None else places)
    for p in seen:
        if p and os.access(p, os.X_OK) and py_version(p) == PY_WANTED:
            return str(p)
    return None


def run(cmd, what, env=None):
    """Run one setup command with its output on screen. Stops with a plain message when it fails."""
    try:
        code = subprocess.call([str(c) for c in cmd], env=env, stdin=subprocess.DEVNULL)
    except OSError as e:
        raise Stop(f"{what} could not start ({e}).")
    if code != 0:
        raise Stop(f"{what} failed (exit {code}). The lines above say why. Nothing else was changed after it.")


def merge_settings(text):
    """Merge the keys of a JSON object into the user's settings file. Returns the file's path."""
    try:
        new = json.loads(text)
    except ValueError as e:
        raise Stop(f"--settings-json is not valid JSON ({e}). Nothing was saved.")
    if not isinstance(new, dict):
        raise Stop("--settings-json must be one JSON object, like '{\"owner\": {\"name\": \"Sam\"}}'. Nothing was saved.")
    path = common.user_settings_path()
    old = common._read_settings(path, "the settings file") if path.exists() else {}
    common.write_json(path, common._merge(old, new))
    say(f"Saved {len(new)} setting{'s' if len(new) != 1 else ''} to {path}: {', '.join(sorted(new)) or '(none)'}")
    return path


def make_sandbox():
    if VENV_PY.exists():
        v = py_version(VENV_PY)
        if v == PY_WANTED:
            say(f"The Python sandbox is already there: {VENV}")
            return
        raise Stop(f"The folder {VENV} holds a Python sandbox that is "
                   f"{'broken' if v is None else 'Python %d.%d' % v}, not Python 3.12. "
                   f"Move that folder to the Trash and run install again.")
    python = find_python312()
    if not python:
        raise Stop("Python 3.12 is not on this Mac. Run this one line, then run install again:\n\n"
                   f"  {BREW_PYTHON}\n")
    say(f"Making the Python sandbox in {VENV} with {python}")
    run([python, "-m", "venv", str(VENV)], "Making the Python sandbox")


def install_packages():
    if not LOCK.is_file():
        raise Stop(f"{LOCK} is missing from the kit.")
    say(f"Installing the pinned packages from {LOCK.name} (this downloads a few hundred MB the first time)")
    run([VENV_PY, "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-r", str(LOCK)],
        "Installing the Python packages")


def get_vendor():
    """Clone Vibe Editing into vendor/ and pin it to exactly doctor.VENDOR_COMMIT. Its own
    setup script, onboarding and tools are never run."""
    target, want = doctor.VENDOR_DIR, doctor.VENDOR_COMMIT
    git = shutil.which("git")
    if not git:
        raise Stop("git is not on this Mac. Run this one line (Apple's own tools), then run install again:\n\n"
                   "  xcode-select --install\n")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if not (target / ".git").exists():
        if target.exists() and any(target.iterdir()):
            raise Stop(f"{target} is there but it is not a download made by this script. "
                       f"Move it to the Trash and run install again.")
        target.parent.mkdir(parents=True, exist_ok=True)
        say(f"Downloading the Vibe Editing kit into {target}")
        run([git, "clone", "--no-recurse-submodules", doctor.VENDOR_URL, str(target)], "Downloading the Vibe Editing kit", env)
    if doctor.vendor_head() != want:
        run([git, "-C", str(target), "-c", "advice.detachedHead=false", "-c", "core.hooksPath=/dev/null",
             "checkout", "--quiet", "--detach", want], "Pinning the Vibe Editing kit to its version", env)
    head = doctor.vendor_head()
    if head != want:
        raise Stop("STOP. The Vibe Editing kit in vendor/ is NOT at the pinned version.\n"
                   f"  wanted: {want}\n  found:  {head}\n"
                   f"Do not use it. Move {target} to the Trash and run install again.")
    say(f"The Vibe Editing kit is at the pinned version {want}")


def install(settings_json=None):
    if settings_json is not None:
        merge_settings(settings_json)
    make_sandbox()
    install_packages()
    get_vendor()
    cfg = common.load_config()
    common.ensure_content_dirs(cfg)
    say(f"Your content folders are in {cfg.root}")
    say()
    code = doctor.main(cfg, out=say)
    if code != 0:
        say("\nInstall did its part. What is still listed as MISSING above is yours to fix; this script "
            "never runs brew for you. Then run:  python3 scripts/setup_env.py check")
    return code


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        say(__doc__)
        return 0 if argv else 2
    cmd, rest = argv[0], argv[1:]
    settings_json = None
    if "--settings-json" in rest:
        i = rest.index("--settings-json")
        if i + 1 >= len(rest):
            say("--settings-json needs a value, like '{\"owner\": {\"name\": \"Sam\"}}'")
            return 2
        settings_json = rest[i + 1]
        del rest[i:i + 2]
    if rest or cmd not in ("check", "install"):
        say("usage: setup_env.py check | install [--settings-json '{...}']")
        return 2
    try:
        if cmd == "check":
            if settings_json is not None:
                say("--settings-json only works with install")
                return 2
            return doctor.main(common.load_config(), out=say)
        return install(settings_json)
    except Stop as e:
        say(f"\nSetup stopped: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
