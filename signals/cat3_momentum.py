"""
signals/cat3_momentum.py
Category 3: Momentum
RSI(14) + Stochastic(14,3,3) — combined oversold/overbought detection.
Timeframe: 1h
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from signals import SignalResult


RSI_OVERSOLD   = 35
RSI_OVERBOUGHT = 75   # raised from 65 — RSI 65-74 is healthy bull momentum, not a short signal
STOCH_OVERSOLD   = 20
STOCH_OVERBOUGHT = 80


def evaluate(df_1h: pd.DataFrame) -> SignalResult:
    """
    Bullish  (+1): RSI < 35 OR (Stoch %K crosses above %D while both < 20)
                   AND neither indicator is overbought
    Bearish  (-1): RSI > 65 OR (Stoch %K crosses below %D while both > 80)
                   AND neither indicator is oversold
    Neutral  (0):  mixed or mid-range signals
    """
    if len(df_1h) < 20:
        return SignalResult(0, "Insufficient data for momentum", {"bars": len(df_1h)})

    close = df_1h["close"]
    high  = df_1h["high"]
    low   = df_1h["low"]

    # RSI(14)
    rsi_series = ta.rsi(close, length=14)
    rsi_val = float(rsi_series.iloc[-1]) if rsi_series is not None else 50.0

    # Stochastic(14,3,3)
    stoch_df = ta.stoch(high, low, close, k=14, d=3, smooth_k=3)
    if stoch_df is None or stoch_df.empty:
        stoch_k = stoch_d = 50.0
        stoch_prev_k = stoch_prev_d = 50.0
    else:
        k_col = [c for c in stoch_df.columns if "STOCHk" in c][0]
        d_col = [c for c in stoch_df.columns if "STOCHd" in c][0]
        stoch_k = float(stoch_df[k_col].iloc[-1])
        stoch_d = float(stoch_df[d_col].iloc[-1])
        stoch_prev_k = float(stoch_df[k_col].iloc[-2]) if len(stoch_df) > 1 else stoch_k
        stoch_prev_d = float(stoch_df[d_col].iloc[-2]) if len(stoch_df) > 1 else stoch_d

    params = {
        "rsi": round(rsi_val, 2),
        "stoch_k": round(stoch_k, 2),
        "stoch_d": round(stoch_d, 2),
    }

    # Stochastic %K crosses above %D (bullish crossover while oversold)
    stoch_bull_cross = (
        stoch_prev_k <= stoch_prev_d
        and stoch_k > stoch_d
        and stoch_k < STOCH_OVERSOLD
    )
    # Stochastic %K crosses below %D (bearish crossover while overbought)
    stoch_bear_cross = (
        stoch_prev_k >= stoch_prev_d
        and stoch_k < stoch_d
        and stoch_k > STOCH_OVERBOUGHT
    )

    rsi_bull = rsi_val < RSI_OVERSOLD
    rsi_bear = rsi_val > RSI_OVERBOUGHT

    any_bullish = rsi_bull or stoch_bull_cross
    any_bearish = rsi_bear or stoch_bear_cross

    if any_bullish and not any_bearish:
        reason = []
        if rsi_bull:
            reason.append(f"RSI={rsi_val:.1f} oversold")
        if stoch_bull_cross:
            reason.append(f"Stoch bullish crossover ({stoch_k:.1f}/{stoch_d:.1f})")
        return SignalResult(+1, " + ".join(reason), params)

    if any_bearish and not any_bullish:
        reason = []
        if rsi_bear:
            reason.append(f"RSI={rsi_val:.1f} overbought")
        if stoch_bear_cross:
            reason.append(f"Stoch bearish crossover ({stoch_k:.1f}/{stoch_d:.1f})")
        return SignalResult(-1, " + ".join(reason), params)

    return SignalResult(0, f"RSI={rsi_val:.1f}, Stoch={stoch_k:.1f} — mid-range, no signal", params)
