#!/usr/bin/env python3
"""Write the ReelCast Lab background job file and print the commands that switch it on.

  python3 scripts/install_job.py                 write the job file, print the load commands
  python3 scripts/install_job.py --uninstall     print the commands that switch it off and remove it
  python3 scripts/install_job.py --print         show the filled job file, write nothing

The job runs engine/tick.py every 10 minutes (the "tick_seconds" setting). This script never
runs launchctl itself: it prints the exact commands and you run them, so nothing starts in the
background without you seeing it.

Options
  --dest DIR      where the job file goes (default ~/Library/LaunchAgents)
  --python PATH   the Python that runs the tick (default: the one running this script)
"""
import argparse
import os
import plistlib
import shlex
import sys
from pathlib import Path
from xml.sax.saxutils import escape

KIT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT_ROOT / "engine"))

import common  # noqa: E402

LABEL = "com.reelcast-lab.tick"
TEMPLATE = KIT_ROOT / "launchd" / f"{LABEL}.plist.template"
JOB_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def render(cfg, python):
    values = {
        "LABEL": LABEL, "PYTHON": str(python), "TICK": str(KIT_ROOT / "engine" / "tick.py"),
        "KIT_ROOT": str(KIT_ROOT), "INTERVAL": str(int(cfg.get("tick_seconds", 600))),
        "PATH": JOB_PATH, "LOG": os.path.expanduser(cfg["log_path"]),
    }
    text = TEMPLATE.read_text()
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", escape(v))
    plistlib.loads(text.encode())          # refuse to write a job file launchd could not read
    return text


def main(argv=None):
    ap = argparse.ArgumentParser(description="Write the ReelCast Lab job file and print how to load it.")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--print", dest="show", action="store_true")
    ap.add_argument("--dest", default="~/Library/LaunchAgents")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args(argv)
    dest = Path(a.dest).expanduser()
    plist = dest / f"{LABEL}.plist"
    domain = f"gui/{os.getuid()}"
    q = shlex.quote
    if a.uninstall:
        print("To switch the ReelCast Lab job off and remove it, run these two commands yourself:\n")
        print(f"  launchctl bootout {domain}/{LABEL}")
        print(f"  rm {q(str(plist))}")
        print("\nNothing was changed. Your videos and settings are not touched by either command.")
        return 0
    cfg = common.load_config()
    text = render(cfg, a.python)
    if a.show:
        print(text, end="")
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    plist.write_text(text)
    print(f"Wrote {plist}")
    print("\nIt is not running yet. To switch it on, run this command yourself:\n")
    print(f"  launchctl bootstrap {domain} {q(str(plist))}")
    print("\nTo check that it is loaded:\n")
    print(f"  launchctl print {domain}/{LABEL} | head -n 20")
    print(f"\nThe job writes to {os.path.expanduser(cfg['log_path'])}")
    print("To switch it off later: python3 scripts/install_job.py --uninstall")
    return 0


if __name__ == "__main__":
    sys.exit(main())
