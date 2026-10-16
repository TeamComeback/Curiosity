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
PLAN_MODEL = os.getenv("PLAN_MODEL", "") or BRAIN_MODEL                  # overnight game-plan sessions

SCREENER_URL = os.getenv("SCREENER_URL", "")
FRED_API_KEY = "".join(os.getenv("FRED_API_KEY", "").split())   # optional: economy data for the Crash Radar

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

UNIVERSE = [
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO", "AMD", "NFLX",
    "CRM", "ADBE", "ORCL", "INTC", "QCOM", "MU", "PLTR", "UBER", "SHOP", "COIN",
    "JPM", "BAC", "GS", "V", "MA", "XOM", "CVX", "LLY", "UNH", "JNJ",
    "COST", "WMT", "HD", "NKE", "DIS", "BA", "CAT", "GE", "SMH", "XLE",
]

# ---- The race ----
RACE = {
    "start_balance": 1000.0,
    "goal": 10000.0,
    "eliminate_below": 10.0,    # equity under this = eliminated
}

# Competing agents (each keeps its specialty) + a non-competing benchmark.
AGENTS = {
    "trend":   {"name": "Trend Co.", "competing": True,
                "instruments": ["stock", "call", "put"],
                "style": "Trend follower. Trades breakouts to new highs in established uptrends (or "
                         "breakdowns via puts), lets winners run with trailing stops, cuts losers fast."},
    "meanrev": {"name": "Snapback Partners", "competing": True,
                "instruments": ["stock", "call"],
                "style": "Mean-reversion trader. Buys sharp short-term washouts (RSI(2) extremes) in "
                         "stocks that are still above their 200-day average, and sells into the snap-back."},
    "volsell": {"name": "Premium House", "competing": True,
                "instruments": ["spread"],
                "style": "Volatility seller. Sells defined-risk credit spreads on index ETFs when implied "
                         "volatility is rich versus realized, collecting premium as time passes."},
    "blackswan": {"name": "Black Swan & Co.", "competing": True,
                  "instruments": ["put", "debit_spread", "stock"],
                  "style": "Crash and downside specialist: only bets on prices FALLING. Uses the Crash Radar "
                           "(0-100 market stress score) to size up. Calm market: put spreads on the weakest stocks "
                           "and sectors that are breaking down, plus cheap far-out index put spreads as insurance. "
                           "Warning: index and sector puts, inverse ETFs (SH, PSQ, RWM, DOG). Stress: goes in hard, "
                           "2-3x inverse ETFs (SQQQ, SPXU) allowed for a few days. Safe havens (TLT, GLD, UUP) when "
                           "money is running to safety. 'stock' here means ONLY inverse ETFs and safe havens; never "
                           "buys ordinary stocks. Takes profits in stages, because crash rallies reverse violently. "
                           "Its sector focus means the sectors it bets AGAINST."},
    "scalper": {"name": "Rapid Fire Trading", "competing": True, "instruments": ["stock"],
                "style": "Intraday scalper. Trades shares of the most liquid, most active stocks minute by minute: "
                         "opening-range breakouts, pullbacks to VWAP in a trending stock, and volume-backed momentum bursts. "
                         "Small quick targets, tight stops, a time limit on every trade, and always flat by the close. "
                         "Cash account: proceeds settle next day, so the day's cash is split into slices and each slice "
                         "gets one round trip a day. You set the playbook (which stocks, how aggressive, how many slices, "
                         "targets and stops); code executes it every 2 minutes."},
    "baseline": {"name": "Index Booth (SPY)", "competing": False, "instruments": ["stock"],
                 "style": "Buys SPY with its starting cash and holds. Not racing; it's the bar to beat."},
    "monkey": {"name": "Monkey Business", "competing": False, "instruments": ["stock"],
               "style": "Control group. Picks random stocks with random size and holding period under the "
                        "same rules. Not racing; if a firm can't beat it, the firm has no edge."},
}
# Index ETFs plus cheaper, liquid ETFs with $1 strikes so one spread doesn't tie up the whole account
VOL_UNDERLYINGS = ["SPY", "QQQ", "IWM", "XLF", "XLE", "EEM", "GDX", "SLV", "TLT"]
# Black Swan's toolkit and the Crash Radar's inputs
INVERSE_ETFS = {"SPY": "SH", "QQQ": "PSQ", "IWM": "RWM", "DIA": "DOG"}       # 1x: rise when the index falls
LEVERAGED_INVERSE = {"SPY": "SPXU", "QQQ": "SQQQ"}                          # 3x: stress only, short holds
SAFE_HAVENS = ["TLT", "GLD", "UUP"]
RADAR_SYMBOLS = ["SPY", "QQQ", "IWM", "DIA", "HYG", "IEF", "TLT", "GLD", "UUP"]
BEAR_SYMBOLS = list(dict.fromkeys(RADAR_SYMBOLS + list(INVERSE_ETFS.values()) + list(LEVERAGED_INVERSE.values())))

