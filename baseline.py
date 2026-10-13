"""The benchmark: its starting cash into SPY (fractional) and hold. Not racing.
Any new cash added to the race is invested in SPY too, so the comparison stays fair."""


def entries(ctx):
    agent = ctx["state"]["agents"]["baseline"]
    if agent["cash"] - agent.get("reserved", 0) < 25:   # ignore crumbs
        return []
    price = ctx["prices"].get("SPY")
    if not price:
        return []
    return [{"agent": "baseline", "symbol": "SPY", "direction": "long", "price": price, "stop": 0.0,
             "atr": 0.0, "signals": {}, "rule_thesis": "Benchmark: buy and hold SPY", "score": 0}]


def manage(ctx, pos):
    return None
