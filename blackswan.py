"""Black Swan & Co.: the crash firm. It only bets on prices falling, sized by the Crash Radar.

  calm (<40)      put spreads on the weakest stocks and sectors that are breaking down, plus cheap
                  far-out SPY/QQQ put spreads as insurance while puts are cheap
  warning (40-70) index and sector puts, 1x inverse ETFs (SH, PSQ, RWM, DOG)
  stress (70+)    bigger bets; 3x inverse ETFs (SPXU, SQQQ) allowed, held a few days at most
  any level       safe havens (TLT, GLD, UUP) when money is clearly running to safety

Its own guardrails (code, not AI): the most it can put on one trade grows with the radar
(12% of equity when calm, 20% in warning, 35% in stress), and insurance is capped at a small
slice of equity. A drawdown guard halves that cap while equity sits more than 20% below the
starting balance, and quarters it below 40%, so a losing streak can't sink the firm. Exits take profits in stages, sell options before time decay speeds up, and
get out when a breakdown fails."""
from datetime import date

import config
import radar
import store
import universe
from common import debit_spread_mark, manage_long_option, mid, split_today
from indicators import enrich, realized_vol

SIZE_CAP = {"calm": 0.12, "warning": 0.20, "stress": 0.35, "unknown": 0.12}
# Drawdown guard (Oct 2026, after a 31% drawdown): shrink new bets while the firm is under water.
DRAWDOWN_STEPS = [(0.60, 0.25), (0.80, 0.50)]   # (equity / start below this, multiply cap by this)


def _drawdown_mult(equity):
    start = float(config.RACE.get("start_balance") or 0) or equity
    ratio = equity / start if start else 1.0
    for floor, mult in DRAWDOWN_STEPS:
        if ratio < floor:
            return mult
    return 1.0
SECTOR_ETFS = list(universe.SECTOR_ETFS) + ["SMH", "KRE", "XHB", "XRT", "IYT"]
INDEXES = ["SPY", "QQQ", "IWM", "DIA"]
NOT_STOCKS = set(config.BEAR_SYMBOLS) | set(SECTOR_ETFS) | set(config.VOL_UNDERLYINGS) | {"DIA"}


def _equity(ctx):
    a = ctx["state"]["agents"]["blackswan"]
    val = 0.0
    for p in a["positions"].values():
        if p["kind"] == "stock":
            val += ctx["prices"].get(p["symbol"], p["entry"]) * p["qty"]
        else:
            val += p.get("mark", p["entry"]) * 100 * p["qty"]
    return a["cash"] + val


def _hist(ctx, sym):
    df = ctx["bars"].get(sym)
    if df is None:
        return None
    h = enrich(split_today(df))
    return h if len(h) >= 210 else None


def _bear_setup(ctx, sym, p, play):
    """A breakdown: below the 50-day, 50-day under the 200-day, at or near the N-day low."""
    h, price = _hist(ctx, sym), ctx["prices"].get(sym)
    if h is None or price is None:
        return None
    last = h.iloc[-1]
    lo = float(h["low"].tail(p["breakdown_lookback"]).min())
    near = (price / lo - 1) * 100
    roc = float(last["roc20"]) * 100
    if not (price < last["sma50"] and near <= 2.0 and roc < 0):
        return None
    downtrend = last["sma50"] < last["sma200"]
    if play == "weak_stock" and (not downtrend or price < 15):
        return None
    atr = float(last["atr20"])
    return {"agent": "blackswan", "symbol": sym, "price": price, "direction": "short", "play": play,
            "atr": round(atr, 3), "stop": round(price + p["stop_atr"] * atr, 2), "level": round(lo, 2),
            "suggested_instrument": "debit_spread",
            "signals": {"roc20_pct": round(roc, 1), "pct_vs_sma50": round((price / last["sma50"] - 1) * 100, 1),
                        "pct_vs_sma200": round((price / last["sma200"] - 1) * 100, 1),
                        "pct_above_low": round(near, 2), "downtrend": bool(downtrend)},
            "score": -roc + (5 if downtrend else 0) + (3 if near <= 0 else 0),
            "rule_thesis": f"{sym} {'broke' if near <= 0 else 'is testing'} its {p['breakdown_lookback']}-day low "
                           f"{lo:.2f}, {roc:+.1f}% over 20 days" + (", below its 200-day" if price < last["sma200"] else "")}


