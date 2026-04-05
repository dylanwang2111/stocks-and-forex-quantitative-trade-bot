# Agents

The bot's decision-making is distributed across five agents, each with a single responsibility. They communicate through typed dataclasses — no shared mutable state except through `PortfolioStateManager`.

---

## Orchestrator (`agents/orchestrator.py`)

The top-level controller. Owns the APScheduler loop, coordinates all other agents, and enforces the circuit breaker.

### Responsibilities
- Run the 15-minute scan-and-trade cycle
- Schedule weekly portfolio selection and daily pre-screen
- Evaluate exit conditions on all open positions
- Fire hourly portfolio snapshots
- Enforce the circuit breaker (halt trading on drawdown)
- Restore open positions from DB on startup

### Key Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `SCAN_INTERVAL_MINUTES` | 15 | Scan cycle frequency |
| `SWING_HOLDING_DAYS` | 7 | Force-close after N days |
| `CB_DAILY_LOSS_PCT` | 0.03 | Circuit breaker: 3% daily loss |
| `CB_CONSECUTIVE_LOSSES` | 5 | Circuit breaker: 5 consecutive losses |

### Startup Sequence

```python
orchestrator.start()
  1. init_db()                    — create tables if missing
  2. restore_from_db()            — reload open positions (stop/TP from latest snapshot)
  3. HealthMonitor.start()        — daemon thread
  4. Scheduler jobs registered:
     - scan_and_trade()           every 15 min
     - save_snapshot()            CronTrigger(minute=0) — top of every hour
     - _select_portfolio()        CronTrigger(day_of_week=0, hour=0, minute=0)
     - _run_prescreen()           CronTrigger(day_of_week='1-6', hour=5, minute=0)
  5. scheduler.start()            — blocking
```

### Scan-and-Trade Cycle

```python
def scan_and_trade():
    1. scanner.scan_all()                   → ScanResult[]
    2. _check_exits(positions)              → close stops/TPs/signal exits
    3. tradeable_opportunities(results)     → filtered list
    4. For each opportunity (fill all slots):
       a. RiskAgent.compute()               → RiskParams
       b. ExecutionAgent.place_order()      → OrderResult
       c. notify_trade_opened()
    5. Log cycle summary
```

### Exit Conditions (checked every 15 min)

Exits operate in two phases per position:

**Phase 1** — price has not yet reached TP:

| Condition | Trigger | Reason Tag |
|-----------|---------|------------|
| Stop-loss hit | price ≤ stop_price (long) or price ≥ stop_price (short) | `stop_loss` |
| Take-profit hit | price ≥ tp_price (long) or price ≤ tp_price (short) | `take_profit` |

When TP is breached on **2 consecutive cycles** (confirmation guard against single-candle spikes), the position enters Phase 2.

**Phase 2** — price has blown past TP (let the winner run):

| Condition | Trigger | Reason Tag |
|-----------|---------|------------|
| Trailing stop hit | Trailing stop at 2.1×ATR ratchets behind price; only moves in profitable direction | `trailing_stop` |

The hard TP is no longer used as an exit in Phase 2. `stop_price` becomes the trailing stop and is updated each cycle.

**Backstops (apply in both phases):**

| Condition | Trigger | Reason Tag |
|-----------|---------|------------|
| Stale trade | Held ≥ 48h AND price moved < 0.3% in signal direction | `stale_exit` |
| Max hold time | Position held ≥ `SWING_HOLDING_DAYS` | `time_exit` |
| Signal reversal | EMA9 crosses against direction on 1h (after ≥1 day held) | `signal_exit` |

### Circuit Breaker

Halts **new entries** (not exits) when either condition is met:
- Daily realized P&L < -3% of total capital
- 5 or more consecutive losing trades

Circuit breaker resets at midnight UTC (new trading day). All open positions continue to be monitored for exits even while the breaker is tripped.

---

## PortfolioAgent (`agents/portfolio_agent.py`)

Runs **weekly on Monday 00:00 UTC**. Scores all instruments in the CANDIDATE_POOL and selects the best 4 stocks + 2 crypto for the active trading universe.

### Data Used
- **60-day daily OHLCV** (yfinance bulk fetch → IBKR fallback)
- **Macro context**: VIX, GLD, USO 90-day daily (trend detection)
- **Fundamental data**: PE ratio, ROA, net margin, EPS growth (yfinance `Ticker.info`)

