"""Analysis after the last reading (a headless claude -p run by default; the command is in the
settings so tests use a stub), then the decision: winner text, ledger rows and the repost
schedule. Coming later: this half ships switched off, because nothing is posted yet."""
import csv
import json
import re
import subprocess
from datetime import timedelta

from common import CODE_DIR, brand, iso, now, owner_name, parse_time, tool_env
from editing import model_command, read_notes
from state import VERSIONS, all_runs

TEMPLATE = CODE_DIR / "prompts" / "analyst_template.md"
LETTERS = {v[0]: v for v in VERSIONS}
BANNED = ("\u2014", "\u2013")     # the em dash and the en dash
LEDGER_FIELDS = ["id", "row_type", "run_id", "version", "title", "filmed_date", "posted_at",
                 "file", "media_id", "permalink", "views", "score", "winner", "confidence", "recorded_at"]


def live_offer(cfg):
    """(name, keyword) of the one thing the videos currently sell, from the "offer" setting.
    Either may be None."""
    o = cfg.get("offer") or {}
    return (str(o.get("name") or "").strip() or None, str(o.get("keyword") or "").strip() or None)


def live_keyword(cfg):
    return live_offer(cfg)[1]


def offer_line(cfg):
    name, kw = live_offer(cfg)
    if not name and not kw:
        return ("No live offer is set in the settings, so the answer can only be \"no\" or \"too_early\", "
                "never \"yes\".")
    what = name or "the offer"
    return f"The live offer is {what}" + (f" (keyword {kw})." if kw else ".")


def frame_strips(cfg, run):
    out = {}
    adir = run.work / "analysis"
    adir.mkdir(parents=True, exist_ok=True)
    for v in VERSIONS:
        f = run.edited_dir / f"{v}.mp4"
        dst = adir / f"strip_{v[0]}.png"
        r = subprocess.run(["ffmpeg", "-v", "error", "-y", "-t", "3", "-i", str(f), "-vf",
                            "fps=2,scale=180:320,tile=6x1", "-frames:v", "1", str(dst)],
                           capture_output=True, env=tool_env(cfg))
        if r.returncode == 0 and dst.exists():
            out[v] = str(dst)
    return out


def history(cfg, exclude):
    rows = []
    for r in all_runs(cfg):
        if r.id == exclude or r.state not in ("decided", "complete"):
            continue
        w = r.d.get("winner", {})
        final = r.d.get("readings", {}).get("72", {}).get("versions", {})
        rows.append(dict(run_id=r.id, title=r.d.get("title"), winner=w.get("winner"),
                         versions={k: dict(hold_3s=v.get("hold_3s"), avg_pct_watched=v.get("avg_pct_watched"),
                                           views=v.get("views")) for k, v in final.items()}))
    return rows


def write_brief(cfg, run, readings_payload, comments, attempt_errors=None):
    adir = run.work / "analysis"
    adir.mkdir(parents=True, exist_ok=True)
    payload = dict(readings_payload, top_comments=comments)
    (adir / "readings.json").write_text(json.dumps(payload, indent=1))
    hist = history(cfg, run.id)
    (adir / "history.json").write_text(json.dumps(hist, indent=1))
    voice = brand(cfg)["voice"]
    strips = frame_strips(cfg, run)
    cues = []
    for v in VERSIONS:
        p = run.work / "render" / "versions" / v[0] / "cues.txt"
        if p.exists():
            cues.append(f"`{p}` ({v})")
    note = ""
    if attempt_errors:
        note = ("\n**This is a retry.** Your first analysis.json did not validate: " + "; ".join(attempt_errors) + "\n")
    vals = {
        "TITLE": run.d.get("title"), "OWNER": owner_name(cfg), "RUN_ID": run.id, "EDITED_DIR": run.edited_dir,
        "VARIANTS": run.edited_dir / "variants.json",
        "CUES": ", ".join(cues) or "(not available; use variants.json and the transcript)",
        "TRANSCRIPT_TXT": run.work / "transcript.txt", "READINGS": adir / "readings.json",
        "STRIPS": ", ".join(f"`{p}`" for p in strips.values()) or "(not available)",
        "HISTORY": adir / "history.json", "PAST_COUNT": len(hist),
        "OFFER_LINE": offer_line(cfg), "NEED_PAST": cfg.get("past_videos_for_ad_verdict", 8),
        "VOICE_FILE": voice, "VOICE_NOTES": read_notes(voice),
        "DONE": run.work / "ANALYSIS_DONE", "ATTEMPT_NOTE": note,
    }
    text = TEMPLATE.read_text()
    for k, v in vals.items():
        text = text.replace("{{" + k + "}}", str(v))
    brief = run.work / "analysis_brief.md"
    brief.write_text(text)
    return brief


