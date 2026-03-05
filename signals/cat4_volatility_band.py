"""
signals/cat4_volatility_band.py
Category 4: Volatility / Bollinger Band
Price vs BB bands + ATR expansion check.
Timeframe: 1h
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from signals import SignalResult


def evaluate(df_1h: pd.DataFrame) -> SignalResult:
    """
    Bullish  (+1): price near/below lower BB AND ATR expanding (real move, not noise)
    Bearish  (-1): price near/above upper BB AND ATR expanding
    Neutral  (0):  price in middle band or ATR contracting (noise)
    """
    if len(df_1h) < 22:
        return SignalResult(0, "Insufficient data for volatility band", {"bars": len(df_1h)})

    close = df_1h["close"]
    high  = df_1h["high"]
    low   = df_1h["low"]

    # Bollinger Bands (20, 2)
    bb = ta.bbands(close, length=20, std=2)
    if bb is None or bb.empty:
        return SignalResult(0, "Bollinger Bands calculation failed")

    # Identify columns
    lower_col = [c for c in bb.columns if "BBL" in c][0]
    upper_col = [c for c in bb.columns if "BBU" in c][0]
    mid_col   = [c for c in bb.columns if "BBM" in c][0]

    lower = float(bb[lower_col].iloc[-1])
    upper = float(bb[upper_col].iloc[-1])
    mid   = float(bb[mid_col].iloc[-1])
    price = float(close.iloc[-1])

    band_width = upper - lower

    # ATR(14) expansion check
    atr = ta.atr(high, low, close, length=14)
    if atr is None or len(atr) < 5:
        atr_expanding = False
        atr_val = 0.0
    else:
        atr_val = float(atr.iloc[-1])
        # ATR expanding = current > 5-bar avg
        atr_avg_5 = float(atr.iloc[-5:].mean())
        atr_expanding = atr_val > atr_avg_5 * 1.05

    # Proximity thresholds — within 10% of band width
    proximity = band_width * 0.10
    near_lower = price <= lower + proximity
    near_upper = price >= upper - proximity

    params = {
        "price": round(price, 5),
        "bb_lower": round(lower, 5),
        "bb_upper": round(upper, 5),
        "bb_mid": round(mid, 5),
        "atr": round(atr_val, 5),
        "atr_expanding": atr_expanding,
    }

    if near_lower and atr_expanding:
        return SignalResult(
            +1,
            f"Price ({price:.4f}) near lower BB ({lower:.4f}), ATR expanding — bullish reversal zone",
            params,
        )

    if near_upper and atr_expanding:
        return SignalResult(
            -1,
            f"Price ({price:.4f}) near upper BB ({upper:.4f}), ATR expanding — bearish reversal zone",
            params,
        )

    if near_lower and not atr_expanding:
        return SignalResult(
            0,
            f"Price near lower BB but ATR not expanding — possible noise, no vote",
            params,
        )

    if near_upper and not atr_expanding:
        return SignalResult(
            0,
            f"Price near upper BB but ATR not expanding — possible noise, no vote",
            params,
        )

    return SignalResult(
        0,
        f"Price ({price:.4f}) inside BB [{lower:.4f}–{upper:.4f}] — no band signal",
        params,
    )