### Selection Algorithm

```
For each symbol in CANDIDATE_POOL (45 instruments):
  1. Fetch 60d daily OHLCV
  2. Compute EMA9, EMA21, EMA50
  3. Hard gate: EMA9 > EMA21 (stocks require EMA21 > EMA50 too for weekly)
     → Skip if gate fails
  4. Score:
     technical_score  (0–6) — ADX, return, RSI, volume, ATR%
     fundamental_score (0–4, stocks only) — PE, ROA, margin, EPS
     macro_score      (0–3) — sector-specific VIX/trend scoring
     total = technical + fundamental + macro

Sort stocks by total DESC, apply sector cap (max 2 per sector)
Select top 4 stocks + top 2 crypto (BTC force-included as anchor)
Call set_active_universe(selected)
Send Telegram: notify_portfolio_updated()
```

### Scoring Details

**Technical Score (0–6 pts)**:

| Check | Points |
|-------|--------|
| ADX(14) > 25 | +1 |
| ADX(14) > 40 | +1 (additive) |
| 20-day return > 2% | +1 |
| RSI(14) in [45, 70] | +1 |
| Avg volume (20d) > 500k | +1 (stocks only) |
| ATR% in [0.3%, 6%] | +1 |

**Fundamental Score (0–4 pts, stocks only)**:

| Check | Points |
|-------|--------|
| PE ratio ≤ 30 | +1 |
| PE ratio ≤ 18 | +1 (additive) |
| ROA ≥ 5% | +1 |
| Net margin ≥ 10% | +1 |
| EPS growth ≥ 10% YoY | +0.5 |

**Macro Score (0–3 pts, sector-specific)**:

| Sector | Condition | Points |
|--------|-----------|--------|
| Gold | Gold uptrend (GLD EMA20 > EMA60) | +1.5 |
| Gold | VIX ≥ 22 (flight to safety) | +1.0 |
| Gold | VIX ≥ 30 (extreme fear) | +0.5 (additive) |
| Energy | Oil uptrend (USO EMA20 > EMA60) | +1.5 |
| Energy | VIX ≥ 22 | +0.5 |
| Tech | VIX < 22 (risk-on) | +1.0 |
| Tech | VIX ≥ 30 | −1.0 |
| Broad (SPY, IWM) | Gold or oil uptrend | +0.5 |

Macro context fetches silently return neutral (0) on yfinance rate-limit — scoring continues without macro bonus.

### Sector Cap

Maximum 2 stocks from any single sector in the final universe:

```python
_SECTOR = {
    "energy":    {XOM, CVX, OXY, COP, SLB, HAL, MPC, VLO, XLE},
    "gold":      {GLD, GOLD, NEM, GDX, GDXJ},
    "tech":      {AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, QQQ, XLK},
    "finance":   {JPM, GS, BAC, XLF},
    "health":    {JNJ, UNH},
    "consumer":  {WMT, COST},
    "industrial":{CAT},
    "broad":     {SPY, IWM},
}
```

---

## PreScreenAgent (`agents/pre_screen_agent.py`)

Runs **daily at 05:00 UTC (Tue–Sun)**. Lighter version of PortfolioAgent using 30-day data and a softer EMA gate.

### Differences from PortfolioAgent

| | PortfolioAgent | PreScreenAgent |
|-|----------------|----------------|
| Schedule | Mon 00:00 UTC | Tue–Sun 05:00 UTC |
| Data lookback | 60 days | 30 days |
| EMA gate (stocks) | EMA9 > EMA21 > EMA50 | EMA9 > EMA21 only |
| Max stocks | 8 | 4 |
| Max crypto | 2 | 2 |
| BTCUSD | Selected on merit | Force-included as anchor |
| Stock selection | Sector-capped greedy | Correlation-aware greedy |

### Purpose

The daily pre-screen provides a mid-week universe refresh without the full weekly analysis. If market conditions shift significantly during the week (e.g. a sector crashes), the pre-screen can swap in better instruments within 24 hours rather than waiting until Monday.

---

## RiskAgent (`agents/risk_agent.py`)

Converts a `ConfidenceResult` + current price into concrete position sizing parameters.

### Algorithm

