"""Overnight game-plan sessions.

Each active agent's scout and strategist meet when the market is closed:
  after the close  (~4:40 PM ET)  review the day, scan finished daily candles, write tomorrow's plan
  evening          (~8:10 PM ET)  check after-hours news; keep, revise or scrap the plan
  pre-market       (~7:40-8:40 AM ET)  check pre-market prices and overnight news; final call
Every session sees the current plan and must say whether it keeps it or changes its mind,
so a company never gets locked into a stale idea. Plans only guide the next day's
decisions: they can't place trades and can't change any rule.
Usage: python plan.py [--session close|evening|premarket] [--force]"""
import json
import sys
from datetime import timedelta

import brain
import config
import health
import store
from broker import Broker
from indicators import enrich, realized_vol, rsi

SESSIONS = {"close": "After-close review", "evening": "Evening check-in", "premarket": "Pre-market call"}


def pick_session(now):
    """Which session fits the current US/Eastern time, or None."""
    h = now.hour + now.minute / 60
    wd = now.weekday()  # Mon=0
    if 16 <= h < 18.5 and wd <= 4:
        return "close"
    if 18.5 <= h < 23.9 and wd in (6, 0, 1, 2, 3):      # Sun-Thu evenings
        return "evening"
    if 6 <= h < 9.4 and wd <= 4:
        return "premarket"
    return None


# ---------- closing-data scans (what the scout brings to the meeting) ----------
def scan(aid, bars, params):
    p = params.get(aid, {})
    out = []
    if aid in ("trend", "meanrev"):
        for sym, df in bars.items():
            if len(df) < 210:
                continue
            h = enrich(df)
            last, prev = h.iloc[-1], h.iloc[:-1]
            close = float(last["close"])
            if aid == "trend":
                lb = int(p.get("breakout_lookback", 55))
                hi = float(prev["high"].tail(lb).max())
                lo = float(prev["low"].tail(lb).min())
                up = last["sma50"] > last["sma200"] and close > last["sma50"]
                down = last["sma50"] < last["sma200"] and close < last["sma50"]
                dist_hi = (hi / close - 1) * 100
                dist_lo = (close / lo - 1) * 100
                if up and dist_hi <= 3:
                    out.append({"symbol": sym, "setup": "near breakout" if dist_hi > 0 else "broke out today",
                                "level": round(hi, 2), "distance_pct": round(dist_hi, 2), "atr": round(float(last["atr20"]), 2),
                                "roc20_pct": round(float(last["roc20"]) * 100, 1)})
                elif down and dist_lo <= 3:
                    out.append({"symbol": sym, "setup": "near breakdown (puts)" if dist_lo > 0 else "broke down today",
                                "level": round(lo, 2), "distance_pct": round(dist_lo, 2), "atr": round(float(last["atr20"]), 2),
                                "roc20_pct": round(float(last["roc20"]) * 100, 1)})
            else:
                r2 = float(rsi(h["close"], 2).iloc[-1])
                if close > last["sma200"] and r2 < p.get("rsi_entry", 10) + 15:
                    out.append({"symbol": sym, "setup": "washout now" if r2 < p.get("rsi_entry", 10) else "getting oversold",
                                "rsi2": round(r2, 1), "close": round(close, 2), "sma5": round(float(last["sma5"]), 2),
                                "pct_above_sma200": round((close / last["sma200"] - 1) * 100, 1),
                                "atr": round(float(last["atr20"]), 2)})
        key = (lambda c: c["distance_pct"]) if aid == "trend" else (lambda c: c["rsi2"])
        return sorted(out, key=key)[:12]
    if aid == "volsell":
        ivh = store.load("iv_history.json", {})
        for und in config.VOL_UNDERLYINGS:
            df = bars.get(und)
            if df is None or len(df) < 210:
                continue
            h = enrich(df)
            last = h.iloc[-1]
            rv = realized_vol(h["close"], 20)
            ivs = [r["iv"] for r in ivh.get(und, [])]
            side = "bull put spreads" if last["close"] > last["sma50"] else (
                "bear call spreads" if last["close"] < last["sma200"] else "stand aside (no clear trend)")
            out.append({"symbol": und, "trend_side": side, "realized_vol20": round(rv, 3),
                        "last_iv": ivs[-1] if ivs else None, "iv_vs_rv": round(ivs[-1] / rv, 2) if ivs and rv else None,
                        "close": round(float(last["close"]), 2)})
    return out


