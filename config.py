"""Central settings.

RULES are the only hard limits. No agent and no self-review can change this file.
Everything else (which setups to take, how many dollars, where stops go, when to exit,
and each agent's strategy parameters inside PARAM_BOUNDS) is the agent's own call."""
import os

PAPER_BASE = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
DATA_BASE = "https://data.alpaca.markets"
API_KEY = "".join(os.getenv("ALPACA_API_KEY", "").split())
API_SECRET = "".join(os.getenv("ALPACA_API_SECRET", "").split())
STOCK_FEED = os.getenv("ALPACA_STOCK_FEED", "iex")
OPTION_FEED = os.getenv("ALPACA_OPTION_FEED", "indicative")

ANTHROPIC_API_KEY = "".join(os.getenv("ANTHROPIC_API_KEY", "").split())
BRAIN_MODEL = os.getenv("BRAIN_MODEL", "claude-haiku-5-5")      # per-run agent decisions
REVIEW_MODEL = os.getenv("REVIEW_MODEL", "claude-sonnet-5-5")   # weekly reviews + final assessment

SCREENER_URL = os.getenv("SCREENER_URL", "")

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "AMD", "NFLX",
    "CRM", "ADBE", "ORCL", "INTC", "QCOM", "MU", "PLTR", "UBER", "SHOP", "COIN",
    "JPM", "BAC", "GS", "V", "MA", "XOM", "CVX", "LLY", "UNH", "JNJ",
    "COST", "WMT", "HD", "NKE", "DIS", "BA", "CAT", "GE", "SMH", "XLE",
]

# ---- The race ----
RACE = {
    "start_balance": 100.0,
    "goal": 5000.0,
    "eliminate_below": 10.0,    # equity under this = eliminated
}

# Competing agents (each keeps its specialty) + a non-competing benchmark.
AGENTS = {
    "trend":   {"name": "Trend agent", "competing": True,
                "instruments": ["stock", "call", "put"],
                "style": "Trend follower. Trades breakouts to new highs in established uptrends (or "
                         "breakdowns via puts), lets winners run with trailing stops, cuts losers fast."},
    "meanrev": {"name": "Mean-reversion agent", "competing": True,
                "instruments": ["stock", "call"],
                "style": "Mean-reversion trader. Buys sharp short-term washouts (RSI(2) extremes) in "
                         "stocks that are still above their 200-day average, and sells into the snap-back."},
    "volsell": {"name": "Vol-selling agent", "competing": True,
                "instruments": ["spread"],
                "style": "Volatility seller. Sells defined-risk credit spreads on index ETFs when implied "
                         "volatility is rich versus realized, collecting premium as time passes."},
    "baseline": {"name": "SPY benchmark", "competing": False, "instruments": ["stock"],
                 "style": "Buys SPY with its $100 and holds. Not racing; it's the bar to beat."},
}
VOL_UNDERLYINGS = ["SPY", "QQQ", "IWM"]

# ---- Hard rules (the only things agents cannot decide) ----
RULES = {
    "no_margin": True,                # can only spend its own available cash
    "options_allowed": ["call", "put", "spread"],  # long options + defined-risk spreads; nothing naked
    "max_new_trades_per_day": 2,      # anti-overtrading
    "max_open_positions": 4,
    "reentry_cooldown_days": 1,       # no re-buying a symbol the same day it was sold
    "min_stock_trade": 2.00,          # smallest fractional-share order, dollars
    "slippage_half_spread": True,     # options P&L haircut = half the bid-ask at entry and exit
}

# Strategy parameters each agent tunes for itself (weekly), within hard bounds.
DEFAULT_PARAMS = {
    "trend":   {"breakout_lookback": 55, "stop_atr": 2.0, "trail_atr": 3.0, "min_rel_volume": 1.2,
                "opt_min_dte": 21, "opt_max_dte": 60, "opt_delta": 0.50},
    "meanrev": {"rsi_entry": 10, "rsi_exit": 70, "stop_atr": 2.5, "max_hold_days": 7,
                "opt_min_dte": 14, "opt_max_dte": 45, "opt_delta": 0.50},
    "volsell": {"short_delta": 0.20, "width": 1, "min_dte": 21, "max_dte": 45,
                "take_profit": 0.50, "stop_mult": 2.0, "exit_dte": 10, "min_iv_rv": 1.10},
}
PARAM_BOUNDS = {
    "trend":   {"breakout_lookback": (10, 100), "stop_atr": (1.0, 5.0), "trail_atr": (1.5, 6.0),
                "min_rel_volume": (0.8, 3.0), "opt_min_dte": (7, 45), "opt_max_dte": (21, 120),
                "opt_delta": (0.25, 0.80)},
    "meanrev": {"rsi_entry": (3, 25), "rsi_exit": (50, 90), "stop_atr": (1.0, 5.0), "max_hold_days": (2, 20),
                "opt_min_dte": (7, 45), "opt_max_dte": (14, 90), "opt_delta": (0.25, 0.80)},
    "volsell": {"short_delta": (0.08, 0.40), "width": (1, 5), "min_dte": (7, 40), "max_dte": (14, 60),
                "take_profit": (0.25, 0.85), "stop_mult": (1.0, 3.0), "exit_dte": (0, 21), "min_iv_rv": (0.9, 1.6)},
}

# Long-option default exits (agents can close earlier at any time)
OPTION_EXITS = {"stop_pct": 0.50, "target_pct": 1.00, "exit_dte": 5}

# When each agent may open new trades (US/Eastern, 24h).
ENTRY_WINDOWS = {
    "trend": ("10:00", "15:30"),
    "meanrev": ("15:00", "15:55"),
    "volsell": ("10:30", "15:00"),
    "baseline": ("09:45", "15:55"),
}
