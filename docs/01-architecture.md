# Architecture Overview

## System Purpose

Automated trading bot for a $2,000 account. Scans 6–10 instruments every 15 minutes, scores signals across 8 independent categories, sizes positions using confidence-tier Half-Kelly, and executes via IBKR (stocks) and OANDA (forex).

Supports paper (simulated) and live trading modes.

---

## Component Map

```
main.py                         ← Entry point
│
├── agents/
│   ├── orchestrator.py         ← 15-min scan loop + scheduler
│   ├── portfolio_agent.py      ← Weekly universe selection (Mon 00:00 UTC)
│   ├── pre_screen_agent.py     ← Daily universe refresh (05:00 UTC, except Mon)
│   ├── signal_engine.py        ← Runs all 8 signal categories
│   ├── confidence_scorer.py    ← Votes → score → position tier
│   ├── risk_agent.py           ← Position sizing, stop/TP calculation
│   └── execution_agent.py      ← Order placement (IBKR / OANDA / paper)
│
├── signals/
│   ├── cat1_trend_direction.py ← EMA crossover + MACD
│   ├── cat2_trend_strength.py  ← ADX
│   ├── cat3_momentum.py        ← MACD line/signal
│   ├── cat4_volatility_band.py ← Bollinger Band breakout
│   ├── cat5_volume.py          ← OBV EMA crossover
│   ├── cat6_price_structure.py ← 5-bar price momentum
│   ├── cat7_multi_timeframe.py ← MTF consensus (double weight)
│   └── cat8_macro_news.py      ← LLM sentiment + macro risk level
│
├── portfolio/
│   ├── watchlist.py            ← Instrument universe + candidate pool
│   ├── scanner.py              ← Per-instrument evaluation
│   ├── state.py                ← In-memory + DB position tracking
│   └── pdt_tracker.py          ← Pattern Day Trader rule enforcement
│
├── regime/
│   └── detector.py             ← Market regime classification
│
├── resilience/
│   ├── correlation_guard.py    ← Block correlated simultaneous positions
│   └── health_monitor.py       ← Broker heartbeat + reconnect
│
├── events/
│   └── event_guard.py          ← Earnings + FOMC blackout windows
│
├── data/fetcher.py             ← OHLCV fetcher with routing + cache
├── database/models.py          ← SQLAlchemy ORM
├── notifications/telegram.py   ← Telegram alerts
├── backtesting/                ← Backtest + walk-forward engine
├── optimization/               ← LLM-guided parameter tuning
├── dashboard.py                ← Streamlit monitoring UI
└── validate.py                 ← Backtest CLI
```

---

## Data Flow: Every 15-Minute Scan Cycle

```
Orchestrator.scan_and_trade()
       │
       ▼
Scanner.scan_all(UNIVERSE)
   For each instrument:
   ├── DataFetcher.fetch_candles(symbol, "5m" / "15m" / "1h")
   │       └── IBKR → yfinance (stocks) | OANDA → IBKR → yfinance (forex)
   │
   ├── RegimeDetector.detect(df_1h)
   │       └── RegimeContext (TRENDING_UP / RANGING / HIGH_VOLATILITY / ...)
   │
   ├── SignalEngine.evaluate(symbol, dfs)
   │       ├── Cat1(df_15m) → vote ±1
   │       ├── Cat2(df_15m) → vote ±1
   │       ├── Cat3(df_1h)  → vote ±1
   │       ├── Cat4(df_1h)  → vote ±1
   │       ├── Cat5(df_5m / df_1h) → vote ±1
   │       ├── Cat6(df_1h)  → vote ±1
   │       ├── Cat7(all dfs) → vote ±2  (double weight)
   │       └── Cat8(cached) → vote ±1 + macro_risk_level
   │
   ├── ConfidenceScorer.score(bundle, regime)
   │       └── ConfidenceResult (bull_score, bear_score, tier, macro_multiplier)
   │
   ├── EventGuard.is_blocked(symbol)
   ├── CorrelationGuard.is_allowed(symbol)
   └── PDTTracker.can_day_trade(symbol)  [stocks only]
       │
       ▼
  ScanResult[] sorted by score DESC
       │
       ▼
  tradeable_opportunities() — filter tradeable + unblocked
       │
  For each opportunity (fill all open slots up to MAX_POSITIONS):
       │
       ▼
  RiskAgent.compute(confidence_result, price, atr)
       └── RiskParams (quantity, stop, TP, risk_dollars)
       │
       ▼
  ExecutionAgent.place_order(symbol, risk_params, ...)
       ├── Paper: synthetic fill at entry_price
       ├── Live Stock: IBKR limit order + bracket
       └── Live Forex: OANDA market order + SL/TP
       │
       ▼
  DB: Trade row (status="open")
  State: Position added to memory
  Telegram: notify_trade_opened()
```

---

## Scheduling

| Schedule | Job | Description |
|----------|-----|-------------|
| Every 15 min | `scan_and_trade()` | Full instrument scan + entry/exit decisions |
| Every 60 min | `save_snapshot()` | Portfolio snapshot to DB + Telegram summary |
| Mon 00:00 UTC | `PortfolioAgent.select()` | Full weekly rebalance (60-day data) |
| 05:00 UTC (Tue–Sun) | `PreScreenAgent.screen()` | Daily universe refresh (30-day data) |

---

## Capital Structure

```
Total Capital: $2,000
├── IBKR Pool:  $1,500 (stocks)
│   ├── Reserve:     $450 (30%)
│   └── Deployable: $1,050
│
└── OANDA Pool: $500 (forex)
    ├── Reserve:     $150 (30%)
    └── Deployable:  $350
```

Max 2 simultaneous positions. Each position risks at most 1% of its broker pool.

---

## Startup Sequence

```
python main.py --mode paper
       │
       ▼
init_db()                     — create SQLite tables if not exist
restore_from_db()             — reload any open positions from DB
HealthMonitor.start()         — daemon thread, heartbeats every 30s
scheduler.start()             — BlockingScheduler (runs until SIGINT)
       │
       ├── PortfolioAgent.select()   [runs immediately on first boot]
       └── scan_and_trade()          [first cycle immediately]
```

---

## Graceful Shutdown

On `SIGINT` (Ctrl+C):
1. Scheduler stops accepting new jobs
2. Running scan completes (not interrupted mid-trade)
3. All open positions persist in DB
4. Next startup calls `restore_from_db()` to reload them

---

## Thread Model

| Thread | Purpose |
|--------|---------|
| Main thread | APScheduler BlockingScheduler |
| HealthMonitor daemon | Broker heartbeats every 30s |
| TelegramSend daemon (per-message) | Fire-and-forget HTTP POST |

All shared state (positions, universe) uses `threading.Lock()` for safe concurrent access.
