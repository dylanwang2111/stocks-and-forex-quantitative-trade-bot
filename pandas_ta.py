"""
pandas_ta.py — compatibility shim for Python 3.10.

The published pandas_ta 0.4.x requires Python ≥ 3.12.
This module reimplements the subset of pandas_ta functions
used by this codebase using pure pandas/numpy so that the
existing `import pandas_ta as ta` lines work unchanged.

Implemented:
  ema, rsi, macd, adx, stoch, bbands, atr, obv, cdl_pattern
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(series: pd.Series, length: int = 9, **kwargs) -> pd.Series | None:
    if series is None or len(series) < length:
        return None
    result = series.ewm(span=length, adjust=False).mean()
    result.name = f"EMA_{length}"
    return result


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(series: pd.Series, length: int = 14, **kwargs) -> pd.Series | None:
    if series is None or len(series) < length + 1:
        return None
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=length - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=length - 1, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result.name = f"RSI_{length}"
    return result


# ── MACD ──────────────────────────────────────────────────────────────────────

def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
    **kwargs,
) -> pd.DataFrame | None:
    if series is None or len(series) < slow + signal:
        return None
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    col_macd = f"MACD_{fast}_{slow}_{signal}"
    col_sig  = f"MACDs_{fast}_{slow}_{signal}"
    col_hist = f"MACDh_{fast}_{slow}_{signal}"
    df = pd.DataFrame({col_macd: macd_line, col_sig: signal_line, col_hist: hist})
    return df


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
    **kwargs,
) -> pd.Series | None:
    if high is None or len(high) < length + 1:
        return None
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    result = tr.ewm(com=length - 1, adjust=False).mean()
    result.name = f"ATRr_{length}"
    return result


# ── ADX ───────────────────────────────────────────────────────────────────────

def adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    length: int = 14,
    **kwargs,
) -> pd.DataFrame | None:
    if high is None or len(high) < length * 2:
        return None
    high = high.astype(np.float64)
    low  = low.astype(np.float64)
    close = close.astype(np.float64)

    prev_high  = high.shift(1)
    prev_low   = low.shift(1)
    prev_close = close.shift(1)

    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)

    dm_plus  = high - prev_high
    dm_minus = prev_low - low
    dm_plus  = dm_plus.where((dm_plus > dm_minus) & (dm_plus > 0), 0.0)
    dm_minus = dm_minus.where((dm_minus > dm_plus) & (dm_minus > 0), 0.0)

    atr_s  = tr.ewm(com=length - 1, adjust=False).mean()
    dmp_s  = dm_plus.ewm(com=length - 1, adjust=False).mean()
    dmn_s  = dm_minus.ewm(com=length - 1, adjust=False).mean()

    di_plus  = 100 * dmp_s / atr_s.replace(0, np.nan)
    di_minus = 100 * dmn_s / atr_s.replace(0, np.nan)

    dx_num = (di_plus - di_minus).abs()
    dx_den = (di_plus + di_minus).replace(0, np.nan)
    dx = 100 * dx_num / dx_den
    adx_s = dx.ewm(com=length - 1, adjust=False).mean()

    col_adx = f"ADX_{length}"
    col_dmp = f"DMP_{length}"
    col_dmn = f"DMN_{length}"
    df = pd.DataFrame({col_adx: adx_s, col_dmp: di_plus, col_dmn: di_minus})
    return df


# ── Stochastic ────────────────────────────────────────────────────────────────

def stoch(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k: int = 14,
    d: int = 3,
    smooth_k: int = 3,
    **kwargs,
) -> pd.DataFrame | None:
    if high is None or len(high) < k + d:
        return None
    lowest  = low.rolling(window=k).min()
    highest = high.rolling(window=k).max()
    raw_k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    stoch_k = raw_k.rolling(window=smooth_k).mean()
    stoch_d = stoch_k.rolling(window=d).mean()
    col_k = f"STOCHk_{k}_{d}_{smooth_k}"
    col_d = f"STOCHd_{k}_{d}_{smooth_k}"
    df = pd.DataFrame({col_k: stoch_k, col_d: stoch_d})
    return df


# ── Bollinger Bands ───────────────────────────────────────────────────────────

def bbands(
    series: pd.Series,
    length: int = 20,
    std: float = 2.0,
    **kwargs,
) -> pd.DataFrame | None:
    if series is None or len(series) < length:
        return None
    mid   = series.rolling(window=length).mean()
    sigma = series.rolling(window=length).std(ddof=0)
    upper = mid + std * sigma
    lower = mid - std * sigma
    std_str = f"{float(std)}"
    col_l = f"BBL_{length}_{std_str}"
    col_m = f"BBM_{length}_{std_str}"
    col_u = f"BBU_{length}_{std_str}"
    df = pd.DataFrame({col_l: lower, col_m: mid, col_u: upper})
    return df


# ── OBV ───────────────────────────────────────────────────────────────────────

def obv(close: pd.Series, volume: pd.Series, **kwargs) -> pd.Series | None:
    if close is None or len(close) < 2:
        return None
    direction = np.sign(close.diff()).fillna(0)
    result = (direction * volume).cumsum()
    result.name = "OBV"
    return result


# ── Candlestick patterns ──────────────────────────────────────────────────────

def cdl_pattern(
    open_: pd.Series,
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    name: str | list[str] = "all",
    **kwargs,
) -> pd.DataFrame | None:
    """
    Lightweight implementation.  Returns a DataFrame with one column per
    requested pattern.  Values: 100 (bullish), -100 (bearish), 0 (none).
    Only implements the patterns actually requested by the codebase:
      hammer, doji, engulfing, shootingstar, invertedhammer
    """
    if isinstance(name, str):
        names = [name] if name != "all" else [
            "hammer", "doji", "engulfing", "shootingstar", "invertedhammer"
        ]
    else:
        names = list(name)

    n = len(close)
    results: dict[str, list[int]] = {p: [0] * n for p in names}

    o = open_.values.astype(float)
    h = high.values.astype(float)
    l = low.values.astype(float)
    c = close.values.astype(float)

    for i in range(n):
        body      = abs(c[i] - o[i])
        rng       = h[i] - l[i]
        if rng == 0:
            continue
        body_top    = max(o[i], c[i])
        body_bottom = min(o[i], c[i])
        upper_wick  = h[i] - body_top
        lower_wick  = body_bottom - l[i]

        for p in names:
            if p == "hammer":
                # Bullish: lower wick >= 2x body, upper wick small, close > open
                if (body > 0 and lower_wick >= 2 * body
                        and upper_wick <= 0.3 * body and c[i] > o[i]):
                    results[p][i] = 100
            elif p == "invertedhammer":
                # Bullish reversal at bottom: upper wick >= 2x body, close > open
                if (body > 0 and upper_wick >= 2 * body
                        and lower_wick <= 0.3 * body and c[i] > o[i]):
                    results[p][i] = 100
            elif p == "shootingstar":
                # Bearish: upper wick >= 2x body, close < open
                if (body > 0 and upper_wick >= 2 * body
                        and lower_wick <= 0.3 * body and c[i] < o[i]):
                    results[p][i] = -100
            elif p == "doji":
                # Body <= 10% of range
                if rng > 0 and body / rng <= 0.1:
                    # Slight bias toward bearish at top, bullish at bottom — return 0 (neutral)
                    results[p][i] = 0
            elif p == "engulfing":
                if i == 0:
                    continue
                prev_body_top    = max(o[i - 1], c[i - 1])
                prev_body_bottom = min(o[i - 1], c[i - 1])
                # Bullish engulfing
                if (c[i - 1] < o[i - 1]           # prev bearish
                        and c[i] > o[i]            # curr bullish
                        and o[i] < prev_body_bottom
                        and c[i] > prev_body_top):
                    results[p][i] = 100
                # Bearish engulfing
                elif (c[i - 1] > o[i - 1]          # prev bullish
                        and c[i] < o[i]            # curr bearish
                        and o[i] > prev_body_top
                        and c[i] < prev_body_bottom):
                    results[p][i] = -100

    return pd.DataFrame(results, index=close.index)
