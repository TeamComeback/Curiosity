"""Rapid Fire Trading: the intraday scalper.

Two speeds:
  every 30 min  the AI strategist sets the playbook: which stocks to watch, how aggressive to be,
                how many cash slices, and its own target / stop / time limit (inside PARAM_BOUNDS)
  every 2 min   code executes it: reads 1-minute bars, manages open scalps, and takes the best
                setup on the watch list when a settled-cash slice is free

Setups (long shares only; cash account, no shorting):
  opening-range breakout   price clears the first 15 minutes' high on heavy volume, above VWAP
  VWAP pullback bounce     an uptrending stock dips to VWAP and turns back up
  momentum burst           a +0.4% move in 5 minutes on 2x volume, not stretched far above VWAP
Exits: target, stop, breakeven once a good gain fades, losing VWAP, a time limit, and flat by 3:50 PM.

Settlement: sale proceeds settle the next trading day, so the day's cash is split into slices and
each slice can make one round trip a day. That is the honest limit on a cash-account scalper."""
import json
import math
from datetime import datetime, time as dtime, timedelta

import numpy as np

import config
import governor
import store
import universe

ET = "America/New_York"
DEFAULT_WATCH = ["SPY", "QQQ", "IWM"]
AGGRESSION = {"stand_down": 0.0, "defensive": 0.5, "normal": 1.0, "aggressive": 1.0}
FIRST_ENTRY, NO_NEW_AFTER, FLAT_BY = "09:45", "15:30", "15:50"


# ---------------- playbook + parameters ----------------
def playbook(state):
    pb = state["agents"]["scalper"].get("playbook") or {}
    return pb if pb.get("date") == store.today() else {}


def params(state):
    p = dict(store.load_params()["scalper"])
    pb = playbook(state)
    for k in ("target_pct", "stop_pct", "max_hold_min", "slices"):
        if pb.get(k) is not None:
            try:
                lo, hi = config.PARAM_BOUNDS["scalper"][k]
                p[k] = type(p[k])(min(max(float(pb[k]), lo), hi))
            except (TypeError, ValueError):
                pass
    return p


def watch_list(state):
    pb = playbook(state)
    w = [s for s in (pb.get("watch") or []) if isinstance(s, str)][:8]
    return w or DEFAULT_WATCH


# ---------------- intraday math ----------------
def session(df):
    if df is None or not len(df):
        return None
    idx = df.index.tz_convert(ET)
    keep = (idx.date == store.now_et().date()) & (idx.time >= dtime(9, 30))
    d = df[keep]
    return d if len(d) else None


def intraday(d):
    tp = (d["high"] + d["low"] + d["close"]) / 3
    vol = d["volume"].replace(0, np.nan)
    vwap = (tp * d["volume"]).cumsum() / vol.cumsum()
    return {"vwap": vwap.ffill(), "ema9": d["close"].ewm(span=9, adjust=False).mean(),
            "ema21": d["close"].ewm(span=21, adjust=False).mean(),
            "avgvol": d["volume"].rolling(20, min_periods=10).mean(), "orb_high": float(d["high"].iloc[:15].max())}


def find_setup(sym, d, price, p, defensive=False):
    if d is None or len(d) < 20 or not price:
        return None
    ind = intraday(d)
    v = float(ind["vwap"].iloc[-1])
    if not v or math.isnan(v):
        return None
    ext = (price / v - 1) * 100
    if price < v or ext > p["max_extension_pct"]:
        return None
    avg = float(ind["avgvol"].iloc[-1] or 0)
    relv = float(d["volume"].iloc[-3:].mean()) / avg if avg else 0
    up = ind["ema9"].iloc[-1] > ind["ema21"].iloc[-1]
    found = None
    if up and float(d["low"].iloc[-4:].min()) <= v * 1.0015 and price > float(d["high"].iloc[-2]):
        found = ("VWAP pullback bounce", 2 + relv)
    elif not defensive and price > ind["orb_high"] and float(d["close"].iloc[-4:-1].max()) <= ind["orb_high"] * 1.001 \
            and relv >= p["min_rel_volume"]:
        found = ("opening-range breakout", 3 + relv)
    elif not defensive and len(d) > 6 and price / float(d["close"].iloc[-6]) - 1 >= 0.004 and relv >= max(2.0, p["min_rel_volume"]):
        found = ("momentum burst", 1 + relv)
    if not found:
        return None
    return {"symbol": sym, "setup": found[0], "score": found[1], "price": price, "vwap": round(v, 4),
            "rel_volume": round(relv, 2), "vs_vwap_pct": round(ext, 2)}


