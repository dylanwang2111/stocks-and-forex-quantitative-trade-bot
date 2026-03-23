# Dashboard (Trade Signet)

The Trade Signet dashboard is a FastAPI + single-page application served from `dashboard_v2.py`. It reads the same SQLite database as the trading bot and does not interfere with trading.

---

## Starting the Dashboard

```bash
# Recommended — via manage.sh
./manage.sh start dashboard        # start only dashboard
./manage.sh start                  # start bot + dashboard
./manage.sh restart dashboard      # restart dashboard only
./manage.sh status                 # show running PIDs

# Manual
uvicorn dashboard_v2:app --host 0.0.0.0 --port 8050 >> logs/dashboard_$(date +%Y%m%d).log 2>&1 &
```

Open `http://localhost:8050` in a browser.

The dashboard reloads each page on navigation and has a 60-second auto-refresh timer on the active page.

---

## Appearance

### Branding

The platform is named **Trade Signet**. The navbar displays the wax-seal emblem (SVG at `static/favicon.svg`) alongside the *Trade Signet* wordmark in Playfair Display italic.

### Dark Mode

A **settings button** (gear icon, top-right of the nav bar) opens a preferences panel with a dark-mode toggle. The theme is persisted in `localStorage` under the key `trade-signet-theme` and applied on every load without flash (FOUC-prevented via inline `<head>` script).

- Light: antique vellum (`#F2EDE2`) with warm ivory cards, bottle-green and claret accents, antique gold (`#8A5C00`) active indicator
- Dark: deep charcoal background, gold accents

### Charts

All time-series charts support:
- **Wheel zoom** — scroll to zoom the time axis
- **Pinch zoom** — touch/trackpad pinch
- **Drag to pan** — click and drag left/right
- **Reset zoom** button — appears on hover after zooming
- **Cross-hair tooltip** — index-mode tooltip showing all series at the cursor

---

## Pages

### Overview

Summary of portfolio health at a glance.

#### Metric Cards

| Card | Value | Sub-label |
|------|-------|-----------|
| Equity (MTM) | `total_capital + realized_pnl + unrealized_pnl` | All-time % change |
| Realized P&L | Sum of all closed trade P&L | Today's realized |
| Unrealized P&L | Live mark-to-market on open positions | Open position count |
| Win Rate | Closed winning trades / total closed | Trade count |
| Open Positions | Count of open `Trade` rows | Drawdown % |
| PDT Remaining | `PDT_LIMIT − used` day trades (rolling 5d) | Used / limit |

Equity is **mark-to-market**: it includes unrealized P&L from all open positions. Unrealized is fetched live via `fetch_candles("1h")` on each overview load — it is not cached.

#### Equity Curve

Event-driven line chart of cumulative realized P&L over time. Each data point is a closed trade or partial close event. Supports zoom/pan.

#### Capital Allocation

Breakdown per broker pool (IBKR / OANDA / TOTAL):

| Column | Description |
|--------|-------------|
| Pool | Broker capital allocation (`IBKR_CAPITAL` / `OANDA_CAPITAL` env vars) |
| Deployed | Entry price × qty for all open positions |
| Available | Pool − reserve − deployed |
| Util % | Deployed / pool |
| Pos | Open position count |

#### Open Positions (Mini Table)

Compact 8-column table of currently open positions. Columns: Symbol, Direction, Entry, Current, To Stop%, To TP%, Unrealized P&L, Phase.

**View full** button (top-right of the card):
- Expands the card to full width
- Switches to the complete 16-column table (identical to the Positions tab)
- Includes: Stop price, Target price, TP Progress bar, Qty, Held days, Left days, Tier, Confidence
- Collapses back to the compact view on second click

---

### Positions

Full table of all open positions.

