"""
data/fetcher.py
Multi-source OHLCV fetcher with 15-min cache TTL.

Real-time routing (period=None):
  Forex  → OANDA (primary) → IBKR (fallback) → yfinance (fallback)
  Stocks → IBKR  (primary) → yfinance (fallback)

Historical / backtest (period explicitly provided):
  All instruments → yfinance only (broker APIs don't serve years of history cheaply)
"""
from __future__ import annotations

import logging
import time
from typing import Dict

import pandas as pd
import yfinance as yf

from data.preprocessor import clean

logger = logging.getLogger(__name__)

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
    "OXY":    "OXY",
    "COP":    "COP",
    "SLB":    "SLB",
    "HAL":    "HAL",
    "MPC":    "MPC",
    "VLO":    "VLO",
    "XLE":    "XLE",
    # Gold & precious metals
    "GOLD":   "GOLD",
    "NEM":    "NEM",
    "GDX":    "GDX",
    "GDXJ":   "GDXJ",
    # Macro proxies (used internally for macro context scoring)
    "USO":    "USO",    # US Oil ETF (crude oil price proxy)
    "^VIX":   "^VIX",  # CBOE Volatility Index (fear gauge)
    # Consumer / Industrials
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

# OANDA REST granularity + candle count for real-time fetch
_OANDA_TF: dict[str, tuple[str, int]] = {
    "5m":  ("M5",  500),
    "15m": ("M15", 500),
    "1h":  ("H1",  730),
    "1d":  ("D",   500),
}

# IBKR (barSizeSetting, durationStr) for reqHistoricalData
_IBKR_TF: dict[str, tuple[str, str]] = {
    "5m":  ("5 mins",  "2 D"),
    "15m": ("15 mins", "5 D"),
    "1h":  ("1 hour",  "30 D"),
    "1d":  ("1 day",   "5 Y"),
}

# ── In-memory cache ────────────────────────────────────────────────────────────
_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 900  # 15 minutes in seconds

_RETRY_DELAYS = (10, 30, 60)  # seconds between retries on rate-limit


# ── Asset type helper ──────────────────────────────────────────────────────────

def _is_forex(symbol: str) -> bool:
    """
    Return True if symbol is a forex pair.
    Checks UNIVERSE_BY_SYMBOL first; falls back to 6-char all-alpha heuristic.
    """
    try:
        from portfolio.watchlist import UNIVERSE_BY_SYMBOL
        inst = UNIVERSE_BY_SYMBOL.get(symbol.upper())
        if inst is not None:
            return inst.asset_type == "forex"
    except Exception:
        pass
    sym = symbol.upper().replace("/", "")
    return len(sym) == 6 and sym.isalpha()


# ── OANDA backend ──────────────────────────────────────────────────────────────

def _oanda_instrument(symbol: str) -> str:
    """Convert 'EURUSD' → 'EUR_USD' for OANDA REST API."""
    sym = symbol.upper().replace("/", "")
    return f"{sym[:3]}_{sym[3:]}"


def _fetch_oanda(symbol: str, timeframe: str) -> pd.DataFrame:
    """
    Fetch real-time OHLCV from OANDA REST API using oandapyV20.
    Returns a normalised DataFrame with UTC index. Raises on failure.
    """
    import oandapyV20
    import oandapyV20.endpoints.instruments as v20_instruments

    from config.settings import settings

    api_key = settings.oanda.api_key
    account_env = settings.oanda.environment  # "practice" | "live"
    if not api_key:
        raise RuntimeError("OANDA_API_KEY not configured")

    granularity, count = _OANDA_TF[timeframe]
    instrument = _oanda_instrument(symbol)

    client = oandapyV20.API(
        access_token=api_key,
        environment=account_env,
    )
    params = {
        "count": count,
        "granularity": granularity,
        "price": "M",  # midpoint (bid/ask average)
    }
    req = v20_instruments.InstrumentsCandles(instrument=instrument, params=params)
    resp = client.request(req)

    candles = resp.get("candles", [])
    if not candles:
        raise RuntimeError(f"OANDA returned no candles for {instrument}")

    rows = []
    for c in candles:
        if not c.get("complete", True):
            continue  # skip in-progress candle
        mid = c["mid"]
        rows.append({
            "time":   c["time"],
            "open":   float(mid["o"]),
            "high":   float(mid["h"]),
            "low":    float(mid["l"]),
            "close":  float(mid["c"]),
            "volume": float(c.get("volume", 0)),
        })

    if not rows:
        raise RuntimeError(f"OANDA returned only incomplete candles for {instrument}")

    df = pd.DataFrame(rows)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df


# ── IBKR backend ───────────────────────────────────────────────────────────────

def _ibkr_contract(symbol: str, is_forex: bool):
    """Build ib_insync contract object for the given symbol."""
    from ib_insync import Forex, Stock

    if is_forex:
        sym = symbol.upper().replace("/", "")
        return Forex(sym)  # e.g. Forex("EURUSD") → CASH on IDEALPRO
    return Stock(symbol.upper(), "SMART", "USD")


