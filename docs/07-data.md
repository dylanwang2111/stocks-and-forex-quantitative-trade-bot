# Data Layer

All OHLCV data flows through a single entry point: `data/fetcher.py`. It handles multi-source routing, symbol mapping, caching, and retries transparently.

---

## Fetch Entry Points

```python
from data.fetcher import fetch_candles, fetch_daily

# Real-time candles (for scanning)
df = fetch_candles(symbol, timeframe)           # uses cache
df = fetch_candles(symbol, timeframe, use_cache=False)  # bypasses cache

# Daily bars (for portfolio scoring)
df = fetch_daily(symbol, period="60d")         # always yfinance, no cache
```

Both return a normalized pandas DataFrame with columns: `open`, `high`, `low`, `close`, `volume` and a DatetimeIndex in UTC.

---

## Source Routing

### Real-time candles (`fetch_candles`)

```
Forex symbol?
  ├── YES:  OANDA (primary)
  │           └── fail → IBKR → fail → yfinance → fail → raise RuntimeError
  └── NO (stock/ETF):
            IBKR (primary)
              └── fail → yfinance → fail → raise RuntimeError
```

The scanner silently handles `RuntimeError` from `fetch_candles` — an instrument with no data is skipped for that cycle.

### Daily bars (`fetch_daily`)

Always uses yfinance. No broker fallback — this is only called during the weekly/daily portfolio selection when markets are closed.

---

## Timeframe Configuration

| Timeframe | yfinance interval | yfinance period | OANDA | IBKR |
|-----------|-------------------|-----------------|-------|------|
| `"5m"` | 5m | 5d | M5 (500 bars) | 5 mins, 2D |
| `"15m"` | 15m | 60d | M15 (500 bars) | 15 mins, 5D |
| `"1h"` | 1h | 730d | H1 (730 bars) | 1 hour, 30D |
| `"1d"` | 1d | 5y | D (500 bars) | 1 day, 5Y |

**Note**: yfinance gives 730 days of 1h data; IBKR gives only 30 days (~195 bars). EMA50(1h) is computable from either. EMA200(1h) is only reliably available via yfinance.

---

## Caching

```python
_CACHE: dict[str, tuple[float, pd.DataFrame]] = {}
_CACHE_TTL = 900  # 15 minutes
```

Cache key: `f"{symbol}_{timeframe}"`

On `fetch_candles(symbol, tf, use_cache=True)`:
1. Check if cache entry exists and age < 15 min
2. If valid: return `cached_df.copy()`
3. If stale or missing: fetch fresh, store in cache, return

The 15-min TTL aligns with the scan interval — each instrument is fetched once per cycle and reused within that cycle (e.g. when the snapshot fires near a scan).

The hourly snapshot (`save_snapshot`) uses `use_cache=True` to reuse prices already fetched during the most recent scan cycle, avoiding extra broker/yfinance calls.

---

## Symbol Mapping

yfinance uses non-standard tickers for forex. The `_SYMBOL_MAP` translates internal symbols to yfinance format:

| Internal | yfinance |
|----------|----------|
| `EURUSD` | `EURUSD=X` |
| `GBPUSD` | `GBPUSD=X` |
| `AUDUSD` | `AUDUSD=X` |
| `NZDUSD` | `NZDUSD=X` |
| `USDJPY` | `JPY=X` |
| `USDCAD` | `CAD=X` |
| `USDCHF` | `CHF=X` |

> **USD-base pairs** (USDJPY, USDCAD, USDCHF): in OANDA, 1 unit = 1 USD of base currency. Quantity sizing does NOT divide by price — `quantity = position_size_usd` directly. P&L is in the quote currency (JPY, CAD, CHF) and must be divided by the current price to convert to USD.

Stock and ETF symbols map 1:1 (`NVDA` → `NVDA`).

IBKR and OANDA use their own symbol formats handled inside `_fetch_ibkr()` and `_fetch_oanda()` respectively.

---

## IBKR Data Fetcher

Uses `ib_insync.reqHistoricalData()` with a short-lived connection per request.

```python
_fetch_ibkr(symbol, timeframe) → pd.DataFrame
  1. Connect to IB Gateway (host/port from settings)
  2. Build Contract (Stock or Forex)
  3. reqHistoricalData(durationStr, barSizeSetting, whatToShow="TRADES")
  4. Disconnect
  5. Normalize columns → lowercase OHLCV
```

Common failure modes:
- `clientId already in use` — previous connection wasn't released; retried with different client ID
- `No security definition found` — symbol not found in IBKR (falls through to yfinance)
- `TimeoutError` — IB Gateway not running or network issue

---

## OANDA Data Fetcher

Uses `oandapyV20.InstrumentsCandles` API.

```python
_fetch_oanda(symbol, timeframe) → pd.DataFrame
  1. Map symbol to OANDA format: "EURUSD" → "EUR_USD"
  2. Request candles (count=500, granularity from config)
  3. Parse bid/ask mid-point
  4. Normalize → OHLCV DataFrame
```

OANDA is only used for forex symbols. It provides the most reliable and lowest-latency forex data.

---

## yfinance Bulk Fetch

Portfolio scoring uses a single bulk download to avoid rate limits:

```python
# In PortfolioAgent / PreScreenAgent:
raw = yf.download(
    tickers=" ".join(symbols),   # all 40 at once
    period="60d",
    interval="1d",
    group_by="ticker",
    auto_adjust=True,
    progress=False,
)
```

A single bulk call is far less likely to be rate-limited than 40 sequential `Ticker.history()` calls. If the bulk call fails, the agent falls back to IBKR sequential fetch.

---

## Rate Limit Handling

yfinance rate limits (`YFRateLimitError`) are handled at multiple levels:

| Context | Behavior |
|---------|----------|
| Scan cycle (real-time) | IBKR primary → yfinance skipped or retried once |
| Portfolio scoring | Bulk fetch fails → IBKR sequential fallback |
| Macro context (VIX/GLD/USO/EVZ) | Silent fallback to neutral (0 macro score / EVZ=7.0) |
| Snapshot P&L | Cached price used (15-min TTL); WARNING logged if all sources fail |

The bot never blocks or crashes on a rate limit. All fetch paths have graceful fallbacks.

---

## DataFrame Format

All returned DataFrames are normalized:

```python
# Columns (lowercase):
df.columns  # → ['open', 'high', 'low', 'close', 'volume']

# Index: DatetimeIndex in UTC
df.index    # → DatetimeIndex(['2026-03-13 13:00:00+00:00', ...])

# Types: all float64
df.dtypes   # → float64 for all columns
```

Signals expect this exact format. The normalisation step runs inside each `_fetch_*` function before returning.
