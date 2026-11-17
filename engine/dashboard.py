"""Rebuilds <content root>/Lab_Data/dashboard.html: one self contained page (no external
scripts), neutral colours, the system font, phone friendly. A card per video with its 5 versions,
then an all-time section. Charts are inline SVG. Coming later: it fills up once posting and
reading numbers are wired."""
import html
from collections import Counter

from state import VERSIONS, all_runs

MAIN, ACCENT, INK, PAPER, MUTED = "#1f2937", "#93c5fd", "#111827", "#f3f4f6", "#6b7280"
COLS = [("views", "Views"), ("likes", "Likes"), ("comments", "Comments"), ("shares", "Shares"),
        ("saved", "Saves"), ("hold_3s", "Past 3 s %"), ("avg_pct_watched", "Avg watched %")]


def e(x):
    return html.escape("" if x is None else str(x))


def spark(values, w=90, h=26):
    vals = [v for v in values if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = []
    for i, v in enumerate(values):
        if v is None:
            continue
        x = 4 + i * (w - 8) / max(1, len(values) - 1)
        y = h - 4 - (v - lo) / rng * (h - 8)
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-label="24, 48, 72 hour views">'
            f'<polyline fill="none" stroke="{MAIN}" stroke-width="2" points="{" ".join(pts)}"/></svg>')


def bars(pairs, w=320, bh=22):
    if not pairs:
        return "<p class=muted>No decided videos yet.</p>"
    top = max(v for _, v in pairs) or 1
    h = bh * len(pairs) + 6
    rows = []
    for i, (label, v) in enumerate(pairs):
        bw = (w - 150) * v / top
        y = 3 + i * bh
        rows.append(f'<text x="0" y="{y + 15}" font-size="12" fill="{INK}">{e(label)}</text>'
                    f'<rect x="140" y="{y + 3}" width="{bw:.1f}" height="{bh - 8}" rx="3" fill="{ACCENT}"/>'
                    f'<text x="{144 + bw:.1f}" y="{y + 15}" font-size="12" fill="{INK}">{v}</text>')
    return f'<svg width="100%" viewBox="0 0 {w} {h}" role="img" aria-label="wins by change">{"".join(rows)}</svg>'


def line_chart(points, w=320, h=120):
    vals = [v for _, v in points if v is not None]
    if len(vals) < 2:
        return "<p class=muted>Needs two decided videos.</p>"
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    pts = []
    for i, (_, v) in enumerate(points):
        if v is None:
            continue
        x = 20 + i * (w - 40) / max(1, len(points) - 1)
        y = h - 20 - (v - lo) / rng * (h - 40)
        pts.append(f"{x:.1f},{y:.1f}")
    return (f'<svg width="100%" viewBox="0 0 {w} {h}" role="img" aria-label="3 second hold over time">'
            f'<text x="0" y="12" font-size="11" fill="{MUTED}">{hi:.0f}%</text>'
            f'<text x="0" y="{h - 6}" font-size="11" fill="{MUTED}">{lo:.0f}%</text>'
            f'<polyline fill="none" stroke="{MAIN}" stroke-width="2.5" points="{" ".join(pts)}"/></svg>')


def changes(run):
    out = {}
    import json
    vp = run.edited_dir / "variants.json" if run.edited_dir else None
    if vp and vp.exists():
        for v in json.loads(vp.read_text()).get("versions", []):
            out[v["id"]] = v.get("change_vs_A")
    return out


