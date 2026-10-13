"""The monkey: a control firm that picks stocks at random.

Same starting cash, same rulebook, same default stop and the same fractional shares as the real
firms, but no skill at all: random stock, random size, random holding period. If an AI
firm can't beat the monkey over time, its results are luck, not edge. Not racing."""
import random

import store
from indicators import enrich
from common import split_today


def entries(ctx):
    agent = ctx["state"]["agents"]["monkey"]
    rng = random.Random(f"{store.today()}-{len(agent.get('positions', {}))}-{agent.get('closed', 0)}")
    if rng.random() > 0.35:                      # throws a dart on roughly a third of the runs
        return []
    pool = [s for s in ctx["candidates"] if s in ctx["bars"] and s in ctx["prices"]]
    if not pool:
        return []
    sym = rng.choice(pool)
    hist = enrich(split_today(ctx["bars"][sym]))
    if len(hist) < 30:
        return []
    price, atr = ctx["prices"][sym], float(hist["atr20"].iloc[-1])
    return [{"agent": "monkey", "symbol": sym, "direction": "long", "price": price,
             "stop": round(price - 2.0 * atr, 2), "atr": round(atr, 3), "signals": {},
             "hold_days": rng.randint(1, 7), "size_pct": rng.uniform(0.25, 0.6),
             "rule_thesis": f"Dart landed on {sym}", "score": 0}]


def manage(ctx, pos):
    price = ctx["prices"].get(pos["symbol"])
    if price is not None and pos.get("stop") and price <= pos["stop"]:
        return "stop loss"
    from datetime import date
    held = (store.now_et().date() - date.fromisoformat(pos["opened"][:10])).days
    if held >= pos.get("hold_days", 5):
        return f"random holding period over ({held} days)"
    return None
