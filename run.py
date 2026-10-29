"""One race cycle (every 30 min in market hours via GitHub Actions):
  default exits -> each agent decides (close / tighten / open) -> rulebook checks ->
  execute -> mark to market -> fail/elimination check -> finish check -> dashboard.
Usage: python run.py [--force]"""
import math
import sys
import uuid

import requests

import brain, config, governor, health, store
import plan as plan_mod
import focus
import universe
from broker import AlpacaError, Broker
from indicators import enrich
import baseline, blackswan, hyper, meanrev, monkey, radar, scalper, trend, volsell

REGISTRY = {"trend": trend, "meanrev": meanrev, "volsell": volsell, "blackswan": blackswan, "scalper": scalper, "hyper": hyper,
            "baseline": baseline, "monkey": monkey}
from common import mid, pick_option, pick_put_spread, split_today

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


# ---------------- balance changes ----------------
def sync_race_settings(state):
    """Apply a raised starting balance / goal from config to a race already running.
    Every firm (and the benchmark and control) gets the same extra cash; past equity points and peaks
    shift by that amount so charts stay continuous and returns are measured from the new start."""
    race = state["race"]
    old = race.get("start_balance", 500.0)
    new = config.RACE["start_balance"]
    race["goal"] = config.RACE["goal"]
    if new <= old + 1e-9:
        race["start_balance"] = max(old, new)
        return
    amount = round(new - old, 2)
    for a in state["agents"].values():
        if a.get("joined") and a["closed"] == 0 and not a["positions"] and abs(a["cash"] - old) < 0.01:
            a["cash"], a["peak"] = new, new                 # untouched firm: simply starts at the new balance
            continue
        old_peak = a.get("peak") or old
        dd_dollars = a.get("max_dd", 0) * old_peak
        a["cash"] = round(a["cash"] + amount, 2)
        a["peak"] = round(old_peak + amount, 2)
        a["max_dd"] = round(dd_dollars / a["peak"], 4) if a["peak"] else 0
    curve = store.load("equity.json", [])
    for row in curve:
        for aid in list(row.get("agents", {})):
            row["agents"][aid] = round(row["agents"][aid] + amount, 2)
    store.save("equity.json", curve)
    race["start_balance"] = new
    note = (f"Every firm's starting balance raised to ${new:,.0f} (+${amount:,.0f} cash each; open positions unchanged). "
            f"Goal is now ${config.RACE['goal']:,.0f}.")
    store.journal({"type": "topup", "note": note})
    store.ledger({"type": "topup", "note": note})


