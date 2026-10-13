"""Sector focus: each firm decides which sectors to lean into or stay away from.

The planners set a focus with a reason (see plan.py). It's backed by the firm's own record:
P&L, win rate and number of trades per sector from the permanent ledger. A focus expires
after FOCUS_DAYS trading days unless the planners renew it, so a firm can't get stuck on a
sector that stopped paying. Scanners then show focus-sector setups first and drop avoided ones."""
from datetime import date, timedelta

import store
import universe

FOCUS_DAYS = 10
MAX_LEAN = 3


def sector_stats(aid):
    rows = {}
    for e in store.load("ledger.json", []):
        if e.get("agent") != aid or e.get("type") != "trade_closed":
            continue
        s = rows.setdefault(e.get("sector") or "Broad market / other", {"trades": 0, "wins": 0, "pnl": 0.0})
        s["trades"] += 1
        s["wins"] += 1 if (e.get("pnl") or 0) > 0 else 0
        s["pnl"] = round(s["pnl"] + (e.get("pnl") or 0), 2)
    for s in rows.values():
        s["win_rate_pct"] = round(100 * s["wins"] / s["trades"]) if s["trades"] else None
        s["avg_pnl"] = round(s["pnl"] / s["trades"], 2) if s["trades"] else None
    return dict(sorted(rows.items(), key=lambda kv: -kv[1]["pnl"]))


def current(agent):
    f = agent.get("focus") or {}
    if not f or (f.get("expires") and f["expires"] < store.today()):
        return None
    return f


def apply(aid, agent, proposal, why):
    """Set a firm's focus from a planning session's answer. Returns a ledger note if it changed."""
    if not isinstance(proposal, dict):
        return None
    lean = [s for s in proposal.get("lean_in", []) if s in universe.SECTORS][:MAX_LEAN]
    avoid = [s for s in proposal.get("avoid", []) if s in universe.SECTORS and s not in lean]
    old = current(agent) or {}
    expires = (date.fromisoformat(store.today()) + timedelta(days=round(FOCUS_DAYS * 7 / 5))).isoformat()
    new = {"lean_in": lean, "avoid": avoid, "why": str(proposal.get("why") or why or "")[:300],
           "set_at": store.now_et().isoformat(timespec="minutes"), "expires": expires}
    agent["focus"] = new
    if sorted(lean) == sorted(old.get("lean_in", [])) and sorted(avoid) == sorted(old.get("avoid", [])):
        return None
    return new


def rank(found, agent, limit=6):
    """Order a scanner's setups: focus sectors first, avoided sectors dropped (unless nothing's left)."""
    f = current(agent)
    for c in found:
        c["sector"] = universe.sector_of(c.get("underlying", c["symbol"]))
    if not f:
        return found[:limit]
    keep = [c for c in found if c["sector"] not in f.get("avoid", [])] or found
    lean = [c for c in keep if c["sector"] in f.get("lean_in", [])]
    rest = [c for c in keep if c["sector"] not in f.get("lean_in", [])]
    return (lean[:max(limit - 2, 1)] + rest)[:limit] if lean else rest[:limit]
