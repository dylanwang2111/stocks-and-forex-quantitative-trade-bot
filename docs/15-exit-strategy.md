# Exit Strategy

Every open position is evaluated every 15 minutes by `_check_exits()` in `agents/orchestrator.py`. Exits fall into two categories: **price-based** (stop / target) and **backstops** (time, signal reversal).

---

## Stop & Target Setup (at entry)

Levels are computed by `RiskAgent` using ATR(14) on 1-hour bars at the moment of entry.

**Stop-loss** is fixed per asset type:

| Broker | Instrument | Stop-Loss |
|--------|------------|-----------|
| IBKR   | Stocks     | entry ± 2.0 × ATR |
| OANDA  | Forex      | entry ± 1.5 × ATR |

**Take-profit** uses separate multiplier tables for stocks and forex, because forex 1h ATR is ~10–20× smaller as a percentage of price than stocks (EURUSD ≈ 0.07% per bar vs stocks ≈ 1%). Using the same multiplier would produce a tiny ~0.3% TP for forex; the higher forex table targets ~0.5–1% (50–100 pips).

**Stocks** (`_ATR_TP_MULT_BY_TIER`):

| Position Tier | TP Mult | SL Mult | R:R |
|---------------|---------|---------|-----|
| SMALL  (55–64) | 4.0× | 2.0× | 2.0:1 |
| MEDIUM (65–74) | 5.0× | 2.0× | 2.5:1 |
| LARGE  (75–84) | 6.0× | 2.0× | 3.0:1 |
| FULL   (≥ 85)  | 6.5× | 2.0× | 3.25:1 |

**Forex** (`_ATR_TP_MULT_BY_TIER_FOREX`):

| Position Tier | TP Mult | SL Mult | R:R | Approx pips (ATR=8) |
|---------------|---------|---------|-----|---------------------|
| SMALL  (55–64) |  8.0× | 1.5× | 5.3:1 | ~64 pips / ~0.59% |
| MEDIUM (65–74) | 10.0× | 1.5× | 6.7:1 | ~80 pips / ~0.74% |
| LARGE  (75–84) | 12.0× | 1.5× | 8.0:1 | ~96 pips / ~0.89% |
| FULL   (≥ 85)  | 13.0× | 1.5× | 8.7:1 | ~104 pips / ~0.96% |

When ATR is unavailable, a flat 2% fallback is used for stop; TP defaults to 5.0× the stop distance.

---

## Two-Phase Exit Model

### Phase 1 — Price has not yet reached TP (`TP_progress < 100%`)

Only one trigger is active: the **hard stop-loss**.

- Price hits `stop_price` → position closed immediately at stop price
- Price reaches TP → **not** closed. Instead, the TP breach streak counter increments.
- TP is used purely as a **phase trigger**, not an exit.

This means positions can run past their initial TP target if momentum continues.

### Phase 2 — TP confirmed (two consecutive 15-min cycles past TP)

Entry into Phase 2 requires the **TP confirmation guard**: price must be past TP on **2 consecutive scan cycles** (≥ 30 minutes). A single-candle spike through TP does not trigger Phase 2.

On the cycle that `tp_breach_streak` reaches 2:

1. **Partial exit**: 50% of the position is closed at the current price.
2. The remaining 50% is held and trailed.

From this point, the position is permanently in Phase 2 — even if price later dips back below TP, the trailing stop remains active (tracked via `partial_exit_done` flag).

### Trailing Stop (Phase 2 only)

Every 15-minute cycle while in Phase 2, the stop ratchets behind price:

```
long:  new_stop = max(current_stop, current_price − 2.1 × ATR)
short: new_stop = min(current_stop, current_price + 2.1 × ATR)
```

The stop **only ever moves in the profitable direction**. When price reverses and hits the trailing stop, the remaining 50% is closed at the stop price.

---

## TP Progress on the Dashboard

The dashboard shows `TP_progress%` = how far price has travelled from entry toward (and past) the original TP:

