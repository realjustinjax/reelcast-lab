"""Shared helpers for the ReelCast Lab engine (Python standard library only).

Settings, the clock (fakeable), logging, atomic JSON, paths and small ffprobe helpers.

Settings are merged in this order, later wins:
  1. engine/config.json                      the shipped defaults
  2. ~/.config/reelcast-lab/settings.json    the user's own settings (written by setup; may not
                                             exist). REELCAST_LAB_SETTINGS names another file.
  3. the JSON file named by REELCAST_LAB_CONFIG  (tests and the practice run use it)
  4. overrides passed in code
  5. REELCAST_LAB_ROOT                       replaces content_root
Then every string value is expanded: a leading ~, {kit_root} (the folder that holds engine/)
and {content_root}. Written as //{kit_root} or //{content_root} (the absolute path form of a
Claude Code permission rule) the placeholder keeps exactly two leading slashes.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CODE_DIR = Path(__file__).resolve().parent
KIT_ROOT = CODE_DIR.parent
CONFIG_PATH = CODE_DIR / "config.json"
USER_SETTINGS = "~/.config/reelcast-lab/settings.json"
LAB_DIR = "Lab_Data"
HOMEBREW_PREFIXES = ("/opt/homebrew", "/usr/local")


# ------------------------------------------------------------------ config
def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def expand(s, values):
    """Fill {name} placeholders and a leading ~ in one string."""
    for k, v in values.items():
        v = str(v)
        if v.startswith("/"):
            s = s.replace("//{" + k + "}", "/" + v)
        s = s.replace("{" + k + "}", v)
    if s == "~" or s.startswith("~/"):
        s = os.path.expanduser(s)
    return s


def _expand_all(x, values):
    if isinstance(x, str):
        return expand(x, values)
    if isinstance(x, list):
        return [_expand_all(v, values) for v in x]
    if isinstance(x, dict):
        return {k: (v if k.startswith("_") else _expand_all(v, values)) for k, v in x.items()}
    return x


def user_settings_path():
    return Path(os.environ.get("REELCAST_LAB_SETTINGS") or USER_SETTINGS).expanduser()


def _read_settings(path, what):
    try:
        data = json.loads(Path(path).read_text())
    except ValueError as e:
        raise SystemExit(f"ReelCast Lab: {what} {path} is not valid JSON ({e})")
    if not isinstance(data, dict):
        raise SystemExit(f"ReelCast Lab: {what} {path} must hold one JSON object")
    return data


def load_config(overrides=None):
    global _DEFAULT_TZ
    cfg = json.loads(CONFIG_PATH.read_text())
    user = user_settings_path()
    if user.exists():
        cfg = _merge(cfg, _read_settings(user, "the settings file"))
    extra = os.environ.get("REELCAST_LAB_CONFIG")
    if extra:
        cfg = _merge(cfg, _read_settings(extra, "the REELCAST_LAB_CONFIG file"))
    if overrides:
        cfg = _merge(cfg, overrides)
    if os.environ.get("REELCAST_LAB_ROOT"):
        cfg["content_root"] = os.environ["REELCAST_LAB_ROOT"]
    values = {"kit_root": str(KIT_ROOT)}
    values["content_root"] = expand(str(cfg["content_root"]), values)
    cfg = Config(_expand_all(cfg, values))
    _DEFAULT_TZ = cfg.tz
    return cfg


def local_zone():
    """The Mac's own time zone (where /etc/localtime points), or UTC when it cannot be read."""
    try:
        target = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in target:
            return ZoneInfo(target.split("zoneinfo/", 1)[1])
    except (OSError, ValueError, ZoneInfoNotFoundError):
        pass
    return ZoneInfo("UTC")


class Config(dict):
    @property
    def kit_root(self):
        return KIT_ROOT

    @property
    def root(self):
        return Path(os.path.expanduser(self["content_root"]))

    @property
    def lab(self):
        return self.root / LAB_DIR

    @property
    def runs_dir(self):
        return self.lab / "runs"

    @property
    def tz(self):
        name = self.get("timezone") or "auto"
        if name == "auto":
            return local_zone()
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"settings: timezone {name!r} is not a known zone name "
                             f"(use \"auto\" or a name like Europe/London)")

    def path(self, key):
        return Path(os.path.expanduser(self[key]))

    def fill(self, s, **kw):
        kw.setdefault("content_root", str(self.root))
        kw.setdefault("kit_root", str(KIT_ROOT))
        return expand(s, kw)


def dry_run():
    return bool(os.environ.get("REELCAST_LAB_DRY_RUN"))


def owner_name(cfg):
    return (cfg.get("owner") or {}).get("name") or "the owner"


def content_dirs(cfg):
    """Every folder the engine expects inside the ReelCast Lab folder."""
    r = cfg.root
    return [r / "RAW" / "Singles", r / "RAW" / "Multi-part", r / "RAW" / "Needs-attention",
            r / "Edited", r / "Originals", r / "Lab_Data" / "runs", r / "Lab_Data" / "brand"]


def ensure_content_dirs(cfg):
    """Create any missing folder from content_dirs. Never touches files."""
    for d in content_dirs(cfg):
        d.mkdir(parents=True, exist_ok=True)


# Characters a folder path may not hold, because the editor's quoted commands and safety rules
# cannot carry them safely. A space and an apostrophe are fine.
UNSAFE_PATH_CHARS = '*?[]{}\\"$`\n'


