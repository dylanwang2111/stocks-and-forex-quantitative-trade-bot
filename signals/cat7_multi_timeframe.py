"""
signals/cat7_multi_timeframe.py
Category 7: Multi-Timeframe Alignment (DOUBLE WEIGHT = ±2 votes)
EMA9 vs EMA21 alignment across 5m, 15m, and 1h simultaneously.
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from signals import SignalResult


def evaluate(
    df_5m:  pd.DataFrame,
    df_15m: pd.DataFrame,
    df_1h:  pd.DataFrame,
) -> SignalResult:
    """
    Checks EMA9 vs EMA21 on each timeframe.

    Returns (double weight):
        vote=+2  All 3 timeframes bullish
        vote=+1  2 of 3 timeframes bullish, 0 bearish
        vote= 0  Mixed or no agreement
        vote=-1  2 of 3 timeframes bearish, 0 bullish
        vote=-2  All 3 timeframes bearish
    """
    results: dict[str, int] = {}
    details: dict[str, dict] = {}

    for label, df in [("5m", df_5m), ("15m", df_15m), ("1h", df_1h)]:
        vote, meta = _ema_vote(df, label)
        results[label] = vote
        details[label] = meta

    bull_count = sum(1 for v in results.values() if v > 0)
    bear_count = sum(1 for v in results.values() if v < 0)

    params = {
        "votes": results,
        "details": details,
        "bull_timeframes": bull_count,
        "bear_timeframes": bear_count,
    }

    if bull_count == 3:
        return SignalResult(+2, "All 3 timeframes bullish (5m+15m+1h EMA9>EMA21)", params)
    if bull_count == 2 and bear_count == 0:
        bullish_tfs = [tf for tf, v in results.items() if v > 0]
        return SignalResult(+1, f"2/3 timeframes bullish ({'+'.join(bullish_tfs)})", params)
    if bear_count == 3:
        return SignalResult(-2, "All 3 timeframes bearish (5m+15m+1h EMA9<EMA21)", params)
    if bear_count == 2 and bull_count == 0:
        bearish_tfs = [tf for tf, v in results.items() if v < 0]
        return SignalResult(-1, f"2/3 timeframes bearish ({'+'.join(bearish_tfs)})", params)

    return SignalResult(0, f"Mixed MTF alignment ({results}) — no vote", params)


def _ema_vote(df: pd.DataFrame, label: str) -> tuple[int, dict]:
    if len(df) < 21:
        return 0, {"error": f"Not enough bars ({len(df)}) for {label}"}

    close = df["close"]
    ema9  = ta.ema(close, length=9)
    ema21 = ta.ema(close, length=21)

    if ema9 is None or ema21 is None:
        return 0, {"error": "EMA calculation failed"}

    v9  = float(ema9.iloc[-1])
    v21 = float(ema21.iloc[-1])

    meta = {"ema9": round(v9, 5), "ema21": round(v21, 5)}

    if v9 > v21:
        return +1, meta
    if v9 < v21:
        return -1, meta
    return 0, meta
