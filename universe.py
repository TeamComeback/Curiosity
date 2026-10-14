"""The stock universe the scouts search.

Weekly:  every active, tradable, fractionable US stock/ETF on Alpaca, ranked by daily dollar
         volume; the most liquid ~500 (priced $5+) become the core list. Each one is tagged with
         the sector it trades like, by comparing its recent daily moves to the 11 sector ETFs.
Daily:   today's most-active stocks and biggest movers are added on top.
Always:  the firms' watchlists, held positions and (if connected) the screener's picks.
Saved to data/universe.json so every run and planning session uses the same list."""
from datetime import timedelta

import numpy as np
import pandas as pd

import config
import store

CORE_SIZE = 500
MIN_PRICE = 5.0
SECTOR_ETFS = {
    "XLK": "Technology", "XLF": "Financials", "XLE": "Energy", "XLV": "Health Care",
    "XLY": "Consumer Discretionary", "XLP": "Consumer Staples", "XLI": "Industrials",
    "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real Estate", "XLC": "Communication Services",
}
SECTORS = list(SECTOR_ETFS.values()) + ["Broad market / other"]
EXCHANGES = {"NYSE", "NASDAQ", "ARCA", "AMEX", "BATS"}


def load():
    return store.load("universe.json", {})


def core_symbols():
    u = load()
    return u.get("core") or list(config.UNIVERSE)


def sectors():
    return load().get("sectors", {})


def sector_of(sym):
    return sectors().get(sym) or SECTOR_ETFS.get(sym) or "Broad market / other"


def is_stale(days=7):
    u = load()
    built = u.get("built")
    return not built or (store.now_et().date() - pd.Timestamp(built).date()).days >= days


def _snapshots(b, symbols):
    out = {}
    for i in range(0, len(symbols), 200):
        chunk = symbols[i:i + 200]
        try:
            js = b._d("/v2/stocks/snapshots", params={"symbols": ",".join(chunk), "feed": config.STOCK_FEED})
            out.update(js if isinstance(js, dict) else {})
        except Exception as e:  # noqa: BLE001 - skip a bad batch, keep the rest
            print(f"[universe] snapshot batch failed: {type(e).__name__}")
    return out


def tag_sectors(bars):
    """Assign each stock the sector ETF its last 60 daily returns correlate with most."""
    rets = {s: df["close"].pct_change().tail(60) for s, df in bars.items() if len(df) >= 61}
    etf = {e: rets[e] for e in SECTOR_ETFS if e in rets}
    tags = {}
    for sym, r in rets.items():
        if sym in SECTOR_ETFS:
            tags[sym] = SECTOR_ETFS[sym]
            continue
        best, best_c = None, 0.35          # weak resemblance to every sector -> broad market / other
        for e, er in etf.items():
            joined = pd.concat([r, er], axis=1).dropna()
            if len(joined) < 40:
                continue
            c = float(np.corrcoef(joined.iloc[:, 0], joined.iloc[:, 1])[0, 1])
            if c > best_c:
                best, best_c = SECTOR_ETFS[e], c
        tags[sym] = best or "Broad market / other"
    return tags


def build_weekly(b):
    assets = b._t("GET", "/v2/assets", params={"status": "active", "asset_class": "us_equity"})
    pool = sorted({a["symbol"] for a in assets
                   if a.get("tradable") and a.get("fractionable") and a.get("exchange") in EXCHANGES
                   and a["symbol"].isalpha() and len(a["symbol"]) <= 5})
    print(f"[universe] {len(pool)} tradable, fractionable listings; ranking by dollar volume")
    snaps = _snapshots(b, pool)
    ranked = []
    for sym, sn in snaps.items():
        bar = (sn or {}).get("prevDailyBar") or (sn or {}).get("dailyBar") or {}
        px, vol = float(bar.get("c") or 0), float(bar.get("v") or 0)
        if px >= MIN_PRICE and vol > 0:
            ranked.append((px * vol, sym))
    ranked.sort(reverse=True)
    core = [s for _, s in ranked[:CORE_SIZE]]
    core = list(dict.fromkeys(core + list(config.UNIVERSE) + list(SECTOR_ETFS) + config.VOL_UNDERLYINGS))
    bars = {}
    for i in range(0, len(core), 50):
        try:
            bars.update(b.daily_bars(core[i:i + 50], days=120))
        except Exception as e:  # noqa: BLE001
            print(f"[universe] bars batch failed: {type(e).__name__}")
    tags = tag_sectors(bars)
    u = load()
    u.update({"built": store.now_et().isoformat(timespec="minutes"), "core": core, "sectors": tags, "pool": pool,
              "pool_size": len(pool), "ranked_size": len(ranked)})
    store.save("universe.json", u)
    counts = pd.Series(list(tags.values())).value_counts().to_dict() if tags else {}
    print(f"[universe] core list: {len(core)} symbols; sectors: {counts}")
    return u


def daily_extras(b):
    """Today's most active stocks and top movers (cached for the day)."""
    u = load()
    if u.get("daily", {}).get("date") == store.today() and u["daily"].get("filtered"):
        return u["daily"]["symbols"]
    syms = []
    try:
        js = b._d("/v1beta1/screener/stocks/most-actives", params={"by": "volume", "top": 50})
        syms += [x["symbol"] for x in js.get("most_actives", [])]
    except Exception as e:  # noqa: BLE001
        print(f"[universe] most-actives unavailable: {type(e).__name__}")
    try:
        js = b._d("/v1beta1/screener/stocks/movers", params={"top": 25})
        syms += [x["symbol"] for x in js.get("gainers", []) + js.get("losers", [])]
    except Exception as e:  # noqa: BLE001
        print(f"[universe] movers unavailable: {type(e).__name__}")
    pool = set(u.get("pool") or [])
    if not pool:                                  # older list without the pool saved: fetch it once
        try:
            assets = b._t("GET", "/v2/assets", params={"status": "active", "asset_class": "us_equity"})
            pool = {a["symbol"] for a in assets if a.get("tradable") and a.get("fractionable")
                    and a.get("exchange") in EXCHANGES and a["symbol"].isalpha() and len(a["symbol"]) <= 5}
            u["pool"] = sorted(pool)
        except Exception as e:  # noqa: BLE001
            print(f"[universe] asset list unavailable: {type(e).__name__}")
    # only real, fractionable listings: drops warrants, units and rights (e.g. ...W, ...U) that show up as movers
    syms = [s for s in dict.fromkeys(syms) if s.isalpha() and len(s) <= 5 and (not pool or s in pool)]
    u["daily"] = {"date": store.today(), "symbols": syms, "filtered": bool(pool)}
    store.save("universe.json", u)
    return syms


def candidates(b, state, extra=()):
    """Everything the scouts look at this run."""
    held = {p.get("underlying", p["symbol"]) for a in state["agents"].values() for p in a["positions"].values()}
    watch = {w["symbol"] for a in state["agents"].values() if a.get("status") == "active"
             for w in (a.get("plan") or {}).get("watchlist", [])}
    try:
        daily = daily_extras(b)
    except Exception:  # noqa: BLE001
        daily = []
    return list(dict.fromkeys(core_symbols() + daily + sorted(watch) + sorted(held) + list(extra)))
