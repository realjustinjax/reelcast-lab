#!/usr/bin/env python3
"""ReelCast Lab renderer: five reel versions (A to E) from one edit_plan.json.

Everything video specific (cut plan, text fixes, key words, hook, crop, overrides) lives in the
plan. The look (caption preset, fonts, end card) comes from the brand folder in the content
root, with the kit's neutral defaults in brand/default/ for anything missing.

Run it with the kit's Python sandbox (numpy, Pillow, fontTools), the "sandbox_python" setting.
ffmpeg is found through the "ffmpeg_dir" setting, so nothing needs to be on PATH first.

Subcommands
  prepare  --out-dir DIR [--width 1440] RAW [RAW ...]
           Makes DIR/sdr.mp4 (SDR BT.709 working copy, 30 fps) and DIR/audio.wav (48 kHz mono
           16 bit) from one raw file, or several joined in the order given. HLG or PQ input is
           tone mapped (zscale linear, npl 100, hable, desat 0); SDR input is only scaled.
           Writes DIR/prepare.json with what it found.
  render   PLAN [A B ...] [--plan-only]
           Renders the named versions (default all in the plan) into the plan's out_dir.
  variants PLAN [--source TEXT]
           Writes out_dir/variants.json from the plan, the render meta and ffprobe.

edit_plan.json (paths may be relative to the plan file; a full example with notes is in
examples/edit_plan.example.json):
  {
   "inputs": {"video": "sdr.mp4", "audio": "audio.wav", "transcript": "transcript.json"},
   "out_dir": "...", "work_dir": "...",
   "preset": ".../caption_preset.json", "end_card": ".../end-card.png", "fonts_dir": ".../fonts",
   "top_crop": 0.0, "eye_line": 0.43,
   "text": {"131": "5.5", "132": ""},         display fixes, "" merges into the previous word
   "key_words": [3, 78],                      source word indices shown in the accent colour
   "in_overrides": {"162": 105.71},           hand-set cut in-point (s) for a range starting at word
   "out_overrides": {"195": 116.45},          hand-set cut out-point (s) for a range ending at word
   "versions": {
     "A_control":       {"ranges": [[0, 8, 1.0], ...], "change": "..."},
     "B_onscreen_hook": {"ranges": "A_control", "change": "...",
                         "hook": {"lines": ["line one", "line two"], "key_words": ["fix"], "seconds": 3.0}},
     "C_cold_open":     {"ranges": [...]},
     "D_tight_cut":     {"ranges": [...], "text": {"195": "cause?"}},
     "E_visual_open":   {"ranges": "A_control", "visual_open": {"start_zoom": 1.2, "ramp_s": 3.0}}
   }
  }
  ranges: [first_word_index, last_word_index, zoom], inclusive. A string names another version
  whose ranges are reused.
  preset, end_card, fonts_dir, top_crop and eye_line are optional: left out, they come from the
  brand folder and the "framing" settings.
  A hand-set cut point always wins: two ranges that sit next to each other in the transcript are
  normally joined without a cut, but not when an in_override or out_override names their seam.
"""
import json
import math
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
from PIL import ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

VERSION_NAMES = ["A_control", "B_onscreen_hook", "C_cold_open", "D_tight_cut", "E_visual_open"]

FPS = 30
SR = 48000
OW, OH = 1080, 1920
DEFAULT_TOP_CROP = 0.0     # share of the raw frame height cut off the top (settings: framing.top_crop)
DEFAULT_EYE_LINE = 0.43    # eye line as a fraction of the raw frame height (settings: framing.eye_line)
E_START_ZOOM = 1.20
E_RAMP_S = 3.0
ENDCARD_S = 2.0
PAUSE_SPLIT = 0.45         # cut pauses longer than this inside a kept run
ON_DB, OFF_DB = -40.0, -45.0
SIL_DB = -43.0             # quieter than this for SIL_MIN seconds inside a kept run = a pause to cut
SIL_MIN = 0.45
SIL_KEEP = 0.10            # leave this much of the pause after a word so sentences still breathe
SIL_KEEP_PRE = 0.15        # ...and this much before the next word (soft h/f/s onsets sit below SIL_DB)
HDR_TRANSFERS = {"arib-std-b67", "smpte2084"}

_SETTINGS = None


def settings():
    """The engine settings, read once (shipped defaults, the user's file, any override file)."""
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = common.load_config()
    return _SETTINGS


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=common.tool_env(settings()))
    if r.returncode != 0:
        print(" ".join(map(str, cmd))[:2000])
        print(r.stderr[-3000:])
        raise SystemExit(1)
    return r


def letter(name):
    return name.split("_")[0]