def _long_etf(ctx, sym, p, play, why, hold_days=None):
    h, price = _hist(ctx, sym), ctx["prices"].get(sym)
    if h is None or price is None:
        return None
    last = h.iloc[-1]
    atr = float(last["atr20"])
    c = {"agent": "blackswan", "symbol": sym, "price": price, "direction": "long", "play": play,
         "atr": round(atr, 3), "stop": round(price - p["stop_atr"] * atr, 2), "suggested_instrument": "stock",
         "signals": {"roc20_pct": round(float(last["roc20"]) * 100, 1),
                     "pct_vs_sma50": round((price / last["sma50"] - 1) * 100, 1)},
         "score": float(last["roc20"]) * 100, "rule_thesis": why}
    if hold_days:
        c["hold_days"] = hold_days
    return c


def entries(ctx):
    p = ctx["params"]["blackswan"]
    r = ctx.get("radar") or {}
    lvl, score = r.get("tier", "unknown"), r.get("score") or 0
    parts = r.get("parts", {})
    agent = ctx["state"]["agents"]["blackswan"]
    eq = _equity(ctx)
    cap = round(eq * SIZE_CAP.get(lvl, 0.12) * _drawdown_mult(eq), 2)
    out = []

    # 1) weakest stocks breaking down (any level)
    stocks = sorted([c for c in (_bear_setup(ctx, s, p, "weak_stock") for s in ctx["candidates"] if s not in NOT_STOCKS) if c],
                    key=lambda c: -c["score"])
    # 2) weakest sectors (any level)
    sectors = sorted([c for c in (_bear_setup(ctx, s, p, "sector") for s in SECTOR_ETFS) if c], key=lambda c: -c["score"])
    # 3) the indexes themselves, plus inverse ETFs (warning and up; 3x funds only in stress)
    idx, inverse = [], []
    if lvl in ("warning", "stress"):
        idx = sorted([c for c in (_bear_setup(ctx, s, p, "index") for s in INDEXES) if c], key=lambda c: -c["score"])
        for c in idx[:2]:
            inv = config.INVERSE_ETFS.get(c["symbol"])
            lev = config.LEVERAGED_INVERSE.get(c["symbol"]) if lvl == "stress" else None
            if lev:
                inverse.append(_long_etf(ctx, lev, p, "leveraged", f"3x short {c['symbol']} for a fast move while the radar "
                                         f"is at {score}/100; held {p['leveraged_max_days']} days at most", p["leveraged_max_days"]))
            if inv:
                inverse.append(_long_etf(ctx, inv, p, "inverse", f"{inv} rises as {c['symbol']} falls; radar {score}/100 ({lvl})"))
        inverse = [c for c in inverse if c]
    # 4) safe havens when money is running to safety
    havens = []
    if parts.get("safety", 0) >= 50 or lvl == "stress":
        for sym in config.SAFE_HAVENS:
            h, price = _hist(ctx, sym), ctx["prices"].get(sym)
            if h is not None and price and price > h.iloc[-1]["sma50"] and h.iloc[-1]["roc20"] > 0:
                havens.append(_long_etf(ctx, sym, p, "haven", f"Flight to safety: {sym} up {h.iloc[-1]['roc20'] * 100:.1f}% "
                                        f"in 20 days and above its 50-day while stocks wobble"))
        havens = sorted([c for c in havens if c], key=lambda c: -c["score"])
    # 5) cheap insurance in calm markets: far-out index put spread while puts are cheap
    insurance = []
    has_insurance = any(x.get("play") == "insurance" for x in agent["positions"].values())
    spy = _hist(ctx, "SPY")
    if lvl == "calm" and not has_insurance and spy is not None and p["insurance_pct"] > 0:
        rv = realized_vol(spy["close"], 20)
        if rv < 0.16:
            price = ctx["prices"].get("SPY") or float(spy["close"].iloc[-1])
            insurance.append({"agent": "blackswan", "symbol": "SPY", "price": price, "direction": "short", "play": "insurance",
                              "atr": 0, "stop": 0, "suggested_instrument": "debit_spread",
                              "max_dollars": round(_equity(ctx) * p["insurance_pct"] * _drawdown_mult(_equity(ctx)), 2),
                              "opt": {"opt_delta": 0.20, "opt_min_dte": 75, "opt_max_dte": 150, "spread_width_pct": 0.05},
                              "signals": {"spy_realized_vol_pct": round(rv * 100, 1), "radar": score},
                              "score": 1, "rule_thesis": f"Insurance while it's cheap: SPY realized vol only {rv:.0%}; a far-out "
                                                         f"put spread costs little and pays big in a sharp drop"})

    # the menu the firm sees, best plays for the current radar level first
    if lvl == "stress":
        out = idx[:2] + inverse[:3] + sectors[:1] + havens[:1] + stocks[:1]
    elif lvl == "warning":
        out = idx[:1] + inverse[:1] + sectors[:2] + stocks[:2] + havens[:1]
    else:
        out = stocks[:3] + sectors[:1] + insurance + havens[:1]
    out = [c for c in out if c][:12]
    for c in out:
        c.setdefault("max_dollars", cap)
        c["radar"] = {"score": score, "tier": lvl}
    return out


