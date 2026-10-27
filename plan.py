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
import focus
import universe
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
def scan(aid, bars, params, b=None, agent_for_scan=None):
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
        for r in out:
            r["sector"] = universe.sector_of(r["symbol"])
        f = focus.current(agent_for_scan) if agent_for_scan else None
        ranked = sorted(out, key=key)
        if f:   # keep the focus sectors visible in the scan, drop avoided ones
            ranked = [r for r in ranked if r["sector"] in f.get("lean_in", [])][:8] + \
                     [r for r in ranked if r["sector"] not in f.get("lean_in", []) + f.get("avoid", [])]
        return ranked[:20]
    if aid == "scalper":          # liquid stocks that move a lot in a day: a scalper's hunting ground
        for sym, df in bars.items():
            if len(df) < 25 or sym in set(config.BEAR_SYMBOLS) - {"SPY", "QQQ", "IWM", "DIA"}:
                continue
            h = enrich(df)
            last = h.iloc[-1]
            close = float(last["close"])
            dollar_vol = float((h["close"] * h["volume"]).tail(20).mean())
            if close < 10 or dollar_vol < 2e8:
                continue
            out.append({"symbol": sym, "close": round(close, 2), "atr_pct": round(float(last["atr20"]) / close * 100, 2),
                        "avg_dollar_volume_m": round(dollar_vol / 1e6), "change_today_pct": round(float(h["close"].pct_change().iloc[-1]) * 100, 2),
                        "sector": universe.sector_of(sym)})
        return sorted(out, key=lambda r: -r["atr_pct"])[:20]
    if aid == "blackswan":
        import blackswan as bs
        bp = params.get("blackswan", {})
        for sym, df in bars.items():
            if len(df) < 210:
                continue
            h = enrich(df)
            last = h.iloc[-1]
            close = float(last["close"])
            lo = float(h.iloc[:-1]["low"].tail(int(bp.get("breakdown_lookback", 20))).min())
            dist = (close / lo - 1) * 100
            kind = ("sector" if sym in bs.SECTOR_ETFS else "index" if sym in bs.INDEXES else
                    "haven" if sym in config.SAFE_HAVENS else None if sym in bs.NOT_STOCKS else "stock")
            if kind == "haven":
                if close > last["sma50"] and last["roc20"] > 0:
                    out.append({"symbol": sym, "setup": "safe haven rising", "roc20_pct": round(float(last["roc20"]) * 100, 1)})
            elif kind and close < last["sma50"] and dist <= 3 and last["roc20"] < 0 and (kind != "stock" or (last["sma50"] < last["sma200"] and close >= 15)):
                out.append({"symbol": sym, "kind": kind, "setup": "broke down today" if dist <= 0 else "near breakdown (put spread)",
                            "level": round(lo, 2), "distance_pct": round(dist, 2), "roc20_pct": round(float(last["roc20"]) * 100, 1),
                            "below_200d": bool(close < last["sma200"]), "sector": universe.sector_of(sym)})
        return sorted(out, key=lambda c: (c.get("distance_pct", 99), c.get("roc20_pct", 0)))[:20]
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
            if b is not None:                       # fresh at-the-money implied vol from the options chain
                try:
                    import volsell as vs
                    now = store.now_et().date()
                    lo = (now + timedelta(days=params["volsell"].get("min_dte", 21))).isoformat()
                    hi = (now + timedelta(days=params["volsell"].get("max_dte", 45))).isoformat()
                    px = float(last["close"])
                    chain = b.option_chain(und, "put", lo, hi, round(px * .97, 2), round(px * 1.03, 2))
                    near = sorted([(abs(vs.parse_occ(k)["strike"] - px), v.get("impliedVolatility")) for k, v in chain.items()
                                   if vs.parse_occ(k) and v.get("impliedVolatility")])[:4]
                    if near:
                        ivs = vs.record_iv(und, sum(x[1] for x in near) / len(near))
                except Exception as e:  # noqa: BLE001
                    print(f"[plan:volsell] IV lookup failed for {und}: {type(e).__name__}")
            side = "bull put spreads" if last["close"] > last["sma50"] else (
                "bear call spreads" if last["close"] < last["sma200"] else "stand aside (no clear trend)")
            out.append({"symbol": und, "trend_side": side, "realized_vol20": round(rv, 3),
                        "last_iv": ivs[-1] if ivs else None, "iv_vs_rv": round(ivs[-1] / rv, 2) if ivs and rv else None,
                        "close": round(float(last["close"]), 2)})
    return out


