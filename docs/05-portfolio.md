# Portfolio Management

Four components manage the instrument universe and position state.

---

## Watchlist (`portfolio/watchlist.py`)

Defines all tradeable instruments and their metadata.

### Instrument

```python
@dataclass
class Instrument:
    symbol: str               # e.g. "NVDA", "BTCUSD"
    broker: str               # "ibkr" | "oanda"
    asset_type: str           # "stock" | "crypto"
    yf_symbol: str            # Yahoo Finance ticker (e.g. "BTC-USD")
    active_hours_utc: tuple   # (open_hour, close_hour) — e.g. (13, 20) for US stocks
    slippage_pct: float       # Expected slippage (0.001 = 0.1%)
    min_position_usd: float   # Minimum position size
    correlated_with: list[str]  # Symbols that cannot be held simultaneously
```

### CANDIDATE_POOL (38 instruments)

The full universe evaluated weekly/daily by PortfolioAgent/PreScreenAgent:

| Sector | Symbols |
|--------|---------|
| Broad ETFs | SPY, QQQ, IWM |
| Tech ETFs | XLK |
| Finance ETF | XLF |
| Energy ETF | XLE |
| Tech stocks | AAPL, MSFT, NVDA, AMD, GOOGL, AMZN, META, TSLA |
| Finance | JPM, GS, BAC |
| Healthcare | JNJ, UNH, LLY |
| Energy | XOM, CVX, OXY, COP, SLB, HAL, MPC, VLO |
| Gold | GLD, GOLD, NEM, GDX, GDXJ |
| Consumer | WMT, COST |
| Industrial | CAT |
| Crypto | BTCUSD, ETHUSD |

### Active UNIVERSE

A thread-safe subset of CANDIDATE_POOL (6–10 instruments) that the scanner evaluates every 15 minutes. Updated weekly by PortfolioAgent and daily by PreScreenAgent.

```python
# Atomic universe swap (thread-safe)
set_active_universe(instruments: list[Instrument])

# Read snapshot (consistent view for one scan cycle)
get_universe_snapshot() → list[Instrument]
```

Both methods use a `threading.Lock()` to prevent the scanner from reading a partially-updated list mid-swap.

### Correlation Rules

Instruments with high correlation cannot be held simultaneously. Groups:

| Group | Instruments |
|-------|-------------|
| Broad market | SPY ↔ QQQ |
| Tech cluster | QQQ ↔ NVDA, AMD, MSFT, AAPL |
| Energy cluster | XOM ↔ CVX ↔ OXY ↔ COP ↔ XLE |
| Gold cluster | GOLD ↔ NEM ↔ GDX ↔ GDXJ |
| Consumer | WMT ↔ COST |
| Healthcare | JNJ ↔ UNH |
| Crypto | BTCUSD ↔ ETHUSD |

---

## Scanner (`portfolio/scanner.py`)

Evaluates every instrument in the active universe every 15 minutes and returns ranked `ScanResult` objects.

### Per-Instrument Flow

```
For each instrument in get_universe_snapshot():
  1. Skip if position already open for this symbol
  2. Skip if outside active trading hours
  3. fetch_candles(symbol, "5m" / "15m" / "1h")
  4. RegimeDetector.detect(df_1h) → RegimeContext
  5. SignalEngine.evaluate(symbol, dfs) → SignalBundle
  6. ConfidenceScorer.score(bundle, regime) → ConfidenceResult
  7. Log signal to SignalLog table (DB)
  8. EventGuard.is_blocked(symbol) → skip if earnings/FOMC
  9. CorrelationGuard.is_allowed(symbol) → skip if correlated position held
  10. PDTTracker.can_day_trade(symbol) → skip if PDT limit reached (stocks)
  11. EMA50(1h) trend filter:
      - Long: blocked if price < EMA50(1h)
      - Short: blocked if price > EMA50(1h)
  Return ScanResult
```

### ScanResult

```python
@dataclass
class ScanResult:
    symbol: str
    confidence_result: ConfidenceResult
    regime: RegimeContext
    bundle: SignalBundle
    blocked: bool
    block_reason: str         # e.g. "correlation: BTCUSD held", "PDT limit"
    atr: float                # from 1h data (used for adaptive stops)
    ema50_1h: float           # for EMA50 filter
    current_price: float      # last 1h close
```