| Column | Description |
|--------|-------------|
| Symbol | Instrument |
| Dir | long / short badge |
| Phase | Phase 1 / Phase 2 — past TP / Phase 2 — trailing |
| Entry | Fill price |
| Current | Last 1h close (fetched live) |
| Stop | Current stop price (red) |
| Target | Original TP price (amber) |
| To Stop% | Distance from current price to stop as % (positive = not yet hit) |
| To TP% | Distance from current price to TP as % |
| TP Progress | Progress bar: % of the entry→TP range covered; >100% = past target |
| Unreal P&L | USD P&L: for stocks/non-USD forex `(current − entry) × qty`; for USD-base forex (USDJPY, USDCHF, USDCAD) `(current − entry) / current × qty` to convert from quote currency to USD |
| Qty | Units held |
| Held | Trading days held (see note below) |
| Left | `SWING_HOLDING_DAYS − Held` |
| Tier | SMALL / MEDIUM / LARGE / FULL |
| Conf | Confidence score at entry |

**Held / Left day counting**:
- **Stocks and forex**: weekdays only — Saturday and Sunday do not consume holding budget
- **Crypto (BTCUSD, ETHUSD)**: calendar days — market runs 24/7

This matches exactly how the time-exit trigger counts days in the orchestrator (`_check_exits()`).

---

### Signals

Recent signal scan results from the `signal_log` table. Grouped by symbol, showing the latest scan per instrument.

| Column | Description |
|--------|-------------|
| Time | UTC timestamp |
| Symbol | Instrument |
| Direction | Dominant signal direction |
| Score | Overall confidence score (0–100) |
| Tier | Position tier at that score |
| Cat 1–8 | Individual signal category votes |

---

### Trades

History of all closed trades and partial closes. Supports filtering by symbol and direction.

**Summary row**: total realized P&L, win rate, trade count, average P&L.

| Column | Description |
|--------|-------------|
| Symbol | |
| Dir | |
| Entry / Exit | Prices |
| Realized P&L | Closed trade P&L in USD |
| Exit Reason | stop_loss / trailing_stop / partial_take_profit / signal_exit / time_exit |
| Duration | Entry to exit |
| Phase | Which phase the exit occurred in |

---

### Costs

Estimated trading costs per closed trade.

| Column | Description |
|--------|-------------|
| Symbol | |
| Gross P&L | Raw trade P&L |
| Est. Cost | Commission estimate (IBKR tiered / OANDA spread) |
| Net P&L | Gross − cost |

Summary: total gross, total cost, total net.

---

### Strategies

Per-symbol performance breakdown across closed trades.

| Column | Description |
|--------|-------------|
| Symbol | |
| Trades | Closed trade count |
| Win Rate | |
| Avg P&L | Mean P&L per trade |
| Total P&L | Cumulative |
| Sharpe | Annualized (trade-level) |

Includes a score distribution histogram showing the density of confidence scores at entry.

---

### Optimizer

Interface for the Gemini optimization pipeline.

- **Run Optimization** button triggers a walk-forward backtest + Gemini proposals
- Proposals are displayed with current/proposed values, rationale, and statistical test results
- Each proposal can be individually approved or rejected
- Applied changes are logged to the `optimization_cycles` table

See [Optimization](10-optimization.md) for full pipeline details.

---

### Status

System health at a glance:

- **Bot status**: Running / Stopped (inferred from snapshot age — running if last snapshot < 90 min ago)
- **Circuit breaker**: Tripped / Clear, reason, daily P&L vs limit
- **Broker health**: IBKR and OANDA connection state (HEALTHY / DEGRADED / DOWN)
- **Recent events**: Last 20 entries from the `event_log` table

---

## Data Sources

| Data | Source |
|------|--------|
| Open positions | `trades` table (status = 'open') |
| Current prices | `fetch_candles("1h")` — OANDA → IBKR → yfinance routing |
| Stop / TP levels | Latest `portfolio_snapshots.positions_detail` JSON |
| Closed trades | `trades` table (status = 'closed') |
| Signal history | `signal_log` table |
| Equity curve | `trades` + `event_log` (partial closes) |
| System events | `event_log` table |
| Drawdown | `portfolio_snapshots.drawdown_pct` (latest snapshot) |
| PDT count | `PDTTracker.count_day_trades_rolling()` |
