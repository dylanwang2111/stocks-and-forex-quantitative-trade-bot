"""
signals/cat1_trend_direction.py
Category 1: Trend Direction
EMA9 vs EMA21 + MACD histogram — both must agree for a vote.
Timeframe: 15m
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from signals import SignalResult


def evaluate(df_15m: pd.DataFrame) -> SignalResult:
    """
    Returns:
        vote=+1 if EMA9 > EMA21 AND MACD histogram > 0 (bullish)
        vote=-1 if EMA9 < EMA21 AND MACD histogram < 0 (bearish)
        vote=0  if signals disagree or insufficient data
    """
    if len(df_15m) < 26:
        return SignalResult(0, "Insufficient data for trend direction", {"bars": len(df_15m)})

    close = df_15m["close"]

    ema9  = ta.ema(close, length=9)
    ema21 = ta.ema(close, length=21)

    if ema9 is None or ema21 is None:
        return SignalResult(0, "EMA calculation failed")

    ema9_val  = float(ema9.iloc[-1])
    ema21_val = float(ema21.iloc[-1])
    ema_bull  = ema9_val > ema21_val
    ema_bear  = ema9_val < ema21_val

    # MACD(12,26,9) — histogram = MACD line - Signal line
    macd_df = ta.macd(close, fast=12, slow=26, signal=9)
    if macd_df is None or macd_df.empty:
        return SignalResult(0, "MACD calculation failed")

    hist_col = [c for c in macd_df.columns if "h" in c.lower() or "hist" in c.lower()]
    if not hist_col:
        return SignalResult(0, "MACD histogram column not found")

    hist_val = float(macd_df[hist_col[0]].iloc[-1])
    macd_bull = hist_val > 0
    macd_bear = hist_val < 0

    params = {
        "ema9": round(ema9_val, 5),
        "ema21": round(ema21_val, 5),
        "macd_hist": round(hist_val, 6),
    }

    if ema_bull and macd_bull:
        return SignalResult(+1, "EMA9>EMA21 and MACD histogram positive — bullish trend", params)
    if ema_bear and macd_bear:
        return SignalResult(-1, "EMA9<EMA21 and MACD histogram negative — bearish trend", params)

    return SignalResult(0, "EMA and MACD disagree — no clear trend direction", params)