PLAN_SYSTEM = """You are the planning team (scout + strategist) of {name}, one of {n_firms} AI trading firms in a
paper-trading race: each started with ${start:,.0f}, first to ${goal:,.0f} wins, below $10 is eliminated.
Your specialty: {style}
Allowed instruments: {instruments}. Rules you cannot change: cash only (no margin, never more than
available settled cash; sale proceeds settle the next trading day), no naked options. No daily trade limit or position cap: plan as many trades as the
evidence supports, and note that selling today frees cash for tomorrow.

This is the "{session}" planning session while the market is closed. You get your current plan (if any),
today's results, your open positions, a fresh scan of finished daily candles, and recent news.
Think hard, then decide whether to KEEP, REVISE or SCRAP your plan. Changing your mind is expected
whenever the new information is better; never keep a plan just because you made it. If nothing is worth
trading, say so: an empty watchlist is a valid plan.

Sector focus: the scan covers roughly 500 liquid stocks, each tagged with the sector it trades like.
You can lean into up to 3 sectors (your setups there get looked at first) and avoid others. Lean in
only where your own results ("my_results_by_sector") or today's scan give real evidence it's working;
back out when it stops paying. A focus expires after 10 trading days unless you renew it.
Sector names must come from: {sectors}.

The "crash_radar" (0-100 market stress: calm under 40, warning 40-70, stress 70+) is shared by every firm.

Reply with ONLY a JSON object:
{{"decision":"keep|revise|scrap|new",
 "why":"one or two sentences on what changed your mind (or why the plan still holds)",
 "summary":"tomorrow's plan in 1-3 sentences",
 "watchlist":[{{"symbol":"...","setup":"...","trigger":"the price/condition that would make you act",
               "instrument":"{inst_choices}","max_dollars":<number>,"conviction":1-5}}],
 "position_plans":[{{"symbol":"...","plan":"hold / tighten stop to X / exit at open if Y"}}],
 "avoid":["symbol or situation to stay away from, with a reason"],
 "brainstorm":"one new idea or tweak to test, or empty",
 "sector_focus":{{"lean_in":["sector", "..."],"avoid":["sector"],"why":"the evidence for this focus"}}}}"""


def session_for(aid, session, state, scan_rows, news, prices, b):
    a = config.AGENTS[aid]
    agent = state["agents"][aid]
    current = agent.get("plan") or {}
    journal = store.load("journal.json", [])
    day = store.today()
    view = {
        "session": SESSIONS[session], "time_et": store.now_et().strftime("%a %Y-%m-%d %H:%M"),
        "balance": {"cash": agent["cash"], "available_settled": __import__("governor").available(agent),
                    "settling_next_day": store.unsettled(agent)},
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
        "my_plan_scorecard": scorecard_summary(agent),
        "my_results_by_sector": focus.sector_stats(aid),
        "current_sector_focus": focus.current(agent),
        "crash_radar": {k: (__import__("radar").current() or {}).get(k) for k in ("score", "tier", "parts", "facts", "economy")},
    }
    if not config.ANTHROPIC_API_KEY:
        d = {"decision": "new" if not current else "keep", "why": "Rules-only mode: plan is the top scan results.",
             "summary": "Watch the strongest scan results.",
             "watchlist": [{"symbol": r["symbol"], "setup": r.get("setup", r.get("trend_side", "")), "trigger": "scanner signal",
                            "instrument": "spread" if aid == "volsell" else "debit_spread" if aid == "blackswan" else "stock",
                            "max_dollars": 40, "conviction": 2}
                           for r in scan_rows[:3] if r.get("symbol")],
             "position_plans": [], "avoid": [], "brainstorm": ""}
    else:
        system = PLAN_SYSTEM.format(name=a["name"], goal=config.RACE["goal"], start=config.RACE["start_balance"],
                                    sectors=", ".join(universe.SECTORS), style=a["style"],
                                    instruments=", ".join(a["instruments"]), session=SESSIONS[session],
                                    inst_choices="|".join(a["instruments"]),
                                    n_firms=sum(1 for x in config.AGENTS.values() if x["competing"]))
        d, err = None, None
        for attempt in range(2):                # one retry: long answers occasionally get cut off
            try:
                d = brain._call(config.PLAN_MODEL, system, json.dumps(view, default=str), 3500, 150)
                break
            except Exception as e:  # noqa: BLE001
                err = type(e).__name__
        if d is None:                           # keep the old plan if the session fails
            print(f"[plan:{aid}] session failed: {err}")
            store.journal({"type": "plan_error", "agent": aid, "note": f"{SESSIONS[session]} failed ({err}); kept the previous plan"})
            return None
    d["watchlist"] = [w for w in (d.get("watchlist") or []) if isinstance(w, dict) and w.get("symbol")][:8]
    # Clamp every planned dollar amount to the settled cash the firm actually has right now.
    # Plans were being written against stale budgets (e.g. $186.88 planned vs $17.13 settled),
    # which wasted runs on orders the governor then had to shrink or refuse.
    settled = max(0.0, float(view["balance"]["available_settled"] or 0))
    clamped = 0
    for w in d["watchlist"]:
        w["symbol"] = str(w["symbol"]).upper().strip()[:12]
        try:
            md = float(w.get("max_dollars")) if w.get("max_dollars") is not None else None
        except (TypeError, ValueError):
            md = None
        if md is None or md > settled:
            w["max_dollars"] = round(settled, 2)
            clamped += md is not None and md > settled
    revisions = list(current.get("revisions", [])) if current.get("for_day") == next_trading_day(store.now_et()) else []
    revisions.append({"at": store.now_et().isoformat(timespec="minutes"), "session": SESSIONS[session],
                      "decision": d.get("decision", "revise"), "why": str(d.get("why", ""))[:400],
                      "watch": [{"symbol": w["symbol"], "instrument": w.get("instrument", "stock"), "setup": str(w.get("setup", ""))[:80]}
                                for w in d["watchlist"]]})
    plan = {"for_day": next_trading_day(store.now_et()), "made_at": store.now_et().isoformat(timespec="minutes"),
            "session": SESSIONS[session], "decision": d.get("decision", "revise"), "why": str(d.get("why", ""))[:400],
            "summary": str(d.get("summary", ""))[:600], "watchlist": d["watchlist"],
            "position_plans": (d.get("position_plans") or [])[:6], "avoid": (d.get("avoid") or [])[:6],
            "brainstorm": str(d.get("brainstorm", ""))[:300], "revisions": revisions[-6:],
            "_sector_focus": d.get("sector_focus"),
            "settled_cash_at_plan": round(settled, 2), "budgets_clamped": int(clamped)}
    return plan