def resolve_name(arg):
    for n in VERSION_NAMES:
        if arg == n or arg == letter(n):
            return n
    raise SystemExit(f"unknown version {arg!r}; use one of {VERSION_NAMES} or A to E")


# ---------------------------------------------------------------- prepare
def probe(path):
    r = run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", path])
    return json.loads(r.stdout)


def video_info(path):
    d = probe(path)
    v = next((s for s in d["streams"] if s.get("codec_type") == "video"), None)
    if v is None:
        raise SystemExit(f"no video stream in {path}")
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    if "rotate" in (v.get("tags") or {}):
        rot = int(v["tags"]["rotate"])
    w, h = int(v["width"]), int(v["height"])
    if abs(rot) % 180 == 90:
        w, h = h, w
    trc = v.get("color_transfer") or ""
    return dict(path=str(path), width=w, height=h, rotation=rot, transfer=trc,
                primaries=v.get("color_primaries"), matrix=v.get("color_space"),
                hdr=trc in HDR_TRANSFERS, duration=float(d["format"].get("duration", 0)),
                has_audio=any(s.get("codec_type") == "audio" for s in d["streams"]))


def prepare(raws, out_dir, width=1440):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    infos = [video_info(r) for r in raws]
    ow = width
    first = infos[0]
    oh = int(round(first["height"] * ow / first["width"] / 2)) * 2
    inputs, fv, fa = [], [], []
    for k, inf in enumerate(infos):
        inputs += ["-i", inf["path"]]
        if inf["hdr"]:
            tin = inf["transfer"]
            pin = inf["primaries"] or "bt2020"
            mat = inf["matrix"] or "bt2020nc"
            chain = (f"zscale=w={ow}:h={oh}:tin={tin}:pin={pin}:min={mat}:t=linear:npl=100,"
                     f"format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
                     f"zscale=t=bt709:m=bt709:r=tv,format=yuv420p")
        else:
            chain = f"scale={ow}:{oh}:flags=lanczos,format=yuv420p"
        fv.append(f"[{k}:v]{chain},fps={FPS},setsar=1[v{k}]")
        if inf["has_audio"]:
            fa.append(f"[{k}:a]aresample={SR},aformat=sample_fmts=s16:channel_layouts=mono[a{k}]")
        else:
            fa.append(f"anullsrc=r={SR}:cl=mono,atrim=end={inf['duration']:.3f},aformat=sample_fmts=s16[a{k}]")
    n = len(infos)
    fc = ";".join(fv + fa) + ";" + "".join(f"[v{k}][a{k}]" for k in range(n)) + \
        f"concat=n={n}:v=1:a=1[vo][ao]"
    sdr, wav = out_dir / "sdr.mp4", out_dir / "audio.wav"
    run(["ffmpeg", "-y", "-hide_banner", *inputs, "-filter_complex", fc,
         "-map", "[vo]", "-c:v", "libx264", "-profile:v", "high", "-preset", "fast", "-crf", "12",
         "-pix_fmt", "yuv420p", "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
         "-color_range", "tv", str(sdr),
         "-map", "[ao]", "-c:a", "pcm_s16le", "-ar", str(SR), "-ac", "1", str(wav)])
    offs, t = [], 0.0
    for inf in infos:
        offs.append(round(t, 3))
        t += inf["duration"]
    rep = dict(inputs=infos, part_offsets_s=offs, video=str(sdr), audio=str(wav), width=ow, height=oh,
               tone_mapped=any(i["hdr"] for i in infos), duration_s=round(t, 3))
    (out_dir / "prepare.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps(dict(video=str(sdr), audio=str(wav), tone_mapped=rep["tone_mapped"], duration_s=rep["duration_s"])))
    return rep


# ---------------------------------------------------------------- the plan
class Plan:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.base = self.path.parent
        d = json.loads(self.path.read_text())
        self.d = d
        inp = d.get("inputs", {})
        self.video = self.p(inp["video"])
        self.audio = self.p(inp["audio"])
        self.transcript = self.p(inp["transcript"])
        self.out_dir = self.p(d["out_dir"])
        self.work_dir = self.p(d.get("work_dir") or (self.base / "render"))
        look = common.brand(settings()) if not all(d.get(k) for k in ("preset", "fonts_dir", "end_card")) else {}
        self.preset_path = self.p(d.get("preset") or look["preset"])
        self.fonts = self.p(d.get("fonts_dir") or look["fonts_dir"])
        self.end_card = self.p(d.get("end_card") or look["end_card"])
        fr = settings().get("framing") or {}
        tc = d.get("top_crop")
        el = d.get("eye_line")
        self.top_crop = float(fr.get("top_crop", DEFAULT_TOP_CROP) if tc is None else tc)
        self.eye_line = float(fr.get("eye_line", DEFAULT_EYE_LINE) if el is None else el)
        if not 0.0 <= self.top_crop < 0.9:
            raise SystemExit(f"top_crop must be from 0.0 up to 0.9 (got {self.top_crop})")
        self.text = {int(k): v for k, v in (d.get("text") or {}).items()}
        self.key = set(int(x) for x in (d.get("key_words") or []))
        self.in_ovr = {int(k): float(v) for k, v in (d.get("in_overrides") or {}).items()}
        self.out_ovr = {int(k): float(v) for k, v in (d.get("out_overrides") or {}).items()}
        self.versions = d["versions"]
        missing = [n for n in VERSION_NAMES if n not in self.versions]
        extra = [n for n in self.versions if n not in VERSION_NAMES]
        if extra:
            raise SystemExit(f"edit plan has unknown version names {extra}; use {VERSION_NAMES}")
        self.missing = missing

    def p(self, x):
        x = Path(str(x)).expanduser()
        return x if x.is_absolute() else (self.base / x).resolve()

    def ranges(self, name, seen=()):
        v = self.versions[name]
        r = v["ranges"]
        if isinstance(r, str):
            ref = resolve_name(r)
            if ref in seen:
                raise SystemExit(f"ranges loop at {name}")
            return self.ranges(ref, seen + (name,))
        out = []
        for item in r:
            if len(item) == 2:
                wa, wb, z = item[0], item[1], 1.0
            else:
                wa, wb, z = item
            out.append(([int(wa), int(wb)], float(z)))
        return out


class Renderer:
    def __init__(self, plan):
        self.plan = plan
        self.words = json.loads(plan.transcript.read_text())["words"]
        self.NW = len(self.words)
        with wave.open(str(plan.audio)) as w:
            if w.getframerate() != SR or w.getsampwidth() != 2:
                raise SystemExit(f"audio must be 48 kHz 16 bit (run prepare): {plan.audio}")
            a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768.0
            if w.getnchannels() > 1:
                a = a.reshape(-1, w.getnchannels()).mean(1)
        hop = SR // 100
        nf = len(a) // hop
        self.NF = nf
        self.RMS = 20 * np.log10(np.sqrt((a[:nf * hop].reshape(nf, hop) ** 2).mean(1)) + 1e-9)
        tc = plan.top_crop
        keep = 1 - tc
        # the kept window is the bottom KEEP of the height and the centered KEEP of the width
        self.base_crop = f"crop=w='trunc(iw*{keep}/2)*2':h='trunc(ih*{keep}/2)*2':x='(iw-ow)/2':y='ih-oh',"
        self.FY = round((plan.eye_line - tc) / keep, 4)   # eye line inside the cropped frame

    # ---------- acoustic snapping ----------
    def onset(self, i):
        """Measured acoustic onset of word i (seconds)."""
        words, RMS, NF = self.words, self.RMS, self.NF
        s = words[i]["start"]
        lo = s - 0.30
        if i > 0:
            lo = max(lo, words[i - 1]["end"] + 0.03)
        k0, k1 = int(lo * 100), int((s + 0.6) * 100)
        for k in range(max(k0, 0), min(k1, NF - 4)):
            if RMS[k] > ON_DB and (RMS[k + 1:k + 4] > OFF_DB + 2).all():
                return k / 100.0
        return s

    def offset(self, j):
        """Measured acoustic end of word j (seconds)."""
        words, RMS, NF = self.words, self.RMS, self.NF
        e = words[j]["end"]
        hi = e + 0.40
        if j + 1 < self.NW:
            hi = min(hi, self.onset(j + 1) - 0.03)
        k = int((e - 0.05) * 100)
        last = k
        quiet = 0
        while k < int(hi * 100) and k < NF:
            if RMS[k] > OFF_DB:
                last = k
                quiet = 0
            else:
                quiet += 1
                if quiet >= 8:
                    break
            k += 1
        return max(e, (last + 1) / 100.0)

    def plan_segments(self, plan_ranges, in_ovr, out_ovr):
        """Plan ranges -> frame-aligned source pieces [{i,o,z,wa,wb,join}].
        Snaps range edges to measured acoustic onsets/ends and removes interior silences
        (gaps between words AND pauses Whisper absorbed into a long word)."""
        words, RMS, NW = self.words, self.RMS, self.NW
        for (wa, wb), _ in plan_ranges:
            if not (0 <= wa <= wb < NW):
                raise SystemExit(f"range [{wa}, {wb}] is outside the transcript (0 to {NW - 1})")
        ranges = []
        for (wa, wb), z in plan_ranges:
            prev = ranges[-1] if ranges else None
            # continuous speech across two plan ranges: only the zoom changes, no time cut.
            # A hand-set cut point on either side of the seam always wins, so it is a real cut.
            join = (prev is not None and prev["wb"] + 1 == wa
                    and words[wa]["start"] - words[wa - 1]["end"] <= PAUSE_SPLIT
                    and wa not in in_ovr and prev["wb"] not in out_ovr)
            if join:
                t = round(((words[wa - 1]["end"] + words[wa]["start"]) / 2) * FPS) / FPS
                prev["o"] = t
                t_in = t
            elif wa in in_ovr:
                t_in = in_ovr[wa]
            else:
                t_in = math.floor((self.onset(wa) - 0.05) * FPS) / FPS
            t_out_raw = out_ovr[wb] if wb in out_ovr else self.offset(wb) + 0.05
            cap = self.onset(wb + 1) - 0.02 if wb + 1 < NW else 1e9
            t_out = math.ceil(t_out_raw * FPS) / FPS
            if wb in out_ovr:
                t_out = round(out_ovr[wb] * FPS) / FPS
            elif t_out > cap:
                t_out = math.floor(cap * FPS) / FPS
            # a hand-set out-point can land on voiced sound, so give it a longer fade than a silent seam
            if t_out <= t_in:
                raise SystemExit(f"range [{wa}, {wb}] ends at {t_out:.3f} s, not after its start at {t_in:.3f} s; "
                                 f"check its in_overrides and out_overrides")
            ranges.append(dict(wa=wa, wb=wb, z=z, i=t_in, o=t_out, join=join,
                               fo=0.03 if wb in out_ovr else 0.008))
        segs = []
        for r in ranges:
            k0, k1 = int(round(r["i"] * 100)), int(round(r["o"] * 100))
            pieces, cur, k = [], r["i"], k0
            while k < k1:
                if RMS[k] < SIL_DB:
                    j = k
                    while j < k1 and RMS[j] < SIL_DB:
                        j += 1
                    if (j - k) / 100 >= SIL_MIN and k > k0 + 5 and j < k1 - 5:
                        a_ = math.ceil((k / 100 + SIL_KEEP) * FPS) / FPS
                        b_ = math.floor((j / 100 - SIL_KEEP_PRE) * FPS) / FPS
                        if b_ - a_ >= 0.1:
                            pieces.append((cur, a_))
                            cur = b_
                    k = j
                else:
                    k += 1
            pieces.append((cur, r["o"]))
            for m, (pi, po) in enumerate(pieces):
                segs.append(dict(i=pi, o=po, z=r["z"], wa=r["wa"], wb=r["wb"],
                                 join=r["join"] if m == 0 else False, first=(m == 0),
                                 fo=r["fo"] if m == len(pieces) - 1 else 0.008))
        return segs

    # ---------- caption words in output time ----------
    def display_words(self, segs, text):
        words, KEY = self.words, self.plan.key
        groups, off = [], 0.0
        for s in segs:
            if s["first"]:
                groups.append(dict(wa=s["wa"], wb=s["wb"], join=s["join"], p=[]))
            groups[-1]["p"].append((s["i"], s["o"], off))
            off += s["o"] - s["i"]

        def fwd(t, P):   # time that falls in a removed pause snaps to the next kept piece
            for i_, o_, f_ in P:
                if t < i_:
                    return f_
                if t <= o_:
                    return f_ + t - i_
            i_, o_, f_ = P[-1]
            return f_ + o_ - i_

        def back(t, P):  # ...or back to the end of the previous piece
            last = P[0][2]
            for i_, o_, f_ in P:
                if t < i_:
                    return last
                if t <= o_:
                    return f_ + t - i_
                last = f_ + o_ - i_
            return last

        out = []
        for g in groups:
            P = g["p"]
            for i in range(g["wa"], g["wb"] + 1):
                t = text.get(i, words[i]["word"].strip())
                if t == "":
                    if out:
                        out[-1]["end"] = max(out[-1]["end"], back(words[i]["end"], P))
                    continue
                st = fwd(max(words[i]["start"], P[0][0]), P)
                en = back(min(words[i]["end"], P[-1][1]), P)
                out.append(dict(src=i, text=t, start=round(st, 3), end=round(max(en, st + 0.05), 3),
                                key=i in KEY, cut=(i == g["wa"] and not g["join"])))
        return out, off

    # ---------- render one version ----------
    def render(self, name, preset, plan_only=False):
        plan = self.plan
        v = plan.versions[name]
        text = {**plan.text, **{int(k): t for k, t in (v.get("text") or {}).items()}}
        in_ovr = {**plan.in_ovr, **{int(k): float(t) for k, t in (v.get("in_overrides") or {}).items()}}
        out_ovr = {**plan.out_ovr, **{int(k): float(t) for k, t in (v.get("out_overrides") or {}).items()}}
        segs = self.plan_segments(plan.ranges(name), in_ovr, out_ovr)
        dw, body = self.display_words(segs, text)
        vdir = plan.work_dir / "versions" / letter(name)
        vdir.mkdir(parents=True, exist_ok=True)
        hook = v.get("hook")
        ass, cues, hook_layout = build_ass(dw, body, preset, plan.fonts, hook=hook)
        (vdir / "captions.ass").write_text(ass)
        hook_p = vdir / "hook.json"
        if hook_layout:
            hook_p.write_text(json.dumps(hook_layout, indent=1))   # where each hook line sits, for the gate
        elif hook_p.exists():
            hook_p.unlink()
        (vdir / "segments.json").write_text(json.dumps(segs, indent=1))
        (vdir / "cut_words.txt").write_text(
            "\n".join((" ".join(w["text"] for w in dw if s["wa"] <= w["src"] <= s["wb"]) if s["first"] else "   ...")
                      + f"   [{s['i']:.3f}-{s['o']:.3f} z{s['z']}{' join' if s['join'] else ''}]" for s in segs))
        (vdir / "cues.txt").write_text("\n".join(f"{c[0]['start']:7.2f} " + " ".join(
            ('*' if w['key'] else '') + strip_disp(w['text']) for w in c) for c in cues))
        print(f"{name}: {len(segs)} segments, body {body:.2f}s, total {body + ENDCARD_S:.2f}s")
        if plan_only:
            return segs, body
        vo = v.get("visual_open")
        ramp = bool(vo)
        start_zoom = float((vo or {}).get("start_zoom", E_START_ZOOM))
        ramp_s = float((vo or {}).get("ramp_s", E_RAMP_S))
        inputs, fv, fa = [], [], []
        off = 0.0
        n = len(segs)
        src_v, src_a = str(plan.video), str(plan.audio)
        base_crop, FY = self.base_crop, self.FY
        for k, s in enumerate(segs):
            dur = s["o"] - s["i"]
            nfr = int(round(dur * FPS))
            ns = nfr * (SR // FPS)
            inputs += ["-ss", f"{s['i']:.4f}", "-t", f"{dur + 0.2:.4f}", "-i", src_v]
            inputs += ["-ss", f"{s['i']:.4f}", "-t", f"{dur + 0.2:.4f}", "-i", src_a]
            vi, ai = 2 * k, 2 * k + 1
            z = s["z"]
            if ramp and off < ramp_s:
                # ease-out from start_zoom to the segment zoom over the first ramp_s of the output
                u = f"min(1\\,({off:.4f}+it)/{ramp_s})"
                zexpr = f"{z}+({start_zoom}-{z})*pow(1-{u}\\,3)"
                vf = (f"[{vi}:v]setpts=PTS-STARTPTS,trim=end_frame={nfr},{base_crop}scale=2880:5120:flags=bicubic,"
                      f"zoompan=z='{zexpr}':x='(iw-iw/zoom)/2':y='{FY}*(ih-ih/zoom)':d=1:s={OW}x{OH}:fps={FPS},"
                      f"setsar=1,format=yuv420p[v{k}]")
            else:
                vf = (f"[{vi}:v]setpts=PTS-STARTPTS,trim=end_frame={nfr},{base_crop}"
                      f"crop=w='trunc(iw/{z}/2)*2':h='trunc(ih/{z}/2)*2':x='(iw-ow)/2':y='{FY}*(ih-oh)',"
                      f"scale={OW}:{OH}:flags=lanczos,setsar=1,fps={FPS},format=yuv420p[v{k}]")
            fade_in = "" if s["join"] else ",afade=t=in:st=0:d=0.008"
            nxt_join = k + 1 < n and segs[k + 1]["join"]
            fo = s.get("fo", 0.008)
            fade_out = "" if nxt_join else f",afade=t=out:st={dur - fo:.4f}:d={fo}"
            af = (f"[{ai}:a]aresample={SR},aformat=sample_fmts=fltp:channel_layouts=mono,"
                  f"atrim=end_sample={ns},asetpts=PTS-STARTPTS{fade_in}{fade_out}[a{k}]")
            fv.append(vf)
            fa.append(af)
            off += dur
        ass_path = str(vdir / "captions.ass").replace(":", r"\:")
        fdir = str(plan.fonts).replace(":", r"\:")
        fc = ";".join(fv + fa) + ";" + "".join(f"[v{k}][a{k}]" for k in range(n)) + \
            f"concat=n={n}:v=1:a=1[vc][ac];[vc]ass=filename='{ass_path}':fontsdir='{fdir}'[vo]"
        (vdir / "filter.txt").write_text(fc)
        body_mov = vdir / "body.mov"
        run(["ffmpeg", "-y", "-hide_banner", *inputs, "-/filter_complex", str(vdir / "filter.txt"),
             "-map", "[vo]", "-map", "[ac]", "-c:v", "libx264", "-preset", "fast", "-crf", "12",
             "-pix_fmt", "yuv420p", "-c:a", "pcm_s16le", str(body_mov)])
        # ---- loudness: measure pass ----
        chain = "highpass=f=80,acompressor=threshold=-24dB:ratio=2.5:attack=8:release=120:makeup=2"
        r = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(body_mov), "-vn", "-af",
                 f"{chain},ebur128", "-f", "null", "-"])
        summ = r.stderr[r.stderr.rfind("Summary:"):]
        I_meas = float(next(l for l in summ.splitlines() if l.strip().startswith("I:")).split()[1])
        gain = -14.0 - (I_meas + 3.01)  # dual-mono stereo delivery reads +3.01 LU over the mono measurement
        ln = f"volume={gain:.2f}dB,alimiter=limit=0.84:attack=3:release=60:level=false"
        print(f"  loudness {I_meas} LUFS -> gain {gain:+.2f} dB")
        # ---- final: body + end card, audio processed + silent tail ----
        plan.out_dir.mkdir(parents=True, exist_ok=True)
        final = plan.out_dir / f"{name}.mp4"
        fc2 = (f"[1:v]scale={OW}:{OH}:flags=lanczos,setsar=1,fps={FPS},format=yuv420p,"
               f"trim=end_frame={int(ENDCARD_S * FPS)},fade=t=in:st=0:d=0.2[ec];"
               f"[0:v]setsar=1[b];[b][ec]concat=n=2:v=1:a=0,format=yuv420p,"
               f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv[v];"
               f"[0:a]{chain},{ln},aresample={SR},apad=pad_dur={ENDCARD_S},"
               f"atrim=end={body + ENDCARD_S:.4f},pan=stereo|c0=c0|c1=c0[a]")
        run(["ffmpeg", "-y", "-hide_banner", "-i", str(body_mov),
             "-loop", "1", "-framerate", str(FPS), "-t", f"{ENDCARD_S + 0.5}", "-i", str(plan.end_card),
             "-filter_complex", fc2, "-map", "[v]", "-map", "[a]",
             "-c:v", "libx264", "-profile:v", "high", "-preset", "slow", "-crf", "18", "-pix_fmt", "yuv420p",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
             "-r", str(FPS), "-c:a", "aac", "-b:a", "192k", "-ar", str(SR), "-ac", "2",
             "-movflags", "+faststart", str(final)])
        print(f"  -> {final}")
        return segs, body