def exit_reason(pos, price, vwap, p, now):
    hhmm = now.strftime("%H:%M")
    entry = pos["entry"]
    gain = (price / entry - 1) * 100
    pos["peak_gain"] = max(pos.get("peak_gain", gain), gain)
    held = (now - datetime.fromisoformat(pos["opened"])).total_seconds() / 60
    target, stop = pos.get("target_pct", p["target_pct"]), pos.get("stop_pct", p["stop_pct"])
    if hhmm >= FLAT_BY:
        return f"flat before the close ({gain:+.2f}%)"
    if gain >= target:
        return f"target hit ({gain:+.2f}%)"
    if gain <= -stop:
        return f"stop ({gain:+.2f}%)"
    if pos["peak_gain"] >= target * 0.6 and gain <= 0.05:
        return f"gave back a {pos['peak_gain']:+.2f}% gain: out at breakeven"
    if vwap and held >= 4 and price < vwap * 0.998:
        return f"lost VWAP: setup failed ({gain:+.2f}%)"
    if held >= pos.get("max_hold_min", p["max_hold_min"]):
        return f"time limit, {held:.0f} min ({gain:+.2f}%)"
    return None


# ---------------- 30-minute cycle hooks (run.py) ----------------
def entries(ctx):
    return []          # the scalper enters from the 2-minute loop, not the 30-minute cycle


def manage(ctx, pos):
    """Backup exits on the 30-minute cycle (the 2-minute loop normally handles them)."""
    price = ctx["prices"].get(pos["symbol"])
    if price is None:
        return None
    return exit_reason(pos, price, None, params(ctx["state"]), store.now_et())


PLAYBOOK_SYSTEM = """You are the strategist of Rapid Fire Trading, an intraday scalper in a paper-trading race
({n_firms} AI firms, each started with ${start:,.0f}; first to ${goal:,.0f} wins). You don't place trades
yourself: code checks 1-minute charts every 2 minutes and executes YOUR playbook. Every 30 minutes you
set it.

Setups the code trades (long shares only): opening-range breakouts on heavy volume, pullbacks to VWAP in
an uptrending stock, and volume-backed momentum bursts. Exits: your target, your stop, breakeven if a gain
fades, losing VWAP, your time limit, and always flat by 3:50 PM.

Cash account: sale proceeds settle the next trading day. The day's cash is split into "slices"; each slice
can make one round trip a day. More slices = more, smaller trades. Fewer = bigger trades, fewer chances.

Pick up to 8 symbols from the candidates (most liquid, most active today), how aggressive to be
("stand_down" = no new trades, "defensive" = VWAP bounces only at half size, "normal", "aggressive" =
every setup at full size), and your numbers within these bounds: {bounds}.
Be a disciplined scalper: liquid names with real volume and clean trends; stand down when it's choppy.

Reply with ONLY a JSON object:
{{"watch":["SYM", "..."], "aggression":"stand_down|defensive|normal|aggressive", "slices":<int>,
 "target_pct":<number>, "stop_pct":<number>, "max_hold_min":<int>, "why":"one or two sentences"}}"""


