# Trade Bot

Automated trading bot designed for a $500 account. Scans stocks and forex every 15 minutes, scores signals across 8 independent categories, and sizes positions using confidence-tier Half-Kelly. Runs paper or live against IBKR (stocks) and OANDA (forex).

**Version: v1.1.0**

---

## What it does

- Scans up to 25 instruments from a candidate pool, selects the best trending ones daily
- Scores each instrument across 8 signal categories (trend, momentum, volume, macro, etc.)
- Applies LLM-based macro risk assessment (Groq / Gemini) to reduce position size in high-risk environments
- Sizes positions using a 4-tier Half-Kelly system (25/50/75/100% of max)
- Enforces PDT rules, correlation guards, event blackouts (earnings, FOMC), and a circuit breaker
- Sends Telegram notifications for trades, portfolio updates, and daily summaries
- Persists all signals, trades, and snapshots to SQLite (dev) or PostgreSQL (prod)

---

## Architecture

```
main.py                         ← Entry point (paper / live / validate / optimize / dashboard)
│
├── agents/
│   ├── orchestrator.py         ← 15-min scan loop + scheduler (APScheduler)
│   ├── portfolio_agent.py      ← Weekly universe selection (Monday 00:00 UTC)
│   ├── pre_screen_agent.py     ← Daily universe refresh (05:00 UTC, except Monday)
│   ├── signal_engine.py        ← Aggregates all 8 signal categories
│   ├── confidence_scorer.py    ← Maps votes → score → position tier + macro multiplier
│   ├── risk_agent.py           ← Position sizing, stop/TP, risk capping
│   └── execution_agent.py      ← Order placement (IBKR / OANDA / paper)
│
├── signals/
│   ├── cat1_trend_direction.py ← EMA crossover + MACD
│   ├── cat2_trend_strength.py  ← ADX
│   ├── cat3_momentum.py        ← RSI
│   ├── cat4_volatility_band.py ← Bollinger Band squeeze
│   ├── cat5_volume.py          ← Volume confirmation
│   ├── cat6_price_structure.py ← Support/resistance
│   ├── cat7_multi_timeframe.py ← MTF alignment (double weight)
│   └── cat8_macro_news.py      ← LLM news sentiment + macro risk level
│
├── portfolio/
│   ├── watchlist.py            ← Instrument universe + thread-safe swap
│   ├── scanner.py              ← Per-instrument evaluation loop
│   ├── state.py                ← In-memory + DB portfolio state
│   └── pdt_tracker.py          ← Pattern Day Trader rule enforcement
│
├── regime/detector.py          ← Market regime (trending/ranging/volatile)
├── events/event_guard.py       ← Earnings + FOMC blackout windows
├── resilience/
│   ├── health_monitor.py       ← Broker heartbeat + reconnect logic
│   └── correlation_guard.py    ← Blocks correlated simultaneous positions
│
├── data/fetcher.py             ← yfinance data fetcher with cache + retry
├── database/models.py          ← SQLAlchemy ORM (5 tables)
├── optimization/pipeline.py    ← LLM-guided parameter optimization
├── backtesting/backtest_runner.py ← Vectorbt / pandas backtest engine
├── notifications/telegram.py   ← Telegram alerts
├── dashboard.py                ← Streamlit monitoring dashboard
└── validate.py                 ← Backtest validator CLI
```

---

## Signal System

8 independent categories, each votes `+1 / -1 / 0`. Cat7 (multi-timeframe) is double-weighted.

| Cat | Signal | Max vote |
|-----|--------|----------|
| 1 | Trend direction (EMA + MACD) | ±1 |
| 2 | Trend strength (ADX) | ±1 |
| 3 | Momentum (RSI) | ±1 |
| 4 | Volatility band (Bollinger) | ±1 |
| 5 | Volume confirmation | ±1 |
| 6 | Price structure | ±1 |
| 7 | Multi-timeframe alignment | **±2** |
| 8 | Macro / news (LLM) + risk gate | ±1 |

**Max raw score: 9. Normalised to 0–100.**

### Position tiers

| Score | Tier | Position size |
|-------|------|---------------|
| < 55 | NO_TRADE | 0% |
| 55–64 | WATCH | 0% |
| 65–74 | SMALL | 25% of max |
| 75–84 | MEDIUM | 50% of max |
| 85–94 | LARGE | 75% of max |
| ≥ 95 | FULL | 100% of max |

**Max position: $333 (2/3 of $500 capital). Always keep $150 cash reserve.**

### Macro risk gate (Cat8)

Cat8 LLM now returns a `risk_level` alongside its vote:

| Risk level | Position multiplier |
|------------|-------------------|
| LOW | 1.0× (no change) |
| MEDIUM | 0.75× |
| HIGH | 0.50× |

This scales down position size for wars, FOMC events, geopolitical shocks — without blocking the trade entirely.

---

## Universe

### Fixed instruments (initial)
- **Stocks**: SPY, QQQ, NVDA, AAPL (Alpaca / IBKR, fractional shares)
- **Forex**: EUR/USD (primary, no PDT), GBP/USD (London session)

### Dynamic selection
- **Weekly** (Monday 00:00 UTC): `PortfolioAgent` scores all 25 candidates on 60-day daily bars (full EMA stack gate). Selects top 6 stocks + 2 forex.
- **Daily** (05:00 UTC, Tue–Sun): `PreScreenAgent` scores all 25 candidates on 30-day bars (softer EMA9 > EMA21 gate). Selects top 4 stocks + 2 forex. EUR/USD always force-included.

