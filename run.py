"""One race cycle (every 30 min in market hours via GitHub Actions):
  default exits -> each agent decides (close / tighten / open) -> rulebook checks ->
  execute -> mark to market -> fail/elimination check -> finish check -> dashboard.
Usage: python run.py [--force]"""
import math
import sys
import uuid

import requests

import brain, config, governor, health, store
from broker import AlpacaError, Broker
from indicators import enrich
import baseline, meanrev, trend, volsell

REGISTRY = {"trend": trend, "meanrev": meanrev, "volsell": volsell, "baseline": baseline}
from common import mid, pick_option, split_today

SLIP = config.RULES["slippage_half_spread"]


def cid(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def in_window(aid):
    lo, hi = config.ENTRY_WINDOWS[aid]
    return lo <= store.now_et().strftime("%H:%M") <= hi


def screener_candidates():
    if not config.SCREENER_URL:
        return []
    try:
        js = requests.get(config.SCREENER_URL, params={"limit": 15}, timeout=20).json()
        return [c["ticker"].upper() for c in js.get("candidates", []) if c.get("ticker")]
    except Exception as e:
        print(f"[screener] unavailable: {e}")
        return []


def market_regime(bars, prices):
    df, price = bars.get("SPY"), prices.get("SPY")
    if df is None or price is None:
        return {"spy_trend": "unknown"}
    h = enrich(split_today(df)).iloc[-1]
    trend = "up" if price > h["sma200"] and h["sma50"] > h["sma200"] else ("down" if price < h["sma200"] else "mixed")
    return {"spy_trend": trend, "spy_vs_sma200_pct": round((price / h["sma200"] - 1) * 100, 1),
            "spy_roc20_pct": round(float(h["roc20"]) * 100, 1)}


# ---------------- valuation ----------------
def position_value(pos, prices):
    """Signed market value of a position (spreads are a liability)."""
    if pos["kind"] == "stock":
        px = prices.get(pos["symbol"], pos["entry"])
        pos["last"] = px
        return px * pos["qty"]
    if pos["kind"] == "option":
        return pos.get("mark", pos["entry"]) * 100 * pos["qty"]
    return -pos.get("mark", pos["credit"]) * 100 * pos["qty"]


def agent_equity(agent, prices):
    return round(agent["cash"] + sum(position_value(p, prices) for p in agent["positions"].values()), 2)


# ---------------- execution ----------------
def _fill(b, sub, seconds=20):
    o = b.wait_fill(sub["id"], seconds)
    return o if o.get("status") == "filled" else None


def open_trade(b, state, aid, cand, order, ctx):
    agent = state["agents"][aid]
    inst = order["instrument"]
    p = ctx["params"].get(aid, {})
    default_stop = cand.get("stop") or 0
    stop = default_stop
    agent_stop = order.get("stop")
    if agent_stop and default_stop:  # agents may tighten, never loosen past the default
        stop = max(agent_stop, default_stop) if cand.get("direction") != "short" else min(agent_stop, default_stop)

    if inst == "stock":
        dollars = round(min(float(order.get("dollars") or 0), governor.available(agent)), 2)
        ok, why = governor.check_order(state, aid, cand, inst, dollars)
        if not ok:
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"], "note": why})
        qty = math.floor(dollars / cand["price"] * 10000) / 10000
        o = _fill(b, b.submit_stock(cand["symbol"], qty, "buy", cid(aid)))
        if not o:
            return store.journal({"type": "unfilled", "agent": aid, "symbol": cand["symbol"]})
        qty, fill = float(o["filled_qty"]), float(o["filled_avg_price"])
        cost = round(qty * fill, 2)
        pos = {"kind": "stock", "symbol": cand["symbol"], "qty": qty, "entry": fill, "cost": cost}

    elif inst in ("call", "put"):
        budget = min(float(order.get("dollars") or 0), governor.available(agent))
        opt = pick_option(b, cand["symbol"], inst, cand["price"], p, budget)
        if not opt:
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"],
                                  "note": f"no {inst} affordable within ${budget:.2f}"})
        n = int(budget // (opt["ask"] * 100))
        cost_est = n * opt["ask"] * 100
        ok, why = governor.check_order(state, aid, cand, inst, cost_est)
        if not ok or n < 1:
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"], "note": why})
        o = _fill(b, b.submit_option(opt["symbol"], n, "buy", opt["ask"], "buy_to_open", cid(aid)))
        if not o:
            return store.journal({"type": "unfilled", "agent": aid, "symbol": opt["symbol"]})
        n, fill = int(float(o["filled_qty"])), float(o["filled_avg_price"])
        slip = round(opt["spread"] / 2 * 100 * n, 2) if SLIP else 0
        cost = round(fill * 100 * n + slip, 2)
        pos = {"kind": "option", "symbol": opt["symbol"], "underlying": cand["symbol"], "opt_type": inst,
               "qty": n, "entry": fill, "mark": fill, "expiry": opt["expiry"], "strike": opt["strike"],
               "delta": round(opt["delta"], 2), "cost": cost}

    elif inst == "spread":
        n = max(1, int(order.get("contracts") or 1))
        while n > 1 and cand["max_loss_per"] * n > governor.available(agent):
            n -= 1
        ok, why = governor.check_order(state, aid, cand, inst, cand["max_loss_per"] * n)
        if not ok:
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"], "note": why})
        try:  # Alpaca writes net credits as a negative limit price; fall back if rejected
            sub = b.submit_mleg(cand["legs"], n, -cand["credit"], cid(aid))
        except AlpacaError as e:
            if "limit" not in str(e).lower():
                raise
            sub = b.submit_mleg(cand["legs"], n, cand["credit"], cid(aid))
        o = _fill(b, sub, 30)
        if not o:
            return store.journal({"type": "unfilled", "agent": aid, "symbol": cand["symbol"],
                                  "note": f"credit {cand['credit']} not filled"})
        n = int(float(o["filled_qty"]))
        credit = abs(float(o.get("filled_avg_price") or cand["credit"]))
        slip = round(min(cand["half_spread"], 0.10) * 100 * n, 2) if SLIP else 0
        cost = round(-(credit * 100 * n) + slip, 2)       # negative cost = cash received
        agent["reserved"] = round(agent["reserved"] + cand["width"] * 100 * n, 2)
        pos = {"kind": "spread", "symbol": cand["symbol"], "underlying": cand["symbol"], "qty": n,
               "legs": cand["legs"], "structure": cand["structure"], "expiry": cand["expiry"],
               "credit": credit, "mark": credit, "entry": credit, "width": cand["width"],
               "short_strike": cand["short_strike"], "long_strike": cand["long_strike"], "cost": cost}
    else:
        return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"], "note": f"bad instrument {inst}"})

    agent["cash"] = round(agent["cash"] - pos["cost"], 2)
    pos.update({"direction": cand.get("direction", "long"), "stop": stop, "atr": cand.get("atr", 0),
                "opened": store.now_et().isoformat(timespec="seconds"),
                "thesis": order.get("thesis") or cand.get("rule_thesis", ""),
                "confidence": order.get("confidence", 0), "signals": cand.get("signals", {})})
    pid = cid("pos")
    agent["positions"][pid] = pos
    if aid != "baseline":
        state["entries"]["counts"][aid] = governor.entries_today(state, aid) + 1
    store.journal({"type": "open", "agent": aid, "symbol": pos["symbol"], "instrument": inst,
                   "qty": pos["qty"], "price": pos["entry"], "cost": pos["cost"], "stop": stop,
                   "thesis": pos["thesis"], "confidence": pos["confidence"]})


def close_trade(b, state, aid, pid, reason):
    agent = state["agents"][aid]
    pos = agent["positions"][pid]
    if pos["kind"] == "stock":
        o = _fill(b, b.submit_stock(pos["symbol"], pos["qty"], "sell", cid("x" + aid)))
        if not o:
            return store.journal({"type": "exit_unfilled", "agent": aid, "symbol": pos["symbol"], "note": reason})
        exit_px = float(o["filled_avg_price"])
        proceeds = round(exit_px * pos["qty"], 2)
    elif pos["kind"] == "option":
        q = b.option_quotes([pos["symbol"]]).get(pos["symbol"]) or {}
        m, spr = mid(q)
        bid = float(q.get("bp") or 0)
        limit = max(bid, 0.01)
        o = _fill(b, b.submit_option(pos["symbol"], pos["qty"], "sell", limit, "sell_to_close", cid("x" + aid)))
        if not o:
            return store.journal({"type": "exit_unfilled", "agent": aid, "symbol": pos["symbol"], "note": reason})
        exit_px = float(o["filled_avg_price"])
        slip = (spr or 0) / 2 * 100 * pos["qty"] if SLIP else 0
        proceeds = round(exit_px * 100 * pos["qty"] - slip, 2)
    else:
        legs = [{"symbol": pos["legs"][0]["symbol"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"},
                {"symbol": pos["legs"][1]["symbol"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"}]
        half = pos.get("exit_half_spread", 0.03)
        limit = round(pos.get("mark", pos["credit"]) + max(half, 0.01), 2)
        o = _fill(b, b.submit_mleg(legs, pos["qty"], limit, cid("x" + aid)), 30)
        if not o:
            return store.journal({"type": "exit_unfilled", "agent": aid, "symbol": pos["symbol"], "note": reason})
        exit_px = min(abs(float(o.get("filled_avg_price") or limit)), pos["width"])
        slip = min(half, 0.10) * 100 * pos["qty"] if SLIP else 0
        proceeds = round(max(-(exit_px * 100 * pos["qty"]) - slip, -pos["width"] * 100 * pos["qty"]), 2)
        agent["reserved"] = round(max(agent["reserved"] - pos["width"] * 100 * pos["qty"], 0), 2)
    pnl = round(proceeds - pos["cost"], 2)
    agent["cash"] = round(agent["cash"] + proceeds, 2)
    agent["closed"] += 1
    agent["wins"] += 1 if pnl > 0 else 0
    agent["last_exits"][pos.get("underlying", pos["symbol"])] = store.today()
    del agent["positions"][pid]
    store.journal({"type": "close", "agent": aid, "symbol": pos["symbol"], "qty": pos["qty"],
                   "entry": pos["entry"], "exit": exit_px, "pnl": pnl, "note": reason, "opened": pos["opened"],
                   "confidence": pos.get("confidence"), "thesis": pos.get("thesis")})


def liquidate(b, state, aid, reason):
    for pid in list(state["agents"][aid]["positions"]):
        try:
            close_trade(b, state, aid, pid, reason)
        except AlpacaError as e:
            store.journal({"type": "error", "agent": aid, "note": str(e)[:200]})


# ---------------- agent view ----------------
def agent_view(state, aid, cands, ctx, can_open, standings):
    agent = state["agents"][aid]
    eq = agent_equity(agent, ctx["prices"])
    positions = []
    for pid, p in agent["positions"].items():
        val = position_value(p, ctx["prices"])
        positions.append({"position_id": pid, "kind": p["kind"], "symbol": p["symbol"],
                          "underlying": p.get("underlying"), "qty": p["qty"], "entry": p["entry"],
                          "mark": p.get("mark", p.get("last")), "underlying_stop": p.get("stop"),
                          "unrealized_pnl": round(val - p["cost"], 2),
                          "opened": p["opened"], "expiry": p.get("expiry"), "structure": p.get("structure"),
                          "thesis": p.get("thesis")})
    keep = ("symbol", "direction", "price", "stop", "level", "target_hint", "structure", "short_strike",
            "long_strike", "expiry", "credit", "width", "max_loss_per", "signals", "rule_thesis")
    journal = store.load("journal.json", [])
    return {
        "time_et": store.now_et().strftime("%Y-%m-%d %H:%M"),
        "race": {"goal": config.RACE["goal"], "standings": standings},
        "balance": {"cash": agent["cash"], "available": governor.available(agent), "equity": eq,
                    "start": config.RACE["start_balance"], "progress_to_goal_pct": round(eq / config.RACE["goal"] * 100, 2)},
        "can_open": can_open,
        "trades_left_today": max(config.RULES["max_new_trades_per_day"] - governor.entries_today(state, aid), 0),
        "positions": positions,
        "candidates": [{"candidate_id": i, **{k: c[k] for k in keep if k in c},
                        **({"max_loss_per_contract": c["max_loss_per"]} if "max_loss_per" in c else {})}
                       for i, c in enumerate(cands)],
        "option_note": "For call/put orders, 'dollars' is your premium budget; the system buys the contract "
                       "nearest your target delta (params.opt_delta) that fits it, or skips if none fits.",
        "params": ctx["params"].get(aid, {}),
        "regime": ctx["regime"],
        "recent_closed_trades": [j for j in journal if j.get("type") == "close" and j.get("agent") == aid][-8:],
        "my_notes": agent["notes"][-8:],
    }


# ---------------- reporting ----------------
def write_dashboard(state, regime, equities):
    curve = store.load("equity.json", [])
    stamp = store.now_et().strftime("%Y-%m-%d")
    curve = [r for r in curve if r["date"] != stamp] + [{"date": stamp, "agents": equities}]
    store.save("equity.json", curve)
    agents = []
    for aid, a in state["agents"].items():
        eq = equities.get(aid, a["cash"])
        a["peak"] = max(a.get("peak", eq), eq)
        a["max_dd"] = round(max(a.get("max_dd", 0), 1 - eq / a["peak"] if a["peak"] else 0), 4)
        ret = eq / config.RACE["start_balance"] - 1
        agents.append({"id": aid, "name": config.AGENTS[aid]["name"], "competing": config.AGENTS[aid]["competing"],
                       "equity": eq, "cash": a["cash"], "available": governor.available(a), "status": a["status"],
                       "status_reason": a["status_reason"], "multiple": round(eq / config.RACE["start_balance"], 2),
                       "progress_pct": round(min(eq / config.RACE["goal"], 1) * 100, 2),
                       "open": len(a["positions"]), "closed": a["closed"],
                       "win_rate": round(100 * a["wins"] / a["closed"], 1) if a["closed"] else None,
                       "max_dd_pct": round(a["max_dd"] * 100, 1),
                       "risk_adj": round(ret / max(a["max_dd"], 0.05), 2),
                       "notes": a["notes"][-3:][::-1]})
    positions = [{"agent": aid, "position_id": pid, **{k: p.get(k) for k in (
        "kind", "symbol", "underlying", "structure", "opt_type", "qty", "entry", "last", "mark", "stop",
        "expiry", "opened", "thesis", "confidence", "cost")}}
        for aid, a in state["agents"].items() for pid, p in a["positions"].items()]
    store.save("dashboard.json", {
        "updated": store.now_et().isoformat(timespec="minutes"),
        "race": state["race"], "regime": regime,
        "brain": "AI (" + config.BRAIN_MODEL + ")" if config.ANTHROPIC_API_KEY else "rules-only",
        "rules": config.RULES, "agents": agents, "equity_curve": curve, "positions": positions,
        "journal": store.load("journal.json", [])[-60:][::-1],
        "reviews": store.load("reviews.json", [])[-6:][::-1],
        "params": store.load_params(),
    })


def check_finish(b, state, equities):
    race = state["race"]
    if race["status"] != "running":
        return
    leaders = [aid for aid, eq in equities.items()
               if config.AGENTS[aid]["competing"] and state["agents"][aid]["status"] == "active"
               and eq >= config.RACE["goal"]]
    alive = [aid for aid in config.AGENTS if config.AGENTS[aid]["competing"]
             and state["agents"][aid]["status"] == "active"]
    if not leaders and alive:
        return
    winner = max(leaders, key=lambda a: equities[a]) if leaders else None
    finished_at = store.now_et().isoformat(timespec="seconds")
    if winner:
        state["agents"][winner].update(status="winner", status_reason=f"first to ${config.RACE['goal']:,.0f}",
                                       status_at=finished_at)
    race.update(status="finished", winner=winner, finished_at=finished_at,
                final_standings=sorted([{"agent": a, "equity": equities[a], "status": state["agents"][a]["status"],
                                         "competing": config.AGENTS[a]["competing"]}
                                        for a in equities], key=lambda r: -r["equity"]))
    store.journal({"type": "race_end", "agent": winner or "",
                   "note": f"{config.AGENTS[winner]['name']} reached ${equities[winner]:,.2f}" if winner
                   else "All competing agents failed"})
    journal = store.load("journal.json", [])
    payload = {"standings": race["final_standings"], "goal": config.RACE["goal"],
               "agents": {aid: {k: a[k] for k in ("status", "status_reason", "closed", "wins", "max_dd", "notes")}
                          for aid, a in state["agents"].items()},
               "trades": [j for j in journal if j.get("type") == "close"][-200:]}
    race["assessment"] = brain.final_assessment(payload)
    for aid in state["agents"]:
        liquidate(b, state, aid, "race over")


# ---------------- main ----------------
def main(force=False, broker=None):
    if broker is None:
        checks = health.run_checks()
        for c in checks:
            print(("OK   " if c["ok"] else "FAIL ") + c["name"] + ": " + c["detail"])
        if any(c["name"] == "Anthropic API" and not c["ok"] for c in checks):
            config.ANTHROPIC_API_KEY = ""  # bad key: run agents on fallback rules instead of failing every call
        blocking = [c for c in checks if not c["ok"] and c["name"] in ("GitHub secrets", "Alpaca paper account")]
        if blocking:
            raise SystemExit("Stopped: " + blocking[0]["detail"])
    b = broker or Broker()
    if not b.clock().get("is_open") and not force:
        print("Market closed; nothing to do.")
        return
    state = store.load_state() or store.init_state()
    if state["race"]["status"] != "running":
        print("Race finished. Delete data/state.json to start a new race.")
        return
    governor.entries_today(state, "trend")  # rolls the daily counter
    params = store.load_params()

    held = {p.get("underlying", p["symbol"]) for a in state["agents"].values() for p in a["positions"].values()}
    cands = list(dict.fromkeys(config.UNIVERSE + screener_candidates()))
    symbols = list(dict.fromkeys(["SPY"] + config.VOL_UNDERLYINGS + cands + sorted(held)))
    bars = {}
    for i in range(0, len(symbols), 50):
        bars.update(b.daily_bars(symbols[i:i + 50]))
    prices = b.latest_prices(symbols)
    regime = market_regime(bars, prices)
    ctx = {"broker": b, "bars": bars, "prices": prices, "regime": regime, "params": params,
           "state": state, "candidates": cands, "vol_underlyings": config.VOL_UNDERLYINGS}

    # 1) default exits (always enforced)
    for aid, agent in state["agents"].items():
        for pid, pos in list(agent["positions"].items()):
            try:
                reason = REGISTRY[aid].manage(ctx, pos)
                if reason:
                    close_trade(b, state, aid, pid, reason)
            except AlpacaError as e:
                store.journal({"type": "error", "agent": aid, "symbol": pos["symbol"], "note": str(e)[:200]})

    # 2) each agent decides
    standings = [{"agent": aid, "equity": agent_equity(a, prices), "status": a["status"]}
                 for aid, a in state["agents"].items() if config.AGENTS[aid]["competing"]]
    for aid, agent in state["agents"].items():
        if agent["status"] != "active":
            continue
        ok, why = governor.can_trade_today(state, aid)
        can_open = ok and in_window(aid)
        try:
            found = REGISTRY[aid].entries(ctx) if can_open else []
        except AlpacaError as e:
            store.journal({"type": "error", "agent": aid, "note": str(e)[:200]})
            found = []
        if aid == "volsell" and found:
            agent["min_trade_seen"] = min(c["max_loss_per"] for c in found)
        if not found and not agent["positions"]:
            continue
        view = agent_view(state, aid, found, ctx, True if can_open else (why if not ok else "outside entry window"), standings)
        d = brain.decide(aid, view)
        for c in d.get("close", []):
            pid = c.get("position_id")
            if pid in agent["positions"]:
                close_trade(b, state, aid, pid, "agent: " + str(c.get("reason", ""))[:120])
        for s in d.get("adjust_stops", []):
            pos = agent["positions"].get(s.get("position_id"))
            if pos and s.get("stop") is not None:
                new = float(s["stop"])
                tighter = new > (pos.get("stop") or 0) if pos.get("direction") != "short" else new < (pos.get("stop") or 1e12)
                if tighter:
                    pos["stop"] = round(new, 2)
        if can_open:
            for order in d.get("open", [])[:config.RULES["max_new_trades_per_day"]]:
                i = order.get("candidate_id")
                if not isinstance(i, int) or not 0 <= i < len(found):
                    continue
                if not governor.can_trade_today(state, aid)[0]:
                    break
                try:
                    open_trade(b, state, aid, found[i], order, ctx)
                except AlpacaError as e:
                    store.journal({"type": "error", "agent": aid, "symbol": found[i]["symbol"], "note": str(e)[:200]})
        if d.get("note"):
            agent["notes"].append({"date": store.today(), "note": str(d["note"])[:300]})
            agent["notes"] = agent["notes"][-40:]

    # 3) mark, fail rules, finish line
    equities = {aid: agent_equity(a, prices) for aid, a in state["agents"].items()}
    for aid, a in state["agents"].items():
        min_cost = a.get("min_trade_seen", 50.0) if aid == "volsell" else config.RULES["min_stock_trade"]
        changed = governor.update_status(state, aid, equities[aid], min_cost)
        if changed:
            store.journal({"type": changed, "agent": aid, "note": a["status_reason"]})
            if changed == "eliminated":
                liquidate(b, state, aid, "eliminated")
                equities[aid] = agent_equity(a, prices)
    check_finish(b, state, equities)
    write_dashboard(state, regime, equities)
    store.save("state.json", state)
    print("Done. " + " | ".join(f"{aid} ${eq:,.2f} ({state['agents'][aid]['status']})" for aid, eq in equities.items()))


if __name__ == "__main__":
    try:
        main(force="--force" in sys.argv)
    except SystemExit as e:
        if e.code not in (None, 0):
            health.record_crash(RuntimeError(str(e.code)))
            print(e.code)
            sys.exit(1)
    except Exception as e:  # save the reason where the dashboard and repo can show it
        health.record_crash(e)
        raise