```
1. Reject if tier is NO_TRADE or WATCH → return None

2. broker_cap = settings.bot.broker_capital(broker)
   deployable  = broker_cap × (1 - cash_reserve_pct)

3. max_position_usd = deployable × 0.667   (leave 33% buffer for concurrent positions)

4. position_usd = max_position_usd × tier.size_fraction()
   # SMALL=0.25, MEDIUM=0.50, LARGE=0.75, FULL=1.00

5. Apply volatility/macro multiplier:
   - LLM-derived from Cat8 risk_level  (HIGH=0.5×, MEDIUM=0.75×, LOW=1.0×)

6. ATR volatility scaling (if ATR available):
   atr_pct = atr / entry_price
   if atr_pct > TARGET_ATR_PCT (2%):
       scale_factor = TARGET_ATR_PCT / atr_pct
       position_usd × max(scale_factor, 0.35)   ← floor at 35%

7. Clamp to available_cash(broker) — never exceed what's free

8. quantity = position_usd / entry_price
   → Stocks / Crypto: round to 4 decimal places (fractional shares)

9. Compute stops and take-profits:
   ATR-based (preferred):
     Long stop  = entry - ATR_SL_MULT × atr
     Long TP    = entry + ATR_TP_MULT × atr
     Short stop = entry + ATR_SL_MULT × atr
     Short TP   = entry - ATR_TP_MULT × atr
   Fixed % (fallback when ATR unavailable):
     stop = entry ± 1.5%
     tp   = entry ± 3.0%   (2:1 reward-to-risk)

10. risk_dollars = |entry - stop| × quantity

11. Risk cap enforcement:
    max_risk = broker_cap × RISK_PER_TRADE  (1%)
    if risk_dollars > max_risk:
        capped_qty = max_risk / |entry - stop|
        quantity = floor(capped_qty)
        recompute position_usd, risk_dollars
```

### ATR Multipliers

**Stop-loss** multipliers are fixed per asset type:

| Asset Type | SL Multiplier |
|------------|---------------|
| Stock | 2.0× ATR |
| Crypto | 2.0× ATR |

**Take-profit** multipliers scale with position tier:

| Position Tier | TP Multiplier |
|---------------|---------------|
| SMALL  | 4.0× ATR |
| MEDIUM | 5.0× ATR |
| LARGE  | 6.0× ATR |
| FULL   | 6.5× ATR |

### RiskParams Output

```python
@dataclass
class RiskParams:
    position_size_usd: float
    quantity: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    risk_dollars: float
    position_tier: str
    size_fraction: float
```

---

## ExecutionAgent (`agents/execution_agent.py`)

Places and closes orders, persists trades to the database, and syncs in-memory state.

### Order Routing

```
place_order(symbol, risk_params, direction, ...)
  │
  ├── asset_type == "stock"  →  _place_ibkr_order()
  │     ├── paper: synthetic fill at entry_price
  │     └── live:  limit order + bracket (stop + TP) via ib_insync
  │
  └── asset_type == "crypto"  →  _place_oanda_order()
        ├── paper: synthetic fill at entry_price
        └── live:  market order with SL/TP via oandapyV20
```

### Paper Mode

In paper mode, both brokers simulate fills:
- `filled_price = risk_params.entry_price`
- No network calls to IBKR or OANDA
- Trade is recorded in DB and position added to in-memory state exactly as in live mode

### Post-Fill Actions

After a successful fill (paper or live):
1. Resolve `filled_price` (use `entry_price` if broker returns None — safety net)
2. Insert `Trade` row in DB (status=`"open"`)
3. Create `Position` object and add to `PortfolioStateManager`
4. Return `OrderResult(success=True, filled_price=..., broker=...)`

### Close Position

```
close_position(position, reason)
  │
  ├── Get exit price from broker (or use last known price for paper)
  │
  ├── state_manager.close_position(symbol, exit_price, exit_time)
  │     ├── Remove from in-memory _positions dict
  │     └── Update DB: Trade.status="closed", exit_price, pnl_usd, pnl_pct
  │
  └── Return OrderResult
```

### OrderResult

```python
@dataclass
class OrderResult:
    success: bool
    order_id: str | None
    filled_price: float | None
    error: str | None
    broker: str   # "ibkr" | "oanda"
```
