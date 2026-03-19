# Optimization Pipeline

The optimization pipeline uses Gemini to propose parameter changes, validates them with walk-forward backtests, and applies statistical significance tests before deploying any changes.

---

## Overview

```bash
# Run optimization (interactive — requires human approval)
python main.py --mode optimize

# Auto-approve all statistically significant changes
python main.py --mode optimize --auto-approve
```

Requires `GEMINI_API_KEY` in `.env`.

---

## How It Works

```
1. Define candidate parameters to optimize
2. Run in-sample walk-forward backtest with current params
3. Send results to Gemini → receive proposed changes (max 3)
4. For each proposal:
   a. Run OOS walk-forward with proposed params
   b. Statistical significance test (binomial + Bonferroni correction)
   c. Compare OOS Sharpe vs baseline Sharpe
5. Present approved changes for human review (or auto-approve)
6. Apply approved changes to config
7. Log OptimizationCycle to DB
```

---

## Optimizable Parameters

Only three parameters are tunable per optimization cycle. All others are locked to protect capital allocation, position sizing, and signal construction.

| Parameter | Env Var | Default | Range | Description |
|-----------|---------|---------|-------|-------------|
| `confidence_threshold` | `MIN_CONFIDENCE` | 55.0 | 55–85 | Minimum confidence score to enter a trade |
| `atr_sl_mult` | — | 2.0 (stock), 1.5 (forex) | 1.0–4.0 | ATR multiplier for the Phase 1 hard stop-loss |
| `atr_tp_mult` | — | 4.0 (SMALL tier, stocks) | 2.0–10.0 | ATR multiplier for the Phase 1 take-profit target (stocks only; forex uses a separate fixed table: 8–13×) |

**Locked parameters** (never proposed by Gemini):
`total_capital`, `ibkr_capital`, `oanda_capital`, `cash_reserve`, `max_positions`, `max_stocks`, `max_forex`, `max_crypto`, `risk_per_trade`, `target_atr_pct`, `swing_holding_days`, `partial_exit_fraction`, `rsi_period`, `ema_length`

Changes are capped at ±20% from the current value per cycle to keep adjustments conservative.

---

## Gemini Prompt

The pipeline sends Gemini:
1. Current strategy parameters
2. In-sample walk-forward results (Sharpe, drawdown, win rate per window)
3. Per-symbol breakdown
4. Instructions to propose ≤ 3 changes with reasoning

Gemini returns a JSON proposal:
```json
{
  "proposals": [
    {
      "parameter": "MIN_CONFIDENCE",
      "current": 55,
      "proposed": 60,
      "reasoning": "Raising threshold reduces noise trades. Win rate increases at score ≥ 60."
    }
  ]
}
```

---

## Statistical Tests

Each proposal is validated with two tests:

### Binomial Test
- Null hypothesis: Win rate = 50% (coin flip)
- Tests whether OOS win rate is significantly above chance
- Requires p < 0.05

### Bonferroni Correction
- Adjusts significance threshold for multiple comparisons (3 proposals × 4 OOS windows = 12 tests)
- Corrected threshold: p < 0.05 / 12 ≈ 0.004
- Prevents false positives from testing many parameter combinations

### OOS Sharpe Gate
- Proposed OOS Sharpe must exceed baseline OOS Sharpe
- Prevents accepting changes that improve in-sample but hurt OOS

---

## Approval Gate

By default, approved changes require human confirmation:

```
Optimization complete.
2 change(s) approved, 1 rejected.

APPROVED:
  MIN_CONFIDENCE: 55 → 60  (OOS Sharpe: 1.21 → 1.38, p=0.003)
  ATR_SL_MULT: 2.0 → 1.8   (OOS Sharpe: 1.21 → 1.29, p=0.012)

REJECTED:
  SWING_HOLDING_DAYS: 5 → 3  (OOS Sharpe degraded: 1.21 → 0.94)

Apply approved changes? [y/N]:
```

With `--auto-approve`, the prompt is skipped and changes are applied immediately.

A Telegram notification is sent when optimization completes:
```
🔬 OPT READY: 2 change(s) approved, 1 rejected.
Open dashboard → Optimizer tab to review and apply.
```

---

## Logging

Every optimization run is logged to the `optimization_cycles` table:

```python
OptimizationCycle(
    started_at=...,
    completed_at=...,
    in_sample_sharpe=1.21,
    oos_sharpe=1.38,
    proposals=[...],    # all Gemini proposals
    approved=[...],     # approved changes
    rejected=[...],     # rejected changes
    p_value=0.003,
    applied=True,
)
```

---

## Cautions

- **Overfitting risk**: More optimization cycles increase the chance of curve-fitting. Run at most monthly.
- **Small sample**: With a $2,000 account and max 2 positions, trade count is low. Statistical tests may not be conclusive.
- **Regime dependency**: Parameters optimized in a trending market may underperform in a ranging market. Walk-forward helps but doesn't eliminate this.
- **Keep records**: All cycles are logged in DB. Review before applying consecutive optimization rounds.