```
long:  (current − entry) / (tp − entry) × 100
short: (entry − current) / (entry − tp) × 100
```

Values > 100% mean price has blown through the original target. The TP line is still displayed for reference after Phase 2 entry.

---

## Backstop Exits (both phases)

These override price-based checks and fire on the same 15-minute cycle.

| Backstop | Condition | Fill Price |
|----------|-----------|------------|
| **Time exit** | Held ≥ `SWING_HOLDING_DAYS` (default 5, configurable via env) | Current price |
| **Signal reversal** | EMA9 crosses against position direction on 1h (held ≥ 1 day) | Current price |

---

## Fill Prices (paper vs live)

| Exit reason | Fill price used |
|-------------|----------------|
| `stop_loss` | `stop_price` (hard) |
| `trailing_stop` | `stop_price` at time of trigger |
| `partial_take_profit` | Current price at Phase 2 entry cycle |
| `time_exit` | Last 1h close |
| `signal_exit` | Last 1h close |

In live mode the broker closes the position at market; the fill price recorded in the DB is the actual broker fill.

---

## State Persistence

Exit state is stored in `PortfolioSnapshot.positions_detail` (JSON) and restored on bot restart:

| Field | Meaning |
|-------|---------|
| `tp_breach_streak` | Consecutive cycles price has been past TP |
| `partial_exit_done` | True once the 50% partial close has been executed |
| `stop_price` | Current stop (ratchets upward in Phase 2) |

If the bot restarts mid-Phase-2, positions are restored with `partial_exit_done=True` and continue trailing correctly.

---

## Summary Flowchart

```
Every 15-min cycle per position:
│
├─ Fetch current 1h price
│
├─ Update tp_breach_streak
│   ├─ price past TP → streak += 1
│   └─ price below TP → streak = 0
│
├─ in_phase2 = (streak ≥ 2) OR partial_exit_done
│
├─ Phase 2 path:
│   ├─ if NOT partial_exit_done → close 50% at current price
│   ├─ ratchet trailing stop (2.1×ATR)
│   └─ if price hit trailing stop → close remaining 50%
│
├─ Phase 1 path:
│   └─ if price hit hard stop → close 100%
│
├─ Time exit: held ≥ SWING_HOLDING_DAYS → close
└─ Signal exit: EMA9 reversed on 1h → close
```

---

## Key Files

| File | Responsibility |
|------|---------------|
| `agents/orchestrator.py` — `_check_exits()` | Main exit loop, streak tracking, phase dispatch |
| `agents/orchestrator.py` — `_update_trailing_stop()` | Ratchets stop at 2.1×ATR |
| `agents/orchestrator.py` — `_has_signal_reversal()` | EMA9/EMA21 cross check |
| `agents/execution_agent.py` — `partial_close_position()` | Broker routing for 50% close |
| `agents/execution_agent.py` — `close_position()` | Broker routing for full close |
| `portfolio/state.py` — `partial_close_position()` | Reduces in-memory qty + DB update |
| `agents/risk_agent.py` | Computes initial stop/TP from ATR at entry |

---

## Tests

All exit strategy behaviour is covered in `tests/test_exit_logic.py` (18 tests):

```bash
python3 tests/test_exit_logic.py
```

| Test group | What's covered |
|------------|---------------|
| `partial_close_*` | P&L math (long/short), qty reduction, DB update, `partial_exit_done` flag |
| `snapshot_*` / `restore_*` | Exit state persists across bot restarts |
| `phase1_*` | Hard stop fires; TP does not close position in Phase 1 |
| `tp_streak_*` | Streak increments / resets correctly |
| `phase2_*` | Partial close at streak=2, no double close, trailing stop fires (long/short) |
| `phase2_permanent_*` | Phase 2 stays active via `partial_exit_done` even when streak resets |
| `exec_agent_partial_*` | Paper mode IBKR and OANDA partial close via ExecutionAgent |
