"""The Crash Radar: a 0-100 market-stress score everyone can see.

Market part (from daily bars we already pull):
  trend       SPY below its 50- and 200-day averages
  breadth     share of our ~500 stocks still above their 50-day average (falling breadth = warning)
  volatility  SPY realized volatility, or option implied volatility when we have it
  drawdown    how far SPY is off its 52-week high
  credit      junk bonds (HYG) lagging Treasuries (IEF) over 20 days
  safety      Treasuries and gold (TLT, GLD) beating SPY over 20 days
Economy part (optional, needs the free FRED_API_KEY secret), refreshed once a day:
  yield curve (10y minus 2y), weekly jobless claims vs their 1-year low, junk-bond spreads,
  and the Sahm recession indicator. It sets the background; prices drive the timing.

Score under 40 = calm, 40-70 = warning, 70+ = stress. Saved to data/radar.json with a daily history."""
from datetime import timedelta

import requests

import config
import store
from common import split_today
from indicators import enrich, realized_vol

WEIGHTS = {"trend": 0.20, "breadth": 0.20, "volatility": 0.15, "drawdown": 0.15, "credit": 0.15, "safety": 0.15}
ECON_WEIGHT = 0.25
FRED_SERIES = {"T10Y2Y": "yield_curve", "ICSA": "jobless_claims", "BAMLH0A0HYM2": "junk_spread",
               "SAHMREALTIME": "sahm"}
LABELS = {"trend": "SPY trend", "breadth": "Breadth", "volatility": "Volatility", "drawdown": "Drawdown",
          "credit": "Credit stress", "safety": "Flight to safety", "economy": "Economy (FRED)"}


def clamp(x):
    return max(0.0, min(100.0, 100.0 * x))


def tier(score):
    return "stress" if score >= 70 else ("warning" if score >= 40 else "calm")


def _closes(bars, sym):
    df = bars.get(sym)
    if df is None or len(df) == 0:
        return None
    done = split_today(df)
    return done if len(done) >= 30 else None


def market_parts(bars, prices):
    parts, facts = {}, {}
    spy = _closes(bars, "SPY")
    if spy is not None and len(spy) >= 210:
        h = enrich(spy).iloc[-1]
        px = prices.get("SPY") or float(spy["close"].iloc[-1])
        t = (35 if px < h["sma50"] else 0) + (40 if px < h["sma200"] else 0) + (25 if h["sma50"] < h["sma200"] else 0)
        parts["trend"] = float(t)
        facts["spy_vs_sma50_pct"] = round((px / h["sma50"] - 1) * 100, 1)
        facts["spy_vs_sma200_pct"] = round((px / h["sma200"] - 1) * 100, 1)
        hi = float(spy["high"].tail(252).max())
        dd = 1 - px / hi
        parts["drawdown"] = clamp(dd / 0.15)
        facts["spy_off_high_pct"] = round(dd * 100, 1)
        rv = realized_vol(spy["close"], 20)
        vol = clamp((rv - 0.12) / 0.23)
        ivh = store.load("iv_history.json", {}).get("SPY", [])
        if ivh and ivh[-1]["date"] >= (store.now_et().date() - timedelta(days=5)).isoformat():
            iv = ivh[-1]["iv"]
            vol = max(vol, clamp((iv - 0.14) / 0.26))
            facts["spy_iv_pct"] = round(iv * 100, 1)
        parts["volatility"] = vol
        facts["spy_realized_vol_pct"] = round(rv * 100, 1)

    above = total = 0
    for sym, df in bars.items():
        if sym in config.BEAR_SYMBOLS:
            continue
        d = split_today(df)
        if len(d) < 50:
            continue
        total += 1
        px = prices.get(sym) or float(d["close"].iloc[-1])
        above += px > float(d["close"].tail(50).mean())
    if total >= 50:
        pct = 100 * above / total
        parts["breadth"] = clamp((65 - pct) / 40)
        facts["pct_above_50d"] = round(pct, 1)
        facts["breadth_sample"] = total

    hyg, ief = _closes(bars, "HYG"), _closes(bars, "IEF")
    if hyg is not None and ief is not None:
        ratio = (hyg["close"] / ief["close"]).dropna()
        if len(ratio) >= 21:
            chg = float(ratio.iloc[-1] / ratio.iloc[-21] - 1)
            parts["credit"] = clamp((0.01 - chg) / 0.04)
            facts["junk_vs_treasuries_20d_pct"] = round(chg * 100, 2)

    def roc(sym):
        d = _closes(bars, sym)
        return float(d["close"].iloc[-1] / d["close"].iloc[-21] - 1) if d is not None and len(d) >= 21 else None
    s, t, g = roc("SPY"), roc("TLT"), roc("GLD")
    if s is not None and t is not None and g is not None:
        diff = (t + g) / 2 - s
        parts["safety"] = clamp(diff / 0.08)
        facts["havens_vs_spy_20d_pct"] = round(diff * 100, 1)
    return parts, facts