def strip_disp(t):
    return t.rstrip(".,")


def make_cues(dw, preset, font=None, space=0.0):
    L = preset["layout"]
    maxpx = OW * L["max_line_width_pct"] / 100

    def too_wide(ws):
        if font is None:
            return False
        ts_ = [strip_disp(x["text"]) for x in ws]
        return sum(font.getlength(t) for t in ts_) + space * (len(ts_) - 1) > maxpx
    maxc, maxw, maxw2 = L["max_chars_per_line"], L["max_words_per_screen"], L["max_words_after_merge"]
    gap_split = preset["timing"]["pause_split_s"]
    cues, cur = [], []
    for k, w in enumerate(dw):
        txt = " ".join(strip_disp(x["text"]) for x in cur + [w])
        if cur and (w["cut"] or len(cur) + 1 > maxw or len(txt) > maxc or too_wide(cur + [w])
                    or w["start"] - cur[-1]["end"] > gap_split):
            cues.append(cur)
            cur = []
        cur.append(w)
        if w["text"].endswith((".", "?", "!")) or (w["text"].endswith(",") and len(cur) >= 2):
            cues.append(cur)
            cur = []
    if cur:
        cues.append(cur)
    # fold lone words into a neighbour (prefer previous) when it stays readable
    merged = []
    for c in cues:
        if len(c) == 1 and merged:
            p = merged[-1]
            txt = " ".join(strip_disp(x["text"]) for x in p + c)
            if len(p) + 1 <= maxw2 and len(txt) <= maxc + 4 and c[0]["start"] - p[-1]["end"] <= gap_split \
                    and not p[-1]["text"].endswith((".", "?", "!")) and not c[0]["cut"] and not too_wide(p + c):
                merged[-1] = p + c
                continue
        merged.append(c)
    out = []
    for k, c in enumerate(merged):
        if len(c) == 1 and k + 1 < len(merged):
            n = merged[k + 1]
            txt = " ".join(strip_disp(x["text"]) for x in c + n)
            if len(n) + 1 <= maxw2 and len(txt) <= maxc + 4 and n[0]["start"] - c[0]["end"] <= gap_split \
                    and not c[0]["text"].endswith((".", "?", "!")) and not n[0]["cut"] and not too_wide(c + n):
                merged[k + 1] = c + n
                continue
        out.append(c)
    return out


