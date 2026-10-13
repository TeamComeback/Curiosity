from . import baseline, meanrev, trend, volsell

REGISTRY = {"trend": trend, "meanrev": meanrev, "volsell": volsell, "baseline": baseline}
