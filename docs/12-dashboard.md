# Dashboard

The Streamlit monitoring dashboard provides a real-time view of the bot's portfolio, positions, signals, and system health.

---

## Starting the Dashboard

```bash
python main.py --mode dashboard
# Open http://localhost:8501

# Or run independently
streamlit run dashboard.py
```

The dashboard can run alongside the trading bot — it reads from the same SQLite database and does not interfere with trading.

---

## Sections

### Portfolio Metrics (Top Row)

Five summary metrics displayed as cards:

| Metric | Source |
|--------|--------|
| Total Capital | `settings.bot.total_capital` |
| Realized P&L | Sum of closed trades today (UTC) from DB |
| Unrealized P&L | Fetched live via `fetch_candles("1h")` for each open position |
| Total P&L | Realized + Unrealized |
| PDT Used | `PDTTracker.count_day_trades_rolling()` / limit |

Unrealized P&L is computed in real-time on each page load — it is not cached.

---

### Equity Curve

Line chart of `PortfolioSnapshot.total_equity` over time, drawn from the hourly snapshot history. Shows the portfolio's growth (or decline) since the bot started.

---

### Open Positions

Table of all currently open positions:

| Column | Description |
|--------|-------------|
| Symbol | Instrument |
| Direction | long / short |
| Tier | SMALL / MEDIUM / LARGE / FULL |
| Qty | Units held |
| Entry Price | Fill price |
| Current Price | Last 1h close (fetched live) |
| Unrealized P&L | (current − entry) × qty for long; reversed for short |
| Entry Time | UTC timestamp |

Current price and unrealized P&L are fetched fresh on each dashboard load.

---

### Recent Trades

Table of the last 20 closed trades from the `trades` table:

| Column | Description |
|--------|-------------|
| Symbol | |
| Direction | |
| Entry / Exit | Prices |
| P&L USD | Realized |
| P&L % | |
| Exit Reason | stop_loss / take_profit / signal_exit / time_exit |
| Duration | Entry to exit time |

---

### Signal Log

Recent signal scores from the `signal_log` table. Grouped by symbol, showing the last N scans. Useful for diagnosing why an instrument is or is not being entered.

| Column | Description |
|--------|-------------|
| Time | UTC |
| Symbol | |
| Direction | |
| Score | dominant_score (0–100) |
| Tier | Position tier at that score |
| Regime | Market regime at scan time |
| Cat 1–8 | Individual votes |

---

### Broker Health

Status of IBKR and OANDA connections, read from the `event_log` table:
- Last heartbeat time
- Connection state (HEALTHY / DEGRADED / DOWN)
- Recent connection events

---

### System Events

Recent entries from the `event_log` table:
- Earnings blackouts
- Circuit breaker trips
- PDT warnings
- Broker reconnects
- Order errors

---

## Refresh

The dashboard does not auto-refresh. Use the browser refresh button or Streamlit's built-in rerun (`R` key) to update data.

For automatic refresh, add to `.streamlit/config.toml`:
```toml
[server]
runOnSave = true
```

Or use Streamlit's `st.rerun()` with a timer (adds latency).

---

## Docker

When running via Docker, the dashboard is a separate container:

```bash
docker compose up -d dashboard
# Open http://your-server:8501
```

See [Deployment](13-deployment.md) for full Docker configuration.
