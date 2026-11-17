"""Readings at 24, 48 and 72 hours after the last trial of the set posted.

Coming later: no reader is wired yet. InsightsReader names the metrics. FakeReader is for tests
and the practice run; NotWiredReader refuses. Empty answers are retried each tick for up to
readings.retry_hours, then the reading is kept and marked provisional. The 24 hour reading is
always provisional (numbers can lag 48 hours).
Derived: watched past 3 s = 100 minus skip rate; average percent watched = average watch time
divided by the version's length (ig_reels_avg_watch_time is in milliseconds)."""
import hashlib
from datetime import timedelta

from common import dry_run, parse_time, read_json

METRICS = ["views", "reach", "likes", "comments", "shares", "saved", "total_interactions",
           "ig_reels_avg_watch_time", "ig_reels_video_view_total_time", "reels_skip_rate", "reposts"]


class NotWired(RuntimeError):
    pass


class InsightsReader:
    name = "base"

    def read(self, media_id, age_hours=None):
        """dict of METRICS (missing keys allowed) or None when Instagram returned nothing."""
        raise NotImplementedError

    def comments(self, media_id, limit=10):
        return []


class NotWiredReader(InsightsReader):
    name = "not_wired"

    def read(self, media_id, age_hours=None):
        raise NotWired("posting and reading numbers are coming later; no reader is wired yet.")


class FakeReader(InsightsReader):
    """Made-up but stable numbers. <lab>/fake-metrics.json can pin answers:
    {"FAKE00001": {"24": {...}, "48": null}} (null = an empty answer)."""
    name = "fake"

    def __init__(self, cfg):
        self.pins = read_json(cfg.lab / "fake-metrics.json", {})

    def read(self, media_id, age_hours=None):
        pin = self.pins.get(media_id)
        if pin is not None:
            key = str(int(round(age_hours or 72)))
            nearest = min((k for k in pin), key=lambda k: abs(int(k) - int(key)), default=None)
            return pin.get(nearest) if nearest else None
        seed = int(hashlib.sha256(media_id.encode()).hexdigest(), 16)
        f = min(1.0, max(0.2, (age_hours or 72) / 72))
        views = int((250 + seed % 1800) * f)
        return dict(views=views, reach=int(views * 0.8), likes=int(views * (0.03 + (seed >> 8) % 5 / 100)),
                    comments=int(views * (0.002 + (seed >> 12) % 4 / 1000)), shares=int(views * 0.004),
                    saved=int(views * (0.003 + (seed >> 16) % 3 / 1000)), total_interactions=int(views * 0.05),
                    ig_reels_avg_watch_time=int(12000 + (seed >> 20) % 30000),
                    ig_reels_video_view_total_time=int(views * 15000),
                    reels_skip_rate=round(30 + (seed >> 24) % 400 / 10, 1), reposts=(seed >> 28) % 3)

    def comments(self, media_id, limit=10):
        return ["This is so true", "How do I set this up?", "Saving this"][:limit]


def get_reader(cfg):
    if dry_run():
        return FakeReader(cfg)       # a practice run never reads real numbers, whatever the settings say
    name = cfg["readings"]["reader"]
    if name == "fake":
        return FakeReader(cfg)
    if name == "not_wired":
        return NotWiredReader()
    raise ValueError(f"unknown reader {name!r}")


def due_times(set_done_at, hours_list):
    t0 = parse_time(set_done_at)
    return {h: t0 + timedelta(hours=h) for h in hours_list}


def empty(m):
    return not m or all(m.get(k) in (None, 0) for k in ("views", "reach", "likes"))


def derive(m, length_s):
    out = dict(m or {})
    sk = out.get("reels_skip_rate")
    out["hold_3s"] = round(100 - sk, 1) if sk is not None else None
    aw = out.get("ig_reels_avg_watch_time")
    out["avg_pct_watched"] = round(aw / 1000.0 / length_s * 100, 1) if (aw is not None and length_s) else None
    return out
