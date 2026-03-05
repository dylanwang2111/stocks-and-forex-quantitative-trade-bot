"""
signals/cat5_volume.py
Category 5: Volume
Volume ratio vs 20-period MA + OBV 10-bar trend.
Timeframe: 5m intraday, 1h swing
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pandas_ta as ta

from signals import SignalResult


VOLUME_RATIO_THRESHOLD = 1.5   # current vol must be 1.5x average


def evaluate(df: pd.DataFrame) -> SignalResult:
    """
    Bullish  (+1): vol_ratio > 1.5 on up candle AND OBV 10-bar trend rising
    Bearish  (-1): vol_ratio > 1.5 on down candle AND OBV 10-bar trend falling
    Neutral  (0):  low volume or conflicting OBV

    Works with any timeframe DataFrame (auto-detects from data length).
    """
    if len(df) < 22:
        return SignalResult(0, "Insufficient data for volume analysis", {"bars": len(df)})

    close  = df["close"]
    volume = df["volume"]

    # ── Volume ratio ───────────────────────────────────────────────────────────
    vol_ma = volume.rolling(20).mean()
    vol_ma_val = float(vol_ma.iloc[-1])
    current_vol = float(volume.iloc[-1])

    if vol_ma_val <= 0:
        return SignalResult(0, "Volume MA is zero — possibly forex data without volume")

    vol_ratio = current_vol / vol_ma_val

    # ── Candle direction ───────────────────────────────────────────────────────
    candle_bullish = float(close.iloc[-1]) >= float(close.iloc[-2])

    # ── OBV 10-bar trend ──────────────────────────────────────────────────────
    obv = ta.obv(close, volume)
    if obv is None or len(obv) < 11:
        obv_rising = None
    else:
        obv_10_ago = float(obv.iloc[-10])
        obv_now    = float(obv.iloc[-1])
        obv_rising = obv_now > obv_10_ago

    params = {
        "vol_ratio": round(vol_ratio, 2),
        "vol_ma": round(vol_ma_val, 0),
        "current_vol": round(current_vol, 0),
        "candle_bullish": candle_bullish,
        "obv_rising": obv_rising,
    }

    high_volume = vol_ratio >= VOLUME_RATIO_THRESHOLD

    # For forex (near-zero volume), relax the check and use OBV trend only
    if vol_ma_val < 1:
        if obv_rising is True:
            return SignalResult(+1, "OBV trending up (forex volume proxy)", params)
        if obv_rising is False:
            return SignalResult(-1, "OBV trending down (forex volume proxy)", params)
        return SignalResult(0, "No volume signal (forex)", params)

    if high_volume and candle_bullish and obv_rising:
        return SignalResult(
            +1,
            f"High volume ({vol_ratio:.1f}x avg) on up candle + OBV rising — bullish participation",
            params,
        )

    if high_volume and not candle_bullish and obv_rising is False:
        return SignalResult(
            -1,
            f"High volume ({vol_ratio:.1f}x avg) on down candle + OBV falling — bearish participation",
            params,
        )

    return SignalResult(
        0,
        f"Volume ratio={vol_ratio:.1f}x — insufficient or conflicting signals",
        params,
    )
