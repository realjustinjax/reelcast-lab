#!/usr/bin/env python3
"""Build your own reel end card (a 1080x1920 PNG) from the template in this folder.

  python3 build_end_card.py --name "Your Name" --title "What you do" \\
      --photo ~/Pictures/me-square.jpg \\
      --out "~/ReelCast Lab/Lab_Data/brand/end-card.png"

Everything on the card comes from the arguments; nothing is baked in:
  --name        the big line (required)
  --title       the smaller line under the name (optional)
  --kicker      the small line above the name (default "Check out more from")
  --photo       a square picture shown in a circle (optional)
  --logo        a logo shown near the bottom (optional, PNG with a clear background works best)
  --bg          background colour, hex (default #1f2937)
  --accent      accent colour for the kicker and the photo ring, hex (default #4cc9f0)
  --text        text colour, hex (default #ffffff)
  --platforms   comma list of icon names; each needs <name>.svg in the icons folder (ships empty, see icons/README.txt)
  --icons-dir   another folder of icons to use instead
  --font        a .ttf or .otf file (default: the kit's brand/default/fonts/Inter-Bold.otf)
  --out         where to write the PNG (default: end-card.png in the current folder)
  --html-only   write the filled page next to --out (same name, .html) and stop; no picture
  --chrome      the Google Chrome program, used headless to draw the page

The picture step needs Google Chrome. Standard library only otherwise.
"""
import argparse
import html
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
TEMPLATE = HERE / "end-card.html"
DEFAULT_FONT = HERE.parent / "default" / "fonts" / "Inter-Bold.otf"
DEFAULT_CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
# circle colour behind each platform icon
PLATFORM_BG = {
    "linkedin": "#0A66C2",
    "instagram": "linear-gradient(45deg,#FEDA75 0%,#FA7E1E 25%,#D62976 50%,#962FBF 75%,#4F5BD5 100%)",
    "youtube": "#FF0000",
    "tiktok": "#0B0B0B",
}
HEX = re.compile(r"#?[0-9a-fA-F]{6}")


def colour(value, what):
    if not HEX.fullmatch(value or ""):
        raise SystemExit(f"{what} must be a hex colour like #1f2937 (got {value!r})")
    return "#" + value.lstrip("#").lower()


def darker(hexcolour, k=0.75):
    h = hexcolour.lstrip("#")
    return "#" + "".join(f"{round(int(h[i:i + 2], 16) * k):02x}" for i in (0, 2, 4))


def file_url(path, what):
    p = pathlib.Path(path).expanduser().resolve()
    if not p.is_file():
        raise SystemExit(f"{what} not found: {p}")
    return p.as_uri()


def icon(name, icons_dir=None):
    d = pathlib.Path(icons_dir).expanduser() if icons_dir else HERE / "icons"
    p = d / f"{name}.svg"
    if not p.is_file():
        have = ", ".join(sorted(x.stem for x in d.glob("*.svg"))) or "none (see icons/README.txt)"
        raise SystemExit(f"no icon for platform {name!r}; available: {have}")
    svg = re.sub(r"<title>.*?</title>", "", p.read_text())
    return svg.replace("<svg ", '<svg fill="#fff" ', 1)


def build_html(a):
    bg = colour(a.bg, "--bg")
    platforms = [x.strip().lower() for x in (a.platforms or "").split(",") if x.strip()]
    icons = "\n".join(f'<div class="ic" style="background:{PLATFORM_BG.get(n, "#374151")}">{icon(n, a.icons_dir)}</div>'
                      for n in platforms)
    name = html.escape(a.name)
    values = {
        "FONT_URL": file_url(a.font, "font file"),
        "TEXT": colour(a.text, "--text"), "BG": bg, "BG2": darker(bg), "ACCENT": colour(a.accent, "--accent"),
        "PAD_BOTTOM": "220" if a.logo else "0",
        "PHOTO_URL": file_url(a.photo, "photo") if a.photo else "",
        "PHOTO_BLOCK": '<div class="photo"></div>' if a.photo else "",
        "KICKER": html.escape(a.kicker),
        "NAME": name, "NAME_PX": "128" if len(a.name) <= 16 else ("100" if len(a.name) <= 22 else "80"),
        "TITLE_BLOCK": f'<div class="title">{html.escape(a.title)}</div>' if a.title else "",
        "PLATFORM_BLOCK": (f'<div class="on">On these platforms</div>\n    <div class="row">{icons}</div>'
                           if platforms else ""),
        "LOGO_BLOCK": f'<img class="logo" src="{file_url(a.logo, "logo")}">' if a.logo else "",
    }
    page = TEMPLATE.read_text()
    for k, v in values.items():
        page = page.replace("{{" + k + "}}", v)
    return page


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build a reel end card (1080x1920 PNG).")
    ap.add_argument("--name", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--kicker", default="Check out more from")
    ap.add_argument("--photo", default="")
    ap.add_argument("--logo", default="")
    ap.add_argument("--bg", default="#1f2937")
    ap.add_argument("--accent", default="#4cc9f0")
    ap.add_argument("--text", default="#ffffff")
    ap.add_argument("--platforms", default="")
    ap.add_argument("--icons-dir", default=None, help="folder of <platform>.svg files (default: icons/ beside this script)")
    ap.add_argument("--font", default=str(DEFAULT_FONT))
    ap.add_argument("--out", default="end-card.png")
    ap.add_argument("--html-only", action="store_true")
    ap.add_argument("--chrome", default=DEFAULT_CHROME)
    a = ap.parse_args(argv)
    out = pathlib.Path(a.out).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    page = out.with_suffix(".html")
    page.write_text(build_html(a))
    if a.html_only:
        print("wrote", page)
        return 0
    if not pathlib.Path(a.chrome).exists():
        raise SystemExit(f"Google Chrome not found at {a.chrome}. Install it, or pass --chrome, or use "
                         f"--html-only and take the picture of {page} yourself.")
    try:
        subprocess.run(
            [a.chrome, "--headless=new", "--hide-scrollbars", "--force-device-scale-factor=1",
             "--window-size=1080,1920", "--allow-file-access-from-files",
             f"--screenshot={out}", page.as_uri()],
            check=True, capture_output=True, timeout=120,
        )
    finally:
        page.unlink(missing_ok=True)
    print("wrote", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