def analyst_command(cfg, run, brief):
    return model_command(cfg, "analyst", run, brief)


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def validate(a, past_count, need_past=8, banned=BANNED):
    """Returns (errors, notes). Coerces a premature ad 'yes' to 'too_early' (noted, not an error)."""
    errs, notes = [], []
    if not isinstance(a, dict):
        return ["analysis.json is not an object"], notes
    for k in ("summary", "why_stayed_past_3s", "why_stayed_or_left"):
        if not isinstance(a.get(k), str) or not a[k].strip():
            errs.append(f"{k} missing")
    claims = a.get("claims")
    if not isinstance(claims, list) or not claims:
        errs.append("claims must be a non-empty list")
    else:
        for i, c in enumerate(claims):
            if not isinstance(c, dict) or c.get("label") not in ("supported", "hypothesis"):
                errs.append(f"claims[{i}].label must be supported or hypothesis")
            elif str(c.get("version", ""))[:1] not in LETTERS:
                errs.append(f"claims[{i}].version must be A to E")
            elif not str(c.get("claim", "")).strip():
                errs.append(f"claims[{i}].claim missing")
    lr = a.get("learning")
    if not isinstance(lr, dict) or not str(lr.get("lesson", "")).strip():
        errs.append("learning.lesson missing")
    if not isinstance(a.get("evergreen"), bool):
        errs.append("evergreen must be true or false")
    eu = a.get("evergreen_until")
    if eu not in (None, "") and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(eu)):
        errs.append("evergreen_until must be YYYY-MM-DD or null")
    caps = a.get("repost_captions")
    if not isinstance(caps, list) or len(caps) != 4 or not all(isinstance(c, str) and c.strip() for c in caps):
        errs.append("repost_captions must be 4 non-empty strings")
    ad = a.get("ad_verdict")
    if not isinstance(ad, dict) or ad.get("verdict") not in ("no", "yes", "too_early"):
        errs.append("ad_verdict.verdict must be no, yes or too_early")
    elif ad["verdict"] == "yes":
        if past_count < need_past:
            ad["verdict"] = "too_early"
            ad["reason"] = (f"Changed from yes by the engine: only {past_count} past videos, the rule needs "
                            f"{need_past}. Analyst's reason: {ad.get('reason', '')}")
            notes.append("ad verdict yes coerced to too_early (not enough history)")
        else:
            for k in ("reason", "primary_text", "headline", "button", "audience"):
                if not str(ad.get(k, "")).strip():
                    errs.append(f"ad_verdict.{k} required for yes")
    if any(b in s for s in _strings(a) for b in banned if b):
        errs.append("em or en dash (or another banned string) found; use commas, periods or parentheses")
    return errs, notes


# ------------------------------------------------------------------ decision
def ledger_path(cfg):
    return cfg.lab / "results-ledger.csv"


def ledger_append(cfg, rows):
    """Append only. Old rows are never rewritten; the latest row per id wins when read."""
    p = ledger_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    new = not p.exists()
    with open(p, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LEDGER_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in LEDGER_FIELDS})


def ledger_latest(cfg):
    p = ledger_path(cfg)
    out = {}
    if not p.exists():
        return out
    with open(p, newline="") as f:
        for r in csv.DictReader(f):
            out[r["id"]] = r
    return out


def repost_schedule(cfg, decided_at, days_list):
    """Due times N days after the winner text at the same local wall-clock time (DST safe)."""
    local = parse_time(decided_at).astimezone(cfg.tz)
    out = []
    for d in days_list:
        due_local = (local.replace(tzinfo=None) + timedelta(days=d)).replace(tzinfo=cfg.tz)
        out.append(dict(days=d, due=iso(due_local), status="pending"))
    return out


def ledger_rows_for_decision(run, decision):
    t = iso(now())
    created = run.d.get("parts", [{}])[0].get("created") if run.d.get("parts") else None
    rows = [dict(id=run.id, row_type="FILMED", run_id=run.id, title=run.d.get("title"),
                 filmed_date=created or run.d.get("created"), recorded_at=t)]
    final = run.d.get("readings", {}).get("72", {}).get("versions", {})
    for v, tr in run.d.get("trials", {}).items():
        rows.append(dict(id=f"{run.id}:{v[0]}", row_type="POSTED", run_id=run.id, version=v,
                         title=run.d.get("title"), posted_at=tr["posted_at"], file=str(run.edited_dir / f"{v}.mp4"),
                         media_id=tr["media_id"], permalink=tr["permalink"],
                         views=(final.get(v) or {}).get("views", ""), score=decision["scores"].get(v, ""),
                         winner="yes" if v == decision["winner"] else "no", confidence=decision["confidence"],
                         recorded_at=t))
    return rows