CAL = []   # trading calendar loaded from Alpaca: [{"date", "open", "close"}]


def _hours(hhmm):
    h, m = hhmm.split(":")[:2]
    return int(h) + int(m) / 60


def next_trading_day(now):
    days = {c["date"]: c for c in CAL}
    today = now.date().isoformat()
    if days:
        if today in days and now.hour + now.minute / 60 < _hours(days[today]["close"]):
            return today
        later = sorted(d for d in days if d > today)
        if later:
            return later[0]
    d = now.date() if now.hour < 16 else now.date() + timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d.isoformat()


def pick_session_cal(now):
    """Holiday- and early-close-aware version of pick_session."""
    if not CAL:
        return pick_session(now)
    days = {c["date"]: c for c in CAL}
    today, h = now.date().isoformat(), now.hour + now.minute / 60
    tomorrow = (now.date() + timedelta(days=1)).isoformat()
    if today in days and _hours(days[today]["close"]) <= h < 18.5:
        return "close"
    if 18.5 <= h < 23.9 and tomorrow in days:
        return "evening"
    if 6 <= h < 9.4 and today in days:
        return "premarket"
    return None


def grade(aid, agent, bars):
    """After the close: score the plan made for today. Did the picks move the planned way,
    did the firm act on them, and did changing its mind overnight help?"""
    plan = agent.get("plan") or {}
    today = store.today()
    if plan.get("for_day") != today or any(s.get("day") == today for s in agent.get("scorecard", [])):
        return None

    def move(sym, inst, setup=""):
        df = bars.get(sym)
        if df is None or len(df) < 2:
            return None
        if inst == "spread":   # bull put spreads want the underlying up, bear call spreads want it down
            inst = "put" if "call" in str(setup).lower() else "stock"
        if inst == "debit_spread":
            inst = "put"
        last_day = str(df.index[-1].tz_convert("America/New_York").date()) if df.index.tz is not None else str(df.index[-1].date())
        if last_day != today:
            return None
        r = float(df["close"].iloc[-1] / df["close"].iloc[-2] - 1) * 100
        return round(-r if inst == "put" else r, 2)

    picks = [{"symbol": w["symbol"], "instrument": w.get("instrument", "stock"),
              "move_pct": move(w["symbol"], w.get("instrument", "stock"), w.get("setup", ""))}
             for w in plan.get("watchlist", [])]
    revs = plan.get("revisions", [])
    first = revs[0].get("watch", []) if revs else []
    final_syms = {p["symbol"] for p in picks}
    dropped = [{"symbol": w["symbol"], "move_pct": move(w["symbol"], w.get("instrument", "stock"), w.get("setup", ""))}
               for w in first if isinstance(w, dict) and w["symbol"] not in final_syms]
    opened = {j.get("symbol") for j in store.load("journal.json", [])
              if j.get("agent") == aid and j.get("type") == "open" and j.get("time", "").startswith(today)}
    held = {p.get("underlying", p["symbol"]) for p in agent["positions"].values()}
    acted = [p["symbol"] for p in picks if p["symbol"] in opened or p["symbol"] in held]
    scored = [p["move_pct"] for p in picks if p["move_pct"] is not None]
    dscored = [d["move_pct"] for d in dropped if d["move_pct"] is not None]
    avg = round(sum(scored) / len(scored), 2) if scored else None
    davg = round(sum(dscored) / len(dscored), 2) if dscored else None
    entry = {"day": today, "picks": picks, "acted": acted, "avg_move_pct": avg,
             "right_way": sum(1 for m in scored if m > 0), "scored": len(scored),
             "dropped": dropped, "dropped_avg_move_pct": davg,
             "mind_change_value": round(avg - davg, 2) if avg is not None and davg is not None else None,
             "changed_mind": any(r.get("decision") in ("revise", "scrap") for r in revs[1:])}
    agent["scorecard"] = (agent.get("scorecard", []) + [entry])[-30:]
    return entry


