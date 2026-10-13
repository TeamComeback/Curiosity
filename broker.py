"""Thin Alpaca REST client (paper trading + market data)."""
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests

import config


class AlpacaError(Exception):
    pass


class Broker:
    def __init__(self):
        if not (config.API_KEY and config.API_SECRET):
            raise AlpacaError("Missing ALPACA_API_KEY / ALPACA_API_SECRET")
        self.s = requests.Session()
        self.s.headers.update({
            "APCA-API-KEY-ID": config.API_KEY,
            "APCA-API-SECRET-KEY": config.API_SECRET,
            "accept": "application/json",
        })

    # ---------- plumbing ----------
    def _req(self, method, url, **kw):
        for attempt in range(3):
            r = self.s.request(method, url, timeout=30, **kw)
            if r.status_code == 429:
                time.sleep(2 + attempt * 2)
                continue
            if r.status_code >= 400:
                raise AlpacaError(f"{method} {url} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.text else {}
        raise AlpacaError(f"Rate limited: {url}")

    def _t(self, method, path, **kw):
        return self._req(method, config.PAPER_BASE + path, **kw)

    def _d(self, path, **kw):
        return self._req("GET", config.DATA_BASE + path, **kw)

    # ---------- account ----------
    def clock(self):
        return self._t("GET", "/v2/clock")

    def account(self):
        return self._t("GET", "/v2/account")

    def positions(self):
        return {p["symbol"]: p for p in self._t("GET", "/v2/positions")}

    # ---------- stock data ----------
    def daily_bars(self, symbols, days=320):
        start = (datetime.now(timezone.utc) - timedelta(days=int(days * 1.5))).strftime("%Y-%m-%d")
        out, token = {}, None
        while True:
            params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start,
                      "feed": config.STOCK_FEED, "limit": 10000, "adjustment": "split"}
            if token:
                params["page_token"] = token
            js = self._d("/v2/stocks/bars", params=params)
            for sym, bars in (js.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = js.get("next_page_token")
            if not token:
                break
        frames = {}
        for sym, bars in out.items():
            df = pd.DataFrame(bars).rename(columns={"t": "time", "o": "open", "h": "high",
                                                     "l": "low", "c": "close", "v": "volume"})
            df["time"] = pd.to_datetime(df["time"])
            frames[sym] = df.set_index("time")[["open", "high", "low", "close", "volume"]]
        return frames

    def latest_prices(self, symbols):
        js = self._d("/v2/stocks/trades/latest", params={"symbols": ",".join(symbols), "feed": config.STOCK_FEED})
        return {s: float(t["p"]) for s, t in (js.get("trades") or {}).items()}

    # ---------- options data ----------
    def option_chain(self, underlying, type_, exp_gte, exp_lte, strike_gte=None, strike_lte=None):
        out, token = {}, None
        while True:
            params = {"feed": config.OPTION_FEED, "type": type_, "limit": 1000,
                      "expiration_date_gte": exp_gte, "expiration_date_lte": exp_lte}
            if strike_gte is not None:
                params["strike_price_gte"] = strike_gte
            if strike_lte is not None:
                params["strike_price_lte"] = strike_lte
            if token:
                params["page_token"] = token
            js = self._d(f"/v1beta1/options/snapshots/{underlying}", params=params)
            out.update(js.get("snapshots") or {})
            token = js.get("next_page_token")
            if not token:
                break
        return out

    def option_quotes(self, symbols):
        if not symbols:
            return {}
        js = self._d("/v1beta1/options/quotes/latest",
                     params={"symbols": ",".join(symbols), "feed": config.OPTION_FEED})
        return js.get("quotes") or {}

    # ---------- orders ----------
    def submit_stock(self, symbol, qty, side, client_id):
        """Fractional quantities allowed (market, day)."""
        q = f"{qty:.6f}".rstrip("0").rstrip(".")
        body = {"symbol": symbol, "qty": q, "side": side, "type": "market",
                "time_in_force": "day", "client_order_id": client_id}
        return self._t("POST", "/v2/orders", json=body)

    def submit_option(self, symbol, qty, side, limit_price, intent, client_id):
        body = {"symbol": symbol, "qty": str(int(qty)), "side": side, "type": "limit",
                "limit_price": f"{limit_price:.2f}", "time_in_force": "day",
                "position_intent": intent, "client_order_id": client_id}
        return self._t("POST", "/v2/orders", json=body)

    def submit_mleg(self, legs, qty, limit_price, client_id):
        body = {"order_class": "mleg", "qty": str(qty), "type": "limit",
                "limit_price": f"{limit_price:.2f}", "time_in_force": "day",
                "client_order_id": client_id, "legs": legs}
        return self._t("POST", "/v2/orders", json=body)

    def order(self, order_id):
        return self._t("GET", f"/v2/orders/{order_id}")

    def cancel(self, order_id):
        try:
            self._t("DELETE", f"/v2/orders/{order_id}")
        except AlpacaError:
            pass

    def wait_fill(self, order_id, seconds=20):
        end = time.time() + seconds
        o = {}
        while time.time() < end:
            o = self.order(order_id)
            if o.get("status") == "filled":
                return o
            if o.get("status") in ("canceled", "rejected", "expired"):
                return o
            time.sleep(1.5)
        self.cancel(order_id)
        return self.order(order_id)
