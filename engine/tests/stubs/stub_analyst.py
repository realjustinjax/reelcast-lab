#!/usr/bin/env python3
"""Stub analyst: writes a valid analysis.json (or an invalid one first with --bad-first).
usage: stub_analyst.py RUN_DIR [--bad-first] [--not-evergreen]"""
import json, sys
from pathlib import Path
run_dir = Path(sys.argv[1])
run = json.loads((run_dir / "run.json").read_text())
ed = Path(run["edited_dir"])
flag = run_dir / "stub_analyst_called"
n = int(flag.read_text()) + 1 if flag.exists() else 1
flag.write_text(str(n))
if ("--bad-first" in sys.argv and n == 1) or "--bad-always" in sys.argv:
    (ed / "analysis.json").write_text(json.dumps({"summary": "half done"}))
    sys.exit(0)
w = run.get("winner", {}).get("winner", "A_control")
a = {"summary": "Stub analysis. The winner kept people past the opening.",
     "why_stayed_past_3s": "The first line is a clear claim.",
     "why_stayed_or_left": "People left during the setup.",
     "claims": [{"version": w[0], "change": "its one change", "claim": "It held the most viewers.", "label": "hypothesis"}],
     "learning": {"tested": "five openings", "winner": w[0], "margin": "small", "lesson": "Lead with the payoff.", "limits": "one video"},
     "evergreen": "--not-evergreen" not in sys.argv, "evergreen_until": None,
     "repost_captions": ["Caption one #AI", "Caption two #AI", "Caption three #AI", "Caption four #AI"],
     "ad_verdict": {"verdict": "yes", "reason": "stub says yes"}}
(ed / "analysis.json").write_text(json.dumps(a, indent=1))
rep = ed / "REPORT.md"
rep.write_text("## Results\n\nStub results.\n\n" + (rep.read_text() if rep.exists() else ""))
(run_dir / "work" / "ANALYSIS_DONE").write_text("ok\n")
