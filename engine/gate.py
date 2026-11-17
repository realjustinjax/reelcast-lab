"""Quality gate (script, offline). What it checks on the five finished files:

- 5 files exist and are not identical
- 1080x1920, H.264, AAC, under 300 MB, SDR (no HLG or PQ transfer)
- finished length of A at most 240 s (the 4 minute rule applies to the finished cut)
- speech starts in the first 0.2 s (silencedetect)
- integrated loudness within -14 +/- 1.5 LUFS
- captions burned in: tesseract on sampled caption crops, words found in the transcript
- caption font: stroke width of the burned captions against the reference render, so a thin
  fallback font fails
- the on-screen hook of B: every line read on its own (a hook is usually two lines, and a
  single line read of a two line strip comes back as garbage)
- caption.txt and the hook text against text_rules.banned_strings (the em dash and the en dash
  by default)

Captions are found by colour: the fill colours and the outline colour come from the caption
preset the video was rendered with.
"""
import difflib
import hashlib
import json
import re
import statistics
import subprocess
import unicodedata
from pathlib import Path

from common import brand, media_summary, tool_env
from state import VERSIONS

W, H = 1080, 1920


def sh(cfg, cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, env=tool_env(cfg), **kw)


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def speech_start(cfg, f):
    r = sh(cfg, ["ffmpeg", "-hide_banner", "-nostats", "-t", "2", "-i", f, "-af",
                 "silencedetect=noise=-35dB:d=0.05", "-f", "null", "-"], text=True)
    err = r.stderr
    starts = [float(x) for x in re.findall(r"silence_start: (-?[\d.]+)", err)]
    ends = [float(x) for x in re.findall(r"silence_end: ([\d.]+)", err)]
    if starts and starts[0] <= 0.1:
        return ends[0] if ends else 2.0
    return 0.0


def loudness(cfg, f):
    r = sh(cfg, ["ffmpeg", "-hide_banner", "-nostats", "-i", f, "-af", "ebur128", "-f", "null", "-"], text=True)
    summ = r.stderr[r.stderr.rfind("Summary:"):]
    for line in summ.splitlines():
        if line.strip().startswith("I:"):
            return float(line.split()[1])
    return None