# ---------------- economy data (FRED) ----------------
def _fred_series(sid):
    r = requests.get("https://api.stlouisfed.org/fred/series/observations", timeout=20,
                     params={"series_id": sid, "api_key": config.FRED_API_KEY, "file_type": "json",
                             "sort_order": "desc", "limit": 70})
    r.raise_for_status()
    return [(o["date"], float(o["value"])) for o in r.json().get("observations", []) if o.get("value") not in (".", "", None)]


def economy():
    """Daily-cached economy readings from FRED. Returns (score or None, facts)."""
    if not config.FRED_API_KEY:
        return None, {"status": "off (add the FRED_API_KEY secret to turn on)"}
    cache = store.load("fred.json", {})
    if cache.get("date") != store.today():
        data, errors = {}, []
        for sid in FRED_SERIES:
            try:
                data[sid] = _fred_series(sid)
            except Exception as e:  # noqa: BLE001 - never print details: the URL holds the key
                errors.append(f"{sid}: {type(e).__name__}")
        if data:
            cache = {"date": store.today(), "series": {k: v[:70] for k, v in data.items()}, "errors": errors}
            store.save("fred.json", cache)
        elif not cache:
            return None, {"status": "FRED unavailable: " + "; ".join(errors)[:120]}
    s = cache.get("series", {})
    scores, facts = [], {"status": "on", "as_of": cache.get("date")}
    if s.get("T10Y2Y"):
        v = s["T10Y2Y"][0][1]
        scores.append(clamp((0.5 - v) / 1.0))
        facts["yield_curve_10y_2y"] = v
    if s.get("ICSA") and len(s["ICSA"]) >= 8:
        vals = [v for _, v in s["ICSA"]]
        avg4 = sum(vals[:4]) / 4
        low = min(sum(vals[i:i + 4]) / 4 for i in range(0, min(len(vals), 52) - 3))
        rise = avg4 / low - 1
        scores.append(clamp(rise / 0.30))
        facts["jobless_claims_4wk"] = round(avg4)
        facts["claims_vs_1yr_low_pct"] = round(rise * 100, 1)
    if s.get("BAMLH0A0HYM2"):
        v = s["BAMLH0A0HYM2"][0][1]
        scores.append(clamp((v - 3.0) / 3.5))
        facts["junk_bond_spread_pct"] = v
    if s.get("SAHMREALTIME"):
        v = s["SAHMREALTIME"][0][1]
        scores.append(clamp(v / 0.5))
        facts["sahm_indicator"] = v
    return (round(sum(scores) / len(scores), 1) if scores else None), facts


# ---------------- the score ----------------
def compute(bars, prices, record=True):
    parts, facts = market_parts(bars, prices)
    if not parts:
        return store.load("radar.json", {}).get("now") or {"score": None, "tier": "unknown", "parts": {}, "facts": {}}
    wsum = sum(WEIGHTS[k] for k in parts)
    market = sum(WEIGHTS[k] * v for k, v in parts.items()) / wsum
    econ, efacts = economy()
    score = market if econ is None else (1 - ECON_WEIGHT) * market + ECON_WEIGHT * econ
    shown = {k: round(v) for k, v in parts.items()}
    if econ is not None:
        shown["economy"] = round(econ)
    now = {"score": round(score), "tier": tier(score), "market_score": round(market),
           "economy_score": None if econ is None else round(econ), "parts": shown,
           "facts": facts, "economy": efacts, "at": store.now_et().isoformat(timespec="minutes")}
    if record:
        save(now)
    return now


def save(now):
    """Store the reading and its daily history; log a journal line when the tier changes."""
    data = store.load("radar.json", {})
    prev = (data.get("now") or {}).get("tier")
    hist = [r for r in data.get("history", []) if r["date"] != store.today()]
    hist.append({"date": store.today(), "score": now["score"], "tier": now["tier"]})
    store.save("radar.json", {"now": now, "history": hist[-260:], "labels": LABELS})
    if prev and prev != now["tier"]:
        top = sorted(now["parts"].items(), key=lambda kv: -kv[1])[:2]
        store.journal({"type": "radar", "agent": "blackswan",
                       "note": f"Crash Radar moved from {prev} to {now['tier'].upper()} ({now['score']}/100). "
                               "Biggest signals: " + ", ".join(f"{LABELS[k]} {v}" for k, v in top)})


def current():
    return store.load("radar.json", {}).get("now") or {}