# ---------------- valuation ----------------
def position_value(pos, prices):
    """Signed market value of a position (spreads are a liability)."""
    if pos["kind"] == "stock":
        px = prices.get(pos["symbol"], pos["entry"])
        pos["last"] = px
        return px * pos["qty"]
    if pos["kind"] in ("option", "debit_spread"):
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
    p = {**ctx["params"].get(aid, {}), **cand.get("opt", {})}
    if cand.get("max_dollars") is not None and order.get("dollars") is not None:   # firm's own per-trade cap
        order = {**order, "dollars": min(float(order["dollars"]), float(cand["max_dollars"]))}
    default_stop = cand.get("stop") or 0
    stop = default_stop
    agent_stop = order.get("stop")
    px = cand.get("price") or 0
    if agent_stop and px and ((cand.get("direction") == "short") == (float(agent_stop) <= px)):
        agent_stop = None              # a stop on the wrong side of the price would fire instantly: ignore it
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
            governor.outcome(aid, cand["symbol"], "not filled")
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
            if ok:
                governor.outcome(aid, cand["symbol"], "not placed: budget too small for one contract")
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"], "note": why if not ok else "budget too small for one contract"})
        o = _fill(b, b.submit_option(opt["symbol"], n, "buy", opt["ask"], "buy_to_open", cid(aid)))
        if not o:
            governor.outcome(aid, cand["symbol"], "not filled")
            return store.journal({"type": "unfilled", "agent": aid, "symbol": opt["symbol"]})
        n, fill = int(float(o["filled_qty"])), float(o["filled_avg_price"])
        slip = round(opt["spread"] / 2 * 100 * n, 2) if SLIP else 0
        cost = round(fill * 100 * n + slip, 2)
        pos = {"kind": "option", "symbol": opt["symbol"], "underlying": cand["symbol"], "opt_type": inst,
               "qty": n, "entry": fill, "mark": fill, "expiry": opt["expiry"], "strike": opt["strike"],
               "delta": round(opt["delta"], 2), "cost": cost}

    elif inst == "debit_spread":
        budget = min(float(order.get("dollars") or 0), governor.available(agent))
        sp = pick_put_spread(b, cand["symbol"], cand["price"], p, budget)
        if not sp:
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"],
                                  "note": f"no put spread affordable within ${budget:.2f}"})
        n = int(budget // (sp["debit"] * 100))
        ok, why = governor.check_order(state, aid, cand, inst, n * sp["debit"] * 100)
        if not ok or n < 1:
            if ok:
                governor.outcome(aid, cand["symbol"], "not placed: budget too small for one spread")
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"], "note": why if not ok else "budget too small for one spread"})
        legs = [{"symbol": sp["long"]["symbol"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                {"symbol": sp["short"]["symbol"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"}]
        limit = round(min(sp["debit"] + max(sp["half_spread"], 0.02), sp["width"] * 0.9), 2)   # meet the market so it fills
        o = _fill(b, b.submit_mleg(legs, n, limit, cid(aid)), 30)
        if not o:
            governor.outcome(aid, cand["symbol"], "not filled")
            return store.journal({"type": "unfilled", "agent": aid, "symbol": cand["symbol"],
                                  "note": f"debit {limit} not filled"})
        n = int(float(o["filled_qty"]))
        fill = abs(float(o.get("filled_avg_price") or limit))
        slip = round(min(sp["half_spread"], 0.10) * 100 * n, 2) if SLIP else 0
        pos = {"kind": "debit_spread", "symbol": cand["symbol"], "underlying": cand["symbol"], "qty": n,
               "legs": legs, "structure": "bear put spread", "expiry": sp["expiry"], "entry": fill, "mark": fill,
               "width": sp["width"], "long_strike": sp["long"]["strike"], "short_strike": sp["short"]["strike"],
               "cost": round(fill * 100 * n + slip, 2)}

    elif inst == "spread":
        n = max(1, int(order.get("contracts") or 1))
        while n > 1 and cand["max_loss_per"] * n > governor.available(agent):
            n -= 1
        ok, why = governor.check_order(state, aid, cand, inst, cand["max_loss_per"] * n)
        if not ok:
            return store.journal({"type": "rule_block", "agent": aid, "symbol": cand["symbol"], "note": why})
        # Work the order like a trader: start at the middle of the bid-ask, then step toward the price a
        # market maker will actually pay, never accepting less than the firm's minimum credit.
        mid_credit = cand["credit"]
        natural = max(round(mid_credit - cand.get("half_spread", 0), 2), 0.01)
        floor = round(max(float(order.get("min_credit") or 0) or mid_credit * 0.8, 0.01), 2)
        ladder = []
        for f in (0.0, 0.34, 0.67, 1.0):
            px = round(mid_credit - f * (mid_credit - natural), 2)
            if px >= floor and px not in ladder:
                ladder.append(px)
        o, tried = None, []
        for px in ladder or [mid_credit]:
            tried.append(px)
            try:  # Alpaca writes net credits as a negative limit price; fall back if rejected
                sub = b.submit_mleg(cand["legs"], n, -px, cid(aid))
            except AlpacaError as e:
                if "limit" not in str(e).lower():
                    raise
                sub = b.submit_mleg(cand["legs"], n, px, cid(aid))
            o = _fill(b, sub, 20)
            if o:
                break
        if not o:
            governor.outcome(aid, cand["symbol"], "not filled")
            return store.journal({"type": "unfilled", "agent": aid, "symbol": cand["symbol"],
                                  "note": f"credit not filled at {', '.join(f'{x:.2f}' for x in tried)} "
                                          f"(mid {mid_credit:.2f}, natural {natural:.2f}, floor {floor:.2f})"})
        n = int(float(o["filled_qty"]))
        credit = abs(float(o.get("filled_avg_price") or tried[-1]))
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
    if pos["cost"] < 0:                                   # a credit received settles next trading day
        store.add_unsettled(agent, -pos["cost"], f"credit {cand['symbol']}")
    if cand.get("hold_days"):
        pos["hold_days"] = cand["hold_days"]
    if cand.get("play"):
        pos["play"] = cand["play"]
    pos.update({"direction": cand.get("direction", "long"), "stop": stop, "atr": cand.get("atr", 0),
                "opened": store.now_et().isoformat(timespec="seconds"),
                "thesis": order.get("thesis") or cand.get("rule_thesis", ""),
                "confidence": order.get("confidence", 0), "signals": cand.get("signals", {})})
    pid = cid("pos")
    agent["positions"][pid] = pos
    governor.outcome(aid, cand["symbol"], f"filled: {pos['qty']:g} at {pos['entry']:g}")
    if aid != "baseline":
        state["entries"]["counts"][aid] = governor.entries_today(state, aid) + 1
    pos["sector"] = universe.sector_of(cand["symbol"])
    store.journal({"type": "open", "agent": aid, "symbol": pos["symbol"], "instrument": inst,
                   "qty": pos["qty"], "price": pos["entry"], "cost": pos["cost"], "stop": stop,
                   "thesis": pos["thesis"], "confidence": pos["confidence"]})
    plan = state["agents"][aid].get("plan") or {}
    store.ledger({"type": "trade_opened", "agent": aid, "position_id": pid, "symbol": pos["symbol"],
                  "underlying": cand["symbol"], "sector": pos["sector"], "instrument": inst, "qty": pos["qty"],
                  "price": pos["entry"], "cost": pos["cost"], "stop": stop, "why": pos["thesis"],
                  "confidence": pos["confidence"], "signals": pos.get("signals", {}),
                  "from_plan": cand["symbol"] in {w["symbol"] for w in plan.get("watchlist", [])}
                               and plan.get("for_day") == store.today()})


def close_trade(b, state, aid, pid, reason, qty=None):
    """Close a position, or part of it (qty) when a firm takes profits in stages."""
    agent = state["agents"][aid]
    pos = agent["positions"][pid]
    nq = pos["qty"]
    if qty and pos["kind"] != "spread" and 0 < qty < pos["qty"]:
        nq = qty
    if pos["kind"] == "stock":
        o = _fill(b, b.submit_stock(pos["symbol"], nq, "sell", cid("x" + aid)))
        if not o:
            return store.journal({"type": "exit_unfilled", "agent": aid, "symbol": pos["symbol"], "note": reason})
        exit_px = float(o["filled_avg_price"])
        nq = float(o.get("filled_qty") or nq)
        proceeds = round(exit_px * nq, 2)
    elif pos["kind"] == "debit_spread":
        legs = [{"symbol": pos["legs"][0]["symbol"], "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
                {"symbol": pos["legs"][1]["symbol"], "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"}]
        half = pos.get("exit_half_spread", 0.03)
        limit = max(round(pos.get("mark", pos["entry"]) - min(max(half, 0.01), 0.05), 2), 0.01)
        try:  # selling a spread is a net credit: Alpaca writes it as a negative limit price
            sub = b.submit_mleg(legs, nq, -limit, cid("x" + aid))
        except AlpacaError as e:
            if "limit" not in str(e).lower():
                raise
            sub = b.submit_mleg(legs, nq, limit, cid("x" + aid))
        o = _fill(b, sub, 30)
        if not o:
            return store.journal({"type": "exit_unfilled", "agent": aid, "symbol": pos["symbol"], "note": reason})
        exit_px = min(abs(float(o.get("filled_avg_price") or limit)), pos["width"])
        slip = min(half, 0.10) * 100 * nq if SLIP else 0
        proceeds = round(max(exit_px * 100 * nq - slip, 0), 2)
    elif pos["kind"] == "option":
        quote = b.option_quotes([pos["symbol"]]).get(pos["symbol"]) or {}
        m, spr = mid(quote)
        bid = float(quote.get("bp") or 0)
        limit = max(bid, 0.01)
        o = _fill(b, b.submit_option(pos["symbol"], nq, "sell", limit, "sell_to_close", cid("x" + aid)))
        if not o:
            return store.journal({"type": "exit_unfilled", "agent": aid, "symbol": pos["symbol"], "note": reason})
        exit_px = float(o["filled_avg_price"])
        slip = (spr or 0) / 2 * 100 * nq if SLIP else 0
        proceeds = round(exit_px * 100 * nq - slip, 2)
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
    if nq < pos["qty"]:                                   # partial: keep the rest open
        pos.setdefault("orig_cost", pos["cost"])
        part_cost = pos["cost"] * nq / pos["qty"]
        pnl = round(proceeds - part_cost, 2)
        agent["cash"] = round(agent["cash"] + proceeds, 2)
        store.add_unsettled(agent, proceeds, f"sold part {pos['symbol']}")
        pos["qty"] = round(pos["qty"] - nq, 4) if pos["kind"] == "stock" else int(pos["qty"] - nq)
        pos["cost"] = round(pos["cost"] - part_cost, 2)
        pos["realized"] = round(pos.get("realized", 0) + pnl, 2)
        pos["trimmed"] = True
        store.journal({"type": "trim", "agent": aid, "symbol": pos["symbol"], "qty": nq, "exit": exit_px,
                       "pnl": pnl, "note": reason})
        store.ledger({"type": "trade_closed", "partial": True, "agent": aid, "position_id": pid, "symbol": pos["symbol"],
                      "underlying": pos.get("underlying", pos["symbol"]), "sector": pos.get("sector"),
                      "instrument": pos["kind"], "qty": nq, "entry": pos["entry"], "exit": exit_px, "pnl": pnl,
                      "opened": pos["opened"], "why_entered": pos.get("thesis"), "why_exited": reason})
        return None
    pnl = round(proceeds - pos["cost"] + pos.get("realized", 0), 2)       # whole trade, including earlier trims
    agent["cash"] = round(agent["cash"] + proceeds, 2)
    store.add_unsettled(agent, proceeds, f"sold {pos['symbol']}")
    agent["closed"] += 1
    agent["wins"] += 1 if pnl > 0 else 0
    agent["last_exits"][pos.get("underlying", pos["symbol"])] = store.today()
    del agent["positions"][pid]
    store.journal({"type": "close", "agent": aid, "symbol": pos["symbol"], "qty": pos["qty"],
                   "entry": pos["entry"], "exit": exit_px, "pnl": pnl, "note": reason, "opened": pos["opened"],
                   "confidence": pos.get("confidence"), "thesis": pos.get("thesis")})
    store.ledger({"type": "trade_closed", "agent": aid, "position_id": pid, "symbol": pos["symbol"],
                  "underlying": pos.get("underlying", pos["symbol"]),
                  "sector": pos.get("sector") or universe.sector_of(pos.get("underlying", pos["symbol"])),
                  "instrument": pos["kind"], "entry": pos["entry"], "exit": exit_px, "pnl": pnl,
                  "return_pct": round(pnl / abs(pos.get("orig_cost", pos["cost"])) * 100, 1) if pos.get("cost") else None,
                  "opened": pos["opened"], "why_entered": pos.get("thesis"), "why_exited": reason})


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
    keep = ("symbol", "sector", "direction", "price", "stop", "level", "target_hint", "structure", "short_strike", "size_pct",
            "play", "max_dollars", "suggested_instrument", "hold_days",
            "long_strike", "expiry", "credit", "width", "max_loss_per", "signals", "rule_thesis")
    journal = store.load("journal.json", [])
    return {
        "time_et": store.now_et().strftime("%Y-%m-%d %H:%M"),
        "race": {"goal": config.RACE["goal"], "standings": standings},
        "balance": {"cash": agent["cash"], "available": governor.available(agent), "settling": store.unsettled(agent),
                    "settlement_note": "available = settled cash. Sale proceeds settle the next trading day (cash account, "
                                       "T+1); you can't spend them until then.", "equity": eq,
                    "start": config.RACE["start_balance"], "progress_to_goal_pct": round(eq / config.RACE["goal"] * 100, 2)},
        "can_open": can_open,
        "trades_today": governor.entries_today(state, aid),
        "positions": positions,
        "candidates": [{"candidate_id": i, **{k: c[k] for k in keep if k in c},
                        **({"max_loss_per_contract": c["max_loss_per"]} if "max_loss_per" in c else {})}
                       for i, c in enumerate(cands)],
        "option_note": "For call/put orders, 'dollars' is your premium budget; the system buys the contract "
                       "nearest your target delta (params.opt_delta) that fits it, or skips if none fits. For "
                       "debit_spread (bear put spread), 'dollars' is the most you'll pay, which is also the most "
                       "you can lose; the system buys the put near your target delta and sells a cheaper one below it.",
        "params": ctx["params"].get(aid, {}),
        "regime": ctx["regime"],
        "crash_radar": {k: (ctx.get("radar") or {}).get(k) for k in ("score", "tier", "parts", "facts", "economy")},
        "recent_closed_trades": [j for j in journal if j.get("type") == "close" and j.get("agent") == aid][-8:],
        "blocked_today": [{"symbol": j.get("symbol"), "why": j.get("note")} for j in journal
                          if j.get("agent") == aid and j.get("type") in ("rule_block", "unfilled", "error")
                          and j.get("time", "").startswith(store.today())][-6:],
        "sector_focus": focus.current(agent),
        "my_results_by_sector": focus.sector_stats(aid),
        "my_notes": agent["notes"][-8:],
        "game_plan": ({k: (agent.get("plan") or {}).get(k) for k in ("summary", "watchlist", "position_plans", "avoid", "why", "made_at")}
                      if (agent.get("plan") or {}).get("for_day") == store.today() else None),
    }


# ---------------- reporting ----------------
REVIEW_SESSIONS = 20


def thesis_reviews(curve):
    """Flag any racing firm that has trailed SPY on every one of the last 20 sessions.

    Compares each firm's return since the race start with the SPY benchmark's on each
    session's closing equity. The flag clears itself the first session the firm is back
    at or above SPY. Needs 20 sessions of history before it can fire."""
    start = config.RACE["start_balance"]
    rows = [r for r in curve if "baseline" in r.get("agents", {})][-REVIEW_SESSIONS:]
    out = {}
    if len(rows) < REVIEW_SESSIONS:
        return out
    for aid, cfg in config.AGENTS.items():
        if not cfg.get("competing"):
            continue
        gaps = []
        for r in rows:
            eq = r["agents"].get(aid)
            if eq is None:
                break
            gaps.append((eq / start - 1) - (r["agents"]["baseline"] / start - 1))
        if len(gaps) == REVIEW_SESSIONS and all(g < 0 for g in gaps):
            out[aid] = {"due": True, "gap_pts": round(gaps[-1] * 100, 2), "since": rows[0]["date"]}
    return out


def write_dashboard(state, regime, equities):
    curve = store.load("equity.json", [])
    stamp = store.now_et().strftime("%Y-%m-%d")
    curve = [r for r in curve if r["date"] != stamp] + [{"date": stamp, "agents": equities}]
    store.save("equity.json", curve)
    agents = []
    review = thesis_reviews(curve)
    for aid, a in state["agents"].items():
        eq = equities.get(aid, a["cash"])
        a["peak"] = max(a.get("peak", eq), eq)
        a["max_dd"] = round(max(a.get("max_dd", 0), 1 - eq / a["peak"] if a["peak"] else 0), 4)
        ret = eq / config.RACE["start_balance"] - 1
        agents.append({"id": aid, "name": config.AGENTS[aid]["name"], "competing": config.AGENTS[aid]["competing"],
                       "exhibition": bool(config.AGENTS[aid].get("exhibition")), "reached_goal": a.get("reached_goal"),
                       "paused_reason": config.AGENTS[aid].get("paused_reason"),
                       "equity": eq, "cash": a["cash"], "available": governor.available(a),
                       "settling": store.unsettled(a), "status": a["status"],
                       "status_reason": a["status_reason"], "multiple": round(eq / config.RACE["start_balance"], 2),
                       "progress_pct": round(min(eq / config.RACE["goal"], 1) * 100, 2),
                       "open": len(a["positions"]), "closed": a["closed"],
                       "win_rate": round(100 * a["wins"] / a["closed"], 1) if a["closed"] else None,
                       "max_dd_pct": round(a["max_dd"] * 100, 1),
                       "risk_adj": round(ret / max(a["max_dd"], 0.05), 2),
                       "notes": a["notes"][-3:][::-1],
                       "thesis_review": review.get(aid)})
    positions = [{"agent": aid, "position_id": pid, **{k: p.get(k) for k in (
        "kind", "symbol", "underlying", "structure", "opt_type", "qty", "entry", "last", "mark", "stop",
        "expiry", "opened", "thesis", "confidence", "cost", "realized", "play", "width")}}
        for aid, a in state["agents"].items() for pid, p in a["positions"].items()]
    store.save("dashboard.json", {
        "updated": store.now_et().isoformat(timespec="minutes"),
        "race": state["race"], "regime": regime, "start_balance": config.RACE["start_balance"],
        "brain": "AI (" + config.BRAIN_MODEL + ")" if config.ANTHROPIC_API_KEY else "rules-only",
        "rules": config.RULES, "agents": agents, "equity_curve": curve, "positions": positions,
        "journal": store.load("journal.json", [])[-60:][::-1],
        "reviews": store.load("reviews.json", [])[-6:][::-1],
        "params": store.load_params(),
        "plans": {aid: a.get("plan") for aid, a in state["agents"].items() if a.get("plan")},
        "scorecards": {aid: plan_mod.scorecard_summary(a) for aid, a in state["agents"].items() if a.get("scorecard")},
        "calendar": store.load("calendar.json", []),
        "radar": store.load("radar.json", {}),
        "compliance_today": (lambda c: {"approved": len(c.get("approved", [])), "denied": len(c.get("denied", [])),
                                        "last_denied": (c.get("denied") or [{}])[-1].get("reason")})(
                                store.load("compliance.json", {}).get(store.today(), {})),
        "playbooks": {aid: a.get("playbook") for aid, a in state["agents"].items() if a.get("playbook")},
        "focus": {aid: focus.current(a) for aid, a in state["agents"].items() if focus.current(a)},
        "sector_stats": {aid: focus.sector_stats(aid) for aid in state["agents"]},
        "universe": {k: store.load("universe.json", {}).get(k) for k in ("built", "pool_size")} |
                    {"core_size": len(store.load("universe.json", {}).get("core", []))},
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
    store.add_missing_agents(state)
    if state["race"]["status"] == "running":
        sync_race_settings(state)
    if state["race"]["status"] != "running":
        print("Race finished. Delete data/state.json to start a new race.")
        return
    governor.entries_today(state, "trend")  # rolls the daily counter
    params = store.load_params()

    held = {p.get("underlying", p["symbol"]) for a in state["agents"].values() for p in a["positions"].values()}
    cands = universe.candidates(b, state, extra=screener_candidates())
    symbols = list(dict.fromkeys(["SPY"] + config.VOL_UNDERLYINGS + config.BEAR_SYMBOLS + config.SAFE_HAVENS
                                 + blackswan.SECTOR_ETFS + cands + sorted(held)))
    bars = {}
    for i in range(0, len(symbols), 50):
        bars.update(b.daily_bars(symbols[i:i + 50]))
    prices = b.latest_prices(symbols)
    regime = market_regime(bars, prices)
    try:
        crash = radar.compute(bars, prices)
    except Exception as e:  # noqa: BLE001 - the radar must never stop a run
        print(f"[radar] failed: {type(e).__name__}")
        crash = radar.current()
    regime["crash_radar"] = crash.get("score")
    ctx = {"radar": crash, "broker": b, "bars": bars, "prices": prices, "regime": regime, "params": params,
           "state": state, "candidates": cands, "vol_underlyings": config.VOL_UNDERLYINGS}

    # 1) default exits (always enforced)
    for aid, agent in state["agents"].items():
        for pid, pos in list(agent["positions"].items()):
            try:
                reason = REGISTRY[aid].manage(ctx, pos)
                if isinstance(reason, dict):        # take profits in stages: sell part, keep the rest
                    close_trade(b, state, aid, pid, reason["reason"], qty=reason.get("qty"))
                elif reason:
                    close_trade(b, state, aid, pid, reason)
            except AlpacaError as e:
                store.journal({"type": "error", "agent": aid, "symbol": pos["symbol"], "note": str(e)[:200]})

    # 2) each agent decides
    standings = [{"agent": aid, "equity": agent_equity(a, prices), "status": a["status"]}
                 for aid, a in state["agents"].items() if config.AGENTS[aid]["competing"]]
    for aid, agent in state["agents"].items():
        if agent["status"] != "active":
            continue
        if aid in scalper.PROFILES:          # the AI sets the scalpers' playbooks; the 2-minute loop trades them
            if config.AGENTS[aid].get("no_new_entries"):
                continue                     # retired: no playbook, no AI call
            try:
                scalper.update_playbook({**ctx, "equity": agent_equity(agent, prices)}, state, b, aid)
            except Exception as e:  # noqa: BLE001
                print(f"[scalper] playbook failed: {type(e).__name__}")
            continue
        ok, why = governor.can_trade_today(state, aid)
        paused = config.AGENTS[aid].get("no_new_entries")
        can_open = ok and in_window(aid) and not paused
        if paused:
            why = config.AGENTS[aid].get("paused_reason", "no new entries")
        try:
            found = focus.rank(REGISTRY[aid].entries(ctx), agent, limit=8 if aid == "blackswan" else 6) if can_open else []
        except AlpacaError as e:
            store.journal({"type": "error", "agent": aid, "note": str(e)[:200]})
            found = []
        if aid == "volsell" and found:
            agent["min_trade_seen"] = min(c["max_loss_per"] for c in found)
        if not found and not agent["positions"]:
            continue
        view = agent_view(state, aid, found, ctx, True if can_open else (why if (not ok or paused) else "outside entry window"), standings)
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
            for order in d.get("open", []):
                i = order.get("candidate_id")
                if not isinstance(i, int) or not 0 <= i < len(found):
                    continue
                if not governor.can_trade_today(state, aid)[0]:
                    break
                try:
                    open_trade(b, state, aid, found[i], order, ctx)
                except AlpacaError as e:
                    governor.outcome(aid, found[i]["symbol"], "broker error")
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
    for aid, a in state["agents"].items():           # exhibition firms can reach the goal without ending the race
        if config.AGENTS[aid].get("exhibition") and not a.get("reached_goal") and equities[aid] >= config.RACE["goal"]:
            a["reached_goal"] = store.now_et().isoformat(timespec="minutes")
            store.journal({"type": "exhibition_goal", "agent": aid, "note": f"{config.AGENTS[aid]['name']} reached "
                           f"${equities[aid]:,.2f} (exhibition: the race continues)"})
    check_finish(b, state, equities)
    write_dashboard(state, regime, equities)
    store.save("state.json", state)
    print("Done. " + " | ".join(f"{aid} ${eq:,.2f} ({state['agents'][aid]['status']})" for aid, eq in equities.items()))


if __name__ == "__main__":
    import alerts
    try:
        main(force="--force" in sys.argv)
        alerts.flush("Agent race: trading update")
    except SystemExit as e:
        if e.code not in (None, 0):
            health.record_crash(RuntimeError(str(e.code)))
            alerts.setup_problem(e.code)
            print(e.code)
            sys.exit(1)
    except Exception as e:  # save the reason where the dashboard and repo can show it
        health.record_crash(e)
        alerts.setup_problem(f"The trading run crashed ({type(e).__name__}). Details are on the dashboard.")
        raise