def _fetch_ibkr(symbol: str, timeframe: str) -> pd.DataFrame:
    """
    Fetch real-time OHLCV from IBKR via ib_insync reqHistoricalData.
    Uses client_id + 1 to avoid conflict with the execution agent.
    Returns a normalised DataFrame with UTC index. Raises on failure.

    ib_insync's eventkit dependency calls asyncio.get_event_loop() at import
    time. APScheduler's ThreadPoolExecutor threads have no event loop — set one
    before any ib_insync import so eventkit initialises correctly.
    """
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    from ib_insync import IB, util

    from config.settings import settings

    host     = settings.ibkr.host
    port     = settings.ibkr.port
    clientId = settings.ibkr.client_id + 1  # +1 avoids conflict with execution agent

    bar_size, duration = _IBKR_TF[timeframe]
    forex = _is_forex(symbol)
    what_to_show = "MIDPOINT" if forex else "TRADES"
    contract = _ibkr_contract(symbol, forex)

    ib = IB()
    try:
        ib.connect(host, port, clientId=clientId, timeout=10, readonly=True)
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=False,
            formatDate=2,  # UTC timestamps
        )
    finally:
        if ib.isConnected():
            ib.disconnect()

    if not bars:
        raise RuntimeError(f"IBKR returned no bars for {symbol}")

    df = util.df(bars)[["date", "open", "high", "low", "close", "volume"]].copy()
    df = df.rename(columns={"date": "time"})
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.set_index("time").sort_index()
    return df


# ── yfinance backend ───────────────────────────────────────────────────────────

def _fetch_with_retry(yf_sym: str, interval: str, period: str) -> pd.DataFrame:
    """
    Fetch OHLCV via yf.download() with exponential backoff on rate-limit errors.
    yf.download() is less aggressively rate-limited than Ticker.history().
    """
    for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
        try:
            raw = yf.download(
                yf_sym,
                period=period,
                interval=interval,
                auto_adjust=True,
                progress=False,
                multi_level_index=False,
            )
            return raw
        except Exception as exc:
            if "rate limit" in str(exc).lower() or "too many requests" in str(exc).lower():
                if delay is None:
                    raise
                logger.warning("yfinance rate limit; retry %d/%d in %ds…", attempt, len(_RETRY_DELAYS), delay)
                time.sleep(delay)
            else:
                raise
    return pd.DataFrame()  # unreachable


def _fetch_yfinance(symbol: str, timeframe: str, period: str) -> pd.DataFrame:
    """Fetch via yfinance and return a raw (un-normalised) DataFrame."""
    yf_sym = _SYMBOL_MAP.get(symbol.upper(), symbol.upper())
    interval, _ = _TF_CONFIG[timeframe]
    return _fetch_with_retry(yf_sym, interval=interval, period=period)


# ── Routing ────────────────────────────────────────────────────────────────────

def _fetch_realtime(symbol: str, timeframe: str) -> pd.DataFrame:
    """
    Fetch real-time OHLCV using broker APIs with yfinance fallback.

    Routing:
      Forex  → OANDA → IBKR → yfinance
      Stocks → IBKR  → yfinance
    """
    forex = _is_forex(symbol)
    errors: list[str] = []

    if forex:
        # 1. OANDA primary
        try:
            df = _fetch_oanda(symbol, timeframe)
            logger.debug("fetch_realtime %s/%s: OANDA OK (%d bars)", symbol, timeframe, len(df))
            return df
        except Exception as exc:
            errors.append(f"OANDA: {exc}")
            logger.warning("fetch_realtime %s/%s OANDA failed: %s", symbol, timeframe, exc)

        # 2. IBKR fallback
        try:
            df = _fetch_ibkr(symbol, timeframe)
            logger.debug("fetch_realtime %s/%s: IBKR OK (%d bars)", symbol, timeframe, len(df))
            return df
        except Exception as exc:
            errors.append(f"IBKR: {exc}")
            logger.warning("fetch_realtime %s/%s IBKR failed: %s", symbol, timeframe, exc)

    else:
        # 1. IBKR primary (stocks)
        try:
            df = _fetch_ibkr(symbol, timeframe)
            logger.debug("fetch_realtime %s/%s: IBKR OK (%d bars)", symbol, timeframe, len(df))
            return df
        except Exception as exc:
            errors.append(f"IBKR: {exc}")
            logger.warning("fetch_realtime %s/%s IBKR failed: %s", symbol, timeframe, exc)

    # Final fallback: yfinance
    _, default_period = _TF_CONFIG[timeframe]
    try:
        raw = _fetch_yfinance(symbol, timeframe, default_period)
        if raw.empty:
            raise RuntimeError("empty response")
        logger.debug("fetch_realtime %s/%s: yfinance fallback OK", symbol, timeframe)
        return raw
    except Exception as exc:
        errors.append(f"yfinance: {exc}")

    raise RuntimeError(
        f"All data sources failed for {symbol}/{timeframe}: " + "; ".join(errors)
    )


# ── Public API ─────────────────────────────────────────────────────────────────

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
        period:    Override the default period string (e.g. "30d").
                   When provided, always uses yfinance (historical/backtest context).
                   When None, uses broker APIs with yfinance fallback (real-time context).
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

    if period is not None:
        # Historical / backtest path → yfinance only
        raw = _fetch_yfinance(symbol, timeframe, period)
        if raw.empty:
            raise RuntimeError(
                f"yfinance returned empty data for {symbol} "
                f"(timeframe={timeframe}, period={period})"
            )
    else:
        # Real-time path → broker APIs with fallback
        raw = _fetch_realtime(symbol, timeframe)

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
