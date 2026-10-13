"""Vol-selling agent's scanner: defined-risk credit spreads on index ETFs when implied
vol is rich versus realized. Bull put spreads in uptrends, bear call spreads in
downtrends. Default exits: profit target, loss stop, days-to-expiry cutoff."""
from datetime import date, timedelta

import store
from indicators import enrich, realized_vol
from common import mid, parse_occ, split_today


def record_iv(und, iv):
    hist = store.load("iv_history.json", {})
    rows = [r for r in hist.get(und, []) if r["date"] != store.today()]
    rows.append({"date": store.today(), "iv": round(iv, 4)})
    hist[und] = rows[-400:]
    store.save("iv_history.json", hist)
    return [r["iv"] for r in hist[und]]


def iv_rank(series):
    if len(series) < 60:
        return None
    lo, hi = min(series), max(series)
    return 100 * (series[-1] - lo) / (hi - lo) if hi > lo else 50.0


def entries(ctx):
    p = ctx["params"]["volsell"]
    b = ctx["broker"]
    out = []
    today = store.now_et().date()
    for und in ctx["vol_underlyings"]:
        df = ctx["bars"].get(und)
        price = ctx["prices"].get(und)
        if df is None or price is None:
            continue
        hist = enrich(split_today(df))
        if len(hist) < 210:
            continue
        last = hist.iloc[-1]
        if price > last["sma50"]:
            side = "put"
        elif price < last["sma50"] and price < last["sma200"]:
            side = "call"
        else:
            continue  # no clear trend: stand aside (iron condors are a later upgrade)
        lo_k, hi_k = (price * 0.80, price * 1.02) if side == "put" else (price * 0.98, price * 1.20)
        chain = b.option_chain(und, side, (today + timedelta(days=p["min_dte"])).isoformat(),
                               (today + timedelta(days=p["max_dte"])).isoformat(),
                               round(lo_k, 2), round(hi_k, 2))
        rows = []
        for sym, snap in chain.items():
            meta = parse_occ(sym)
            q = snap.get("latestQuote") or {}
            m, spr = mid(q)
            if not meta or m is None:
                continue
            rows.append({"symbol": sym, **meta, "mid": m, "spread": spr,
                         "delta": (snap.get("greeks") or {}).get("delta"),
                         "iv": snap.get("impliedVolatility")})
        if not rows:
            continue
        target = today + timedelta(days=(p["min_dte"] + p["max_dte"]) // 2)
        expiry = min({r["expiry"] for r in rows}, key=lambda e: abs((e - target).days))
        rows = [r for r in rows if r["expiry"] == expiry]
        atm = sorted([r for r in rows if r["iv"]], key=lambda r: abs(r["strike"] - price))[:4]
        if not atm:
            continue
        iv = sum(r["iv"] for r in atm) / len(atm)
        ivs = record_iv(und, iv)
        rv = realized_vol(hist["close"], 20)
        ratio = iv / rv if rv > 0 else 0
        rank = iv_rank(ivs)
        rich = (rank is not None and rank >= 50) or (rank is None and ratio >= p["min_iv_rv"])
        if not rich:
            continue
        otm = [r for r in rows if r["delta"] is not None and
               ((side == "put" and r["strike"] < price) or (side == "call" and r["strike"] > price))]
        if not otm:
            continue
        short = min(otm, key=lambda r: abs(abs(r["delta"]) - p["short_delta"]))
        want = short["strike"] - p["width"] if side == "put" else short["strike"] + p["width"]
        further = [r for r in rows if (r["strike"] <= want if side == "put" else r["strike"] >= want)]
        if not further:
            continue
        long = min(further, key=lambda r: abs(r["strike"] - want))
        width = abs(short["strike"] - long["strike"])
        credit = round(short["mid"] - long["mid"], 2)
        if width <= 0 or credit <= 0.03 or credit >= width:
            continue
        out.append({
            "agent": "volsell", "kind": "spread", "direction": "short_vol", "symbol": und, "underlying": und, "price": price,
            "structure": "bull put spread" if side == "put" else "bear call spread",
            "legs": [
                {"symbol": short["symbol"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                {"symbol": long["symbol"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
            ],
            "short_strike": short["strike"], "long_strike": long["strike"], "expiry": expiry.isoformat(),
            "credit": credit, "width": width, "max_loss_per": round((width - credit) * 100, 2),
            "half_spread": round((short["spread"] + long["spread"]) / 2, 3),
            "signals": {"iv": round(iv, 3), "rv20": round(rv, 3), "iv_rv": round(ratio, 2),
                        "iv_rank": None if rank is None else round(rank), "short_delta": round(short["delta"], 2),
                        "dte": (expiry - today).days},
            "rule_thesis": f"IV {iv:.0%} vs realized {rv:.0%} (x{ratio:.2f}); sell {short['strike']:g}/{long['strike']:g} "
                           f"{side}s for {credit:.2f} credit, {(expiry - today).days} DTE",
            "score": ratio,
        })
    return out


def close_cost(ctx, pos):
    quotes = ctx["broker"].option_quotes([l["symbol"] for l in pos["legs"]])
    sq, lq = quotes.get(pos["legs"][0]["symbol"]), quotes.get(pos["legs"][1]["symbol"])
    if not sq or not lq:
        return None, None
    sm, ss = mid(sq)
    lm, ls = mid(lq)
    if sm is None or lm is None:
        return None, None
    return round(sm - lm, 2), round((ss + ls) / 2, 3)


def manage(ctx, pos):
    p = ctx["params"]["volsell"]
    cost, half = close_cost(ctx, pos)
    dte = (date.fromisoformat(pos["expiry"]) - store.now_et().date()).days
    if cost is not None:
        pos["mark"] = cost
        pos["exit_half_spread"] = half
        if cost <= pos["credit"] * (1 - p["take_profit"]):
            return f"profit target ({p['take_profit']:.0%} of credit)"
        if cost >= pos["credit"] * (1 + p["stop_mult"]):
            return "loss stop"
    if dte <= p["exit_dte"]:
        return f"time exit ({dte} DTE)"
    return None
