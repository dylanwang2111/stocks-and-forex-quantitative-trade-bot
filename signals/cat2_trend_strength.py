"""
signals/cat2_trend_strength.py
Category 2: Trend Strength
ADX(14) — acts as a trend multiplier, not purely directional.
Timeframe: 15m
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from signals import SignalResult


# ADX thresholds
ADX_STRONG  = 25   # confirmed trend
ADX_WEAK    = 20   # no meaningful trend


def evaluate(df_15m: pd.DataFrame) -> SignalResult:
    """
    Returns:
        vote=+1  ADX > 25 AND +DI > -DI (trend present, bullish bias)
        vote=-1  ADX > 25 AND -DI > +DI (trend present, bearish bias)
        vote=0   ADX < 20 (ranging — weakens other trend signals in scorer)

    Note: When ADX is between 20–25 the vote is 0 (transition zone).
    """
    if len(df_15m) < 28:
        return SignalResult(0, "Insufficient data for ADX", {"bars": len(df_15m)})

    adx_df = ta.adx(df_15m["high"], df_15m["low"], df_15m["close"], length=14)

    if adx_df is None or adx_df.empty:
        return SignalResult(0, "ADX calculation failed")

    adx_val = float(adx_df["ADX_14"].iloc[-1])
    dmp_val = float(adx_df["DMP_14"].iloc[-1])   # +DI
    dmn_val = float(adx_df["DMN_14"].iloc[-1])   # -DI

    params = {
        "adx": round(adx_val, 2),
        "plus_di": round(dmp_val, 2),
        "minus_di": round(dmn_val, 2),
    }

    if adx_val > ADX_STRONG:
        if dmp_val > dmn_val:
            return SignalResult(
                +1,
                f"ADX={adx_val:.1f} — strong trend confirmed, bullish bias (+DI > -DI)",
                params,
            )
        else:
            return SignalResult(
                -1,
                f"ADX={adx_val:.1f} — strong trend confirmed, bearish bias (-DI > +DI)",
                params,
            )

    if adx_val < ADX_WEAK:
        return SignalResult(
            0,
            f"ADX={adx_val:.1f} — weak/no trend (ranging market)",
            params,
        )

    # Transition zone 20–25
    return SignalResult(
        0,
        f"ADX={adx_val:.1f} — transition zone, insufficient conviction",
        params,
    )
