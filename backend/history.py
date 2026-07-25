from __future__ import annotations


import json
import os
import tempfile
import threading
import time

from vconstants import map_name_from_path

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
_PATH = os.path.join(_DATA_DIR, "rr_history.json")

_LOCK = threading.Lock()
_MAX_POINTS = 2000
_REFRESH_TTL = 600.0
_last_refresh = 0.0

def _load() -> dict:
    try:
        with open(_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("points"), list):
            return d
    except Exception:
        pass
    return {"points": []}

def _save() -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=_DATA_DIR, prefix=".rrhist-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(_HIST, fh, separators=(",", ":"))
            os.replace(tmp, _PATH)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    except Exception:
        pass

_HIST = _load()
_IDS = {p.get("matchId") for p in _HIST["points"] if p.get("matchId")}

def record(point: dict) -> None:
    with _LOCK:
        if point.get("matchId") in _IDS:
            return
        _IDS.add(point.get("matchId"))
        _HIST["points"].append(point)
        _HIST["points"].sort(key=lambda p: p.get("ts") or 0)
        _HIST["points"] = _HIST["points"][-_MAX_POINTS:]
        _save()

def refresh(auth) -> None:
    global _last_refresh
    now = time.time()
    if now - _last_refresh < _REFRESH_TTL:
        return
    _last_refresh = now
    try:
        auth.headers()
        cu = auth.pd_get(
            f"/mmr/v1/players/{auth.puuid}/competitiveupdates"
            f"?startIndex=0&endIndex=20&queue=competitive")
        added = False
        for m in (cu or {}).get("Matches", []) or []:
            mid = m.get("MatchID")
            if not mid or mid in _IDS:
                continue
            delta = m.get("RankedRatingEarned")
            ts = int((m.get("MatchStartTime") or 0) / 1000) or None
            if ts is None:
                continue
            result = None
            if isinstance(delta, (int, float)) and delta != 0:
                result = "Victory" if delta > 0 else "Defeat"
            with _LOCK:
                _IDS.add(mid)
                _HIST["points"].append({
                    "matchId": mid,
                    "ts": ts,
                    "map": map_name_from_path(m.get("MapID") or ""),
                    "result": result,
                    "delta": delta,
                    "tier": m.get("TierAfterUpdate"),
                    "rr": m.get("RankedRatingAfterUpdate"),
                })
                added = True
        if added:
            with _LOCK:
                _HIST["points"].sort(key=lambda p: p.get("ts") or 0)
                _HIST["points"] = _HIST["points"][-_MAX_POINTS:]
                _save()
    except Exception:
        pass


_MIN_N = 4

def _wl(pts) -> tuple[int, int]:
    w = sum(1 for p in pts if p.get("result") == "Victory")
    l = sum(1 for p in pts if p.get("result") == "Defeat")
    return w, l

def _pct(pts) -> float | None:
    w, l = _wl(pts)
    return round(100 * w / (w + l), 1) if (w + l) >= _MIN_N else None

_DAYPARTS = [
    ("morning", 5, 12), ("afternoon", 12, 17),
    ("evening", 17, 22), ("night", 22, 29),
]

def _daypart(hour: int) -> str:
    for name, lo, hi in _DAYPARTS:
        if lo <= hour < hi or lo <= hour + 24 < hi:
            return name
    return "night"