# ---------------- exits ----------------
def _trail_short(ctx, pos, p):
    """Underlying stop for bearish bets: trails down as the stock falls; hit = the breakdown failed."""
    price = ctx["prices"].get(pos.get("underlying", pos["symbol"]))
    if price is None or not pos.get("atr") or not pos.get("stop"):
        return None
    pos["low_water"] = min(pos.get("low_water", price), price)
    pos["stop"] = min(pos["stop"], round(pos["low_water"] + p["trail_atr"] * pos["atr"], 2))
    if price >= pos["stop"]:
        return "breakdown failed (underlying back above stop)"
    return None


def _stages(pos, mark, entry):
    """Profit-taking in stages: sell half on a double, then exit if it gives back 30% from its peak."""
    pos["peak_mark"] = max(pos.get("peak_mark", mark), mark)
    if mark >= entry * 2 and not pos.get("trimmed") and pos["qty"] >= 2:
        return {"reason": f"took half off at {mark / entry - 1:+.0%}", "qty": int(pos["qty"] // 2)}
    if pos["peak_mark"] >= entry * 1.6 and mark <= pos["peak_mark"] * 0.70:
        return f"locked in gains: down 30% from the peak after {pos['peak_mark'] / entry - 1:+.0%}"
    return None


def manage(ctx, pos):
    p = ctx["params"]["blackswan"]
    play = pos.get("play")
    held = (store.now_et().date() - date.fromisoformat(pos["opened"][:10])).days

    if pos["kind"] == "stock":
        price = ctx["prices"].get(pos["symbol"])
        if price is None:
            return None
        if play == "leveraged" and held >= round(pos.get("hold_days", p["leveraged_max_days"]) * 7 / 5):
            return f"3x fund held {held} days: out before decay eats the gains"
        if pos.get("atr"):
            pos["high_water"] = max(pos.get("high_water", price), price)
            pos["stop"] = max(pos.get("stop", 0), round(pos["high_water"] - p["trail_atr"] * pos["atr"], 2))
            if price <= pos["stop"]:
                return "trailing stop"
            if not pos.get("trimmed") and price >= pos["entry"] + 3 * pos["atr"] and pos["qty"] * price >= 10:
                return {"reason": "took half off after a 3-ATR gain", "qty": round(pos["qty"] / 2, 4)}
        r = ctx.get("radar") or {}
        if play in ("inverse", "leveraged") and r.get("tier") == "calm" and (r.get("parts", {}).get("trend", 100) == 0):
            return "market recovered (radar calm, SPY back above its averages)"
        return None

    if pos["kind"] == "option":                      # single long put
        why = _trail_short(ctx, pos, p)
        if why:
            return why
        q = ctx["broker"].option_quotes([pos["symbol"]]).get(pos["symbol"])
        if q:
            m, _ = mid(q)
            if m is not None:
                st = _stages(pos, m, pos["entry"])
                if st:
                    pos["mark"] = round(m, 2)
                    return st
        return manage_long_option(ctx, pos)

    if pos["kind"] == "debit_spread":
        mark, half = debit_spread_mark(ctx["broker"], pos)
        dte = (date.fromisoformat(pos["expiry"]) - store.now_et().date()).days
        if mark is not None:
            pos["mark"], pos["exit_half_spread"] = mark, half
            if mark >= pos["width"] * 0.85:
                return f"near max value ({mark:.2f} of {pos['width']:g} wide)"
            st = _stages(pos, mark, pos["entry"])
            if st:
                return st
            if play != "insurance" and mark <= pos["entry"] * (1 - p["stop_pct"]):
                return f"spread stop ({mark / pos['entry'] - 1:.0%})"
        if play != "insurance":
            why = _trail_short(ctx, pos, p)
            if why:
                return why
        if dte <= p["exit_dte"]:
            return f"time exit ({dte} DTE): sold before time decay speeds up"
    return None