def ass_color(hexrgb):
    h = hexrgb.lstrip("#")
    return f"&H{h[4:6]}{h[2:4]}{h[0:2]}&".upper()


def ts(t):
    t = max(0.0, t)
    cs = int(round(t * 100))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


def build_ass(dw, body_dur, preset, fonts_dir, hook=None):
    sc = OH / preset["video"]["height"]
    FS = int(round(preset["font_size_px"] * sc))
    sh = preset["shadow"]
    bord = round(sh["stroke_px"] * sc, 1)
    shad = round(sh["offset_y"] * sc, 1)
    outline = ass_color(sh["stroke_color"])
    shc = ass_color(sh["color"])
    accent = ass_color(preset["colors"]["key"])
    white = ass_color(preset["colors"]["speaker"])
    L = preset["layout"]
    Y = int(round(OH * L["y_percent_from_top"] / 100))
    CX = OW * L["x_percent_from_left"] / 100
    maxw_px = OW * L["max_line_width_pct"] / 100
    an = preset["animation"]
    # libass sizes a font so (winAscent + winDescent) == Fontsize, not the em; measure at the same scale
    from fontTools.ttLib import TTFont
    font_path = Path(fonts_dir) / preset["font_file"]
    if not font_path.exists():
        raise SystemExit(f"caption font missing: {font_path} (a fallback font would fail the gate)")
    _tt = TTFont(str(font_path))
    EM = _tt["head"].unitsPerEm / (_tt["OS/2"].usWinAscent + _tt["OS/2"].usWinDescent)
    font = ImageFont.truetype(str(font_path), int(round(FS * EM)))
    space = font.getlength(" ")
    fam = preset["weights"]["base"]
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OW}
PlayResY: {OH}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,{fam},{FS},&H00FFFFFF,&H00FFFFFF,&H00{outline[2:-1]},&H{sh['alpha']:02X}{shc[2:-1]},0,0,0,0,100,100,0,0,1,{bord},{shad},5,0,0,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ev = []
    cues = make_cues(dw, preset, font, space)
    for k, c in enumerate(cues):
        st = c[0]["start"]
        en = cues[k + 1][0]["start"] if k + 1 < len(cues) else body_dur
        if k + 1 < len(cues) and en - c[-1]["end"] > 0.8:
            en = c[-1]["end"] + 0.5
        en = min(en, body_dur)
        toks = [strip_disp(w["text"]) for w in c]
        widths = [font.getlength(t) for t in toks]
        total = sum(widths) + space * (len(toks) - 1)
        if total > maxw_px:
            print(f"  ! cue too wide ({total:.0f}px): {' '.join(toks)}")
        x = CX - total / 2
        for w, t, wd in zip(c, toks, widths):
            cx = x + wd / 2
            x += wd + space
            ws = max(st, w["start"])
            col = accent if w["key"] else white
            pop = an["pop_from_pct"]
            tag = (f"{{\\an5\\pos({cx:.1f},{Y})\\1c{col}\\blur{sh['blur']}"
                   f"\\fscx{pop}\\fscy{pop}\\t(0,{an['pop_ms']},\\fscx100\\fscy100)"
                   f"\\alpha&H80&\\t(0,{an['fade_ms']},\\alpha&H00&)}}")
            ev.append(f"Dialogue: 1,{ts(ws)},{ts(en)},Pop,,0,0,0,,{tag}{t}")
    hook_layout = None
    if hook:
        # there is rarely clear room above the head, so the hook sits below the chin, above the
        # captions, in (usually) two lines
        lines = hook.get("lines") or []
        hkey = set(hook.get("key_words") or [])
        secs = float(hook.get("seconds", 3.0))
        y0 = float(hook.get("y", 0.614))
        gap = float(hook.get("line_gap", 0.042))
        HFS = int(round(FS * 0.74))
        hfont = ImageFont.truetype(str(font_path), int(round(HFS * EM)))
        hook_layout = dict(seconds=secs, font_px=HFS, frame=[OW, OH],
                           lines=[dict(text=line, y=int(round(OH * (y0 + gap * n)))) for n, line in enumerate(lines)])
        for n, line in enumerate(lines):
            yy = int(round(OH * (y0 + gap * n)))
            parts = []
            for t in line.split(" "):
                col = accent if t in hkey else white
                parts.append(f"{{\\1c{col}}}{t}")
            wpx = hfont.getlength(line)
            tag = (f"{{\\an5\\pos({OW/2:.0f},{yy})\\fs{HFS}\\bord{bord}\\shad{shad}\\blur{sh['blur']}"
                   f"\\fscx112\\fscy112\\t(0,140,\\fscx100\\fscy100)\\fad(0,200)}}")
            ev.append(f"Dialogue: 2,{ts(0)},{ts(secs)},Pop,,0,0,0,,{tag}{' '.join(parts)}")
            if wpx > OW * 0.86:
                print(f"  ! hook line too wide {wpx:.0f}px")
    return hdr + "\n".join(ev) + "\n", cues, hook_layout