def _insights(points: list[dict]) -> list[dict]:
    pts = [p for p in points if p.get("result") in ("Victory", "Defeat")]
    out: list[dict] = []
    if len(pts) < _MIN_N:
        return out
    add = lambda title, text, tone="neutral": out.append(
        {"title": title, "text": text, "tone": tone})

    by_part: dict[str, list] = {}
    for p in pts:
        by_part.setdefault(_daypart(time.localtime(p["ts"]).tm_hour), []).append(p)
    rated = [(name, _pct(ps), len(ps)) for name, ps in by_part.items()
             if _pct(ps) is not None]
    if len(rated) >= 2:
        rated.sort(key=lambda r: r[1], reverse=True)
        best, worst = rated[0], rated[-1]
        if best[1] - worst[1] >= 8:
            add("Time of day",
                f"You win {best[1]:.0f}% of {best[0]} games but only "
                f"{worst[1]:.0f}% in the {worst[0]}.",
                "pos" if best[1] >= 50 else "neutral")

    wknd = [p for p in pts if time.localtime(p["ts"]).tm_wday >= 5]
    wkdy = [p for p in pts if time.localtime(p["ts"]).tm_wday < 5]
    pw, pd = _pct(wknd), _pct(wkdy)
    if pw is not None and pd is not None and abs(pw - pd) >= 8:
        hi, lo, hin, lon = (pw, pd, "weekends", "weekdays") if pw > pd else \
                           (pd, pw, "weekdays", "weekends")
        add("Weekday vs weekend",
            f"{hi:.0f}% win rate on {hin} vs {lo:.0f}% on {lon}.")

    by_map: dict[str, list] = {}
    for p in pts:
        if p.get("map"):
            by_map.setdefault(p["map"], []).append(p)
    mrated = [(m, _pct(ps), len(ps)) for m, ps in by_map.items()
              if _pct(ps) is not None]
    if mrated:
        mrated.sort(key=lambda r: r[1], reverse=True)
        b = mrated[0]
        add("Best map", f"{b[0]} is your best map — {b[1]:.0f}% over {b[2]} games.",
            "pos" if b[1] >= 50 else "neutral")
        if len(mrated) >= 2 and mrated[-1][1] < 50:
            w = mrated[-1]
            add("Worst map",
                f"{w[0]} is rough: {w[1]:.0f}% over {w[2]} games. "
                f"Consider dodging it for a while.", "neg")

    after_w, after_l = [], []
    for prev, cur in zip(pts, pts[1:]):
        (after_w if prev["result"] == "Victory" else after_l).append(cur)
    paw, pal = _pct(after_w), _pct(after_l)
    if paw is not None and pal is not None:
        if paw - pal >= 10:
            add("Tilt check",
                f"After a win you win {paw:.0f}% of the next games; after a "
                f"loss only {pal:.0f}%. Short breaks after losses would pay off.",
                "neg")
        elif pal >= paw:
            add("Mental", f"You bounce back well — {pal:.0f}% win rate right "
                          f"after a loss.", "pos")

    early, late = [], []
    idx, last_ts = 0, 0
    for p in pts:
        idx = idx + 1 if p["ts"] - last_ts <= 3 * 3600 else 1
        last_ts = p["ts"]
        (early if idx <= 3 else late).append(p)
    pe, pl = _pct(early), _pct(late)
    if pe is not None and pl is not None and pe - pl >= 10:
        add("Session length",
            f"First 3 games of a session: {pe:.0f}% wins. Game 4 onwards: "
            f"{pl:.0f}%. Shorter sessions, more RR.", "neg")

    best = cur = worst = curl = 0
    for p in pts:
        if p["result"] == "Victory":
            cur += 1; curl = 0
        else:
            curl += 1; cur = 0
        best = max(best, cur); worst = max(worst, curl)
    if best >= 3:
        add("Longest win streak", f"{best} wins in a row at your peak.", "pos")
    if worst >= 4:
        add("Longest loss streak", f"{worst} straight losses at worst — the "
                                   f"tilt spiral is real.", "neg")

    wins = [p for p in pts if p["result"] == "Victory" and isinstance(p.get("delta"), (int, float))]
    losses = [p for p in pts if p["result"] == "Defeat" and isinstance(p.get("delta"), (int, float))]
    if len(wins) >= _MIN_N and len(losses) >= _MIN_N:
        avg_w = sum(p["delta"] for p in wins) / len(wins)
        avg_l = sum(p["delta"] for p in losses) / len(losses)
        add("RR economy",
            f"You gain {avg_w:+.0f} RR per win and {avg_l:+.0f} per loss — "
            f"break-even at {abs(avg_l) / (avg_w + abs(avg_l)) * 100:.0f}% win rate."
            if (avg_w + abs(avg_l)) > 0 else
            f"You gain {avg_w:+.0f} RR per win and {avg_l:+.0f} per loss.")

    week = [p for p in points if p.get("ts") and p["ts"] >= time.time() - 7 * 86400
            and isinstance(p.get("delta"), (int, float))]
    if week:
        net = sum(p["delta"] for p in week)
        add("Last 7 days", f"{net:+.0f} RR over {len(week)} competitive matches.",
            "pos" if net > 0 else "neg" if net < 0 else "neutral")

    by_hour: dict[int, int] = {}
    for p in pts:
        h = time.localtime(p["ts"]).tm_hour
        by_hour[h] = by_hour.get(h, 0) + 1
    if by_hour:
        h, n = max(by_hour.items(), key=lambda kv: kv[1])
        if n >= _MIN_N:
            add("Prime time", f"Most of your games start around {h:02d}:00 "
                              f"({n} matches).")

    return out

def payload() -> dict:
    with _LOCK:
        pts = list(_HIST["points"])
    try:
        ins = _insights(pts)
    except Exception:
        ins = []
    return {"points": pts, "insights": ins}


if __name__ == "__main__":
    now = int(time.time())

    def _pt(i, **kw):
        p = {"matchId": f"m{i}", "ts": now - i * 3600, "map": "Ascent",
             "result": "Victory" if i % 2 else "Defeat", "delta": 20 if i % 2 else -18,
             "tier": 21, "rr": 50}
        p.update(kw)
        return p

    series = [_pt(i) for i in range(12)]
    assert _insights(series), "a 12-match series should yield some insight"

    assert _insights([_pt(0, delta=20.0)] + series[1:])

    assert _insights([_pt(0, tier=None, rr=None, delta=None)] + series[1:])

    assert _insights(series[:2]) == []
    assert _insights([]) == []

    assert {_daypart(h) for h in range(24)} == {"morning", "afternoon", "evening", "night"}

    print("history self-check OK")