def candidates(b, state):
    """The most liquid, most active names right now, with intraday stats for the strategist."""
    try:
        daily = universe.daily_extras(b)
    except Exception:  # noqa: BLE001
        daily = []
    core = universe.core_symbols()[:40]
    plan = [w["symbol"] for w in (state["agents"]["scalper"].get("plan") or {}).get("watchlist", [])]
    syms = list(dict.fromkeys(DEFAULT_WATCH + plan + daily[:25] + core))[:45]
    start = store.now_et().replace(hour=9, minute=30, second=0, microsecond=0).isoformat()
    bars = {}
    for i in range(0, len(syms), 25):
        bars.update(b.minute_bars(syms[i:i + 25], start))
    prices = b.latest_prices(syms)
    p = params(state)
    rows = []
    for sym in syms:
        d, px = session(bars.get(sym)), prices.get(sym)
        if d is None or len(d) < 5 or not px or px < 10:
            continue
        ind = intraday(d)
        v = float(ind["vwap"].iloc[-1])
        dollar_vol = float((d["close"] * d["volume"]).sum())
        rows.append({"symbol": sym, "price": round(px, 2), "change_today_pct": round((px / float(d["open"].iloc[0]) - 1) * 100, 2),
                     "vs_vwap_pct": round((px / v - 1) * 100, 2) if v else None,
                     "range_today_pct": round((float(d["high"].max()) / float(d["low"].min()) - 1) * 100, 2),
                     "trend_1min": "up" if ind["ema9"].iloc[-1] > ind["ema21"].iloc[-1] else "down",
                     "above_opening_range": px > ind["orb_high"], "dollar_volume_today_m": round(dollar_vol / 1e6, 1),
                     "setup_now": (find_setup(sym, d, px, p) or {}).get("setup")})
    rows.sort(key=lambda r: -r["dollar_volume_today_m"])
    return rows[:30]


def update_playbook(ctx, state, b):
    """The AI strategist's 30-minute call: set the playbook the 2-minute loop will execute."""
    import brain
    agent = state["agents"]["scalper"]
    try:
        cands = candidates(b, state)
    except Exception as e:  # noqa: BLE001
        print(f"[scalper] candidates failed: {type(e).__name__}")
        cands = []
    journal = store.load("journal.json", [])
    today = [j for j in journal if j.get("agent") == "scalper" and j.get("time", "").startswith(store.today())
             and j.get("type") in ("open", "close", "rule_block")]
    view = {"time_et": store.now_et().strftime("%H:%M"), "equity": ctx.get("equity"),
            "settled_cash": governor.available(agent), "settling_until_tomorrow": store.unsettled(agent),
            "open_scalps": [{k: p.get(k) for k in ("symbol", "entry", "opened", "setup")} for p in agent["positions"].values()],
            "today": today[-20:], "current_playbook": playbook(state) or None, "candidates": cands,
            "overnight_plan": {k: (agent.get("plan") or {}).get(k) for k in ("summary", "watchlist")},
            "params_now": params(state), "regime": ctx.get("regime"),
            "crash_radar": {k: (ctx.get("radar") or {}).get(k) for k in ("score", "tier")}}
    old = playbook(state)
    if config.ANTHROPIC_API_KEY:
        system = PLAYBOOK_SYSTEM.format(n_firms=sum(1 for x in config.AGENTS.values() if x["competing"]),
                                        start=config.RACE["start_balance"], goal=config.RACE["goal"],
                                        bounds=json.dumps(config.PARAM_BOUNDS["scalper"]))
        try:
            d = brain._call(config.BRAIN_MODEL, system, json.dumps(view, default=str), 900, 90)
        except Exception as e:  # noqa: BLE001 - keep the old playbook (or defaults) on failure
            print(f"[scalper] playbook error: {type(e).__name__}")
            return
    else:
        d = {"watch": [c["symbol"] for c in cands[:6]] or DEFAULT_WATCH, "aggression": "normal",
             "why": "Rules-only mode: the most liquid names today."}
    valid = {c["symbol"] for c in cands} | set(DEFAULT_WATCH)
    watch = [str(s).upper() for s in (d.get("watch") or []) if str(s).upper() in valid][:8]
    pb = {"date": store.today(), "made_at": store.now_et().isoformat(timespec="minutes"),
          "watch": watch or DEFAULT_WATCH, "aggression": d.get("aggression") if d.get("aggression") in AGGRESSION else "normal",
          "why": str(d.get("why", ""))[:300]}
    for k in ("slices", "target_pct", "stop_pct", "max_hold_min"):
        if d.get(k) is not None:
            pb[k] = d[k]
    agent["playbook"] = pb
    if (sorted(old.get("watch") or []), old.get("aggression")) != (sorted(pb["watch"]), pb["aggression"]):
        store.ledger({"type": "playbook", "agent": "scalper", "watch": pb["watch"], "aggression": pb["aggression"],
                      "why": pb["why"]})
        store.journal({"type": "playbook", "agent": "scalper", "symbol": ", ".join(pb["watch"][:5]),
                       "note": f"{pb['aggression']}: {pb['why']}"})


