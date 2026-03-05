"""
data/fetcher.py
yfinance-based multi-timeframe data fetcher with 15-min cache TTL.
"""
from __future__ import annotations

import time
from typing import Dict

import pandas as pd
import yfinance as yf

from data.preprocessor import clean

# ── yfinance symbol mapping ────────────────────────────────────────────────────
_SYMBOL_MAP: dict[str, str] = {
    # Forex pairs
    "EURUSD": "EURUSD=X",
    "GBPUSD": "GBPUSD=X",
    "USDJPY": "JPY=X",
    "AUDUSD": "AUDUSD=X",
    "USDCAD": "CAD=X",
    "USDCHF": "CHF=X",
    # Stocks / ETFs — yfinance uses the same ticker, listed explicitly for clarity
    "SPY":    "SPY",
    "QQQ":    "QQQ",
    "IWM":    "IWM",
    "GLD":    "GLD",
    "XLK":    "XLK",
    "XLF":    "XLF",
    "AAPL":   "AAPL",
    "MSFT":   "MSFT",
    "NVDA":   "NVDA",
    "GOOGL":  "GOOGL",
    "AMZN":   "AMZN",
    "META":   "META",
    "TSLA":   "TSLA",
    "JPM":    "JPM",
    "GS":     "GS",
    "BAC":    "BAC",
    "JNJ":    "JNJ",
    "UNH":    "UNH",
    "XOM":    "XOM",
    "CVX":    "CVX",
    "WMT":    "WMT",
    "COST":   "COST",
    "CAT":    "CAT",
}

# ── Timeframe config ───────────────────────────────────────────────────────────
# yfinance (interval, period) pairs — period must be ≥ interval limit
_TF_CONFIG: dict[str, tuple[str, str]] = {
    "5m":  ("5m",  "5d"),
    "15m": ("15m", "60d"),
    "1h":  ("1h",  "730d"),
    "1d":  ("1d",  "5y"),
}

# ── In-memory cache ────────────────────────────────────────────────────────────
_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 900  # 15 minutes in seconds


def _yf_symbol(symbol: str) -> str:
    return _SYMBOL_MAP.get(symbol.upper(), symbol.upper())


def _cache_key(symbol: str, timeframe: str) -> str:
    return f"{symbol}:{timeframe}"


def fetch_candles(
    symbol: str,
    timeframe: str = "1h",
    period: str | None = None,
    use_cache: bool = True,
) -> pd.DataFrame:
    """
    Fetch OHLCV candles for symbol at given timeframe.

    Args:
        symbol:    Bot-internal symbol (e.g. "EURUSD", "NVDA")
        timeframe: One of "5m", "15m", "1h", "1d"
        period:    Override the default yfinance period string (e.g. "30d")
        use_cache: Return cached data if < 15 min old

    Returns:
        pd.DataFrame with columns: open, high, low, close, volume
        Index: DatetimeTzAware (UTC)
    """
    if timeframe not in _TF_CONFIG:
        raise ValueError(f"Unknown timeframe '{timeframe}'. Valid: {list(_TF_CONFIG)}")

    key = _cache_key(symbol, timeframe)
    if use_cache and key in _CACHE:
        cached_at, cached_df = _CACHE[key]
        if time.time() - cached_at < _CACHE_TTL:
            return cached_df.copy()

    yf_sym = _yf_symbol(symbol)
    interval, default_period = _TF_CONFIG[timeframe]
    dl_period = period or default_period

    ticker = yf.Ticker(yf_sym)
    raw = ticker.history(interval=interval, period=dl_period, auto_adjust=True)

    if raw.empty:
        raise RuntimeError(
            f"yfinance returned empty data for {yf_sym} "
            f"(interval={interval}, period={dl_period})"
        )

    df = _normalise(raw)
    df = clean(df)

    _CACHE[key] = (time.time(), df)
    return df.copy()


def fetch_multi_tf(symbol: str) -> Dict[str, pd.DataFrame]:
    """
    Fetch 5m, 15m, and 1h candles for symbol in one call.

    Returns:
        {"5m": df5m, "15m": df15m, "1h": df1h}
    """
    result: dict[str, pd.DataFrame] = {}
    for tf in ("5m", "15m", "1h"):
        result[tf] = fetch_candles(symbol, tf)
    return result


def fetch_daily(symbol: str, years: int = 3) -> pd.DataFrame:
    """Fetch daily candles for the given number of years (used in backtesting)."""
    period = f"{years}y"
    return fetch_candles(symbol, "1d", period=period, use_cache=False)


def clear_cache() -> None:
    """Clear all cached data (useful for testing)."""
    _CACHE.clear()


# ── Internal helpers ───────────────────────────────────────────────────────────

def _normalise(raw: pd.DataFrame) -> pd.DataFrame:
    """Standardise column names to lowercase."""
    df = raw.copy()
    df.columns = [c.lower() for c in df.columns]
    # Keep only OHLCV
    df = df[["open", "high", "low", "close", "volume"]].copy()
    # Ensure UTC timezone
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    else:
        df.index = df.index.tz_convert("UTC")
    return df
