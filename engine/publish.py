"""Posting. Coming later: no posting route is wired yet, so this ships an interface, a
FakePublisher for tests and the practice run, and a NotWiredPublisher that refuses with a clear
message. The scheduling is real: 5 trials 2 minutes apart, only inside the publish window
(8 AM to 8 PM in the configured time zone), one video's set per day, the rest queue in order.

Safety: in a practice run (REELCAST_LAB_DRY_RUN) only the FakePublisher is ever used. Before
every trial the window and the PAUSED flag are checked again, and an intent record is saved, so
a crash in the middle of a post can never lead to posting the same version twice."""
import time
from datetime import timedelta

from common import dry_run, iso, now, read_json, write_json

SET_SIZE = 5


class NotWired(RuntimeError):
    pass


class Blocked(RuntimeError):
    """A post was started but its result was never recorded. Needs a person; never re-post."""


class InstagramPublisher:
    name = "base"

    def publish_trial(self, file, caption):
        """Post one Trial Reel (graduation_strategy MANUAL). Returns (media_id, permalink)."""
        raise NotImplementedError

    def publish_reel(self, file, caption):
        """Post a normal reel (reposts). Returns (media_id, permalink)."""
        raise NotImplementedError


class NotWiredPublisher(InstagramPublisher):
    name = "not_wired"
    MESSAGE = ("posting and reading numbers are coming later. Nothing was posted; "
               "the video stays queued and its five files are ready for you to post by hand.")

    def publish_trial(self, file, caption):
        raise NotWired(self.MESSAGE)

    def publish_reel(self, file, caption):
        raise NotWired(self.MESSAGE)


class FakePublisher(InstagramPublisher):
    """Pretends to post. Keeps its record in <lab>/fake-instagram.json so ids survive across ticks."""
    name = "fake"

    def __init__(self, cfg):
        self.path = cfg.lab / "fake-instagram.json"

    def _post(self, kind, file, caption):
        d = read_json(self.path, {"next": 1, "posts": []})
        n = d["next"]
        d["next"] = n + 1
        mid = f"FAKE{n:05d}"
        link = f"https://www.instagram.com/reel/{mid}/"
        d["posts"].append(dict(media_id=mid, kind=kind, file=str(file), caption=caption[:80], at=iso(now())))
        write_json(self.path, d)
        return mid, link

    def publish_trial(self, file, caption):
        return self._post("trial", file, caption)

    def publish_reel(self, file, caption):
        return self._post("reel", file, caption)


def get_publisher(cfg):
    if dry_run():
        return FakePublisher(cfg)      # a practice run never gets a real route, whatever the settings say
    name = cfg["publish"]["publisher"]
    if name == "fake":
        return FakePublisher(cfg)
    if name == "not_wired":
        return NotWiredPublisher()
    raise ValueError(f"unknown publisher {name!r}")


def guard(publisher):
    if dry_run() and not isinstance(publisher, FakePublisher):
        raise RuntimeError(f"a practice run refuses to post through {type(publisher).__name__}")


def _hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def window_bounds(cfg, t):
    local = t.astimezone(cfg.tz)
    sh, sm = _hm(cfg["publish"]["window_start"])
    eh, em = _hm(cfg["publish"]["window_end"])
    start = local.replace(hour=sh, minute=sm, second=0, microsecond=0)
    end = local.replace(hour=eh, minute=em, second=0, microsecond=0)
    return start, end


def in_window(cfg, t, span_s=0):
    """True when a post (or a set taking span_s seconds) fits inside that day's window."""
    start, end = window_bounds(cfg, t)
    local = t.astimezone(cfg.tz)
    return start <= local and local + timedelta(seconds=span_s) <= end


def set_span(cfg):
    return cfg["publish"]["gap_seconds"] * (SET_SIZE - 1)


def day_key(cfg, t):
    return t.astimezone(cfg.tz).strftime("%Y-%m-%d")


def set_allowed_today(cfg, lab, t, run_id):
    days = lab.setdefault("publish_days", {})
    today = days.get(day_key(cfg, t), [])
    if run_id in today:
        return True     # this run's set already started today (finish it)
    return len(today) < cfg["publish"].get("sets_per_day", 1)


def next_window_start(cfg, t):
    start, _ = window_bounds(cfg, t)
    local = t.astimezone(cfg.tz)
    if local < start:
        return start
    return (local + timedelta(days=1)).replace(hour=start.hour, minute=start.minute, second=0, microsecond=0)


def default_sleeper(seconds):
    if not dry_run():
        time.sleep(seconds)


def post_set(cfg, run, publisher, files, caption, lab, sleeper=default_sleeper):
    """Post the remaining trials of a run's set, gap_seconds apart.
    Returns 'done', 'stopped' (window closed or PAUSED; the rest stays queued). Raises NotWired,
    or Blocked when an earlier post has an intent but no result."""
    guard(publisher)
    trials = run.d.setdefault("trials", {})
    intent = run.d.get("publish_intent")
    if intent and intent["version"] not in trials:
        raise Blocked(f"the post of {intent['version']} started at {intent['at']} but its result was never "
                      f"recorded, so it may be live already. Check Instagram, then run: python3 tick.py unblock {run.id}")
    gap = cfg["publish"]["gap_seconds"]
    start = now()
    days = lab.setdefault("publish_days", {})
    k = 0
    for name, f in files:
        if name in trials:
            continue
        if k > 0:
            sleeper(gap)
        t = max(now(), start + timedelta(seconds=gap * k))
        if (cfg.lab / "PAUSED").exists():
            run.event("publish", "stopped: PAUSED appeared", detail=f"{len(trials)} of {len(files)} up")
            return "stopped"
        if not in_window(cfg, t):
            run.event("publish", "stopped: outside the publish window", detail=f"{len(trials)} of {len(files)} up")
            return "stopped"
        run.d["publish_intent"] = dict(version=name, at=iso(t))
        run.save()
        mid, link = publisher.publish_trial(f, caption)
        trials[name] = dict(media_id=mid, permalink=link, posted_at=iso(t))
        run.d["publish_intent"] = None
        dk = day_key(cfg, t)
        if run.id not in days.setdefault(dk, []):
            days[dk].append(run.id)
        run.event("publish", f"trial {name} posted", detail=link)
        k += 1
    return "done" if len(trials) == len(files) else "stopped"
