"""Trend agent's scanner: breakouts to N-day highs in uptrends (long stock or calls) and
breakdowns to N-day lows in downtrends (puts only; no shorting without margin)."""
from indicators import enrich
from common import manage_long_option, session_fraction, split_today, today_volume


def entries(ctx):
    p = ctx["params"]["trend"]
    out = []
    for sym in ctx["candidates"]:
        df, price = ctx["bars"].get(sym), ctx["prices"].get(sym)
        if df is None or price is None:
            continue
        hist = enrich(split_today(df))
        if len(hist) < 210:
            continue
        last = hist.iloc[-1]
        rel_vol = today_volume(df) / max(last["vol20"] * session_fraction(), 1)
        if rel_vol < p["min_rel_volume"]:
            continue
        atr = float(last["atr20"])
        hi = float(hist["high"].tail(p["breakout_lookback"]).max())
        lo = float(hist["low"].tail(p["breakout_lookback"]).min())
        base = {"agent": "trend", "symbol": sym, "price": price, "atr": round(atr, 3),
                "signals": {"rel_volume": round(rel_vol, 2), "roc20_pct": round(float(last["roc20"]) * 100, 1),
                            "pct_vs_sma50": round((price / last["sma50"] - 1) * 100, 1),
                            "pct_vs_sma200": round((price / last["sma200"] - 1) * 100, 1)}}
        if price > hi and last["sma50"] > last["sma200"] and price > last["sma50"]:
            out.append({**base, "direction": "long", "stop": round(price - p["stop_atr"] * atr, 2),
                        "level": round(hi, 2), "score": float(last["roc20"]) * rel_vol,
                        "rule_thesis": f"{p['breakout_lookback']}-day breakout above {hi:.2f} on {rel_vol:.1f}x volume"})
        elif price < lo and last["sma50"] < last["sma200"] and price < last["sma50"]:
            out.append({**base, "direction": "short", "stop": round(price + p["stop_atr"] * atr, 2),
                        "level": round(lo, 2), "score": -float(last["roc20"]) * rel_vol,
                        "rule_thesis": f"{p['breakout_lookback']}-day breakdown below {lo:.2f} on {rel_vol:.1f}x volume (puts)"})
    return sorted(out, key=lambda x: -x["score"])[:6]


def manage(ctx, pos):
    """Hard exits. Stock: trailing ATR stop. Options: premium stop/target/time + underlying stop."""
    p = ctx["params"]["trend"]
    und = pos.get("underlying", pos["symbol"])
    price = ctx["prices"].get(und)
    if price is not None and pos.get("atr"):
        if pos.get("direction", "long") == "long":
            pos["high_water"] = max(pos.get("high_water", price), price)
            trail = round(pos["high_water"] - p["trail_atr"] * pos["atr"], 2)
            pos["stop"] = max(pos.get("stop", 0), trail)
            if price <= pos["stop"]:
                return "trailing stop on underlying"
        else:
            pos["low_water"] = min(pos.get("low_water", price), price)
            trail = round(pos["low_water"] + p["trail_atr"] * pos["atr"], 2)
            pos["stop"] = min(pos.get("stop", 1e12), trail)
            if price >= pos["stop"]:
                return "trailing stop on underlying"
    if pos["kind"] == "option":
        return manage_long_option(ctx, pos)
    return None
