"""Rapid Fire's VWAP pullback only fires on >= min_rel_volume (HQ item p20261031curiosity)."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import scalper

P = {"min_rel_volume": 1.5, "max_extension_pct": 1.5}


def _bars(last_vol_mult):
    n = 30
    close = [100 + i * 0.05 for i in range(n)]
    d = pd.DataFrame({"open": close, "high": [c + 0.02 for c in close], "low": [c - 0.02 for c in close],
                      "close": close, "volume": [1000.0] * n})
    for k in range(1, 4):                       # last 3 bars carry the tested volume
        d.loc[n - k, "volume"] = 1000.0 * last_vol_mult
    return d


def _fake_intraday(d):
    v = pd.Series([float(d["close"].iloc[-1]) - 0.05] * len(d))   # price just above VWAP
    return {"vwap": v, "ema9": pd.Series([2.0] * len(d)), "ema21": pd.Series([1.0] * len(d)),
            "avgvol": pd.Series([1000.0] * len(d)), "orb_high": 1e9}


def _run(mult, monkeypatch):
    monkeypatch.setattr(scalper, "intraday", _fake_intraday)
    d = _bars(mult)
    d.loc[len(d) - 2, "low"] = float(d["close"].iloc[-1]) - 0.05   # dipped to VWAP
    price = float(d["high"].iloc[-2]) + 0.01                          # turned back up
    skips = []
    return scalper.find_setup("XYZ", d, price, P, skips=skips), skips


def test_low_volume_rejected(monkeypatch):
    found, skips = _run(0.9, monkeypatch)
    assert found is None
    assert skips and skips[0]["rel_volume"] == 0.9


def test_heavy_volume_accepted(monkeypatch):
    found, skips = _run(1.6, monkeypatch)
    assert found and found["setup"] == "VWAP pullback bounce" and not skips
