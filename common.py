import re
from datetime import date, timedelta

import pandas as pd

import config
from store import now_et

OCC = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def split_today(df):
    """Completed daily bars only (excludes today's partial bar)."""
    today = now_et().date()
    idx = df.index.tz_convert("America/New_York") if df.index.tz is not None else df.index
    return df[pd.Series(idx.date, index=df.index) < today]


def session_fraction():
    n = now_et()
    mins = (n.hour * 60 + n.minute) - (9 * 60 + 30)
    return min(max(mins / 390, 0.1), 1.0)


def today_volume(df):
    today = now_et().date()
    idx = df.index.tz_convert("America/New_York") if df.index.tz is not None else df.index
    t = df[pd.Series(idx.date, index=df.index) == today]
    return float(t["volume"].iloc[-1]) if len(t) else 0.0


def parse_occ(sym):
    m = OCC.match(sym)
    if not m:
        return None
    root, ymd, cp, strike = m.groups()
    exp = date(2000 + int(ymd[:2]), int(ymd[2:4]), int(ymd[4:]))
    return {"root": root, "expiry": exp, "type": "call" if cp == "C" else "put", "strike": int(strike) / 1000}


def mid(q):
    bid, ask = float(q.get("bp") or 0), float(q.get("ap") or 0)
    if ask <= 0:
        return None, None
    return (bid + ask) / 2, ask - bid


def pick_option(broker, underlying, opt_type, price, p, budget):
    """Single long call/put closest to the target delta inside the DTE window whose
    cost (ask x 100) fits the budget. Returns None if nothing affordable."""
    today = now_et().date()
    lo, hi = (price * 0.85, price * 1.15)
    chain = broker.option_chain(underlying, opt_type, (today + timedelta(days=p["opt_min_dte"])).isoformat(),
                                (today + timedelta(days=p["opt_max_dte"])).isoformat(), round(lo, 2), round(hi, 2))
    best = None
    for sym, snap in chain.items():
        meta = parse_occ(sym)
        q = snap.get("latestQuote") or {}
        delta = (snap.get("greeks") or {}).get("delta")
        ask = float(q.get("ap") or 0)
        if not meta or delta is None or ask <= 0.05 or ask * 100 > budget:
            continue
        score = abs(abs(delta) - p["opt_delta"])
        if best is None or score < best["score"]:
            m, spr = mid(q)
            best = {"symbol": sym, "ask": ask, "mid": m, "spread": spr, "delta": delta,
                    "expiry": meta["expiry"].isoformat(), "strike": meta["strike"], "type": opt_type, "score": score}
    return best


def manage_long_option(ctx, pos):
    """Default exits for long calls/puts: -50% premium stop, +100% target, 5 DTE time exit."""
    e = config.OPTION_EXITS
    q = ctx["broker"].option_quotes([pos["symbol"]]).get(pos["symbol"])
    if q:
        m, spr = mid(q)
        if m is not None:
            pos["mark"] = round(m, 2)
            pos["exit_half_spread"] = round(spr / 2, 3)
            chg = m / pos["entry"] - 1
            if chg <= -e["stop_pct"]:
                return f"option stop ({chg:.0%})"
            if chg >= e["target_pct"]:
                return f"option target ({chg:+.0%})"
    dte = (date.fromisoformat(pos["expiry"]) - now_et().date()).days
    if dte <= e["exit_dte"]:
        return f"time exit ({dte} DTE)"
    return None


def pick_put_spread(broker, underlying, price, p, budget):
    """Bear put (debit) spread: buy the put nearest the target delta, sell a cheaper put about
    spread_width_pct of the price below it, same expiry inside the DTE window. The debit paid is the
    most it can lose. Returns the widest-affordable choice, or None if nothing fits the budget."""
    today = now_et().date()
    chain = broker.option_chain(underlying, "put", (today + timedelta(days=p["opt_min_dte"])).isoformat(),
                                (today + timedelta(days=p["opt_max_dte"])).isoformat(),
                                round(price * 0.60, 2), round(price * 1.05, 2))
    rows = []
    for sym, snap in chain.items():
        meta = parse_occ(sym)
        q = snap.get("latestQuote") or {}
        m, spr = mid(q)
        delta = (snap.get("greeks") or {}).get("delta")
        if meta and m is not None and delta is not None:
            rows.append({"symbol": sym, **meta, "mid": m, "spread": spr, "delta": delta,
                         "ask": float(q.get("ap") or 0), "bid": float(q.get("bp") or 0)})
    if not rows:
        return None
    target = today + timedelta(days=(p["opt_min_dte"] + p["opt_max_dte"]) // 2)
    expiry = min({r["expiry"] for r in rows}, key=lambda e: abs((e - target).days))
    rows = sorted([r for r in rows if r["expiry"] == expiry], key=lambda r: r["strike"])
    long = min(rows, key=lambda r: abs(abs(r["delta"]) - p["opt_delta"]))
    width_want = max(price * p["spread_width_pct"], 1.0)
    best = None
    for mult in (1.0, 0.6, 0.35):                       # narrower if the wide one doesn't fit the budget
        want = long["strike"] - width_want * mult
        lower = [r for r in rows if r["strike"] < long["strike"]]
        if not lower:
            break
        short = min(lower, key=lambda r: abs(r["strike"] - want))
        width = round(long["strike"] - short["strike"], 2)
        debit = round(long["mid"] - short["mid"], 2)
        if width <= 0 or debit <= 0.05 or debit >= width * 0.9:
            continue
        if debit * 100 <= budget:
            best = {"long": long, "short": short, "width": width, "debit": debit, "expiry": expiry.isoformat(),
                    "half_spread": round((long["spread"] + short["spread"]) / 2, 3)}
            break
    return best


def debit_spread_mark(broker, pos):
    """Current value of a bear put spread (long put minus short put, at mids) and half its bid-ask."""
    quotes = broker.option_quotes([l["symbol"] for l in pos["legs"]])
    lq, sq = quotes.get(pos["legs"][0]["symbol"]), quotes.get(pos["legs"][1]["symbol"])
    if not lq or not sq:
        return None, None
    lm, ls = mid(lq)
    sm, ss = mid(sq)
    if lm is None or sm is None:
        return None, None
    return round(max(lm - sm, 0), 2), round((ls + ss) / 2, 3)