### Candidate pool (25 instruments)
ETFs: SPY, QQQ, IWM, GLD, XLK, XLF
Tech: AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA
Finance: JPM, GS, BAC
Healthcare: JNJ, UNH
Energy: XOM, CVX
Consumer: WMT, COST, CAT
Forex: EUR/USD, GBP/USD, USD/JPY, AUD/USD, USD/CAD, USD/CHF

---

## Capital rules ($500 account)

| Rule | Value |
|------|-------|
| Total capital | $500 |
| Cash reserve | $150 (always) |
| Max position | $333 |
| Risk per trade | 1% = $5 |
| Max positions | 2 simultaneously |
| Stop loss | 1.5% from entry |
| Take profit | 3.0% from entry (2:1 R:R) |
| PDT limit | 3 day trades per 5-day window (stocks only) |
| Correlation guard | Never hold SPY+QQQ, QQQ+NVDA, EUR/USD+GBP/USD |

---

## Setup

### 1. Clone and install

```bash
git clone <repo-url> trade-bot
cd trade-bot
pip install -r requirements.txt
```

### 2. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
# Trading mode
TRADING_MODE=paper           # paper | live
TOTAL_CAPITAL=500

# Brokers (leave blank to run without — paper mode stubs orders)
IBKR_ACCOUNT_ID=
IBKR_HOST=127.0.0.1
IBKR_PORT=7497               # 7497=paper, 7496=live

OANDA_API_KEY=
OANDA_ACCOUNT_ID=
OANDA_ENVIRONMENT=practice   # practice | live

# LLM (Cat8 macro signal — at least one recommended)
GROQ_API_KEY=                # Free tier at console.groq.com
GEMINI_API_KEY=              # Free tier at aistudio.google.com

# Notifications (optional)
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### 3. Run

```bash
# Paper trading (default)
python main.py

# Backtest all 6 instruments
python validate.py

# Backtest single symbol
python validate.py --symbol NVDA

# Walk-forward validation
python validate.py --walkforward

# Monitoring dashboard
python main.py --mode dashboard
```

---

## Docker

```bash
# Build and start
docker compose up -d tradebot

# View logs
docker compose logs -f tradebot

# Dashboard
docker compose up -d dashboard
# Open http://your-server:8501
```

See `deploy.md` for full VPS setup and live-mode switching instructions.

---

## Backtesting

The backtest runner uses daily bars as a proxy for the live 15-minute signal engine. It runs without any API keys — just yfinance historical data.

```bash
python validate.py
```

Pass thresholds (Sharpe > 1.2, MaxDD < 20%, Trades ≥ 15 over 3 years):

```
Symbol     Sharpe   WinRate    MaxDD      PF  Trades    Return   Status
------------------------------------------------------------------------
SPY          1.45    62.3%    11.2%    1.82      34     18.4%   PASS ✓
...
```

Note: Yahoo Finance rate-limits aggressive polling. `validate.py` bulk-downloads all symbols in a single API call to avoid this.

---

## Database

SQLite by default (`trade_bot.db`). Switch to PostgreSQL for production:

```env
DATABASE_URL=postgresql://user:pass@localhost/tradebot
```

Tables: `trades`, `signal_log`, `strategy_registry`, `optimization_cycles`, `portfolio_snapshots`, `event_log`

Schema is created automatically on first run via `init_db()`.

---

## Optimization

The LLM-guided optimization pipeline runs walk-forward backtests, applies binomial significance tests + Bonferroni correction, and proposes parameter changes (max 3 per cycle):

```bash
python main.py --mode optimize
# Add --auto-approve to skip human confirmation gate
```

---

## Changelog

### v1.1.0 — Dynamic Pre-Screen + Macro Risk Gate
- **Feature**: `PreScreenAgent` — daily universe refresh at 05:00 UTC from full 25-instrument candidate pool (30-day lookback, soft EMA gate). EUR/USD always force-included as anchor.
- **Feature**: Thread-safe universe swap — `set_active_universe()` and new `get_universe_snapshot()` wrapped in `threading.Lock()`. Scanner reads a consistent snapshot, never a partially-mutated list.
- **Feature**: Macro risk gate — Cat8 LLM now returns `risk_level` (HIGH/MEDIUM/LOW) alongside its vote. Flows through `ConfidenceResult.macro_multiplier` → `RiskAgent` applies it as a position size multiplier (HIGH=0.5×, MEDIUM=0.75×, LOW=1.0×).
- **Feature**: Cat8 cache TTL reduced 3600→900s (15-min refresh for faster macro event response).
- **Feature**: `macro_risk_level` column added to `signal_log` DB table.
- **Fix**: `validate.py` bulk-downloads all symbols in a single `yf.download()` call (was 6 sequential requests → triggered Yahoo Finance rate limit bans).
- **Fix**: `data/fetcher.py` switched from `Ticker.history()` to `yf.download()` (less aggressively rate-limited).

### v1.0.0 — Initial Release
- 8-category independent signal system with regime-adjusted scoring
- 4-tier Half-Kelly position sizing
- PDT tracker, correlation guard, event guard (earnings + FOMC blackouts)
- PortfolioAgent weekly universe selection from 25-instrument candidate pool
- Circuit breaker (3% daily loss / 5 consecutive losses)
- HealthMonitor with broker heartbeat and reconnect
- APScheduler-based 15-minute scan loop
- Walk-forward backtesting with Bonferroni-corrected significance tests
- Telegram notifications
- Streamlit dashboard
- Docker deployment

---

## Disclaimer

This software is for educational and research purposes. Automated trading involves substantial risk of loss. Past backtest results do not guarantee future performance. Never risk money you cannot afford to lose.