# ---- Hard rules (the only things agents cannot decide) ----
RULES = {
    "no_margin": True,                # can only spend its own available cash
    "options_allowed": ["call", "put", "spread", "debit_spread"],  # long options + defined-risk spreads; nothing naked
    # No daily trade limit, no position cap, no re-entry waiting period: each firm trades as often
    # as its analysis says, like any cash-account trader. The only ceiling is a malfunction guard
    # far above normal behavior, so a software bug can't fire off hundreds of orders.
    "max_new_trades_per_day": None,
    "max_open_positions": None,
    "reentry_cooldown_days": 0,
    "malfunction_guard_trades_per_day": 30,
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
    "scalper": {"target_pct": 0.6, "stop_pct": 0.3, "max_hold_min": 45, "slices": 4, "min_rel_volume": 1.5,
                "max_extension_pct": 1.5},
    "blackswan": {"breakdown_lookback": 20, "stop_atr": 2.0, "trail_atr": 3.0, "opt_min_dte": 45,
                  "opt_max_dte": 120, "opt_delta": 0.40, "spread_width_pct": 0.05, "insurance_pct": 0.08,
                  "stop_pct": 0.50, "exit_dte": 21, "leveraged_max_days": 5},
}
PARAM_BOUNDS = {
    "trend":   {"breakout_lookback": (10, 100), "stop_atr": (1.0, 5.0), "trail_atr": (1.5, 6.0),
                "min_rel_volume": (0.8, 3.0), "opt_min_dte": (7, 45), "opt_max_dte": (21, 120),
                "opt_delta": (0.25, 0.80)},
    "meanrev": {"rsi_entry": (3, 25), "rsi_exit": (50, 90), "stop_atr": (1.0, 5.0), "max_hold_days": (2, 20),
                "opt_min_dte": (7, 45), "opt_max_dte": (14, 90), "opt_delta": (0.25, 0.80)},
    "volsell": {"short_delta": (0.08, 0.40), "width": (1, 5), "min_dte": (7, 40), "max_dte": (14, 60),
                "take_profit": (0.25, 0.85), "stop_mult": (1.0, 3.0), "exit_dte": (0, 21), "min_iv_rv": (0.9, 1.6)},
    "scalper": {"target_pct": (0.2, 2.0), "stop_pct": (0.1, 1.0), "max_hold_min": (10, 180), "slices": (1, 8),
                "min_rel_volume": (1.0, 4.0), "max_extension_pct": (0.5, 3.0)},
    "blackswan": {"breakdown_lookback": (10, 60), "stop_atr": (1.0, 5.0), "trail_atr": (1.5, 6.0),
                  "opt_min_dte": (30, 90), "opt_max_dte": (60, 180), "opt_delta": (0.20, 0.60),
                  "spread_width_pct": (0.02, 0.12), "insurance_pct": (0.0, 0.15), "stop_pct": (0.30, 0.70),
                  "exit_dte": (10, 45), "leveraged_max_days": (2, 5)},
}

# Long-option default exits (agents can close earlier at any time)
OPTION_EXITS = {"stop_pct": 0.50, "target_pct": 1.00, "exit_dte": 5}

# When each agent may open new trades (US/Eastern, 24h): the whole session for every firm;
# when to act inside it is the firm's own call.
ENTRY_WINDOWS = {aid: ("09:35", "15:55") for aid in AGENTS}
