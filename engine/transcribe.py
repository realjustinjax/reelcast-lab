"""Transcribe step: make the SDR working copy and wav (render_reel.py prepare), then offline
Whisper (the "transcribe_script" setting, with the "whisper_model" setting). Runs as a
background job; the engine puts ffmpeg on its PATH.

Afterwards the glossary in the settings is applied as DISPLAY fixes only (timing untouched):
work/transcript_fixes.json holds {"text": {index: display}, "flags": [...]} in the same shape
edit_plan.json uses, and work/transcript.txt lists every word with its index for the editor.
"""
import json
import re
import shlex

from common import CODE_DIR

PUNCT = ".,?!;:"


def command(cfg, run):
    custom = cfg.get("transcribe", {}).get("command")
    work = run.work
    if custom:
        return [cfg.fill(c, work=work, run_dir=run.dir) for c in custom]
    py = cfg["sandbox_python"]
    raws = " ".join(shlex.quote(p) for p in run.d["raw_paths"])
    script = (
        "set -e\n"
        f"{shlex.quote(py)} {shlex.quote(str(CODE_DIR / 'render_reel.py'))} prepare --out-dir {shlex.quote(str(work))} {raws}\n"
        f"{shlex.quote(py)} {shlex.quote(cfg['transcribe_script'])} {shlex.quote(str(work / 'audio.wav'))} "
        f"--out {shlex.quote(str(work / 'transcript.json'))} --model {shlex.quote(cfg['whisper_model'])}\n"
    )
    sh = work / "transcribe.sh"
    sh.write_text(script)
    return ["/bin/sh", str(sh)]


def norm(t):
    return re.sub(r"[^a-z0-9]", "", t.lower())


def apply_glossary(words, glossary):
    """Returns (text_overrides, flags). Never changes timing."""
    toks = [w["word"].strip() for w in words]
    nt = [norm(t) for t in toks]
    text, flags = {}, []
    n = len(toks)
    for entry in glossary:
        targets = {norm(h) for h in entry["heard"]}
        disp = entry["display"].split()
        for i in range(n):
            if not nt[i] or i in text:
                continue
            for target in targets:
                acc, j = "", i
                while j < n and j < i + 6:
                    acc += nt[j]
                    if acc == target or not target.startswith(acc):
                        break
                    j += 1
                if acc != target:
                    continue
                span = list(range(i, j + 1))
                action = entry.get("action", "replace")
                if action == "replace" and (entry.get("when_prev") or entry.get("when_next")):
                    prev = nt[i - 1] if i > 0 else ""
                    nxt = nt[j + 1] if j + 1 < n else ""
                    ok = prev in entry.get("when_prev", []) or nxt in entry.get("when_next", [])
                    if not ok:
                        action = "flag"
                if action == "flag":
                    flags.append(dict(index=i, last=j, heard=" ".join(toks[i:j + 1]),
                                      maybe=entry["display"], note=entry.get("note", "")))
                    break
                new = []
                for k in range(len(span)):
                    if k < len(disp) - 1:
                        new.append(disp[k])
                    elif k == len(disp) - 1:
                        new.append(" ".join(disp[k:]) if len(span) == len(disp) else disp[k])
                    else:
                        new.append("")
                if len(span) < len(disp):
                    new[-1] = " ".join(disp[len(span) - 1:])
                tail = re.search(r"[" + re.escape(PUNCT) + r"]+$", toks[j])
                if tail:
                    last = max(k for k, v in enumerate(new) if v)
                    new[last] += tail.group(0)
                if [x for x in new if x] == [t for t in toks[i:j + 1]] and len(span) == len(disp):
                    break   # already right
                for k, idx in enumerate(span):
                    text[idx] = new[k]
                break
    return text, flags


def postprocess(cfg, run):
    work = run.work
    words = json.loads((work / "transcript.json").read_text())["words"]
    text, flags = apply_glossary(words, cfg.get("glossary", []))
    fixes = dict(text={str(k): v for k, v in sorted(text.items())}, flags=flags,
                 _what="Glossary display fixes (timing untouched). Copy 'text' into edit_plan.json "
                       "and decide every flag yourself.")
    (work / "transcript_fixes.json").write_text(json.dumps(fixes, indent=1))
    lines = []
    for i, w in enumerate(words):
        shown = text.get(i)
        extra = f"   -> {shown!r}" if shown is not None else ""
        lines.append(f"{i}\t{w['start']:.2f}\t{w['end']:.2f}\t{w['word'].strip()}{extra}")
    (work / "transcript.txt").write_text("\n".join(lines) + "\n")
    return len(words), len(text), len(flags)
