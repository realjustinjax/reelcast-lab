#!/usr/bin/env python3
"""Test stub for the transcribe job: writes a small transcript.json (and, with --text FILE, uses
that text). usage: stub_transcribe.py RUN_DIR [--text FILE] [--fail]"""
import json, sys
from pathlib import Path
run = Path(sys.argv[1])
if "--fail" in sys.argv:
    sys.exit(3)
text = ("Simple plans beat clever plans. Hear me out. I asked a friend for a short list. It came back with three options "
        "and none of them were right. So I stepped back, slowed down, and wrote a better plan. "
        "Then my friend agreed the plain plan beat the clever ones. Start simple first.")
if "--text" in sys.argv:
    text = Path(sys.argv[sys.argv.index("--text") + 1]).read_text()
words, t = [], 0.0
for w in text.split():
    words.append(dict(word=w, start=round(t, 2), end=round(t + 0.3, 2), prob=0.9))
    t += 0.35
(run / "work").mkdir(parents=True, exist_ok=True)
(run / "work" / "transcript.json").write_text(json.dumps(dict(text=text, words=words)))
