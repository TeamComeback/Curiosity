"""The benchmark: $100 into SPY (fractional) and hold. Not racing."""


def entries(ctx):
    if ctx["state"]["agents"]["baseline"]["positions"]:
        return []
    price = ctx["prices"].get("SPY")
    if not price:
        return []
    return [{"agent": "baseline", "symbol": "SPY", "direction": "long", "price": price, "stop": 0.0,
             "atr": 0.0, "signals": {}, "rule_thesis": "Benchmark: buy and hold SPY", "score": 0}]


def manage(ctx, pos):
    return None