def unsafe_path_chars(path):
    return sorted({c for c in str(path) if c in UNSAFE_PATH_CHARS})


# ------------------------------------------------------------------ tools
def ffmpeg_dir(cfg):
    """The folder holding ffmpeg and ffprobe, or None to use whatever is on PATH.
    "auto" prefers the full Homebrew build (it has the tone mapping and subtitle filters)."""
    v = cfg.get("ffmpeg_dir") or "auto"
    if v != "auto":
        return os.path.expanduser(v)
    for prefix in HOMEBREW_PREFIXES:
        d = Path(prefix) / "opt" / "ffmpeg-full" / "bin"
        if (d / "ffmpeg").exists():
            return str(d)
    return None


def tool_env(cfg):
    """The environment every job and tool call gets: ffmpeg first on PATH, Homebrew last."""
    env = dict(os.environ)
    parts = [p for p in env.get("PATH", "").split(":") if p]
    fd = ffmpeg_dir(cfg)
    if fd:
        parts.insert(0, fd)
    for prefix in HOMEBREW_PREFIXES:
        b = prefix + "/bin"
        if b not in parts and os.path.isdir(b):
            parts.append(b)
    env["PATH"] = ":".join(parts)
    return env


def find_tool(cfg, name):
    """Full path of a command line tool on the engine's PATH, or None."""
    return shutil.which(name, path=tool_env(cfg)["PATH"])


def claude_bin(cfg):
    """The claude command. "auto" looks on PATH, then in the usual install places."""
    v = cfg.get("claude_bin") or "auto"
    if v != "auto":
        return os.path.expanduser(v)
    found = find_tool(cfg, "claude")
    if found:
        return found
    for c in [p + "/bin/claude" for p in HOMEBREW_PREFIXES] + ["~/.local/bin/claude", "~/.claude/local/claude"]:
        c = os.path.expanduser(c)
        if os.path.exists(c):
            return c
    return "claude"


def brand(cfg):
    """Where the look comes from: the user's brand folder first, the kit's neutral defaults for
    anything missing. Returns paths: preset, end_card, fonts_dir, voice, framing."""
    b = cfg.get("brand") or {}
    user = Path(os.path.expanduser(b.get("dir") or str(cfg.lab / "brand")))
    default = Path(os.path.expanduser(b.get("default_dir") or str(KIT_ROOT / "brand" / "default")))

    def pick(name, default_name=None):
        u = user / name
        return u if u.exists() else default / (default_name or name)
    preset = pick("caption_preset.json", "default_pop.json")
    fonts = default / "fonts"
    try:
        font_file = json.loads(preset.read_text()).get("font_file")
        if font_file and (user / "fonts" / font_file).exists():
            fonts = user / "fonts"
    except (OSError, ValueError):
        pass
    return dict(preset=preset, end_card=pick("end-card.png"), fonts_dir=fonts,
                voice=pick("voice.md"), framing=pick("framing.md"))


# ------------------------------------------------------------------ clock
_NOW = None
_DEFAULT_TZ = None


def set_now(dt):
    """Pin the clock (tests). None returns to env / real time."""
    global _NOW
    _NOW = dt


def now():
    if _NOW is not None:
        return _NOW
    env = os.environ.get("REELCAST_LAB_NOW")
    if env:
        return parse_time(env)
    return datetime.now(timezone.utc)


def parse_time(s):
    """ISO time to UTC. A time with no zone is read in the configured zone."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_DEFAULT_TZ or local_zone())
    return dt.astimezone(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def hours(h):
    return timedelta(hours=h)


# ------------------------------------------------------------------ files
def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text())


def write_json(path, data):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix="." + p.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=1)
        f.write("\n")
    os.replace(tmp, p)


def slugify(s, maxlen=48):
    s = re.sub(r"[^A-Za-z0-9]+", "-", s.lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "video"


def log(cfg, msg):
    line = f"{iso(now())} {msg}"
    if sys.stdout.isatty():   # under launchd stdout is the same log file; do not write it twice
        print(line, flush=True)
    p = Path(os.path.expanduser(cfg["log_path"]))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ media
def ffprobe(cfg, path):
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                       capture_output=True, text=True, env=tool_env(cfg), timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {Path(path).name}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)


def media_summary(cfg, path):
    d = ffprobe(cfg, path)
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in d["streams"] if s.get("codec_type") == "audio"), None)
    fmt = d.get("format", {})
    tags = fmt.get("tags", {}) or {}
    out = dict(duration=float(fmt.get("duration") or 0), size=int(fmt.get("size") or 0),
               created=tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
               or ((v or {}).get("tags") or {}).get("creation_time"),
               has_audio=a is not None)
    if v:
        w, h = int(v.get("width", 0)), int(v.get("height", 0))
        rot = 0
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rot = int(float(sd["rotation"]))
        if abs(rot) % 180 == 90:
            w, h = h, w
        out.update(width=w, height=h, rotation=rot, vcodec=v.get("codec_name"), profile=v.get("profile"),
                   pix_fmt=v.get("pix_fmt"), transfer=v.get("color_transfer"),
                   hdr=v.get("color_transfer") in ("arib-std-b67", "smpte2084"),
                   fps=v.get("r_frame_rate"))
    if a:
        out.update(acodec=a.get("codec_name"), sample_rate=int(a.get("sample_rate") or 0),
                   channels=int(a.get("channels") or 0))
    return out
