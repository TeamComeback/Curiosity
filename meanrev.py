"""Mean-reversion agent's scanner: RSI(2) washouts in stocks above their 200-day."""
from datetime import date

import pandas as pd

import store
from indicators import enrich, rsi, sma
from common import manage_long_option, split_today


def _live(hist, price):
    closes = pd.concat([hist["close"], pd.Series([price])], ignore_index=True)
    return float(rsi(closes, 2).iloc[-1]), float(sma(closes, 5).iloc[-1])


def entries(ctx):
    p = ctx["params"]["meanrev"]
    out = []
    for sym in ctx["candidates"]:
        df, price = ctx["bars"].get(sym), ctx["prices"].get(sym)
        if df is None or price is None:
            continue
        hist = enrich(split_today(df))
        if len(hist) < 210:
            continue
        last = hist.iloc[-1]
        r2, s5 = _live(hist, price)
        if not (price > last["sma200"] and r2 < p["rsi_entry"]):
            continue
        atr = float(last["atr20"])
        out.append({"agent": "meanrev", "symbol": sym, "direction": "long", "price": price,
                    "stop": round(price - p["stop_atr"] * atr, 2), "atr": round(atr, 3),
                    "target_hint": round(s5, 2),
                    "signals": {"rsi2": round(r2, 1), "pct_vs_sma200": round((price / last["sma200"] - 1) * 100, 1),
                                "pct_vs_sma5": round((price / s5 - 1) * 100, 1)},
                    "rule_thesis": f"RSI(2) {r2:.0f} washout above the 200-day; snap-back target ~{s5:.2f}",
                    "score": -r2})
    return sorted(out, key=lambda x: -x["score"])[:20]


def manage(ctx, pos):
    p = ctx["params"]["meanrev"]
    und = pos.get("underlying", pos["symbol"])
    price, df = ctx["prices"].get(und), ctx["bars"].get(und)
    if price is not None and df is not None:
        r2, s5 = _live(split_today(df), price)
        if pos.get("stop") and price <= pos["stop"]:
            return "stop loss (underlying)"
        if r2 > p["rsi_exit"] or price > s5:
            return f"snap-back (RSI2 {r2:.0f})"
    held = (store.now_et().date() - date.fromisoformat(pos["opened"][:10])).days
    if held >= p["max_hold_days"]:
        return "time stop"
    if pos["kind"] == "option":
        return manage_long_option(ctx, pos)
    return None