def scorecard_summary(agent):
    sc = agent.get("scorecard", [])
    if not sc:
        return None
    picks = sum(s["scored"] for s in sc)
    right = sum(s["right_way"] for s in sc)
    moves = [s["avg_move_pct"] for s in sc if s["avg_move_pct"] is not None]
    mind = [s["mind_change_value"] for s in sc if s.get("mind_change_value") is not None]
    planned = sum(len(s["picks"]) for s in sc)
    return {"days": len(sc), "picks_scored": picks, "right_way": right,
            "hit_rate_pct": round(100 * right / picks) if picks else None,
            "avg_pick_move_pct": round(sum(moves) / len(moves), 2) if moves else None,
            "acted_on_pct": round(100 * sum(len(s["acted"]) for s in sc) / planned) if planned else None,
            "mind_changes_scored": len(mind), "mind_changes_helped": sum(1 for m in mind if m > 0),
            "last": sc[-1]}


def main(session=None, force=False, only=None):
    now = store.now_et()
    checks = health.run_checks()
    if not all(c["ok"] for c in checks if c["name"] in ("GitHub secrets", "Alpaca paper account")):
        raise SystemExit("Stopped: setup problem, see data/health.json")
    b = Broker()
    global CAL
    try:
        CAL = [{"date": c["date"], "open": c["open"], "close": c["close"]}
               for c in b.calendar((now.date() - timedelta(days=3)).isoformat(), (now.date() + timedelta(days=21)).isoformat())]
        store.save("calendar.json", CAL)
    except Exception as e:  # noqa: BLE001 - fall back to weekdays
        print(f"[plan] calendar unavailable: {type(e).__name__}")
    session = session or pick_session_cal(now)
    if not session:
        print("No planning session at this hour.")
        return
    if b.clock().get("is_open") and not force:
        print("Market is open; planning sessions only run while it's closed.")
        return
    state = store.load_state()
    if not state or state["race"]["status"] != "running":
        print("No running race.")
        return
    before = set(state["agents"])
    store.add_missing_agents(state)      # a firm added mid-race starts planning right away
    joined = set(state["agents"]) - before
    for_day = next_trading_day(now)
    params = store.load_params()

    if universe.is_stale():
        try:
            universe.build_weekly(b)
        except Exception as e:  # noqa: BLE001 - keep last week's list
            print(f"[plan] universe rebuild failed: {type(e).__name__}")
    import blackswan as bs
    symbols = list(dict.fromkeys(["SPY"] + config.VOL_UNDERLYINGS + config.BEAR_SYMBOLS + config.SAFE_HAVENS
                                 + bs.SECTOR_ETFS + universe.candidates(b, state)))
    bars = {}
    for i in range(0, len(symbols), 50):
        bars.update(b.daily_bars(symbols[i:i + 50]))
    try:
        prices = b.latest_prices(symbols)
    except Exception:  # noqa: BLE001
        prices = {}
    since = (now - timedelta(hours=20)).isoformat(timespec="seconds")
    if session == "close" and not any(r.get("date") == store.today() for r in store.load("recaps.json", [])):
        try:                        # the day's recap, written once after the close
            import recap
            recap.main()
        except Exception as e:  # noqa: BLE001
            print(f"[plan] recap failed: {type(e).__name__}")
    if session == "close":          # finished daily candles: refresh the Crash Radar for tonight's planners
        try:
            import radar
            radar.compute(bars, {s_: float(df["close"].iloc[-1]) for s_, df in bars.items() if len(df)})
        except Exception as e:  # noqa: BLE001
            print(f"[plan] radar failed: {type(e).__name__}")

    changed = False
    for aid, agent in state["agents"].items():
        if not config.AGENTS[aid]["competing"] or agent["status"] != "active" or (only and aid != only):
            continue
        cur = agent.get("plan") or {}
        if not force and cur.get("for_day") == for_day and any(r["session"] == SESSIONS[session] for r in cur.get("revisions", [])):
            print(f"[plan:{aid}] {SESSIONS[session]} already done for {for_day}")
            continue
        if session == "close":
            g = grade(aid, agent, bars)
            if g:
                changed = True
                store.journal({"type": "scorecard", "agent": aid,
                               "note": f"Plan graded: {g['right_way']} of {g['scored']} picks moved the planned way"
                                       + (f", avg {g['avg_move_pct']:+.2f}%" if g['avg_move_pct'] is not None else "")})
        rows = scan(aid, bars, params, b, agent)
        watch = [r["symbol"] for r in rows if r.get("symbol")][:12] + [w["symbol"] for w in cur.get("watchlist", [])] + \
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
        store.ledger({"type": "plan", "agent": aid, "session": plan["session"], "for_day": plan["for_day"],
                      "decision": plan["decision"], "why": plan["why"], "summary": plan["summary"],
                      "watchlist": [{k: w.get(k) for k in ("symbol", "setup", "trigger", "instrument", "conviction")}
                                    for w in plan["watchlist"]]})
        nf = focus.apply(aid, agent, plan.pop("_sector_focus", None), plan["why"])
        if nf:
            store.ledger({"type": "focus", "agent": aid, "lean_in": nf["lean_in"], "avoid": nf["avoid"],
                          "why": nf["why"], "expires": nf["expires"]})
            store.journal({"type": "focus", "agent": aid, "note": f"Leaning into {', '.join(nf['lean_in']) or 'no sector'}"
                           + (f"; avoiding {', '.join(nf['avoid'])}" if nf['avoid'] else "") + f". {nf['why']}"})
        store.journal({"type": "plan", "agent": aid, "note": f"{plan['session']}: {plan['decision']}. {plan['why']}",
                       "symbol": ", ".join(w["symbol"] for w in plan["watchlist"][:4])})
    if joined:                           # show the new firm on the dashboard tonight
        import run
        run.write_dashboard(state, store.load("dashboard.json", {}).get("regime", {}),
                            {aid: run.agent_equity(a, prices) for aid, a in state["agents"].items()})
        changed = True
    if changed:
        store.save("state.json", state)
        dash = store.load("dashboard.json", {})
        if dash:
            dash["plans"] = {aid: a.get("plan") for aid, a in state["agents"].items() if a.get("plan")}
            dash["scorecards"] = {aid: scorecard_summary(a) for aid, a in state["agents"].items() if a.get("scorecard")}
            dash["calendar"] = CAL
            dash["focus"] = {aid: focus.current(a) for aid, a in state["agents"].items() if focus.current(a)}
            dash["sector_stats"] = {aid: focus.sector_stats(aid) for aid in state["agents"]}
            dash["journal"] = store.load("journal.json", [])[-60:][::-1]
            dash["plans_updated"] = store.now_et().isoformat(timespec="minutes")
            store.save("dashboard.json", dash)
    print(f"{SESSIONS[session]} done for {for_day}.")


if __name__ == "__main__":
    args = sys.argv[1:]
    sess = args[args.index("--session") + 1] if "--session" in args else None
    import alerts
    try:
        main(sess, force="--force" in args, only=args[args.index("--agent") + 1] if "--agent" in args else None)
        alerts.flush("Agent race: game plan")
    except SystemExit as e:
        if e.code not in (None, 0):
            health.record_crash(RuntimeError(str(e.code)))
            alerts.setup_problem(e.code)
            print(e.code)
            sys.exit(1)
    except Exception as e:
        health.record_crash(e)
        raise