PLAN_SYSTEM = """You are the planning team (scout + strategist) of {name}, one of three AI trading firms in a
paper-trading race: each started with $100, first to ${goal:,.0f} wins, below $10 is eliminated.
Your specialty: {style}
Allowed instruments: {instruments}. Rules you cannot change: cash only, no naked options, max {max_trades}
new trades per day, max {max_pos} open positions.

This is the "{session}" planning session while the market is closed. You get your current plan (if any),
today's results, your open positions, a fresh scan of finished daily candles, and recent news.
Think hard, then decide whether to KEEP, REVISE or SCRAP your plan. Changing your mind is expected
whenever the new information is better; never keep a plan just because you made it. If nothing is worth
trading, say so: an empty watchlist is a valid plan.

Reply with ONLY a JSON object:
{{"decision":"keep|revise|scrap|new",
 "why":"one or two sentences on what changed your mind (or why the plan still holds)",
 "summary":"tomorrow's plan in 1-3 sentences",
 "watchlist":[{{"symbol":"...","setup":"...","trigger":"the price/condition that would make you act",
               "instrument":"stock|call|put|spread","max_dollars":<number>,"conviction":1-5}}],
 "position_plans":[{{"symbol":"...","plan":"hold / tighten stop to X / exit at open if Y"}}],
 "avoid":["symbol or situation to stay away from, with a reason"],
 "brainstorm":"one new idea or tweak to test, or empty"}}"""


def session_for(aid, session, state, scan_rows, news, prices, b):
    a = config.AGENTS[aid]
    agent = state["agents"][aid]
    current = agent.get("plan") or {}
    journal = store.load("journal.json", [])
    day = store.today()
    view = {
        "session": SESSIONS[session], "time_et": store.now_et().strftime("%a %Y-%m-%d %H:%M"),
        "balance": {"cash": agent["cash"], "available": round(agent["cash"] - agent["reserved"], 2)},
        "open_positions": [{"symbol": p["symbol"], "underlying": p.get("underlying"), "kind": p["kind"], "qty": p["qty"],
                            "entry": p["entry"], "stop": p.get("stop"), "expiry": p.get("expiry"),
                            "last_underlying_price": prices.get(p.get("underlying", p["symbol"])), "thesis": p.get("thesis")}
                           for p in agent["positions"].values()],
        "today_trades": [{k: j.get(k) for k in ("type", "symbol", "pnl", "note", "thesis")}
                         for j in journal if j.get("agent") == aid and j.get("time", "").startswith(day)
                         and j.get("type") in ("open", "close", "rule_block")][-12:],
        "fresh_scan": scan_rows,
        "news": news,
        "current_plan": {k: current.get(k) for k in ("summary", "watchlist", "position_plans", "avoid", "made_at", "session")} if current else None,
        "strategy_params": store.load_params().get(aid, {}),
        "my_notes": agent.get("notes", [])[-6:],
    }
    if not config.ANTHROPIC_API_KEY:
        d = {"decision": "new" if not current else "keep", "why": "Rules-only mode: plan is the top scan results.",
             "summary": "Watch the strongest scan results.",
             "watchlist": [{"symbol": r["symbol"], "setup": r.get("setup", r.get("trend_side", "")), "trigger": "scanner signal",
                            "instrument": "spread" if aid == "volsell" else "stock", "max_dollars": 40, "conviction": 2}
                           for r in scan_rows[:3]],
             "position_plans": [], "avoid": [], "brainstorm": ""}
    else:
        system = PLAN_SYSTEM.format(name=a["name"], goal=config.RACE["goal"], style=a["style"],
                                    instruments=", ".join(a["instruments"]), session=SESSIONS[session],
                                    max_trades=config.RULES["max_new_trades_per_day"], max_pos=config.RULES["max_open_positions"])
        try:
            d = brain._call(config.PLAN_MODEL, system, json.dumps(view, default=str), 1800, 120)
        except Exception as e:  # keep the old plan if the session fails
            print(f"[plan:{aid}] session failed: {type(e).__name__}")
            return None
    d["watchlist"] = [w for w in (d.get("watchlist") or []) if isinstance(w, dict) and w.get("symbol")][:8]
    for w in d["watchlist"]:
        w["symbol"] = str(w["symbol"]).upper().strip()[:12]
    revisions = list(current.get("revisions", [])) if current.get("for_day") == next_trading_day(store.now_et()) else []
    revisions.append({"at": store.now_et().isoformat(timespec="minutes"), "session": SESSIONS[session],
                      "decision": d.get("decision", "revise"), "why": str(d.get("why", ""))[:400],
                      "watchlist": [w["symbol"] for w in d["watchlist"]]})
    plan = {"for_day": next_trading_day(store.now_et()), "made_at": store.now_et().isoformat(timespec="minutes"),
            "session": SESSIONS[session], "decision": d.get("decision", "revise"), "why": str(d.get("why", ""))[:400],
            "summary": str(d.get("summary", ""))[:600], "watchlist": d["watchlist"],
            "position_plans": (d.get("position_plans") or [])[:6], "avoid": (d.get("avoid") or [])[:6],
            "brainstorm": str(d.get("brainstorm", ""))[:300], "revisions": revisions[-6:]}
    return plan


