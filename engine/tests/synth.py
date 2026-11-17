"""A small synthetic recording for the render tests: invented words, a tone burst per word, a flat
grey picture. Nothing here is a real video; it only has to behave like one for the renderer
(words with clear starts and ends, pauses between sentences) and for the quality check."""
import array
import json
import math
import subprocess
import wave
from pathlib import Path

SR = 48000
SENTENCES = [
    "Simple plans always beat clever plans.",
    "Write your first draft before lunch.",
    "Read every single line aloud once.",
    "Then remove whatever sounds wrong.",
]
HOOK_LINES = ["Simple plans", "always win"]
WORD_S, GAP_S, PAUSE_S, LEAD_S = 0.30, 0.06, 0.60, 0.30
VERSIONS = ["A_control", "B_onscreen_hook", "C_cold_open", "D_tight_cut", "E_visual_open"]


def words_and_sentences():
    """([{"word", "start", "end"}], [[first_index, last_index], ...])"""
    words, sents, t = [], [], LEAD_S
    for s in SENTENCES:
        first = len(words)
        for w in s.split():
            words.append(dict(word=w, start=round(t, 3), end=round(t + WORD_S, 3), prob=0.99))
            t += WORD_S + GAP_S
        sents.append([first, len(words) - 1])
        t += PAUSE_S - GAP_S
    return words, sents


def write_audio(path, words, tail_s=0.8):
    """48 kHz mono 16 bit: one voiced-sounding burst per word, silence everywhere else."""
    n = int((words[-1]["end"] + tail_s) * SR)
    buf = array.array("h", bytes(2 * n))
    edge = int(0.012 * SR)
    for k, w in enumerate(words):
        a, b = int(w["start"] * SR), int(w["end"] * SR)
        f0 = 150 + 12 * (k % 5)
        for i in range(a, b):
            x = (i - a) / SR
            env = min(1.0, (i - a) / edge, (b - 1 - i) / edge)
            v = 0.30 * math.sin(2 * math.pi * f0 * x) + 0.15 * math.sin(2 * math.pi * 2 * f0 * x) \
                + 0.08 * math.sin(2 * math.pi * 3 * f0 * x)
            buf[i] = int(32767 * 0.6 * env * v)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(buf.tobytes())
    return n / SR


def write_source(folder, env=None, video=True):
    """Writes audio.wav, transcript.json and (with video=True, needs ffmpeg) sdr.mp4 into folder.
    Returns dict(words, sentences, duration)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    words, sents = words_and_sentences()
    dur = write_audio(folder / "audio.wav", words)
    (folder / "transcript.json").write_text(json.dumps(dict(text=" ".join(SENTENCES), words=words)))
    if video:
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=0x6b7280:size=540x960:rate=30:duration={dur:.3f},"
                        "drawbox=x=150:y=230:w=240:h=300:color=0x8b5e3c:t=fill,"      # a head-sized block, so a
                        "drawbox=x=90:y=560:w=360:h=400:color=0x3f6b5a:t=fill",       # punch-in changes the picture
                        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(folder / "sdr.mp4")],
                       check=True, env=env)
    return dict(words=words, sentences=sents, duration=dur)


def five_version_plan(folder, out_dir, look, sents):
    """A plan with all five versions: A is every sentence, C moves the last one to the front,
    D keeps every other sentence, B adds a two line hook, E opens on a punch-in."""
    folder = Path(folder)
    a = [[s[0], s[1], 1.1 if k == 2 else 1.0] for k, s in enumerate(sents)]
    return dict(
        inputs=dict(video=str(folder / "sdr.mp4"), audio=str(folder / "audio.wav"),
                    transcript=str(folder / "transcript.json")),
        out_dir=str(out_dir), work_dir=str(folder / "render"),
        preset=str(look["preset"]), end_card=str(look["end_card"]), fonts_dir=str(look["fonts_dir"]),
        top_crop=0.0, eye_line=0.43, text={}, key_words=[sents[0][0] + 1],
        versions={
            "A_control": dict(ranges=a, change="Control: the story as told."),
            "B_onscreen_hook": dict(ranges="A_control", change="Same cut as A plus an on-screen hook for 3 seconds.",
                                    hook=dict(lines=list(HOOK_LINES), key_words=["win"], seconds=3.0)),
            "C_cold_open": dict(ranges=[a[-1]] + a[:-1], change="Same cut as A with the last line moved to the front."),
            "D_tight_cut": dict(ranges=a[::2], change="Every other sentence of A, faster and shorter."),
            "E_visual_open": dict(ranges="A_control", change="Same cut as A, opens on a 1.2x punch-in easing out.",
                                  visual_open=dict(start_zoom=1.2, ramp_s=3.0)),
        })