# ---------------------------------------------------------------- commands
def cmd_render(argv):
    args = [x for x in argv if not x.startswith("--")]
    plan_only = "--plan-only" in argv
    if not args:
        raise SystemExit("usage: render_reel.py render PLAN [A B ...] [--plan-only]")
    plan = Plan(args[0])
    names = [resolve_name(a) for a in args[1:]] or [n for n in VERSION_NAMES if n in plan.versions]
    preset = json.loads(plan.preset_path.read_text())
    rd = Renderer(plan)
    meta_p = plan.work_dir / "versions" / "meta.json"
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    for nm in names:
        segs, body = rd.render(nm, preset, plan_only)
        ranges = []
        for s in segs:
            if s["join"] and ranges:
                ranges[-1][1] = round(s["o"], 3)
            else:
                ranges.append([round(s["i"], 3), round(s["o"], 3)])
        meta[nm] = dict(body=round(body, 3), ranges=ranges)
        if not plan_only:
            meta_p.parent.mkdir(parents=True, exist_ok=True)
            meta_p.write_text(json.dumps(meta, indent=1))
    if plan_only:
        print(json.dumps({k: v["body"] for k, v in meta.items()}))


def cmd_variants(argv):
    args = [x for x in argv if not x.startswith("--")]
    plan = Plan(args[0])
    source = None
    if "--source" in argv:
        source = argv[argv.index("--source") + 1]
    meta = json.loads((plan.work_dir / "versions" / "meta.json").read_text())
    out = {"source": source or str(plan.video), "end_card": f"{plan.end_card} ({ENDCARD_S}s, silent, 0.2s fade in)",
           "caption_preset": str(plan.preset_path), "post_caption": "caption.txt",
           "top_crop": plan.top_crop, "versions": []}
    for nm in VERSION_NAMES:
        if nm not in plan.versions:
            continue
        f = plan.out_dir / f"{nm}.mp4"
        dur = None
        if f.exists():
            r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(f)])
            dur = round(float(r.stdout.strip()), 2)
        v = plan.versions[nm]
        hook = v.get("hook")
        out["versions"].append({
            "id": letter(nm), "name": nm, "file": f.name, "length_s": dur,
            "length_before_end_card_s": meta.get(nm, {}).get("body"),
            "change_vs_A": "None (this is the control)." if nm == "A_control" else v.get("change", ""),
            "onscreen_hook_text": " ".join(hook["lines"]) if hook else None,
            "onscreen_hook_lines": list(hook["lines"]) if hook else None,
            "kept_ranges_from_raw_s": meta.get(nm, {}).get("ranges"),
        })
    (plan.out_dir / "variants.json").write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps([(v["id"], v["length_s"]) for v in out["versions"]]))


def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "prepare":
        width = 1440
        if "--width" in rest:
            i = rest.index("--width")
            width = int(rest[i + 1])
            del rest[i:i + 2]
        if "--out-dir" not in rest:
            raise SystemExit("usage: render_reel.py prepare --out-dir DIR RAW [RAW ...]")
        i = rest.index("--out-dir")
        out = rest[i + 1]
        del rest[i:i + 2]
        if not rest:
            raise SystemExit("prepare needs at least one raw file")
        prepare(rest, out, width)
    elif cmd == "render":
        cmd_render(rest)
    elif cmd == "variants":
        cmd_variants(rest)
    else:
        raise SystemExit(f"unknown command {cmd!r}; use prepare, render or variants")


if __name__ == "__main__":
    main(sys.argv[1:])
