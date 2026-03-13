# Database

The bot uses SQLAlchemy ORM with SQLite (default) or PostgreSQL (production). The schema is created automatically on first run via `init_db()`.

---

## Configuration

```env
# SQLite (default — development / single-server)
DATABASE_URL=sqlite:///trade_bot.db

# PostgreSQL (production — multi-instance or hosted VPS)
DATABASE_URL=postgresql://user:pass@localhost/tradebot
```

---

## Schema

### `trades` — Open and closed positions

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | Auto-increment |
| `symbol` | String | e.g. `"NVDA"`, `"EURUSD"` |
| `broker` | String | `"ibkr"` or `"oanda"` |
| `direction` | String | `"long"` or `"short"` |
| `entry_price` | Float | Fill price at open |
| `exit_price` | Float | Fill price at close (null if open) |
| `quantity` | Float | Shares or forex units |
| `confidence` | Float | Dominant score (0–100) at entry |
| `position_tier` | String | `SMALL` / `MEDIUM` / `LARGE` / `FULL` |
| `regime` | String | Market regime at entry |
| `stop_price` | Float | Stop-loss price |
| `take_profit_price` | Float | Take-profit price |
| `pnl_usd` | Float | Realized P&L in USD (null if open) |
| `pnl_pct` | Float | Realized P&L as % of entry value |
| `entry_time` | DateTime | UTC |
| `exit_time` | DateTime | UTC (null if open) |
| `status` | String | `"open"` / `"closed"` / `"cancelled"` |
| `exit_reason` | String | `"stop_loss"` / `"take_profit"` / `"signal_exit"` / `"time_exit"` |
| `signal_breakdown` | JSON | Per-category votes, reasons, contributions |

**Indexes**: `(status)`, `(symbol, status)`, `(entry_time)`

---

### `signal_log` — Per-scan signal history

One row per instrument per scan cycle.

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | |
| `timestamp` | DateTime | UTC |
| `symbol` | String | |
| `cat1` through `cat8` | Integer | Vote for each category (±1 or 0; cat7 ±2) |
| `bull_score` | Float | Aggregate bull score (0–100) |
| `bear_score` | Float | Aggregate bear score (0–100) |
| `direction` | String | `"long"` / `"short"` / `"neutral"` |
| `dominant_score` | Float | max(bull, bear) |
| `position_tier` | String | Tier at this score |
| `regime` | String | Regime at scan time |
| `macro_risk_level` | String | `"LOW"` / `"MEDIUM"` / `"HIGH"` (from Cat8) |
| `raw_votes` | JSON | Full vote breakdown with reasons |

---

### `portfolio_snapshots` — Hourly state snapshots

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | |
| `timestamp` | DateTime | UTC (top of hour) |
| `total_equity` | Float | Available cash + deployed capital |
| `available_cash` | Float | Undeployed deployable capital |
| `deployed_capital` | Float | Σ(entry_price × quantity) |
| `daily_pnl` | Float | Realized P&L for the day |
| `unrealized_pnl` | Float | Mark-to-market on open positions |
| `open_positions` | Integer | Count |
| `positions_detail` | JSON | Full position data (used by restore_from_db) |

The `positions_detail` JSON is critical for the startup restore mechanism. It stores stop and take-profit prices for each open position, which are not stored on the `Trade` row itself (the Trade row stores only the original entry parameters).

---

### `strategy_registry` — Strategy versions

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | |
| `name` | String | Strategy identifier |
| `version` | String | Semantic version |
| `params` | JSON | Strategy parameters |
| `created_at` | DateTime | |
| `active` | Boolean | Currently deployed? |

---

### `optimization_cycles` — LLM optimization runs

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | |
| `started_at` | DateTime | |
| `completed_at` | DateTime | |
| `in_sample_sharpe` | Float | Backtest Sharpe on training period |
| `oos_sharpe` | Float | Validation Sharpe on OOS period |
| `proposals` | JSON | Gemini-proposed parameter changes |
| `approved` | JSON | Approved changes |
| `rejected` | JSON | Rejected changes |
| `p_value` | Float | Statistical significance |
| `applied` | Boolean | Were changes deployed? |

---

### `event_log` — System events

| Column | Type | Description |
|--------|------|-------------|
| `id` | Integer PK | |
| `timestamp` | DateTime | UTC |
| `event_type` | String | `"order_error"` / `"connection_loss"` / `"reconnect"` / `"circuit_breaker"` / `"earnings_blackout"` / `"pdt_warning"` |
| `symbol` | String | Related symbol (if any) |
| `description` | String | Human-readable description |
| `metadata` | JSON | Additional context |

---

## Common Queries

```python
from database.models import Trade, SignalLog, get_session

session = get_session(settings.bot.database_url)

# Open positions
open_trades = session.query(Trade).filter(Trade.status == "open").all()

# Today's trades
from datetime import datetime, timezone
today = datetime.now(timezone.utc).date()
daily_trades = session.query(Trade).filter(
    Trade.entry_time >= today,
    Trade.status == "closed"
).all()

# Recent signals for a symbol
signals = session.query(SignalLog).filter(
    SignalLog.symbol == "NVDA"
).order_by(SignalLog.timestamp.desc()).limit(20).all()

# Latest snapshot
from database.models import PortfolioSnapshot
snap = session.query(PortfolioSnapshot).order_by(
    PortfolioSnapshot.timestamp.desc()
).first()
```

---

## Migrations

SQLAlchemy `create_all()` adds new tables automatically but does **not** alter existing columns. For schema changes:

```bash
# Option 1: Drop and recreate (development only — loses all data)
rm trade_bot.db
python -c "from database.models import init_db; init_db()"

# Option 2: Manual ALTER TABLE (production)
sqlite3 trade_bot.db "ALTER TABLE signal_log ADD COLUMN macro_risk_level TEXT DEFAULT 'LOW';"
```

For PostgreSQL, use Alembic migrations (not included — add if needed for production).
