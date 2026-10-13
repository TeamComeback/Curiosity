import math

import numpy as np
import pandas as pd


def sma(s, n):
    return s.rolling(n).mean()


def rsi(close, n):
    d = close.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100)


def atr(df, n=20):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def realized_vol(close, n=20):
    r = np.log(close / close.shift()).dropna()
    return float(r.tail(n).std() * math.sqrt(252))


def enrich(df):
    df = df.copy()
    c = df["close"]
    df["sma5"], df["sma50"], df["sma200"] = sma(c, 5), sma(c, 50), sma(c, 200)
    df["rsi2"] = rsi(c, 2)
    df["atr20"] = atr(df, 20)
    df["vol20"] = df["volume"].rolling(20).mean()
    df["roc20"] = c.pct_change(20)
    return df