def strip_crop(cfg, f, t, y0, hh):
    """One frame at time t, rows y0 to y0 + hh, as raw RGB."""
    y0 = max(0, min(H - 2, int(y0)))
    hh = max(2, min(H - y0, int(hh)))
    r = sh(cfg, ["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", f, "-frames:v", "1",
                 "-vf", f"crop={W}:{hh}:0:{y0}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
    return r.stdout, hh


def caption_crop(cfg, f, t, y_pct, band):
    yc = int(H * y_pct / 100)
    return strip_crop(cfg, f, t, yc - band, 2 * band)


DEFAULT_COLORS = dict(outline=(20, 33, 61), fills=[(255, 255, 255), (76, 201, 240)])
OUTLINE_TOL, FILL_TOL = 40, 55


def _rgb(hexrgb):
    h = str(hexrgb).lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def preset_colors(preset):
    """The colours the captions were drawn with: {"outline": (r, g, b), "fills": [(r, g, b), ...]}."""
    try:
        outline = _rgb(preset["shadow"]["stroke_color"])
        fills = []
        for v in (preset.get("colors") or {}).values():
            if isinstance(v, str) and _rgb(v) not in fills:
                fills.append(_rgb(v))
        return dict(outline=outline, fills=fills or DEFAULT_COLORS["fills"])
    except (KeyError, ValueError, TypeError, IndexError):
        return DEFAULT_COLORS


def run_colors(cfg, run):
    """Caption colours for this run: the preset named in its edit plan, else the brand preset."""
    path = None
    plan = run.work / "edit_plan.json"
    if plan.exists():
        try:
            p = json.loads(plan.read_text()).get("preset")
            if p:
                path = Path(p).expanduser() if Path(p).expanduser().is_absolute() else plan.parent / p
        except ValueError:
            pass
    try:
        return preset_colors(json.loads(Path(path or brand(cfg)["preset"]).read_text()))
    except (OSError, ValueError):
        return DEFAULT_COLORS


def _near(p, c, tol):
    return abs(p[0] - c[0]) < tol and abs(p[1] - c[1]) < tol and abs(p[2] - c[2]) < tol


def caption_strokes(buf, hh, colors=None):
    """Caption fill (the preset's word colours) next to the preset's outline colour.
    Returns (stroke widths, text mask as a set of (x, y)). Stroke widths are horizontal fill runs
    bounded by the outline on both sides (a white shirt is not); the mask keeps fill runs of at
    most 60 px that touch the outline on either side, which is what OCR reads."""
    colors = colors or DEFAULT_COLORS
    oc, fills = colors["outline"], colors["fills"]
    memo_o, memo_f = {}, {}

    def _is_outline(p):
        v = memo_o.get(p)
        if v is None:
            v = memo_o[p] = _near(p, oc, OUTLINE_TOL)
        return v

    def _is_fill(p):
        v = memo_f.get(p)
        if v is None:
            v = memo_f[p] = any(_near(p, c, FILL_TOL) for c in fills)
        return v
    runs, mask = [], set()
    for y in range(0, hh):
        row = buf[y * W * 3:(y + 1) * W * 3]
        if len(row) < W * 3:
            break
        px = [tuple(row[i:i + 3]) for i in range(0, W * 3, 3)]
        x = 0
        while x < W:
            if _is_fill(px[x]):
                s = x
                while x < W and _is_fill(px[x]):
                    x += 1
                left3 = s >= 3 and any(_is_outline(px[s - k]) for k in (1, 2, 3))
                right3 = x + 3 <= W and any(_is_outline(px[x - 1 + k]) for k in (1, 2, 3))
                if left3 and right3 and y % 2 == 0:
                    runs.append(x - s)
                left = s >= 4 and any(_is_outline(px[s - k]) for k in (1, 2, 3, 4))
                right = x + 4 <= W and any(_is_outline(px[x - 1 + k]) for k in (1, 2, 3, 4))
                if (left or right) and x - s <= 60:
                    for xx in range(s, x):
                        mask.add((xx, y))
            else:
                x += 1
    return runs, mask


def ocr_mask(cfg, mask, hh, workdir, tag, rows=None):
    """Write the caption mask as black text on white (PGM) and read it with tesseract as ONE line
    of text (--psm 7). rows=(first, last) keeps only those rows, so each line of a two line
    block can be read on its own."""
    if not mask:
        return ""
    y0, y1 = 0, hh - 1
    if rows:
        y0, y1 = max(0, rows[0] - 6), min(hh - 1, rows[1] + 6)
    oh = y1 - y0 + 1
    p = Path(workdir) / f"ocr_{tag}.pgm"
    data = bytearray(b"\xff" * (W * oh))
    for (x, y) in mask:
        if y0 <= y <= y1:
            data[(y - y0) * W + x] = 0
    p.write_bytes(f"P5 {W} {oh} 255\n".encode() + bytes(data))
    r = subprocess.run(["tesseract", str(p), "stdout", "--psm", "7"], capture_output=True, text=True,
                       env=tool_env(cfg))
    return r.stdout.strip()


def mask_row_bands(mask, min_rows=10, max_gap=3, min_px=3):
    """Split a text mask into its lines: runs of rows that hold text, [(first_row, last_row)]."""
    counts = {}
    for (_, y) in mask:
        counts[y] = counts.get(y, 0) + 1
    rows = sorted(y for y, n in counts.items() if n >= min_px)
    bands, start, last = [], None, None
    for y in rows:
        if start is None:
            start = last = y
        elif y - last <= max_gap + 1:
            last = y
        else:
            bands.append((start, last))
            start = last = y
    if start is not None:
        bands.append((start, last))
    return [b for b in bands if b[1] - b[0] + 1 >= min_rows]


def ocr_hit(ocr_text, transcript_concat, min_chars=5):
    """OCR of big popping captions often drops the spaces ('aboutwiat'), so compare letters only:
    a sample counts when it shares a run of at least min_chars letters with the transcript."""
    o = norm(ocr_text)
    if len(o) < min_chars:
        return 0
    sm = difflib.SequenceMatcher(None, o, transcript_concat, autojunk=False)
    m = sm.find_longest_match(0, len(o), 0, len(transcript_concat))
    return m.size if m.size >= min_chars else 0


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def _describe(s):
    if len(s) == 1 and not s.isalnum():
        return unicodedata.name(s, repr(s)).lower()
    return repr(s)


def sanity(cfg, caption, hook):
    """Built-in text check: caption.txt and the hook text must hold none of the strings in
    text_rules.banned_strings. Returns (ok, detail)."""
    banned = (cfg.get("text_rules") or {}).get("banned_strings") or []
    found = []
    for where, text in (("caption.txt", caption or ""), ("the hook text", hook or "")):
        for b in banned:
            if b and b in text:
                found.append(f"{_describe(b)} in {where}")
    return (not found), ("ok" if not found else "found " + "; ".join(found))


def read_cues(path):
    """cues.txt lines from render_reel.py: '  12.34 *word word'. Returns [(start, text)]."""
    out = []
    for ln in Path(path).read_text().splitlines():
        ln = ln.strip()
        if not ln:
            continue
        t, _, txt = ln.partition(" ")
        try:
            out.append((float(t), txt.replace("*", "").strip()))
        except ValueError:
            continue
    return out


def cue_samples(cues, n, shift=0.0):
    """Up to n cue-start sample times spread over the video, each with that cue's own text."""
    idx = [k for k, (_, txt) in enumerate(cues) if len(norm(txt)) >= 4]
    if not idx:
        return []
    if len(idx) > n:
        idx = [idx[round(j * (len(idx) - 1) / (n - 1))] for j in range(n)]
    out = []
    for k in idx:
        st, txt = cues[k]
        nxt = cues[k + 1][0] if k + 1 < len(cues) else st + 1.0
        out.append((min(st + 0.8, (st + nxt) / 2) + shift, txt))
    return out


def cues_path(run, name):
    plan = run.work / "edit_plan.json"
    wd = run.work / "render"
    if plan.exists():
        try:
            p = json.loads(plan.read_text()).get("work_dir")
            if p:
                wd = Path(p) if Path(p).is_absolute() else (plan.parent / p)
        except ValueError:
            pass
    return wd / "versions" / name[0] / "cues.txt"


def hook_spec(run, vlist):
    """What the on-screen hook of B should say and where it sits. From the renderer's hook.json
    when there is one (each line with its own position), else from variants.json (text only).
    Returns None when B has no hook, else dict(lines=[{"text", "y" or None}], font_px, seconds)."""
    p = cues_path(run, "B_onscreen_hook").parent / "hook.json"
    if p.exists():
        try:
            d = json.loads(p.read_text())
            lines = [dict(text=str(x["text"]), y=x.get("y")) for x in d.get("lines", []) if norm(str(x.get("text", "")))]
            if lines:
                return dict(lines=lines, font_px=d.get("font_px"), seconds=d.get("seconds"))
        except (ValueError, KeyError, TypeError):
            pass
    b = next((v for v in vlist if str(v.get("name") or v.get("id") or "").startswith("B")), None) or {}
    texts = b.get("onscreen_hook_lines") or ([b["onscreen_hook_text"]] if b.get("onscreen_hook_text") else [])
    lines = [dict(text=str(t), y=None) for t in texts if norm(str(t))]
    return dict(lines=lines, font_px=None, seconds=None) if lines else None


def check_hook(cfg, f, spec, tmp, colors=None):
    """Every hook line must be read at one of the sample times. Each line is read on its own:
    cropped by its recorded position when the renderer left one, else by splitting the hook
    strip into its rows of text. Returns (ok, missing lines, what was read)."""
    g = cfg["gate"]
    lines = spec["lines"]
    found = [False] * len(lines)
    reads = []
    secs = spec.get("seconds") or 3.0
    times = [t for t in g.get("hook_sample_times", [1.2, 2.0]) if t < secs - 0.3] or [secs / 2]
    placed = all(isinstance(x.get("y"), (int, float)) for x in lines)

    def hit(got, want):
        w = norm(want)
        return bool(ocr_hit(got, w, min(g["ocr_min_chars"], len(w))))
    for t in times:
        if placed:
            ys = sorted(x["y"] for x in lines)
            pitch = min([b - a for a, b in zip(ys, ys[1:])] or [10 ** 6])
            half = int(max(20, min(0.62 * (spec.get("font_px") or g["hook_band_px"]), pitch / 2)))
            for n, x in enumerate(lines):
                if found[n]:
                    continue
                buf, hh = strip_crop(cfg, f, t, x["y"] - half, 2 * half)
                _, mask = caption_strokes(buf, hh, colors)
                got = ocr_mask(cfg, mask, hh, tmp, f"hook{t}_{n}")
                reads.append(f"{t:.1f}s line {n + 1} want '{x['text']}' read '{got}'")
                found[n] = hit(got, x["text"])
        else:
            buf, hh = caption_crop(cfg, f, t, g["hook_y_pct"], g["hook_band_px"])
            _, mask = caption_strokes(buf, hh, colors)
            got = [ocr_mask(cfg, mask, hh, tmp, f"hook{t}_{k}", rows=b) for k, b in enumerate(mask_row_bands(mask))]
            reads.append(f"{t:.1f}s read {got}")
            for n, x in enumerate(lines):
                found[n] = found[n] or any(hit(r, x["text"]) for r in got)
        if all(found):
            break
    missing = [x["text"] for n, x in enumerate(lines) if not found[n]]
    return (not missing), missing, reads


def ocr_pass(cfg, f, samples, tmp, tag, strokes, colors=None):
    g = cfg["gate"]
    reads, hits = [], 0
    for i, (t, txt) in enumerate(samples):
        buf, hh = caption_crop(cfg, f, t, g["caption_y_pct"], g["caption_band_px"])
        runs, mask = caption_strokes(buf, hh, colors)
        strokes += runs
        got = ocr_mask(cfg, mask, hh, tmp, f"{tag}{i}")
        want = norm(txt)
        need = min(g["ocr_min_chars"], len(want))
        ok = bool(ocr_hit(got, want, need))
        if not ok:    # a second line in the strip (the edge of the hook above) spoils a one line read
            for k, band in enumerate(mask_row_bands(mask)):
                alt = ocr_mask(cfg, mask, hh, tmp, f"{tag}{i}b{k}", rows=band)
                if ocr_hit(alt, want, need):
                    got, ok = alt, True
                    break
        if ok:
            hits += 1
        reads.append(f"{t:.1f}s want '{txt}' read '{got}'")
    return hits, reads


def run_gate(cfg, run, versions=None):
    """Returns dict(ok, font_fail=[names], failures=[text], checks={...})."""
    g = cfg["gate"]
    ed = run.edited_dir
    files = {n: ed / f"{n}.mp4" for n in VERSIONS}
    checks, failures, font_fail = {}, [], []
    missing = [n for n, f in files.items() if not f.exists()]
    if missing:
        return dict(ok=False, font_fail=[], failures=[f"missing files: {', '.join(missing)}"], checks={})
    hashes = {n: sha(f) for n, f in files.items()}
    if len(set(hashes.values())) < len(hashes):
        failures.append("two or more versions are identical files")
    tconcat = ""
    tp = run.work / "transcript.json"
    if tp.exists():
        tconcat = "".join(norm(w["word"]) for w in json.loads(tp.read_text())["words"])
    variants = {}
    vp = ed / "variants.json"
    if vp.exists():
        try:
            variants = json.loads(vp.read_text())
        except ValueError:
            failures.append("variants.json is not valid JSON")
    else:
        failures.append("variants.json is missing")
    vlist = variants.get("versions", []) if isinstance(variants, dict) else []
    if vp.exists() and len(vlist) != 5:
        failures.append(f"variants.json has {len(vlist)} versions, not 5")
    hook = " ".join(v.get("onscreen_hook_text") or "" for v in vlist).strip()
    colors = run_colors(cfg, run)
    ref = g.get("stroke_ref_px")
    min_stroke = max(g.get("stroke_min_px") or 0, (ref or 0) * g.get("stroke_min_ratio", 0.75))
    tmp = run.work / "gate"
    tmp.mkdir(parents=True, exist_ok=True)
    durs = {}
    for n, f in files.items():
        c = dict()
        try:
            m = media_summary(cfg, f)
        except Exception as e:
            failures.append(f"{n}: cannot read ({e})")
            continue
        durs[n] = m["duration"]
        c.update(size_mb=round(m["size"] / 1e6, 1), duration=round(m["duration"], 2),
                 video=f"{m.get('vcodec')} {m.get('width')}x{m.get('height')}", audio=m.get("acodec"),
                 transfer=m.get("transfer"))
        if (m.get("width"), m.get("height")) != (g["width"], g["height"]):
            failures.append(f"{n}: frame is {m.get('width')}x{m.get('height')}, not 1080x1920")
        if m.get("vcodec") != "h264":
            failures.append(f"{n}: video codec {m.get('vcodec')}, not H.264")
        if m.get("acodec") != "aac":
            failures.append(f"{n}: audio codec {m.get('acodec')}, not AAC")
        if m["size"] >= g["max_mb"] * 1e6:
            failures.append(f"{n}: {c['size_mb']} MB, over {g['max_mb']} MB")
        if m.get("hdr"):
            failures.append(f"{n}: HDR transfer ({m.get('transfer')}), must be SDR")
        if n == "A_control" and m["duration"] > cfg["limits"]["finished_max_seconds"]:
            failures.append(f"A_control runs {m['duration']:.0f} s, over the {cfg['limits']['finished_max_seconds']} s limit")
        s0 = speech_start(cfg, f)
        c["speech_start_s"] = round(s0, 3)
        if s0 > g["speech_start_max_s"]:
            failures.append(f"{n}: speech starts at {s0:.2f} s (limit {g['speech_start_max_s']} s)")
        lu = loudness(cfg, f)
        c["lufs"] = lu
        if lu is None or abs(lu - g["loudness_target"]) > g["loudness_tolerance"]:
            failures.append(f"{n}: loudness {lu} LUFS, outside {g['loudness_target']} +/- {g['loudness_tolerance']}")
        if versions and n not in versions:
            checks[n] = c
            continue
        body = max(1.0, m["duration"] - 2.0)
        k = g["ocr_samples"]
        cp = cues_path(run, n)
        strokes = []
        if cp.exists():
            cues = [x for x in read_cues(cp) if x[0] < body - 0.3]
            samples = cue_samples(cues, k)
        else:     # no cue file (the editor did not use render_reel.py): compare with the transcript
            samples = [(body * (0.06 + 0.88 * i / max(1, k - 1)), None) for i in range(k)]
        if samples and samples[0][1] is None:
            reads, hits = [], 0
            for i, (t, _) in enumerate(samples):
                buf, hh = caption_crop(cfg, f, t, g["caption_y_pct"], g["caption_band_px"])
                runs, mask = caption_strokes(buf, hh, colors)
                strokes += runs
                got = ocr_mask(cfg, mask, hh, tmp, f"{n[0]}{i}")
                reads.append(got)
                hits += 1 if ocr_hit(got, tconcat, g["ocr_min_chars"]) else 0
        else:
            hits, reads = ocr_pass(cfg, f, samples, tmp, n[0], strokes, colors)
            if samples and hits < round(len(samples) * g["ocr_min_hit_ratio"]):
                h2, r2 = ocr_pass(cfg, f, cue_samples(cues, k, shift=0.25), tmp, n[0] + "r", strokes, colors)
                c["ocr_resample_hits"] = h2
                if h2 > hits:
                    hits, reads = h2, r2
        c["ocr_samples"] = len(samples)
        c["ocr_hits"] = hits
        c["ocr"] = reads
        if not samples or hits < max(1, round(len(samples) * g["ocr_min_hit_ratio"])):
            failures.append(f"{n}: burned captions not found (OCR matched {hits} of {len(samples)} sampled cues)")
        c["stroke_px"] = statistics.median(strokes) if strokes else None
        if not strokes or c["stroke_px"] < min_stroke:
            font_fail.append(n)
        spec = hook_spec(run, vlist) if n == "B_onscreen_hook" else None
        if spec:
            ok_h, missing, hook_reads = check_hook(cfg, f, spec, tmp, colors)
            c["hook_ocr"] = hook_reads
            if not ok_h:
                failures.append("B_onscreen_hook: the hook text is not on screen in the first seconds (not found: "
                                + "; ".join(f"'{m}'" for m in missing) + ")")
        checks[n] = c
    if len(durs) == 5:
        a = durs["A_control"]
        if durs["D_tight_cut"] >= a:
            failures.append(f"D_tight_cut ({durs['D_tight_cut']:.1f} s) is not shorter than A ({a:.1f} s)")
        for n in ("B_onscreen_hook", "C_cold_open", "E_visual_open"):
            if abs(durs[n] - a) > g["same_length_tolerance_s"]:
                failures.append(f"{n} ({durs[n]:.1f} s) differs from A ({a:.1f} s) by more than "
                                f"{g['same_length_tolerance_s']} s")
    cap = (ed / "caption.txt").read_text() if (ed / "caption.txt").exists() else ""
    if not cap.strip():
        failures.append("caption.txt is missing or empty")
    if len(cap) >= g["caption_max_chars"]:
        failures.append(f"caption.txt has {len(cap)} characters (Instagram limit {g['caption_max_chars']})")
    tags = re.findall(r"#\w+", cap)
    if len(tags) > g["caption_max_hashtags"]:
        failures.append(f"caption.txt has {len(tags)} hashtags (limit {g['caption_max_hashtags']})")
    ok_s, det = sanity(cfg, cap, hook)
    checks["text_check"] = det
    if not ok_s:
        failures.append(f"text check failed: {det}")
    if font_fail:
        checks["font"] = f"stroke under {min_stroke:.1f} px in {', '.join(font_fail)}"
    ok = not failures and not font_fail
    return dict(ok=ok, font_fail=font_fail, failures=failures, checks=checks)
