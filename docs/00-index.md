# Trade Signet Documentation

Complete reference for the Trade Signet automated trading platform.

---

## Contents

| # | Document | Description |
|---|----------|-------------|
| 01 | [Architecture Overview](01-architecture.md) | System design, component map, data flow |
| 02 | [Configuration](02-configuration.md) | Environment variables, settings, capital model |
| 03 | [Signal System](03-signals.md) | All 8 signal categories, scoring, position tiers |
| 04 | [Agents](04-agents.md) | Orchestrator, PortfolioAgent, PreScreenAgent, RiskAgent, ExecutionAgent |
| 05 | [Portfolio Management](05-portfolio.md) | Scanner, State, Watchlist, PDT Tracker |
| 06 | [Risk & Resilience](06-risk-and-resilience.md) | Circuit breaker, correlation guard, event guard, health monitor |
| 07 | [Data Layer](07-data.md) | Fetcher routing, caching, broker fallbacks |
| 08 | [Database](08-database.md) | Schema, ORM models, queries |
| 09 | [Backtesting](09-backtesting.md) | Backtest runner, validate CLI, walk-forward |
| 10 | [Optimization](10-optimization.md) | LLM-guided parameter optimization pipeline |
| 11 | [Notifications](11-notifications.md) | Telegram alerts, all event types |
| 12 | [Dashboard](12-dashboard.md) | FastAPI SPA monitoring dashboard |
| 13 | [Deployment](13-deployment.md) | VPS setup, Docker, live mode |
| 14 | [Operations](14-operations.md) | Running, restarting, logs, common tasks |

---

## Quick Reference

### Start paper trading
```bash
python main.py --mode paper >> logs/paper_$(date +%Y%m%d).log 2>&1 &
```

### Run backtest
```bash
python validate.py
python validate.py --portfolio --walkforward
```

### View dashboard
```bash
./manage.sh start dashboard
# Open http://localhost:8050
```

### Check logs
```bash
tail -f logs/paper_YYYYMMDD.log
```