def card(run):
    d = run.d
    reads = d.get("readings", {})
    final_key = max(reads, key=int) if reads else None
    final = reads.get(final_key, {}).get("versions", {}) if final_key else {}
    win = d.get("winner") or {}
    a = d.get("analysis") or {}
    ch = changes(run)
    rows = []
    for v in VERSIONS:
        m = final.get(v, {})
        trend = spark([reads.get(h, {}).get("versions", {}).get(v, {}).get("views") for h in ("24", "48", "72")])
        badge = f' <span class="badge">Winner</span>' if win.get("winner") == v and d.get("state") in ("decided", "complete") else ""
        link = (d.get("trials") or {}).get(v, {}).get("permalink")
        name = f'<a href="{e(link)}">{e(v[0])}</a>' if link else e(v[0])
        rows.append("<tr><td><b>" + name + "</b>" + badge + f'<div class="chg">{e(ch.get(v[0]) or v[2:].replace("_", " "))}</div></td>'
                    + "".join(f"<td>{e(m.get(k))}</td>" for k, _ in COLS) + f"<td>{trend}</td></tr>")
    head = "<tr><th>Version</th>" + "".join(f"<th>{e(lbl)}</th>" for _, lbl in COLS) + "<th>24/48/72 h</th></tr>"
    prov = " (provisional)" if final_key and reads[final_key].get("provisional") else ""
    meta = [f"State: <b>{e(d.get('state'))}</b>"]
    if final_key:
        meta.append(f"Latest reading: {e(final_key)} h{prov}")
    if win.get("confidence"):
        meta.append(f"Confidence: <b>{e(win['confidence'])}</b>" + (f" ({e('; '.join(win.get('reasons', [])))})" if win.get("reasons") else ""))
    ad = a.get("ad_verdict") or {}
    lesson = (a.get("learning") or {}).get("lesson")
    extra = ""
    if a:
        extra = (f"<p><b>What happened:</b> {e(a.get('summary'))}</p>"
                 + (f"<p><b>Lesson:</b> {e(lesson)}</p>" if lesson else "")
                 + f"<p><b>Ad verdict:</b> {e(ad.get('verdict'))}. {e(ad.get('reason'))}</p>")
    reposts = d.get("reposts") or []
    if reposts:
        extra += "<p class=muted>Reposts: " + ", ".join(
            f"{r['days']} d {e(r['status'])}" + (f" ({e((r.get('reading') or {}).get('metrics', {}).get('views'))} views)" if r.get("reading") else "")
            for r in reposts) + "</p>"
    return (f'<section class="card"><h2>{e(d.get("title"))}</h2><p class="muted">{" · ".join(meta)}</p>'
            f'<div class="scroll"><table>{head}{"".join(rows)}</table></div>{extra}</section>')


def build(cfg):
    runs = [r for r in all_runs(cfg) if r.d.get("trials")]
    runs.sort(key=lambda r: r.d.get("set_done_at", ""), reverse=True)
    decided = [r for r in runs if r.state in ("decided", "complete") and r.d.get("winner")]
    wins = Counter(r.d["winner"]["winner"] for r in decided)
    win_pairs = [(v[0] + " " + v[2:].replace("_", " "), wins.get(v, 0)) for v in VERSIONS]
    hold_pts = []
    for r in sorted(decided, key=lambda r: r.d.get("set_done_at", "")):
        fin = r.d.get("readings", {}).get("72", {}).get("versions", {})
        hs = [m.get("hold_3s") for m in fin.values() if m.get("hold_3s") is not None]
        hold_pts.append((r.id, round(sum(hs) / len(hs), 1) if hs else None))
    body = "".join(card(r) for r in runs) or "<p class=muted>No videos posted yet.</p>"
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>ReelCast Lab</title>
<style>
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:{PAPER};color:{INK}}}
header{{background:{MAIN};color:#fff;padding:18px 16px}} header h1{{margin:0;font-size:22px}}
header p{{margin:4px 0 0;color:{ACCENT};font-size:13px}}
main{{max-width:980px;margin:0 auto;padding:12px}}
.card{{background:#fff;border-radius:10px;padding:14px;margin:12px 0;border-top:4px solid {MAIN}}}
.card h2{{margin:0 0 4px;font-size:18px;color:{MAIN}}}
.muted{{color:{MUTED};font-size:13px}} .scroll{{overflow-x:auto}}
table{{border-collapse:collapse;width:100%;font-size:13px}} th,td{{padding:6px;border-bottom:1px solid #e5e7eb;text-align:right;vertical-align:top}}
th:first-child,td:first-child{{text-align:left;min-width:150px}} .chg{{color:{MUTED};font-size:12px;max-width:260px}}
.badge{{background:{ACCENT};color:{MAIN};border-radius:8px;padding:1px 7px;font-size:11px;font-weight:700}}
a{{color:{MAIN}}} h3{{color:{MAIN}}}
</style></head><body><header><h1>ReelCast Lab</h1><p>5 versions per video, one change each, measured at 24, 48 and 72 hours</p></header>
<main>{body}
<section class="card"><h2>All time</h2><h3>Which change wins most often</h3>{bars(win_pairs if decided else [])}
<h3>3 second hold over time (average of the 5 versions)</h3>{line_chart(hold_pts)}
<p class="muted">Watched past 3 s = 100 minus Instagram's skip rate. Average percent watched stands in for full watches, shares plus saves for profile views: both are estimates.</p></section>
</main></body></html>"""
    out = cfg.lab / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page)
    return str(out)