def next_trading_day(now):
    d = now.date() if now.hour < 16 else now.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def main(session=None, force=False):
    now = store.now_et()
    session = session or pick_session(now)
    if not session:
        print("No planning session at this hour.")
        return
    checks = health.run_checks()
    if not all(c["ok"] for c in checks if c["name"] in ("GitHub secrets", "Alpaca paper account")):
        raise SystemExit("Stopped: setup problem, see data/health.json")
    b = Broker()
    if b.clock().get("is_open") and not force:
        print("Market is open; planning sessions only run while it's closed.")
        return
    state = store.load_state()
    if not state or state["race"]["status"] != "running":
        print("No running race.")
        return
    for_day = next_trading_day(now)
    params = store.load_params()

    symbols = list(dict.fromkeys(["SPY"] + config.VOL_UNDERLYINGS + config.UNIVERSE + sorted(
        {p.get("underlying", p["symbol"]) for a in state["agents"].values() for p in a["positions"].values()})))
    bars = {}
    for i in range(0, len(symbols), 50):
        bars.update(b.daily_bars(symbols[i:i + 50]))
    try:
        prices = b.latest_prices(symbols)
    except Exception:  # noqa: BLE001
        prices = {}
    since = (now - timedelta(hours=20)).isoformat(timespec="seconds")

    changed = False
    for aid, agent in state["agents"].items():
        if not config.AGENTS[aid]["competing"] or agent["status"] != "active":
            continue
        cur = agent.get("plan") or {}
        if not force and cur.get("for_day") == for_day and any(r["session"] == SESSIONS[session] for r in cur.get("revisions", [])):
            print(f"[plan:{aid}] {SESSIONS[session]} already done for {for_day}")
            continue
        rows = scan(aid, bars, params)
        watch = [r["symbol"] for r in rows][:12] + [w["symbol"] for w in cur.get("watchlist", [])] + \
                [p.get("underlying", p["symbol"]) for p in agent["positions"].values()]
        try:
            news = b.news(list(dict.fromkeys(watch)), since) if watch else []
        except Exception:  # noqa: BLE001
            news = []
        plan = session_for(aid, session, state, rows, news[:25], prices, b)
        if not plan:
            continue
        agent["plan"] = plan
        changed = True
        store.journal({"type": "plan", "agent": aid, "note": f"{plan['session']}: {plan['decision']}. {plan['why']}",
                       "symbol": ", ".join(w["symbol"] for w in plan["watchlist"][:4])})
    if changed:
        store.save("state.json", state)
        dash = store.load("dashboard.json", {})
        if dash:
            dash["plans"] = {aid: a.get("plan") for aid, a in state["agents"].items() if a.get("plan")}
            dash["journal"] = store.load("journal.json", [])[-60:][::-1]
            dash["plans_updated"] = store.now_et().isoformat(timespec="minutes")
            store.save("dashboard.json", dash)
    print(f"{SESSIONS[session]} done for {for_day}.")


if __name__ == "__main__":
    args = sys.argv[1:]
    sess = args[args.index("--session") + 1] if "--session" in args else None
    try:
        main(sess, force="--force" in args)
    except SystemExit as e:
        if e.code not in (None, 0):
            health.record_crash(RuntimeError(str(e.code)))
            print(e.code)
            sys.exit(1)
    except Exception as e:
        health.record_crash(e)
        raise
