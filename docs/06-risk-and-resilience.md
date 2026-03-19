# Risk & Resilience

Five independent subsystems protect the bot from market risk, broker failures, and regulatory violations.

---

## Market Regime Detector (`regime/detector.py`)

Classifies the current market environment from 1-hour OHLCV data. The regime is used to scale signal category weights up or down before confidence scoring.

### Regimes

| Regime | Conditions | Trading Implication |
|--------|------------|---------------------|
| `TRENDING_UP` | ADX > 25 AND EMA20 > EMA50 | Boost trend-following signals |
| `TRENDING_DOWN` | ADX > 25 AND EMA20 < EMA50 | Boost trend-following signals (short side) |
| `RANGING` | ADX ≤ 25 | Boost mean-reversion and oscillators |
| `HIGH_VOLATILITY` | ATR ratio > 1.8× OR BB rank > 90th pct | Dampen all signals (0.7×) |
| `LOW_VOLATILITY` | BB rank < 10th pct AND ADX < 20 | Boost mean-reversion, dampen trend |

### Detection Logic

```
Requires minimum 50 bars of 1h data.

1. ATR(14) / 30-day avg ATR  → volatility_ratio
2. BB width percentile vs 6-month history → bb_rank (0–100)
3. ADX(14)
4. EMA20 vs EMA50

Priority order:
  HIGH_VOLATILITY if volatility_ratio > 1.8 OR bb_rank > 90
  LOW_VOLATILITY  if bb_rank < 10 AND ADX < 20
  TRENDING_UP     if ADX > 25 AND EMA20 > EMA50
  TRENDING_DOWN   if ADX > 25 AND EMA20 < EMA50
  RANGING         (default)
```

### Signal Multipliers by Regime

| Category | TRENDING | RANGING | HIGH_VOL | LOW_VOL |
|----------|----------|---------|----------|---------|
| Cat 1 (Trend) | 1.2× | 0.7× | 0.7× | 0.9× |
| Cat 2 (ADX) | 1.2× | 0.7× | 0.7× | 0.9× |
| Cat 3 (MACD) | 1.0× | 1.2× | 0.7× | 1.1× |
| Cat 4 (BB) | 0.8× | 1.3× | 0.7× | 1.2× |
| Cat 5 (Volume) | 1.1× | 0.9× | 0.7× | 0.8× |
| Cat 6 (ROC) | 1.0× | 1.3× | 0.7× | 1.2× |
| Cat 7 (MTF) | 1.2× | 0.7× | 0.7× | 0.9× |
| Cat 8 (Macro) | 1.0× | 1.0× | 1.0× | 1.0× |

---

## Circuit Breaker (`agents/orchestrator.py`)

Hard stop on new entries when losses exceed thresholds. Implemented in the orchestrator.

### Trigger Conditions

| Condition | Threshold | Reset |
|-----------|-----------|-------|
| Daily loss | > 3% of total capital | Midnight UTC |
| Consecutive losses | ≥ 5 in a row | After a winning trade |

### Behavior

- **New entries**: Blocked entirely while tripped
- **Open positions**: Continue to be monitored and closed normally
- **Telegram alert**: `notify_circuit_breaker(reason)` fires when tripped
- **Logging**: `[CIRCUIT BREAKER TRIPPED]` logged at WARNING level

### Implementation

```python
class CircuitBreaker:
    def check(self, daily_pnl: float, consecutive_losses: int) -> bool:
        daily_pct = daily_pnl / settings.bot.total_capital
        if daily_pct < -CB_DAILY_LOSS_PCT:
            return True   # tripped
        if consecutive_losses >= CB_CONSECUTIVE_LOSSES:
            return True
        return False
```

The orchestrator tracks `cb_consecutive` — incremented on each losing close, reset to 0 on a winning close.

---

## Correlation Guard (`resilience/correlation_guard.py`)

Prevents holding two highly correlated positions simultaneously. Applies at **trade entry time**, not during universe selection.

### Rules

1. **Pairwise correlation**: If symbol A is in `correlated_with` list of any currently-open position, entry is blocked.
2. **Forex limit**: Max `MAX_FOREX` forex positions simultaneously (default 2, set via `MAX_FOREX` env var).

### Examples

```
Open position: XLE (energy ETF)
Candidate: XOM → BLOCKED (XOM is in energy correlation group)
Candidate: CVX → BLOCKED
Candidate: NVDA → ALLOWED (different sector)

Open positions: EURUSD, USDJPY (2 forex, at MAX_FOREX default)
Candidate: AUDUSD → BLOCKED (forex limit reached)

Open positions: EURUSD (1 forex)
Candidate: USDJPY → ALLOWED (below MAX_FOREX limit)
Candidate: GBPUSD → BLOCKED (correlated with EURUSD via blacklist)
```

### API

```python
guard.is_allowed(candidate_symbol) → (bool, reason: str)
# Returns (True, "") if allowed
# Returns (False, "XOM is correlated with open position XLE") if blocked
```

---

## Event Guard (`events/event_guard.py`)

Blocks trading in a blackout window around scheduled market events.

### Blackout Windows

| Event Type | Pre-event | Post-event | Total |
|------------|-----------|------------|-------|
| Earnings | 2 hours | 4 hours | 6 hours |
| FOMC | Configurable | Configurable | ~24 hours |

### How It Works

```python
guard.is_blocked(symbol, asset_type) → (bool, reason: str)
  1. Check MacroCalendar for upcoming/recent events for this symbol
  2. If event_time within blackout window: return (True, reason)
  3. Otherwise: return (False, "")
```

### MacroCalendar (`events/macro_calendar.py`)

Stores scheduled earnings dates and FOMC dates. Queried by EventGuard at each scan cycle.

When an event blocks a trade, Telegram fires `notify_event_guard(symbol, reason)`:
```
🚫 BLACKOUT: NVDA
Earnings announcement in 90 minutes.
No new entries until blackout lifts.
```

---

## Health Monitor (`resilience/health_monitor.py`)

Daemon thread that heartbeats both brokers every 30 seconds and automatically reconnects on failure.

### States

| State | Meaning |
|-------|---------|
| `HEALTHY` | Broker responding normally |
| `DEGRADED` | Recent failures, retrying with backoff |
| `DOWN` | Repeated failures, full reconnect needed |

### Behavior

```
Every 30 seconds:
  1. Ping IBKR (ib_insync reqCurrentTime)
  2. Ping OANDA (lightweight API call)
  3. On failure: mark DEGRADED, start exponential backoff
     Backoffs: 30s → 60s → 120s → 240s → 300s (max)
  4. On reconnect success: mark HEALTHY, call on_reconnect()
  5. Log all state changes to EventLog table
```

### Reconnect Callback

When a broker reconnects, `on_reconnect(broker)` is called on the orchestrator:
- Reconciles in-memory positions with the broker's actual account state
- Logs a reconciliation event

### Dashboard Integration

Health status is visible in the dashboard under the **Status** page. The dashboard reads the latest `EventLog` entries to display connection state and history.