# ---------------- the 2-minute loop (scalp.py) ----------------
def tick(b, state):
    """Manage open scalps and take at most one new one. Returns True if anything traded."""
    import run
    agent = state["agents"]["scalper"]
    if agent["status"] != "active" or state["race"]["status"] != "running":
        return False
    now = store.now_et()
    hhmm = now.strftime("%H:%M")
    p, pb = params(state), playbook(state)
    held = [x["symbol"] for x in agent["positions"].values()]
    syms = list(dict.fromkeys(watch_list(state) + held))
    start = now.replace(hour=9, minute=30, second=0, microsecond=0).isoformat()
    bars = b.minute_bars(syms, start)
    prices = b.latest_prices(syms)
    traded = False
    day = agent.get("scalp_day") or {}
    if day.get("date") != store.today():
        eq = agent["cash"] + sum(prices.get(x["symbol"], x["entry"]) * x["qty"] for x in agent["positions"].values())
        agent["scalp_day"] = day = {"date": store.today(), "equity": round(eq, 2)}

    for pid, pos in list(agent["positions"].items()):
        price = prices.get(pos["symbol"])
        if price is None:
            continue
        d = session(bars.get(pos["symbol"]))
        vwap = float(intraday(d)["vwap"].iloc[-1]) if d is not None and len(d) >= 5 else None
        pos["last"] = price
        why = exit_reason(pos, price, vwap, p, now)
        if why:
            run.close_trade(b, state, "scalper", pid, why)
            traded = True

    mult = AGGRESSION.get(pb.get("aggression", "normal"), 1.0)
    if hhmm < FIRST_ENTRY or hhmm > NO_NEW_AFTER or mult == 0 or not governor.can_trade_today(state, "scalper")[0]:
        return traded
    slice_dollars = round(day["equity"] / max(int(p["slices"]), 1) * mult, 2)
    avail = governor.available(agent)
    if avail < max(slice_dollars * 0.5, 20):
        return traded                      # every slice used today: wait for settlement
    found = [f for f in (find_setup(s, session(bars.get(s)), prices.get(s), p, defensive=pb.get("aggression") == "defensive")
                         for s in watch_list(state) if s not in held) if f]
    if not found:
        return traded
    best = max(found, key=lambda f: f["score"])
    cand = {"symbol": best["symbol"], "price": best["price"], "direction": "long", "atr": 0, "play": "scalp",
            "stop": round(best["price"] * (1 - p["stop_pct"] / 100), 2),
            "rule_thesis": f"{best['setup']} on {best['symbol']}: {best['rel_volume']}x volume, "
                           f"{best['vs_vwap_pct']:+.2f}% vs VWAP; target +{p['target_pct']}%, stop -{p['stop_pct']}%"}
    before = set(agent["positions"])
    run.open_trade(b, state, "scalper", cand, {"instrument": "stock", "dollars": min(slice_dollars, avail),
                                               "thesis": cand["rule_thesis"], "confidence": 3}, {"params": store.load_params()})
    for pid in set(agent["positions"]) - before:
        agent["positions"][pid].update({"setup": best["setup"], "target_pct": p["target_pct"], "stop_pct": p["stop_pct"],
                                        "max_hold_min": p["max_hold_min"]})
        traded = True
    return traded
