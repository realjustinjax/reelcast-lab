"""Messages to the owner. A macOS notification is shown only when REELCAST_LAB_DRY_RUN is unset
AND the setting notify.enabled is true (it ships false). Otherwise the message is appended to
<content root>/Lab_Data/notify-outbox.log and nothing else happens.

The title and the body are handed to osascript as arguments. They are never pasted into the
script text, so nothing in a message can run as code."""
import subprocess

from common import dry_run, iso, log, now

OSASCRIPT = "/usr/bin/osascript"
SCRIPT = ("on run argv",
          "display notification (item 2 of argv) with title (item 1 of argv)",
          "end run")


def command(title, body):
    cmd = [OSASCRIPT]
    for line in SCRIPT:
        cmd += ["-e", line]
    return cmd + ["--", str(title), str(body)]


def send(cfg, title, body):
    real = (not dry_run()) and bool(cfg.get("notify", {}).get("enabled"))
    if real:
        try:
            r = subprocess.run(command(title, body), capture_output=True, text=True, timeout=60)
            log(cfg, f"notify sent ({r.returncode}): {title}")
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired) as e:
            log(cfg, f"notify failed: {e}")
            return False
    out = cfg.lab / "notify-outbox.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a") as f:
        f.write(f"{iso(now())}\t{title}\t{body}\n")
    log(cfg, f"notify (outbox only): {title}: {body}")
    return True
