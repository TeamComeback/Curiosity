"""The rulebook: plain code, never AI. Agents decide everything else.
Rules: cash only (no margin), simple options only (long calls/puts, defined-risk spreads),
anti-overtrading caps, and the fail / elimination rules of the race."""
from datetime import date

import config, store

R = config.RULES


def available(agent):
    """Settled cash the firm can spend now: cash minus spread collateral minus proceeds still settling.
    Spending only settled cash is how a cash account avoids good-faith violations."""
    return round(agent["cash"] - agent["reserved"] - store.unsettled(agent), 2)


def entries_today(state, aid):
    if state["entries"]["date"] != store.today():
        state["entries"] = {"date": store.today(), "counts": {}}
    return state["entries"]["counts"].get(aid, 0)


def can_trade_today(state, aid):
    agent = state["agents"][aid]
    if state["race"]["status"] != "running":
        return False, "race finished"
    if agent["status"] != "active":
        return False, f"agent {agent['status']}"
    if R.get("max_new_trades_per_day") and entries_today(state, aid) >= R["max_new_trades_per_day"] and aid != "baseline":
        return False, "daily trade limit reached"
    if entries_today(state, aid) >= R.get("malfunction_guard_trades_per_day", 30) and aid != "baseline":
        return False, "malfunction guard: unusually many orders today, paused until tomorrow"
    if R.get("max_open_positions") and len(agent["positions"]) >= R["max_open_positions"]:
        return False, "max open positions"
    return True, "ok"


def check_order(state, aid, cand, instrument, cost):
    """Validate one agent order. cost = cash the trade consumes (premium, shares, or spread collateral)."""
    agent = state["agents"][aid]
    if instrument not in config.AGENTS[aid]["instruments"]:
        return False, f"{instrument} is outside this agent's specialty"
    if instrument in ("call", "put", "spread", "debit_spread") and instrument not in R["options_allowed"]:
        return False, "option type not allowed"
    if instrument in ("put", "debit_spread") and cand.get("direction") != "short":
        return False, "puts only on breakdown setups"
    if aid == "blackswan" and instrument == "stock" and cand["symbol"] not in (
            list(config.INVERSE_ETFS.values()) + list(config.LEVERAGED_INVERSE.values()) + config.SAFE_HAVENS):
        return False, "Black Swan only buys inverse ETFs and safe havens as shares"
    if instrument in ("stock", "call") and cand.get("direction") == "short":
        return False, "no shorting stock (no margin); use puts"
    if cost <= 0:
        return False, "nothing to buy"
    if R["no_margin"] and cost > available(agent) + 1e-6:
        pend = store.unsettled(agent)
        return False, (f"needs ${cost:.2f}, only ${available(agent):.2f} settled cash available"
                       + (f" (${pend:.2f} more settles next trading day)" if pend else " (no margin)"))
    if instrument == "stock" and cost < R["min_stock_trade"]:
        return False, "below minimum trade size"
    if aid != "baseline" and any(p.get("underlying", p["symbol"]) == cand["symbol"] for p in agent["positions"].values()):
        return False, f"already holding {cand['symbol']}"
    last = agent["last_exits"].get(cand["symbol"])
    if last and R.get("reentry_cooldown_days") and (store.now_et().date() - date.fromisoformat(last)).days < R["reentry_cooldown_days"]:
        return False, "re-entry cooldown"
    if instrument == "stock" and aid != "baseline":
        for other, a in state["agents"].items():
            if other != aid and any(p["kind"] == "stock" and p["symbol"] == cand["symbol"] for p in a["positions"].values()):
                return False, f"{cand['symbol']} shares already held by {other} (shared account)"
    return True, "ok"


def update_status(state, aid, equity, min_trade_cost):
    """Apply the race's fail rules. Returns a new status string if it changed."""
    agent = state["agents"][aid]
    if agent["status"] != "active" or not config.AGENTS[aid]["competing"]:
        return None
    if equity < config.RACE["eliminate_below"]:
        agent.update(status="eliminated", status_reason=f"equity ${equity:.2f} fell below ${config.RACE['eliminate_below']:.0f}",
                     status_at=store.now_et().isoformat(timespec="seconds"))
        return "eliminated"
    total = round(agent["cash"] - agent["reserved"], 2)          # settling cash still counts: it's coming
    if not agent["positions"] and min_trade_cost is not None and total < min_trade_cost:
        agent.update(status="failed", status_reason=f"${total:.2f} left, cheapest possible trade costs ${min_trade_cost:.2f}",
                     status_at=store.now_et().isoformat(timespec="seconds"))
        return "failed"
    return None
