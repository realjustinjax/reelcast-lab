"""Picking the winner (coming later, with posting): score = views 40, comments 20, shares plus
saves 20, likes 20, each measured against the best of the 5. A tie goes to the better 3 second
hold. Low confidence when the top two are within 10 percent or any version has under 200 views."""

WEIGHTS = [("views", 40), ("comments", 20), ("shares_saves", 20), ("likes", 20)]
ORDER = ["A_control", "B_onscreen_hook", "C_cold_open", "D_tight_cut", "E_visual_open"]


def _val(m, key):
    if key == "shares_saves":
        return (m.get("shares") or 0) + (m.get("saved") or 0)
    return m.get(key) or 0


def bests(metrics):
    return {k: max((_val(m, k) for m in metrics.values()), default=0) for k, _ in WEIGHTS}


def scores(metrics, skip=()):
    """metrics: {version: {views, comments, shares, saved, likes, ...}} -> {version: score 0..100}"""
    best = bests(metrics)
    out = {}
    for v, m in metrics.items():
        s = 0.0
        for k, w in WEIGHTS:
            if k in skip:
                continue
            if best[k] > 0:
                s += w * _val(m, k) / best[k]
        out[v] = round(s, 4)
    return out


def decide(metrics, min_views=200, close_pct=10.0, tiny=5):
    sc = scores(metrics)

    def key(v):
        hold = metrics[v].get("hold_3s")
        return (-sc[v], -(hold if hold is not None else -1), ORDER.index(v) if v in ORDER else 99)
    ranked = sorted(sc, key=key)
    winner = ranked[0]
    reasons = []
    tie = len(ranked) > 1 and abs(sc[ranked[0]] - sc[ranked[1]]) < 1e-9
    if tie:
        reasons.append(f"tie on score with {ranked[1]}, decided by the 3 second hold")
    if len(ranked) > 1 and sc[ranked[0]] > 0:
        gap = (sc[ranked[0]] - sc[ranked[1]]) / sc[ranked[0]] * 100
        if gap < close_pct:
            reasons.append(f"top two within {gap:.1f} percent")
    low_views = [v for v, m in metrics.items() if (m.get("views") or 0) < min_views]
    if low_views:
        reasons.append(f"under {min_views} views: {', '.join(low_views)}")
    # a lead that rests on a metric whose best value is tiny (one comment) is not a real lead
    small = [k for k, b in bests(metrics).items() if 0 < b < tiny]
    if small and len(ranked) > 1:
        sc2 = scores(metrics, skip=small)
        if sc2[winner] <= max(s for v, s in sc2.items() if v != winner):
            reasons.append("the lead rests on " + ", ".join(f"{k} (best value {bests(metrics)[k]})" for k in small))
    confidence = "low" if any(not r.startswith("tie") for r in reasons) else "normal"
    return dict(winner=winner, scores=sc, ranked=ranked, confidence=confidence, reasons=reasons, tie=tie)
