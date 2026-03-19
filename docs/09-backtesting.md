# Backtesting

The bot includes two backtest engines and a CLI runner for validation.

---

## Overview

| Engine | File | Use Case |
|--------|------|----------|
| Single-symbol | `backtesting/backtest_runner.py` | Quick per-instrument validation |
| Portfolio | `backtesting/portfolio_backtest.py` | Full multi-instrument simulation |
| CLI | `validate.py` | Entry point for both engines |

Both engines use **daily bars** as a proxy for the live 15-minute signal engine. They run on historical yfinance data — no API keys required.

---

## CLI: validate.py

```bash
# Validate all default instruments (SPY, QQQ, NVDA, AAPL, EURUSD, GBPUSD)
python validate.py

# Single symbol
python validate.py --symbol NVDA

# Walk-forward validation (annual splits)
python validate.py --symbol NVDA --walkforward

# Portfolio-level backtest
python validate.py --portfolio

# Portfolio + walk-forward
python validate.py --portfolio --walkforward

# Custom date range
python validate.py --start 2020-01-01 --end 2025-03-01

# Custom symbol set
python validate.py --symbols SPY,QQQ,EURUSD
```

### Pass Thresholds

| Metric | Threshold | Description |
|--------|-----------|-------------|
| Sharpe ratio | > 1.2 | Risk-adjusted return |
| Max drawdown | < 20% | Worst peak-to-trough |
| Minimum trades | ≥ 15 | Statistical validity |

### Sample Output

```
Symbol     Sharpe   WinRate    MaxDD      PF  Trades    Return   Status
------------------------------------------------------------------------
SPY          1.45    62.3%    11.2%    1.82      34     18.4%   PASS ✓
QQQ          1.38    58.7%    14.1%    1.71      29     15.2%   PASS ✓
NVDA         1.61    64.1%    17.8%    2.04      41     31.7%   PASS ✓
EURUSD       1.22    55.2%    9.3%     1.54      38      8.1%   PASS ✓
```

---

## Single-Symbol Backtest (`backtesting/backtest_runner.py`)

Runs the signal logic on daily bars for a single instrument over a historical period.

### Signal Mapping (Daily Bars)

The live signals are adapted for daily bars:

| Category | Live (15m/1h) | Backtest (1d) |
|----------|---------------|---------------|
| Cat 1 | EMA9/EMA21 crossover (15m) | EMA9/EMA21 crossover (1d) |
| Cat 2 | RSI overbought/oversold (1h) | RSI (1d) |
| Cat 3 | MACD line/signal (1h) | MACD (1d) |
| Cat 4 | BB breakout (1h) | BB breakout (1d) |
| Cat 5 | OBV EMA crossover (5m) | OBV EMA crossover (1d) |
| Cat 6 | 5-bar ROC (1h) | 5-bar ROC (1d) |
| Cat 7 | MTF consensus | Not applicable — uses Cat1 double weight |
| Cat 8 | LLM macro | Regime: EMA50 vs EMA200 |

### Position Management

- Entry: Score ≥ MIN_CONFIDENCE (55) and direction agreement
- Stop-loss: 2× ATR (stocks) or 1.5× ATR (forex)
- Take-profit: tier-based — stocks: SMALL 4×, MEDIUM 5×, LARGE 6×, FULL 6.5×; forex: SMALL 8×, MEDIUM 10×, LARGE 12×, FULL 13× (higher because forex 1h ATR% is ~10–20× smaller)
- Max holding: `SWING_HOLDING_DAYS` calendar days (default 7)

### Short Selling

Enabled when in bear regime:
- EMA50 < EMA200 on daily bars (death cross)
- `bear_score ≥ MIN_CONFIDENCE + 5` (stricter threshold for shorts)

---

## Portfolio Backtest (`backtesting/portfolio_backtest.py`)

Full portfolio simulation on daily bars across all active instruments simultaneously. Respects correlation guards and capital constraints.

### How It Works

```
For each trading day (2020-01-01 to present):
  1. Compute daily signal scores for all symbols
  2. Update open positions (check stop/TP/time exits)
  3. Identify new entry candidates (score ≥ threshold, no correlation conflict)
  4. Select top candidates up to MAX_POSITIONS
  5. Update portfolio equity = cash + mark-to-market

Track:
  - Daily equity curve
  - Per-trade P&L
  - Running Sharpe, max drawdown, win rate
```

### Features

- **ATR-based stops**: Same multipliers as live — stocks: 2× SL / 4× TP; forex: 1.5× SL / 10× TP (portfolio backtest uses the MEDIUM tier as a flat default)
- **Correlation guards**: Energy cluster, gold cluster, forex limit
- **Sector caps**: Max 2 instruments per sector in backtest universe
- **Short selling**: When EMA50 < EMA200 (bear regime)
- **Slippage**: Applied at configurable rate (default 0.1%)

### Trailing Stops

The portfolio backtest implements a simplified trailing stop (single-phase). The live system uses a two-phase model (see [Exit Strategy](15-exit-strategy.md)):
- Long: track peak price, stop trails at `peak − trail_atr × ATR`
- Short: track trough price, stop trails at `trough + trail_atr × ATR`

### Metrics

```
Sharpe Ratio      = annualized(mean_daily_return / std_daily_return × √252)
Max Drawdown      = max(peak_equity - trough_equity) / peak_equity
Win Rate          = winning_trades / total_trades
Profit Factor     = gross_profit / gross_loss
Total Return      = (final_equity - initial_equity) / initial_equity
```

---

## Walk-Forward Validation

Walk-forward splits the backtest period into annual in-sample / out-of-sample windows to detect overfitting.

```
2020: Train 2020, Test 2021
2021: Train 2020–2021, Test 2022
2022: Train 2020–2022, Test 2023
2023: Train 2020–2023, Test 2024
```

For each window:
1. Optimize thresholds on in-sample data
2. Run backtest on OOS data with those thresholds
3. Report OOS Sharpe (the meaningful number)

**Pass criterion**: Average OOS Sharpe > 1.0 across all windows.

---

## Data Caching

To avoid yfinance rate limits during repeated backtests, `validate.py` caches downloaded data to disk:

```
tasks/data_cache/<symbol>_<hash>.pkl
```

The hash is an MD5 of the query parameters (symbol, period, interval). Cached files are reused on subsequent runs.

**Important**: Delete the cache before live/paper trading runs to ensure fresh data:
```bash
rm -rf tasks/data_cache/
```

---

## Interpreting Results

### Sharpe Ratio
- < 1.0: Poor risk-adjusted returns
- 1.0–1.5: Acceptable
- > 1.5: Good
- > 2.0: Excellent (likely overfitted if only in-sample)

### Max Drawdown
- < 10%: Conservative
- 10–20%: Acceptable for aggressive strategies
- > 20%: Too risky for a $2,000 account

### Profit Factor
- < 1.0: Losing strategy
- 1.0–1.5: Marginal
- > 1.5: Good
- > 2.0: Strong

### Common Issues
- **High Sharpe, low trade count**: Overfitted — not enough statistical validity
- **Good in-sample, poor OOS**: Curve-fitted to historical data — walk-forward fails
- **High win rate, negative PF**: Average loss much larger than average win — bad risk:reward ratio
