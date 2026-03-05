"""
signals/cat6_price_structure.py
Category 6: Price Structure
Swing S/R levels + candlestick pattern detection.
Timeframe: 1h
"""
from __future__ import annotations

import pandas as pd
import pandas_ta as ta

from signals import SignalResult


SWING_LOOKBACK = 20       # bars to look back for swing highs/lows
SR_PROXIMITY_PCT = 0.003  # price must be within 0.3% of S/R level


def evaluate(df_1h: pd.DataFrame) -> SignalResult:
    """
    Bullish  (+1): price near swing support + bullish candle pattern
    Bearish  (-1): price near swing resistance + bearish candle pattern
    Neutral  (0):  no S/R proximity or no confirming pattern
    """
    if len(df_1h) < SWING_LOOKBACK + 5:
        return SignalResult(0, "Insufficient data for price structure", {"bars": len(df_1h)})

    close = df_1h["close"]
    high  = df_1h["high"]
    low   = df_1h["low"]
    open_ = df_1h["open"]

    current_price = float(close.iloc[-1])

    # ── Swing S/R detection ────────────────────────────────────────────────────
    recent = df_1h.iloc[-(SWING_LOOKBACK + 1):-1]  # exclude last bar
    swing_highs = _find_swing_highs(recent["high"])
    swing_lows  = _find_swing_lows(recent["low"])

    near_support    = _is_near_level(current_price, swing_lows, SR_PROXIMITY_PCT)
    near_resistance = _is_near_level(current_price, swing_highs, SR_PROXIMITY_PCT)

    # ── Candlestick patterns (pandas-ta) ───────────────────────────────────────
    cdl = ta.cdl_pattern(
        open_, high, low, close,
        name=["hammer", "doji", "engulfing", "shootingstar", "invertedhammer"]
    )

    # pandas-ta returns a DataFrame; each column is 100 (bullish), -100 (bearish), or 0
    bullish_pattern = False
    bearish_pattern = False
    pattern_name = "none"

    if cdl is not None and not cdl.empty:
        last_row = cdl.iloc[-1]
        for col in last_row.index:
            val = int(last_row[col])
            if val == 100:
                bullish_pattern = True
                pattern_name = col
                break
            elif val == -100:
                bearish_pattern = True
                pattern_name = col
                break

    # Fallback: manual hammer / shooting star detection
    if not bullish_pattern and not bearish_pattern:
        bullish_pattern, bearish_pattern, pattern_name = _manual_patterns(
            open_, high, low, close
        )

    params = {
        "price": round(current_price, 5),
        "near_support": near_support,
        "near_resistance": near_resistance,
        "swing_lows": [round(v, 5) for v in swing_lows[:3]],
        "swing_highs": [round(v, 5) for v in swing_highs[:3]],
        "pattern": pattern_name,
        "bullish_pattern": bullish_pattern,
        "bearish_pattern": bearish_pattern,
    }

    if near_support and bullish_pattern:
        return SignalResult(
            +1,
            f"Near swing support + {pattern_name} — bullish structure",
            params,
        )

    if near_resistance and bearish_pattern:
        return SignalResult(
            -1,
            f"Near swing resistance + {pattern_name} — bearish structure",
            params,
        )

    return SignalResult(0, "No S/R proximity with confirming pattern", params)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _find_swing_highs(high_series: pd.Series, order: int = 3) -> list[float]:
    """Find local swing highs using rolling max comparison."""
    vals = high_series.values
    highs = []
    for i in range(order, len(vals) - order):
        window = vals[i - order: i + order + 1]
        if vals[i] == max(window):
            highs.append(float(vals[i]))
    return sorted(set(highs), reverse=True)[:5]  # top 5 unique levels


def _find_swing_lows(low_series: pd.Series, order: int = 3) -> list[float]:
    """Find local swing lows using rolling min comparison."""
    vals = low_series.values
    lows = []
    for i in range(order, len(vals) - order):
        window = vals[i - order: i + order + 1]
        if vals[i] == min(window):
            lows.append(float(vals[i]))
    return sorted(set(lows))[:5]  # bottom 5 unique levels


def _is_near_level(price: float, levels: list[float], pct: float) -> bool:
    for level in levels:
        if level > 0 and abs(price - level) / level <= pct:
            return True
    return False


def _manual_patterns(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
) -> tuple[bool, bool, str]:
    """Simple manual hammer/shooting-star detection as fallback."""
    o = float(open_.iloc[-1])
    h = float(high.iloc[-1])
    l = float(low.iloc[-1])
    c = float(close.iloc[-1])

    body = abs(c - o)
    candle_range = h - l
    if candle_range == 0:
        return False, False, "none"

    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)

    # Hammer: lower wick >= 2x body, upper wick small, closes near high
    if lower_wick >= 2 * body and upper_wick <= 0.3 * body and body > 0:
        return True, False, "hammer"

    # Shooting star: upper wick >= 2x body, lower wick small
    if upper_wick >= 2 * body and lower_wick <= 0.3 * body and body > 0:
        return False, True, "shooting_star"

    # Bullish engulfing (compare last 2 bars)
    if len(close) >= 2:
        prev_o = float(open_.iloc[-2])
        prev_c = float(close.iloc[-2])
        if prev_c < prev_o and c > o and c > prev_o and o < prev_c:
            return True, False, "bullish_engulfing"
        if prev_c > prev_o and c < o and c < prev_o and o > prev_c:
            return False, True, "bearish_engulfing"

    return False, False, "none"
