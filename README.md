# Trade Bot

Automated trading bot for a $2,000 account. Scans 40 instruments every 15 minutes, scores signals across 8 independent categories, and sizes positions using confidence-tier Half-Kelly. Trades stocks via IBKR and forex via OANDA. Runs paper or live.

**Version: v1.2.0**

---

## Features

- **8-category signal system** — trend, momentum, volume, macro, multi-timeframe (double weight)
- **LLM macro risk gate** — Groq/Gemini assesses news and scales position size (HIGH=0.5×, MEDIUM=0.75×)
- **Dynamic universe** — weekly + daily portfolio selection from 40-symbol candidate pool
- **Sector cap** — max 2 stocks per sector; correlation guard blocks conflicting simultaneous positions
- **ATR-based stops** — 2×ATR stop / 4×ATR TP (stocks), 1.5×ATR / 3×ATR (forex)
- **Risk controls** — circuit breaker, PDT tracker, earnings/FOMC blackouts, health monitor
- **Position restore** — open positions reloaded from DB automatically on every restart
- **Telegram alerts** — trade open/close, hourly P&L (realized + unrealized), portfolio updates
- **Streamlit dashboard** — live equity curve, positions, signals, broker health
- **Backtesting + optimization** — walk-forward validation, LLM-guided parameter tuning

---

## Quick Start

```bash
git clone <repo-url> trade-bot && cd trade-bot
pip install -r requirements.txt
cp config/.env.example .env   # edit with your keys
python main.py --mode paper >> logs/paper_$(date +%Y%m%d).log 2>&1 &
```

Runs fully in paper mode without broker connections — yfinance provides data, orders are simulated.

---

## Modes

```bash
python main.py                        # paper trading (default)
python main.py --mode live            # live trading (requires broker keys)
python main.py --mode dashboard       # Streamlit UI at http://localhost:8501
python main.py --mode optimize        # LLM-guided parameter optimization

python validate.py                    # backtest all instruments
python validate.py --portfolio        # full portfolio simulation
python validate.py --portfolio --walkforward  # annual walk-forward
```

---

## Signal System

8 independent categories, each votes `+1 / -1 / 0`. Cat7 (multi-timeframe) is double-weighted.

| Cat | Signal | Weight |
|-----|--------|--------|
| 1 | Trend direction — EMA9/EMA21 + MACD | ±1 |
| 2 | Trend strength — ADX(14) | ±1 |
| 3 | Momentum — MACD line/signal | ±1 |
| 4 | Volatility band — Bollinger breakout | ±1 |
| 5 | Volume — OBV EMA crossover | ±1 |
| 6 | Price structure — 5-bar ROC | ±1 |
| 7 | Multi-timeframe alignment | **±2** |
| 8 | Macro / news — LLM + risk level | ±1 |

**Max raw score: 9. Normalised to 0–100.**

| Score | Tier | Position size |
|-------|------|---------------|
| < 55 | NO_TRADE / WATCH | — |
| 55–69 | SMALL | 25% of max |
| 70–79 | MEDIUM | 50% of max |
| 80–89 | LARGE | 75% of max |
| ≥ 90 | FULL | 100% of max |

---

## Capital Rules

| Rule | Value |
|------|-------|
| Total capital | $2,000 ($1,500 IBKR + $500 OANDA) |
| Cash reserve | 30% per broker pool |
| Risk per trade | 1% of broker pool |
| Max positions | 2 simultaneously |
| Stop loss | 2× ATR (stocks), 1.5× ATR (forex) |
| Take profit | 4× ATR (stocks), 3× ATR (forex) |
| PDT limit | 3 day trades per rolling 5-day window |
| Circuit breaker | 3% daily loss or 5 consecutive losses |

---

## Universe

**Weekly** (Mon 00:00 UTC): top 8 stocks + 2 forex from 40-symbol pool (60-day bars, sector cap).
**Daily** (05:00 UTC): refresh to top 4–6 stocks + 2 forex (30-day bars, EURUSD always included).

**Candidate pool (40):** SPY, QQQ, IWM, XLK, XLF, XLE, AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, JPM, GS, BAC, JNJ, UNH, XOM, CVX, OXY, COP, SLB, HAL, MPC, VLO, GLD, GOLD, NEM, GDX, GDXJ, WMT, COST, CAT, EURUSD, GBPUSD, USDJPY, AUDUSD, USDCAD, USDCHF

---

## Docker

```bash
docker compose up -d tradebot      # start bot
docker compose up -d dashboard     # start dashboard (port 8501)
docker compose logs -f tradebot    # tail logs
```

---

## Documentation

Full documentation is in [`docs/`](docs/00-index.md):

| Doc | Contents |
|-----|----------|
| [Architecture](docs/01-architecture.md) | System design, data flow, scheduling |
| [Configuration](docs/02-configuration.md) | All `.env` settings explained |
| [Signals](docs/03-signals.md) | All 8 categories, scoring, regime multipliers |
| [Agents](docs/04-agents.md) | Orchestrator, portfolio, risk, execution |
| [Portfolio](docs/05-portfolio.md) | Scanner, state manager, watchlist, PDT |
| [Risk & Resilience](docs/06-risk-and-resilience.md) | Circuit breaker, guards, health monitor |
| [Data](docs/07-data.md) | Fetcher routing, caching, fallbacks |
| [Database](docs/08-database.md) | Schema, queries, migrations |
| [Backtesting](docs/09-backtesting.md) | Engines, CLI, walk-forward, metrics |
| [Optimization](docs/10-optimization.md) | LLM-guided tuning pipeline |
| [Notifications](docs/11-notifications.md) | All Telegram event types |
| [Dashboard](docs/12-dashboard.md) | Streamlit UI guide |
| [Deployment](docs/13-deployment.md) | VPS, Docker, live mode, PostgreSQL |
| [Operations](docs/14-operations.md) | Running, logs, troubleshooting |

---

## Changelog

### v1.2.0
- Sector cap (max 2 per sector) — prevents energy/gold dominating all slots
- Position restore on restart — `restore_from_db()` reloads stops/TPs from latest snapshot
- Unrealized P&L in hourly Telegram summary (deployed capital + available cash)
- Live unrealized P&L per position on dashboard
- Hourly snapshot fires at HH:00 UTC (CronTrigger) — restart-stable

### v1.1.0
- `PreScreenAgent` — daily universe refresh from full candidate pool
- Macro risk gate — Cat8 returns `risk_level` → position size multiplier
- Thread-safe universe swap with `threading.Lock()`
- Cat8 cache TTL reduced to 15 min

### v1.0.0
- 8-category signal system, 4-tier Half-Kelly sizing
- IBKR + OANDA dual-broker execution
- PDT tracker, correlation guard, event guard, circuit breaker
- APScheduler 15-min scan loop, walk-forward backtesting
- Telegram notifications, Streamlit dashboard, Docker deployment

---

## Disclaimer

For educational and research purposes. Automated trading involves substantial risk of loss. Past backtest results do not guarantee future performance. Never risk money you cannot afford to lose.