### Output Methods

```python
scan_all() → list[ScanResult]
# Returns all results sorted by dominant_score DESC

tradeable_opportunities(results) → list[ScanResult]
# Filters: not blocked + tier >= SMALL + EMA50 filter passes + slot available
# Used to fill ALL open position slots in one cycle

top_opportunity(results) → ScanResult | None
# Single best unblocked opportunity (legacy, still available)
```

### Logging

Every scan cycle logs per-instrument at INFO level:

```
scan: NVDA   | dir=long  bull=66.7 bear= 0.0 score=66.7 tier=SMALL
scan: BTCUSD | dir=short bull= 0.0 bear=55.6 score=55.6 tier=SMALL
scan: XOM    | dir=long  bull=44.4 bear=11.1 score=44.4 tier=NO_TRADE
```

---

## Portfolio State Manager (`portfolio/state.py`)

Thread-safe in-memory position registry backed by SQLite.

### Capital Model

```python
broker_capital(broker)     # IBKR_CAPITAL or OANDA_CAPITAL from settings
cash_reserve(broker)       # broker_capital × CASH_RESERVE_PCT (30%)
deployable(broker)         # broker_capital × 0.70
deployed_capital(broker)   # Σ(entry_price × quantity) for open positions
available_cash(broker)     # deployable − deployed  (min 0)
```

### Position Object

```python
@dataclass
class Position:
    symbol: str
    broker: str               # "ibkr" | "oanda"
    direction: str            # "long" | "short"
    quantity: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    confidence: float
    position_tier: str
    entry_time: datetime
    db_trade_id: int          # FK into Trade table

    def unrealized_pnl(self, current_price: float) -> float:
        if direction == "long":
            return (current_price - entry_price) × quantity
        else:
            return (entry_price - current_price) × quantity

    def cost_basis(self) -> float:
        return entry_price × quantity
```

### Key Methods

```python
add_position(position)             # Register new open position
close_position(symbol, exit_price, exit_time)  # Remove + update DB
get_position(symbol) → Position | None
all_positions() → list[Position]
can_open_position(symbol) → bool   # Not held + under MAX_POSITIONS
deployed_capital(broker=None)
available_cash(broker=None)
daily_pnl() → float               # Sum of realized P&L for today (UTC)
```

### Position Restore on Startup

On every startup, `restore_from_db()` reloads any positions that were open when the bot last shut down:

```python
restore_from_db() → int:
  1. Query Trade WHERE status='open'
  2. For each open trade, look up stop/TP from latest PortfolioSnapshot.positions_detail JSON
  3. Reconstruct Position objects and load into _positions dict
  4. Fallback: use 1.5% / 3.0% stop/TP if snapshot unavailable
  5. Return count of restored positions
```

This ensures no positions are lost across restarts.

---

## PDT Tracker (`portfolio/pdt_tracker.py`)

Enforces the Pattern Day Trader (PDT) rule for US stock accounts under $25,000.

### The Rule

A **day trade** is opening and closing a stock position on the same calendar day. Accounts under $25,000 are limited to **3 day trades per rolling 5-business-day window**.

Crypto positions are exempt from PDT rules.

### How It Works

```python
can_day_trade() → bool
# Counts same-day closes from Trade table in past 5 business days
# Returns False if count >= PDT_LIMIT (3)

is_day_trade(symbol, entry_time) → bool
# Returns True if entry_time is today (UTC) — closing would be a day trade

record_day_trade(symbol, trade_id)
# Logs the day trade for future rolling count
```

### Scanner Integration

Before entering a stock trade, the scanner calls `can_day_trade()`. If the limit is reached:
- The instrument is marked `blocked=True` with reason `"PDT: 3/3 day trades used"`
- A Telegram warning is sent: `notify_pdt_warning(used=3, limit=3)`
- The instrument is skipped for the remainder of the week

### Swing Mode Fallback

When PDT limit is reached, the bot automatically enters swing-only mode for stocks:
- Existing positions are held normally (exits still fire)
- No new stock entries for the rest of the 5-day window
- Crypto trading continues unaffected
