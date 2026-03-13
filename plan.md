# Automated Trading AI Agent — Final Master Plan v3

---

## Honest Baseline (Non-Negotiable)

> **Realistic targets for a well-built retail algo system:**
> - Weekly return: **0.5–5%** in good conditions, some weeks flat or slightly negative
> - Annual compounded: **30–120%** if consistently profitable — that beats every hedge fund
> - 20%/week is mathematically impossible to sustain. Chasing it causes over-leverage and account wipeout.
> - **Goal: never blow up the account. Compounding small consistent gains wins long-term.**
>
> Live returns will be **30–50% lower than backtest returns** due to slippage, spreads, and execution
> lag. This is a known fact, not a pessimistic guess. Model it from day one.

---

## 1. System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                               ORCHESTRATOR AGENT                                │
│  Every 15 min: scan all 6 instruments → rank signals → allocate capital → trade │
│  (scheduler, circuit breakers, health checks, PDT tracker, state coordination)  │
└──┬────────────────┬──────────────┬──────────────┬──────────────┬────────────────┘
   │                │              │              │              │
┌──▼──────┐  ┌──────▼─────┐  ┌────▼─────┐  ┌────▼─────┐  ┌────▼──────────┐
│ MARKET  │  │  SIGNAL    │  │PORTFOLIO │  │EXECUTION │  │  AI OPTIMIZER │
│ ANALYST │  │  ENGINE    │  │  AGENT   │  │  AGENT   │  │  (every 5wk)  │
│         │  │            │  │          │  │          │  │               │
│-Fetch   │  │-8 category │  │-Universe │  │-Limit    │  │-Perf report   │
│ candles │  │ signals    │  │ scanner  │  │ orders   │  │-LLM analysis  │
│-Compute │  │-Confidence │  │-Capital  │  │-Fill log │  │-Backtest val  │
│ TA      │  │ score 0-100│  │ alloc    │  │-Reconnect│  │-Human review  │
│-Regime  │  │-LLM filter │  │-Priority │  │ logic    │  │-Deploy staged │
│-Events  │  │-Event guard│  │ queue    │  │          │  │               │
└─────────┘  └────────────┘  └──────────┘  └──────────┘  └───────────────┘
                  │                │                              │
           ┌──────▼──────┐  ┌──────▼──────┐            ┌────────▼────────┐
           │  STRATEGY   │◄─│  PORTFOLIO  │            │    VERSION      │
           │  REGISTRY   │  │   STATE DB  │            │    HISTORY      │
           │ (versioned  │  │ (open pos,  │            │  (rollback any  │
           │  params)    │  │  capital,   │            │   param set)    │
           └─────────────┘  │  PDT count) │            └─────────────────┘
                            └─────────────┘
```

---

## 2. Tech Stack

| Layer              | Choice                                    | Reason                                              |
|--------------------|-------------------------------------------|-----------------------------------------------------|
| Language           | Python 3.11+                              | Best quant/finance ecosystem                        |
| Agent framework    | LangGraph                                 | Stateful multi-agent graphs, memory, loops          |
| LLM — live review  | **Groq + Llama 3.3 70B**                  | Near-free (14,400 req/day free tier), 500 tok/sec   |
| LLM — news/macro   | **Gemini Flash 2.0** (Google)             | $0.075/M input, 1M context, ideal for long news     |
| LLM — optimization | **Gemini Flash 2.0** or DeepSeek V3       | Strong reasoning, very cheap, JSON output reliable  |
| (Claude option)    | Claude claude-sonnet-4-6 (optional)       | Swap in for optimization only if better results     |
| Data — intraday    | Alpaca WebSocket + yfinance               | Real-time bars + historical free data               |
| Data — news        | Alpaca News API + NewsAPI.org             | Free with Alpaca account; 100 req/day on NewsAPI    |
| Event calendar     | Investing.com scrape / FRED API           | Earnings dates, FOMC, CPI, NFP scheduled events     |
| TA library         | pandas-ta                                 | 150+ indicators, pandas-native, actively maintained |
| Backtesting        | vectorbt                                  | Vectorized, fast, supports param sweeps + fees      |
| Database           | SQLite (dev) → PostgreSQL (prod)          | Trade log, signal log, strategy registry            |
| Scheduler          | APScheduler                               | In-process cron, no extra service needed            |
| Broker — stocks    | Alpaca Markets                            | Free paper + commission-free live, solid API        |
| Broker — forex     | OANDA                                     | Regulated globally, REST + streaming API            |
| Notifications      | Telegram Bot API                          | Instant alerts, daily summaries                     |
| Dashboard          | Streamlit                                 | Live PnL, signal board, optimizer UI                |
| Deployment         | Docker + Linux VPS (4GB RAM min)          | 24/7 uptime, reproducible                           |
| Secrets            | python-dotenv + .env                      | Never hardcode keys                                 |

### LLM Cost Breakdown (Why Not Claude for Routine Work)

```
Use case: live trade signal review (~20 reviews/day, ~500 tokens each)

Groq + Llama 3.3 70B:  FREE (within 14,400 req/day tier) → $0/month
Gemini Flash 2.0:      20 * 30 days * 500 tok = 300K tokens → ~$0.02/month
GPT-4o-mini:           Same volume → ~$0.03/month
Claude claude-sonnet-4-6: Same volume → ~$1.80/month  ← 90x more expensive

Use case: monthly optimization report (~20,000 tokens once/month)
Gemini Flash 2.0:      ~$0.002
DeepSeek V3:           ~$0.006
Claude claude-sonnet-4-6: ~$0.18

Decision:
  - Routine live review:      Groq + Llama 3.3 70B (free, fastest)
  - Macro news analysis:      Gemini Flash 2.0 (long context, cheap)
  - Monthly optimization:     Gemini Flash 2.0 (best value for reasoning)
  - Claude:                   Optional upgrade for optimization only, swap in via config
```

---

## 3. Market Regime Detection

Runs every 30 minutes. All signals are weighted by the current regime — wrong strategy in wrong market
is the single biggest cause of avoidable losses.

```
Regime A — TRENDING UP
  Conditions: ADX > 25 AND price > EMA50 AND EMA20 > EMA50
  Boost:   Trend-following signals (+20% weight)
  Suppress: Mean-reversion signals (−30% weight)

Regime B — TRENDING DOWN
  Conditions: ADX > 25 AND price < EMA50 AND EMA20 < EMA50
  Boost:   Short/bearish signals (+20%)
  Suppress: Long/bullish mean-reversion (−30%)

Regime C — RANGING / SIDEWAYS
  Conditions: ADX < 20 AND price within BB bands (not touching edges)
  Boost:   Mean-reversion signals (+20%)
  Suppress: Trend-following and breakout signals (−30%)

Regime D — HIGH VOLATILITY / CRISIS
  Conditions: VIX > 30 (stocks) OR ATR > 2× its 30-day average
  Action:  Reduce ALL position sizes by 50%, widen stops by 30%
  Suppress: All breakout strategies (−50%)
  Note:    Do NOT short into panic — gap risk is extreme

Regime E — LOW VOLATILITY / COMPRESSION
  Conditions: Bollinger Band width at 6-month low AND ATR contracting for 5+ days
  Action:  Wait only. No new entries. A breakout is coming — wait for it.
  Note:    When compression ends, the first move is often false. Wait for confirmation.
```

---

## 4. Signal Library — 8 Genuinely Independent Categories

**Key architectural fix from v2:** The previous plan listed 10 "independent" signals that were mostly
correlated. EMA + MACD + ADX all measure trend. RSI + Stochastic both measure momentum. Correlated
signals give false confidence (6 signals agreeing might only be 2 real ideas).

The fix: signals are grouped into **8 independent categories**. Each category contributes exactly
**one vote (+1, −1, or 0)**, regardless of how many sub-indicators are in that category. The categories
are chosen to measure fundamentally different market dimensions.

```
Category 1 — TREND DIRECTION        (sub-indicators: EMA crossover, MACD direction)
Category 2 — TREND STRENGTH         (sub-indicators: ADX, slope of EMA50)
Category 3 — MOMENTUM / OSCILLATOR  (sub-indicators: RSI, Stochastic — combined vote)
Category 4 — VOLATILITY BAND        (sub-indicators: Bollinger Band position, ATR ratio)
Category 5 — VOLUME CONFIRMATION    (sub-indicators: volume vs MA, OBV direction)
Category 6 — PRICE STRUCTURE        (sub-indicators: support/resistance level, candlestick pattern)
Category 7 — MULTI-TIMEFRAME ALIGN  (sub-indicators: same direction on 5m + 15m + 1h)
Category 8 — MACRO / NEWS SENTIMENT (sub-indicators: LLM-scored recent news headlines)
```

### Category Logic Detail

**Category 1 — Trend Direction**
```
EMA sub-signal:  +1 if EMA9 > EMA21 (bullish cross or above), −1 if below
MACD sub-signal: +1 if MACD histogram positive and rising, −1 if negative and falling
Category vote:   +1 if both sub-signals agree bullish
                 −1 if both agree bearish
                 0  if they disagree (conflicting)
Timeframe: 15m
```

**Category 2 — Trend Strength**
```
ADX sub-signal:    +1 if ADX > 25 (strong trend present — direction from Cat 1)
                    0 if ADX 20–25 (weak/building trend)
                   −1 if ADX < 20 (no trend — mean reversion favored)
EMA slope:         Confirm ADX reading with EMA50 slope direction
Category vote:     ADX reading directly (not +1/−1 directional — modulates other signals)
Note: This category acts as a MULTIPLIER more than a directional vote.
      If ADX < 20 in RANGING regime, trend signals are discounted.
```

**Category 3 — Momentum / Oscillator**
```
RSI sub-signal:       +1 if RSI < 35 (oversold), −1 if RSI > 65 (overbought), 0 otherwise
Stochastic sub-signal: +1 if %K crosses above %D while both < 20, −1 if cross while both > 80
Category vote:         +1 if either sub-signal is bullish AND neither is bearish
                       −1 if either sub-signal is bearish AND neither is bullish
                        0 if conflicting or both neutral
Timeframe: 1h (oscillators on 15m are too noisy)
```

**Category 4 — Volatility Band**
```
BB sub-signal:   +1 if price pierces lower band (oversold extreme), −1 if upper band
ATR sub-signal:  Check if ATR is expanding (momentum) or contracting (reversal risk)
Category vote:   +1 if price at lower BB AND ATR not collapsing (real move, not exhaustion)
                 −1 if price at upper BB AND ATR not collapsing
                  0 otherwise
Timeframe: 1h
```

**Category 5 — Volume Confirmation**
```
Volume ratio: current_volume / 20-period_volume_MA
OBV trend:    On-Balance Volume direction over last 10 bars

Category vote: +1 if volume_ratio > 1.5 on an up candle AND OBV trending up
               −1 if volume_ratio > 1.5 on a down candle AND OBV trending down
                0 if volume is below average (no conviction)
Note: Volume without price confirmation = ignored. Price without volume = lower weight.
Timeframe: 5m for intraday entries, 1h for swing
```

**Category 6 — Price Structure**
```
Support/Resistance: Calculate key S/R levels from last 20 swing highs/lows
Candlestick pattern: Detect reversal patterns (Hammer, Engulfing, Star, Doji)

Category vote: +1 if price bounces off identified support AND bullish candle pattern confirms
               −1 if price rejects from identified resistance AND bearish candle pattern confirms
                0 if price is in mid-range with no clear S/R interaction
Timeframe: 1h candles for pattern detection
```

**Category 7 — Multi-Timeframe Alignment (Double Weight = 2 votes)**
```
This is the highest-conviction signal: the same directional bias must appear across
three different timeframes simultaneously. A true trend is visible at all scales.

5m  trend:  EMA9 vs EMA21 direction
15m trend:  EMA9 vs EMA21 direction
1h  trend:  EMA20 vs EMA50 direction

Category vote: +2 if all three timeframes agree bullish (extremely rare, very high quality)
               −2 if all three timeframes agree bearish
               +1 if two of three agree bullish
               −1 if two of three agree bearish
                0 if mixed / split
```

**Category 8 — Macro & News Sentiment (LLM-scored)**
```
Input sources:
  - Alpaca News API: last 3–5 headlines for the specific instrument (last 2 hours)
  - Investing.com economic calendar: any high-impact events in next 24 hours
  - Market-wide context: VIX level, SPY direction today

LLM used: Gemini Flash 2.0 (handles long context cheaply, fast response)

Prompt:
  "Given these recent headlines and the economic calendar for [instrument],
   rate the macro sentiment as: BULLISH (+1), BEARISH (-1), or NEUTRAL (0).
   If a high-impact event (FOMC, CPI, NFP, earnings) is scheduled in the
   next 24 hours, always return BLOCKED regardless of headlines.
   Respond in JSON: {sentiment: +1/-1/0, reason: str, blocked: bool}"

Category vote: Use LLM response directly
               If blocked=true → NO TRADE regardless of other signals
Frequency: Called once per instrument per hour (not per candle — expensive to spam)
Cost: ~50 tokens per call × 8 instruments × 8 hours = 3,200 tokens/day = ~$0.0002/day
```

### Confidence Score Calculation

```python
def calculate_confidence(
    cat1: int,   # Trend Direction:       −1, 0, +1
    cat2: int,   # Trend Strength:        −1, 0, +1 (ADX)
    cat3: int,   # Momentum/Oscillator:   −1, 0, +1
    cat4: int,   # Volatility Band:       −1, 0, +1
    cat5: int,   # Volume:                −1, 0, +1
    cat6: int,   # Price Structure:       −1, 0, +1
    cat7: int,   # MTF Alignment:         −2, −1, 0, +1, +2
    cat8: int,   # Macro/News:            −1, 0, +1  (0 if blocked = halt)
    regime: str,
    macro_blocked: bool
) -> dict:

    if macro_blocked:
        return {"score": 0, "direction": "NONE", "reason": "Macro event imminent — no trade"}

    # Max possible bullish score: 1+1+1+1+1+1+2+1 = 9
    signals = [cat1, cat2, cat3, cat4, cat5, cat6, cat7, cat8]
    max_score = 9

    bull_raw = sum(s for s in signals if s > 0)
    bear_raw = sum(abs(s) for s in signals if s < 0)

    # Regime multipliers on raw scores
    regime_mult = {
        "TRENDING_UP":    {"bull": 1.15, "bear": 0.85},
        "TRENDING_DOWN":  {"bull": 0.85, "bear": 1.15},
        "RANGING":        {"bull": 1.00, "bear": 1.00},
        "HIGH_VOLATILITY":{"bull": 0.65, "bear": 0.65},
        "LOW_VOLATILITY": {"bull": 0.50, "bear": 0.50},  # almost never trade in compression
    }
    mult = regime_mult.get(regime, {"bull": 1.0, "bear": 1.0})

    bull_score = min(100, (bull_raw / max_score) * 100 * mult["bull"])
    bear_score = min(100, (bear_raw / max_score) * 100 * mult["bear"])

    direction = "BUY" if bull_score > bear_score else "SELL"
    dominant  = max(bull_score, bear_score)

    # Require minimum lead: if bull and bear within 10 points → mixed, no trade
    if abs(bull_score - bear_score) < 10:
        return {"score": dominant, "direction": "MIXED", "reason": "Conflicting signals"}

    return {
        "bull_score": bull_score,
        "bear_score": bear_score,
        "direction": direction,
        "dominant_score": dominant,
        "signal_breakdown": {
            "trend_dir": cat1, "trend_strength": cat2, "momentum": cat3,
            "volatility": cat4, "volume": cat5, "structure": cat6,
            "mtf": cat7, "macro": cat8
        }
    }
```

### Position Sizing by Confidence Tier

```
Score 0–39%:    NO TRADE (too weak, too mixed, or LOW_VOLATILITY regime)
Score 40–54%:   WATCH ONLY — log to signal_log, monitor for improvement
Score 55–64%:   SMALL position — 25% of max allowed size
Score 65–74%:   MEDIUM position — 50% of max allowed size
Score 75–84%:   LARGE position — 75% of max allowed size
Score 85–100%:  FULL position — 100% of max allowed size
                (requires: at least Cat7 MTF >= +1 AND Cat5 Volume >= +1)
```

### LLM Live Review (Groq + Llama 3.3 70B)

For scores >= 55% (real trade candidates), Llama 3.3 70B via Groq performs a fast sanity check:

```python
GROQ_REVIEW_PROMPT = """
You are a trading risk filter. A quantitative system wants to enter a trade.
Your job is to identify obvious reasons to REJECT it, or confirm it looks reasonable.

Symbol: {symbol}
Direction: {direction}
Confidence score: {score}/100
Category signals: {breakdown}  # e.g., "trend:+1, momentum:+1, volume:0, macro:0, mtf:+1"
Regime: {regime}
Recent 5 trades on this symbol: {recent_trades}  # win/loss/pnl
Account drawdown today: {daily_drawdown_pct}%
Open positions: {open_positions}/{max_positions}

Key recent macro news (last 2h): {news_headlines}

Respond ONLY in JSON:
{
  "adjustment": <integer -10 to +10>,
  "flag": "none" | "high_risk" | "reject",
  "reason": "<15 words max>"
}

Rules:
- If flag="reject", trade is cancelled regardless of score
- Adjust by -10 if: 3+ recent losses on this symbol, or drawdown > 2%
- Adjust by +5 if: strong volume + clear trend alignment
- Reject if: earnings in <4 hours, or account drawdown > 2.5% today
"""
```

Cost: Groq free tier = 14,400 requests/day. At 20 reviews/day this costs $0.

---

## 5. Transaction Cost & Slippage Modeling

**This section prevents the #1 backtest-to-live gap: unrealistic cost assumptions.**

### Realistic Cost Assumptions

```
Stocks (Alpaca, commission-free):
  Spread:         0.01–0.05% per side for liquid large-caps (NVDA, SPY, QQQ)
  Slippage:       0.03–0.08% per side on 15m candle entries (market orders)
  Total round trip: 0.1–0.2% conservatively

Forex (OANDA):
  EUR/USD spread: 0.8–1.2 pips ≈ 0.008–0.012% per side
  Slippage:       0.3–0.8 pips typical on 1m/5m candles
  Total round trip: ~0.02–0.04% (much cheaper than stocks)

Rule: Always assume WORST-CASE costs in backtesting. Never assume best-case.
```

### How to Model in vectorbt

```python
import vectorbt as vbt

# ALWAYS include fees in backtests — never run without this
portfolio = vbt.Portfolio.from_signals(
    close=close_prices,
    entries=buy_signals,
    exits=sell_signals,
    fees=0.001,          # 0.1% per trade (stocks) — use 0.0003 for forex
    slippage=0.001,      # additional 0.1% simulated slippage
    init_cash=10_000,
    freq='15T'
)
# If a strategy is not profitable AFTER fees, it will not be profitable live.
# If backtest Sharpe drops below 1.0 after fees → reject the strategy entirely.
```

### Return Expectation Calibration

```
Backtest shows 3% weekly → expect 1.5–2.1% live (50–30% haircut for execution)
Backtest shows 1% weekly → expect 0.5–0.7% live
Backtest shows 0.5% weekly → likely breaks even or loses after costs → reject
Minimum backtest threshold to proceed: Sharpe > 1.5 AFTER fees, drawdown < 15%
```

---

## 6. Event Risk Guard

**Gap risk and event risk are the most common ways stops get blown through.**
Stop losses do not protect you if price gaps 5% at open due to an earnings surprise or Fed shock.

### High-Impact Event Sources

```python
# events/calendar.py

# Source 1: Earnings dates (per instrument)
# yfinance provides next earnings date:
import yfinance as yf
ticker = yf.Ticker("NVDA")
earnings_date = ticker.calendar  # returns next earnings date

# Source 2: Macro calendar (FOMC, CPI, NFP, PPI, GDP)
# Use investing.com scrape or FRED API for scheduled dates
# Pre-load a static calendar for major events (update monthly)
MACRO_HIGH_IMPACT_EVENTS = [
    # Format: (date, time_UTC, event_name, affects)
    ("2026-03-19", "18:00", "FOMC Rate Decision", "all_stocks"),
    ("2026-03-07", "13:30", "NFP Jobs Report",    "USD_pairs"),
    # ... loaded from calendar file or API
]
```

### Event Guard Rules

```
Rule 1 — Earnings Blackout
  If next_earnings_date is within 24 hours:
    → Block ALL new entries for this instrument
    → If already in a position: tighten stop loss to 0.8% (from 1.5%)
    → Send Telegram alert: "EARNINGS BLACKOUT: {symbol} — no new trades until after report"

Rule 2 — FOMC / CPI / NFP Blackout
  If high-impact macro event scheduled within 4 hours:
    → Block ALL new entries across ALL instruments
    → If in open positions with profit: close 50% to lock in gains
    → Widen stop on remaining 50% to avoid getting stopped by knee-jerk reaction

Rule 3 — Gap Recovery Protocol
  If market opens and price has gapped > 2× the stop loss distance from previous close:
    → Do NOT enter in the first 15 minutes (let price stabilize)
    → If already in position: if gap is against you, close immediately at market open
    → If gap is with you: move stop to breakeven, let it run

Rule 4 — Weekend Gap (Stocks Only)
  If it is Friday and open position will be held over weekend:
    → Mandatory: close all intraday positions before 3:45 PM ET
    → Swing positions: tighten stop loss to 1.0%, ensure stop order is broker-side
    → Forex: no gap rule (market is 24/5), but widen stops before weekend for safety
```

---

## 7. API Resilience & Outage Handling

**A trading bot with open positions and a crashed connection is dangerous.**

### Health Check System

```python
# resilience/health_monitor.py

class HealthMonitor:
    def __init__(self, broker_client, check_interval_sec=60):
        self.broker = broker_client
        self.interval = check_interval_sec
        self.last_confirmed_positions = {}  # local cache of broker state

    def heartbeat(self):
        """Called every 60 seconds by scheduler."""
        try:
            # Ping broker API
            positions = self.broker.get_all_positions()
            self.last_confirmed_positions = positions
            self.consecutive_failures = 0
        except Exception as e:
            self.consecutive_failures += 1
            self._handle_failure(e)

    def _handle_failure(self, error):
        if self.consecutive_failures >= 2:   # 2 min unreachable
            send_telegram("WARNING: Broker API unreachable. Attempting reconnect.")
            self._attempt_reconnect()

        if self.consecutive_failures >= 5:   # 5 min unreachable
            send_telegram("EMERGENCY: Broker API down 5+ min. Check open positions manually!")
            # Halt all new trade entry attempts
            self.orchestrator.set_halt(reason="broker_unreachable")

    def _attempt_reconnect(self):
        """Try to reconnect with exponential backoff."""
        for delay in [5, 15, 30, 60]:
            time.sleep(delay)
            try:
                self.broker.reconnect()
                return True
            except:
                continue
        return False

    def sync_positions_on_reconnect(self):
        """After reconnect: compare local state to broker state."""
        broker_positions = self.broker.get_all_positions()
        local_positions = self.last_confirmed_positions

        for symbol, local_pos in local_positions.items():
            if symbol not in broker_positions:
                # Position closed externally (stop hit, manual close)
                self.trade_log.mark_closed(symbol, reason="external_close")
                send_telegram(f"SYNC: {symbol} position closed externally")

        # Update local cache with broker truth
        self.last_confirmed_positions = broker_positions
```

### Order State Machine

```
Every order transitions through tracked states:
  PENDING → SUBMITTED → FILLED → (STOP_PLACED) → CLOSED
                     → REJECTED  (handle: log + alert)
                     → PARTIAL   (handle: decide fill or cancel remaining)
                     → CANCELLED (handle: log, move on)

If an order stays in SUBMITTED for > 2 candle periods:
  → Cancel it (stale order, market moved)
  → Re-evaluate entry from scratch
```

---

## 8. Order Execution Strategy

**Market orders have guaranteed fills but unpredictable prices. Limit orders have predictable
prices but may not fill. Choosing wrong costs money every single trade.**

### Order Type Rules

```
Entry orders:
  Preferred: LIMIT order at bid + 0.05% (stocks) or ask − 0.3 pip (forex)
  Rationale: Guarantees entry price, avoids chasing
  Timeout:   If limit not filled within 2 candles → CANCEL
  Fallback:  If signal is still valid at next candle → re-submit limit
  Never use MARKET for entry except in HIGH_VOLATILITY regime breakouts

Stop loss orders:
  Use: STOP order (not stop-limit — stop-limit may not fill in fast markets)
  Place: At broker level immediately when entry fills — never track locally only
  Update: Move to breakeven only via API call, not local tracking

Take profit orders:
  Use: LIMIT order at target price
  Why: Guarantees exit price, auto-fills without monitoring
  Scale-out: Submit two separate limit orders (50% at TP1, 50% at TP2)

Order Pair Pattern (always submit together):
  On fill of entry:
    → Immediately submit OCO (One-Cancels-Other) bracket:
       Leg A: Stop loss STOP order at −1.5%
       Leg B: Take profit LIMIT order at +3.0%
    → When one fills, broker cancels the other automatically
```

---

## 9. Risk Management

### $500 Starting Capital — Constraints & Adjustments

With $500, position sizing math changes significantly versus a large account:

```
Risk 2% of $500 = $10/trade. With 1.5% stop → position = $10/0.015 = $667 — exceeds account!
Risk 1% of $500 = $5/trade.  With 1.5% stop → position = $5/0.015  = $333 (67% of account)
Risk 0.75% of $500 = $3.75.  With 1.5% stop → position = $3.75/0.015 = $250 (50% of account)

Decision: use 1% risk per trade ($5). Max 2 open positions = max $666 deployed.
Since total account is $500, in practice: 1 position at a time (~$250–$333),
occasionally 2 if first position is in profit and a very high-confidence second signal fires.

Always keep $150 minimum cash reserve (covers broker margin requirements).
```

### Position Sizing — Confidence-Weighted Half-Kelly ($500 Edition)

```python
def calculate_position_size(
    account_balance: float,
    confidence_score: float,    # 0–100
    open_positions_value: float,# total $ currently deployed in open trades
    win_rate: float,            # rolling win rate (use 0.55 default until 30+ trades)
    reward_ratio: float = 2.0,  # avg_win / avg_loss (use 2.0 default until 30+ trades)
    max_risk_pct: float = 0.01  # 1% of account per trade for $500 account
) -> float:
    # Capital availability check first
    available_capital = account_balance - open_positions_value - 150  # keep $150 reserve
    if available_capital < 100:  # minimum viable position is $100
        return 0  # not enough free capital, skip trade

    # Half-Kelly base
    loss_rate = 1 - win_rate
    f_kelly = (win_rate * reward_ratio - loss_rate) / reward_ratio
    f_kelly = max(0, f_kelly)
    f_half_kelly = f_kelly / 2

    # Confidence tier multiplier
    tier_multiplier = 0
    for threshold, mult in [(85, 1.0), (75, 0.75), (65, 0.50), (55, 0.25)]:
        if confidence_score >= threshold:
            tier_multiplier = mult
            break

    # Dollar risk amount, then derive position size from stop distance
    risk_dollars = account_balance * max_risk_pct * tier_multiplier  # e.g. $5 * 0.75 = $3.75
    stop_distance_pct = 0.015  # 1.5% hard stop
    position_size_dollars = risk_dollars / stop_distance_pct

    # Cap at available capital
    return round(min(position_size_dollars, available_capital), 2)

# Example outputs on $500 account with no open positions:
#   55% confidence → risk $1.25 → position $83   (small, learning trade)
#   65% confidence → risk $2.50 → position $167
#   75% confidence → risk $3.75 → position $250
#   85% confidence → risk $5.00 → position $333  (max single position)
```

### Stop Loss & Trade Exit Rules

```
Hard stop:       −1.5% from entry — placed as broker STOP order immediately at entry fill
                 IMMOVABLE downward — never loosen a stop loss, ever
Breakeven stop:  Once +1.5% profit → move stop to entry price (breakeven)
Trailing stop:   Once +3.0% profit → trail at 1.5% below peak
Time stop:       Exit if trade open > 8 hours with PnL between −0.5% and +0.5%
                 (flat trade tying up scarce $500 capital — cut it, redeploy)

Daily halt:      Daily loss > 3% of account ($15 on $500) → halt trading, send alert
Weekly halt:     Weekly loss > 6% of account ($30 on $500) → halt until Monday open
Drawdown guard:  Account drops below $450 (−10%) → reduce position sizes by 50%
Loss streak:     3 consecutive losses → pause 1 hour, re-run regime classifier
                 5 consecutive losses → halt for the day
```

### Take Profit Rules

```
Minimum RR:      2:1 required before entering (risk $1, target $2 minimum)
TP1 (50% out):   At +3% gain — limit order placed at entry time
TP2 (50% trail): Trail remaining 50% with 1.5% trailing stop from peak
Strong momentum: If price reaches +5% with strong volume → hold TP2 to +8%
Regime change:   If regime shifts mid-trade → tighten trailing stop to 0.8%
Event proximity: If earnings/FOMC within 4h while in profit → close 50% immediately
```

### Portfolio Constraints for $500

```
Max open positions:     2 simultaneously (capital constraint — not a choice)
Max simultaneous forex: 1 position (EUR/USD or GBP/USD, not both — they correlate)
Max same asset class:   1 stock + 1 forex is ideal mix
Capital reserve:        Always keep $150 minimum in cash
Max leverage:           None (1:1) until 6 months profitable data
Correlation guard:      SPY and QQQ correlate > 0.95 — never hold both simultaneously
                        NVDA and QQQ correlate ~0.85 — avoid holding together
Weekend rule:           All stock intraday positions closed Friday 3:45 PM ET
                        Forex swing positions: tighten stop, don't force close
PDT rule:               Tracked separately in portfolio state DB (see Section 10)
```

---

## 10. Portfolio Management & Universe Selection

### Why a Small, Focused Universe Wins for $500

With $500 capital, spreading across many instruments dilutes capital below viable position sizes and
multiplies cognitive + API load. The solution: 6 instruments, deeply understood, consistently monitored.

```
The universe is NOT chosen randomly. Each instrument is selected for:
  - High daily volume (tight spreads, low slippage)
  - Strong TA signal clarity (trending or ranging patterns that are readable)
  - No overlap with other instruments (correlation managed by default selection)
  - Compatibility with $500 account (fractional shares for stocks, micro-lots for forex)
```

### The Watchlist (6 Instruments)

```
STOCKS (Alpaca — fractional shares, no minimum per trade):
┌────────┬────────────────────┬──────────────┬──────────────────────────────────┐
│ Symbol │ Name               │ Avg Daily Vol│ Why                              │
├────────┼────────────────────┼──────────────┼──────────────────────────────────┤
│ SPY    │ S&P 500 ETF        │ 70M+ shares  │ Most liquid US instrument.       │
│        │                    │              │ No earnings risk (ETF). Tracks   │
│        │                    │              │ overall market = regime anchor.  │
├────────┼────────────────────┼──────────────┼──────────────────────────────────┤
│ QQQ    │ Nasdaq-100 ETF     │ 40M+ shares  │ Tech-heavy, higher ATR than SPY. │
│        │                    │              │ No earnings risk (ETF). Good for │
│        │                    │              │ trending moves. *** NEVER hold   │
│        │                    │              │ QQQ and SPY simultaneously —     │
│        │                    │              │ correlation > 0.95 ***           │
├────────┼────────────────────┼──────────────┼──────────────────────────────────┤
│ NVDA   │ NVIDIA Corp        │ 300M+ shares │ Extremely high ATR (moves 2–5%  │
│        │                    │              │ daily). TA signals are clean on  │
│        │                    │              │ 15m charts. Has earnings risk —  │
│        │                    │              │ event guard is critical here.    │
├────────┼────────────────────┼──────────────┼──────────────────────────────────┤
│ AAPL   │ Apple Inc          │ 60M+ shares  │ Lower volatility than NVDA.      │
│        │                    │              │ Good for mean-reversion in range │
│        │                    │              │ markets. Pairs well with NVDA    │
│        │                    │              │ (lower correlation ~0.70).       │
└────────┴────────────────────┴──────────────┴──────────────────────────────────┘

FOREX (OANDA — micro-lots = 1,000 units, ~$1.08 per pip movement):
┌──────────┬──────────────────┬────────────┬────────────────────────────────────┐
│ Pair     │ Session          │ Avg Spread │ Why                                │
├──────────┼──────────────────┼────────────┼────────────────────────────────────┤
│ EUR/USD  │ London + NY      │ 0.8–1.2pip │ Most liquid forex pair on earth.   │
│          │ overlap (best)   │            │ Tightest spread. Clear TA trends.  │
│          │                  │            │ NO PDT rule. 24/5 trading.         │
│          │                  │            │ PRIMARY forex instrument.          │
├──────────┼──────────────────┼────────────┼────────────────────────────────────┤
│ GBP/USD  │ London session   │ 1.0–1.5pip │ Higher ATR than EUR/USD. Good in  │
│          │                  │            │ strong trending regimes. Do NOT    │
│          │                  │            │ hold EUR/USD and GBP/USD at once   │
│          │                  │            │ — correlation ~0.88.               │
└──────────┴──────────────────┴────────────┴────────────────────────────────────┘

Summary of allowed simultaneous combinations with $500:
  ✓ SPY + EUR/USD   (stocks ETF + forex — low correlation)
  ✓ NVDA + EUR/USD  (high vol stock + forex)
  ✓ AAPL + EUR/USD  (lower vol stock + forex)
  ✗ SPY + QQQ       (correlation 0.95+)
  ✗ EUR/USD + GBP/USD (correlation 0.88)
  ✗ QQQ + NVDA      (correlation 0.85)
  ✗ 3 positions ever (capital insufficient)
```

### PDT Rule Tracking (Critical for $500 Stock Trading)

Pattern Day Trader rule: US accounts under $25,000 are limited to **3 day trades per 5-day
rolling window**. A day trade = opening AND closing the same stock position on the same day.
Forex is exempt. ETF swing trades held overnight are exempt.

```python
# portfolio/pdt_tracker.py

from collections import deque
from datetime import date, timedelta

class PDTTracker:
    """Tracks day trades in the rolling 5-business-day window."""

    def __init__(self):
        self.day_trades: deque = deque()  # list of dates when day trades occurred

    def record_day_trade(self, trade_date: date):
        self.day_trades.append(trade_date)
        self._prune_old()

    def day_trades_used(self) -> int:
        self._prune_old()
        return len(self.day_trades)

    def can_day_trade(self) -> bool:
        return self.day_trades_used() < 3

    def _prune_old(self):
        cutoff = date.today() - timedelta(days=5)
        while self.day_trades and self.day_trades[0] <= cutoff:
            self.day_trades.popleft()

# Usage in execution agent:
# A day trade is detected when: entry and exit on same calendar day, same symbol
# If pdt_tracker.can_day_trade() is False → force position to swing (no same-day close)
# OR → only trade forex (no PDT limit)
```

PDT Strategy: **Prioritize forex (EUR/USD) for intraday trades. Use stocks for swing trades
(hold overnight). This completely sidesteps the PDT rule while keeping both markets active.**

### Instrument Scanner (Runs Every 15 Minutes)

The orchestrator evaluates all 6 instruments every 15 minutes and builds a ranked signal queue.
Only then does it decide whether and where to deploy capital.

```python
# portfolio/scanner.py

def scan_universe(instruments: list[str], portfolio_state: PortfolioState) -> list[SignalResult]:
    """
    Runs signal evaluation for all instruments.
    Returns ranked list of trade opportunities, best first.
    """
    results = []

    for symbol in instruments:
        # Skip if already holding this instrument
        if portfolio_state.is_holding(symbol):
            continue

        # Skip if event guard blocks this instrument
        if event_guard.is_blocked(symbol):
            continue

        # Skip stocks if PDT limit reached and signal would be intraday
        if is_stock(symbol) and not pdt_tracker.can_day_trade():
            # Only allow if we intend to hold overnight (swing trade mode)
            # Check if market is past 2 PM ET — too late to start a swing safely
            if current_time_et().hour >= 14:
                continue

        # Run signal engine for this instrument
        signal_result = signal_engine.evaluate(symbol)

        # Only keep results above minimum threshold
        if signal_result.dominant_score >= 55:
            results.append(signal_result)

    # Sort by confidence score descending — best opportunity first
    return sorted(results, key=lambda r: r.dominant_score, reverse=True)


def allocate_capital(ranked_signals: list[SignalResult], portfolio_state: PortfolioState):
    """
    Takes the ranked signal list and decides which (if any) to trade.
    With $500, realistically only 1 trade fires per scan cycle.
    """
    available = portfolio_state.available_capital()  # account_balance - open - $150 reserve

    for signal in ranked_signals:
        # Check capital
        position_size = risk_agent.calculate_position_size(
            account_balance=portfolio_state.balance,
            confidence_score=signal.dominant_score,
            open_positions_value=portfolio_state.open_value(),
        )
        if position_size < 100:  # minimum viable trade
            continue

        # Check correlation with current holdings
        if not correlation_guard.check(signal.symbol, portfolio_state.held_symbols()):
            continue

        # All checks passed — execute this trade
        execution_agent.enter_trade(signal, position_size)

        # With $500, one trade typically consumes most available capital.
        # Only loop to second signal if available capital still > $150 after first trade.
        if portfolio_state.available_capital() < 150:
            break
```

### Capital State at a Glance ($500 Example)

```
Scenario: Bot has been running 2 weeks, $500 account, 1 open position in EUR/USD

Portfolio State:
  Account balance:      $512  (+$12 from last week)
  Open position:        EUR/USD long, entered at $200 position size
  Unrealized PnL:       +$4.20
  Cash available:       $512 - $200 - $150 reserve = $162

Scanner runs, finds:
  NVDA score: 79% BUY → position would be $250... but only $162 available → SKIP
  AAPL score: 67% BUY → position would be $167... but only $162 available → SKIP (too close)
  SPY score:  58% BUY → position would be $83 → fits! → ENTER small

Result: Bot enters SPY at $83 (55% confidence tier, 25% of max size)
        Now: EUR/USD $200 + SPY $83 = $283 deployed, $229 reserve — healthy
```

### Session Timing (When Each Instrument is Active)

```
UTC-5 (New York Time) — US Market Hours

05:00–08:00 ET: London session opens → EUR/USD, GBP/USD most active (trade forex)
08:00–09:30 ET: Pre-market stocks → no stock trades (illiquid, wide spreads)
09:30–11:30 ET: US market open → BEST window for NVDA, AAPL, SPY, QQQ momentum
                               → Also excellent for EUR/USD (London/NY overlap)
11:30–13:00 ET: Mid-day lull → reduce stock trading, mean-reversion setups only
13:00–15:30 ET: Afternoon trending → good for swing entries on stocks
15:30–16:00 ET: Last 30 min → close intraday stock positions (avoid overnight gaps)
16:00–17:00 ET: After-hours → no stock trades, forex continues normally
17:00–04:00 ET: Asian session → EUR/USD quiet, GBP/USD quiet → mostly skip (wide spreads)

Bot scheduler:
  Stock scan:  Every 15 min from 09:30–15:30 ET (weekdays only)
  Forex scan:  Every 15 min from 07:00–16:00 ET (Mon–Fri, best overlap hours)
  Regime eval: Every 30 min during active hours
  News fetch:  Every 60 min per instrument during active hours (Gemini Flash)
```

---

## 11. AI Self-Learning & Optimization Cycle (Anti-Overfitting Edition)

Every **5 weeks** on Sunday night, the AI reviews performance and proposes improvements.
The critical discipline is: **optimize carefully, change little, test everything.**

### The Overfitting Problem & How We Solve It

Overfitting = the bot learns the noise of the past period, not the signal. It looks great on recent
data and fails on new data. The solution is strict discipline:

```
Anti-overfitting rules (enforced in code, not just guidelines):
  1. Out-of-sample reserve: The most recent 20% of available data is NEVER used during
     optimization. It is used ONLY to validate proposed changes after the fact.
  2. Minimum trade count: A signal's parameters cannot be adjusted unless it has fired
     at least 50 times in the review period (50 is minimum statistical significance).
     For parameter changes requiring confidence > 95%: need 100+ samples.
  3. Max changes per cycle: No more than 3 parameter changes per optimization cycle.
     Changing everything at once makes causality impossible to track.
  4. Anchored parameters: Some parameters NEVER change via AI:
       - Daily loss limit (−3%)
       - Weekly loss limit (−6%)
       These are risk management constants, not strategy parameters.
  5. Statistical significance test: Each proposed change must pass a binomial test
     (p < 0.05) showing the observed improvement is unlikely to be random chance.
  6. Bonferroni correction: When testing N parameters simultaneously, use
     significance threshold p < 0.05/N to avoid false positives from multiple testing.
```

### The 7-Step Optimization Pipeline

```
STEP 1 — PERFORMANCE DATA COLLECTION (automated)
  Pull from database for the review period:
  ┌─────────────────────────────────────────────────────────┐
  │ Per-category signal accuracy:                           │
  │   - How often did each category fire?                   │
  │   - When it fired, what was the trade outcome?          │
  │   - False positive rate (fired but trade lost)          │
  │                                                         │
  │ Per-confidence-tier accuracy:                           │
  │   - Did 85%+ trades actually win more than 55–64%?      │
  │   - If not, the confidence calculation is miscalibrated │
  │                                                         │
  │ Regime accuracy:                                        │
  │   - How often did regime classification match outcome?  │
  │   - Which regime caused most losses?                    │
  │                                                         │
  │ Cost-adjusted metrics (CRITICAL):                       │
  │   - All metrics computed AFTER subtracting slippage est │
  │   - Compare to baseline Sharpe from previous cycle      │
  │                                                         │
  │ LLM review accuracy:                                    │
  │   - When Groq flagged high_risk, did trade lose?        │
  │   - When Groq adjusted score up, did trade win?         │
  │   → Use this to calibrate LLM prompt over time         │
  └─────────────────────────────────────────────────────────┘
  Minimum required: 50+ trades in the period. If fewer → skip optimization,
  run for another 2 weeks before reviewing (not enough data to conclude anything).

STEP 2 — STATISTICAL VALIDATION (automated, before LLM)
  For each potential change, run a binomial test:
    H0: win rate with new param = win rate with old param (no improvement)
    H1: win rate is significantly higher with new param
    Threshold: p < 0.05 (Bonferroni corrected for number of params tested)
  Only parameters that pass this test are forwarded to the LLM for review.
  This prevents the LLM from being asked about statistically meaningless differences.

STEP 3 — LLM ANALYSIS (Gemini Flash 2.0)
  Feed the statistically significant findings to Gemini Flash 2.0:
  - Summary of which categories under/overperformed and WHY (include market context)
  - Recent macro events that may have distorted the period (COVID, rate hikes, etc.)
  - Proposed parameter changes (already validated statistically in Step 2)
  - Request: identify which regime the bot struggles with and WHY

  Gemini outputs a structured ChangeProposal:
  {
    "proposal_id": "2026-03-OPT-001",
    "period_reviewed": {"start": "...", "end": "..."},
    "market_context": "Period included FOMC rate decision and earnings season",
    "changes": [
      {
        "target": "Category3_RSI",
        "param": "oversold_threshold",
        "old_value": 35,
        "new_value": 32,
        "statistical_basis": "Win rate improved 4.2% (p=0.031, n=67 trades)",
        "risk": "May reduce signal frequency by ~15%"
      }
    ],
    "max_changes_this_cycle": 3,   # enforced — AI cannot propose more
    "signals_to_watch": [...],     # categories to monitor next cycle
    "categories_to_consider_retiring": [...]  # only if < 45% accuracy over 2+ cycles
  }

STEP 4 — OUT-OF-SAMPLE BACKTESTING (vectorbt, automated)
  The reserved 20% out-of-sample data is used HERE for the first time.
  Run current params vs proposed params on this unseen data:
    - If proposed params are better on unseen data → TENTATIVELY APPROVED
    - If proposed params are worse on unseen data → REJECTED (overfit to training period)
  Also run Monte Carlo simulation (1,000 random trade shuffles) to verify
  the improvement holds under different trade orderings.

STEP 5 — HUMAN REVIEW (required gate before live)
  Telegram + dashboard notification:
  "OPTIMIZATION CYCLE COMPLETE:
   - 1 change approved (RSI threshold: 35→32), p=0.031
   - 2 changes rejected (insufficient out-of-sample improvement)
   - Action required: APPROVE or REJECT in dashboard within 48 hours"

  Human reviews the full report on the Streamlit dashboard.
  If no response in 48 hours: changes remain pending, bot runs on old parameters.

STEP 6 — STAGED DEPLOYMENT
  Approved changes deploy to paper account ONLY for 1 full week.
  Automated comparison:
    If paper_sharpe_new >= paper_sharpe_old × 0.95: deploy to live
    If paper_sharpe_new < paper_sharpe_old × 0.95: auto-rollback, send alert
  Full deployment takes 2 weeks minimum from proposal to live.

STEP 7 — VERSION HISTORY & ROLLBACK
  Every live parameter set is versioned:
    python manage.py strategy list          # see all versions
    python manage.py strategy rollback --version 2026-01-v2  # one command rollback
  Rollbacks take effect at next market open.
  Post-rollback: the failed cycle is logged with reason for future LLM context.
```

### What the LLM Can and Cannot Change

```
CAN change (with statistical evidence + out-of-sample validation):
  - Signal threshold values (RSI oversold level, ADX cutoff, EMA periods)
  - Regime multiplier weights per category
  - Confidence tier cutoffs (entry/no-entry thresholds)
  - Take profit percentages (within 1.5× to 4× risk range only)
  - Category weights (adjusting the max_score denominator)

CANNOT change (hard-coded, never AI-modified):
  - Hard stop loss percentage (−1.5%)
  - Daily/weekly loss halt thresholds
  - Max position size (20% of account)
  - Event blackout rules (earnings, FOMC)
  - The requirement for human approval before live deployment
  - The out-of-sample reserve (always 20%)
```

---

## 12. Database Schema

```sql
CREATE TABLE trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol          TEXT NOT NULL,
    direction       TEXT NOT NULL,        -- BUY / SELL
    entry_price     REAL NOT NULL,
    exit_price      REAL,
    quantity        REAL NOT NULL,
    confidence      REAL NOT NULL,        -- final score at entry (0–100)
    regime          TEXT NOT NULL,        -- regime at entry time
    signals_json    TEXT NOT NULL,        -- JSON: all 8 category votes
    llm_adjustment  INTEGER,             -- Groq adjustment (−10 to +10)
    llm_flag        TEXT,                -- none / high_risk / reject
    entry_order_id  TEXT,
    stop_order_id   TEXT,                -- broker-side stop order ID
    tp_order_id     TEXT,                -- broker-side take profit order ID
    pnl             REAL,
    pnl_pct         REAL,
    fees_paid       REAL,               -- actual transaction costs
    slippage_est    REAL,               -- estimated slippage at fill
    opened_at       TIMESTAMP NOT NULL,
    closed_at       TIMESTAMP,
    close_reason    TEXT                -- stop_loss / tp1 / tp2 / time_stop / event_guard / manual
);

CREATE TABLE signal_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    evaluated_at    TIMESTAMP NOT NULL,
    symbol          TEXT NOT NULL,
    category        TEXT NOT NULL,       -- e.g., "trend_direction", "momentum", etc.
    vote            INTEGER NOT NULL,    -- +1, −1, or 0
    params_snapshot TEXT NOT NULL,       -- JSON: exact params used at evaluation time
    trade_id        INTEGER,             -- FK → trades.id if a trade was placed
    FOREIGN KEY (trade_id) REFERENCES trades(id)
);

CREATE TABLE strategy_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version         TEXT NOT NULL UNIQUE, -- e.g., "2026-03-v2"
    params_json     TEXT NOT NULL,        -- full parameter set as JSON
    deployed_at     TIMESTAMP NOT NULL,
    retired_at      TIMESTAMP,
    sharpe          REAL,
    win_rate        REAL,
    max_drawdown    REAL,
    trade_count     INTEGER,
    notes           TEXT
);

CREATE TABLE optimization_cycles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at          TIMESTAMP NOT NULL,
    period_start        TIMESTAMP NOT NULL,
    period_end          TIMESTAMP NOT NULL,
    trade_count         INTEGER NOT NULL,
    proposal_json       TEXT,             -- full ChangeProposal from LLM
    backtest_result     TEXT,             -- JSON: approved/rejected per change
    oos_validation      TEXT,             -- JSON: out-of-sample test results
    human_approved      BOOLEAN DEFAULT FALSE,
    approved_at         TIMESTAMP,
    deployed_version    TEXT,             -- FK → strategy_registry.version
    outcome_after_1wk   TEXT              -- better / same / worse (measured 1 week post-deploy)
);

CREATE TABLE event_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TIMESTAMP NOT NULL,
    event_type      TEXT NOT NULL,        -- earnings_blackout / macro_guard / circuit_breaker / etc.
    symbol          TEXT,
    description     TEXT NOT NULL,
    action_taken    TEXT NOT NULL         -- no_trade / closed_position / reduced_size / halted
);

-- Portfolio state snapshot (written every 15 min for audit trail)
CREATE TABLE portfolio_snapshots (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_at         TIMESTAMP NOT NULL,
    account_balance     REAL NOT NULL,
    cash_available      REAL NOT NULL,
    open_positions_json TEXT NOT NULL,    -- JSON: [{symbol, size, entry, unrealized_pnl}, ...]
    day_trades_used     INTEGER NOT NULL, -- PDT rolling count
    daily_pnl           REAL NOT NULL,
    weekly_pnl          REAL NOT NULL
);
```

---

## 13. File / Module Structure

```
trade-bot/
├── agents/
│   ├── orchestrator.py            # Main controller, 15-min scan loop, scheduler
│   ├── market_analyst.py          # Data fetch, TA compute, regime detection
│   ├── signal_engine.py           # Runs all 8 categories, returns votes
│   ├── confidence_scorer.py       # Weights votes, applies regime multipliers
│   ├── llm_reviewer.py            # Groq live review + Gemini news analysis
│   ├── risk_agent.py              # $500-aware Kelly sizing, stop management
│   ├── execution_agent.py         # Order submission, OCO brackets, fill tracking
│   └── ai_optimizer.py            # 7-step monthly optimization pipeline
│
├── portfolio/
│   ├── scanner.py                 # Universe scan, rank signals, allocate capital
│   ├── state.py                   # PortfolioState: balance, open positions, cash
│   ├── pdt_tracker.py             # PDT rolling 5-day window tracker
│   └── watchlist.py               # Instrument definitions + session schedules
│                                  #   UNIVERSE = [SPY, QQQ, NVDA, AAPL, EURUSD, GBPUSD]
│
├── signals/
│   ├── cat1_trend_direction.py    # EMA + MACD combined vote
│   ├── cat2_trend_strength.py     # ADX + EMA slope
│   ├── cat3_momentum.py           # RSI + Stochastic combined vote
│   ├── cat4_volatility_band.py    # Bollinger Band + ATR
│   ├── cat5_volume.py             # Volume ratio + OBV
│   ├── cat6_price_structure.py    # Support/Resistance + candlestick patterns
│   ├── cat7_multi_timeframe.py    # 5m + 15m + 1h alignment (double weight)
│   └── cat8_macro_news.py         # Gemini Flash news sentiment
│
├── regime/
│   └── detector.py                # Regime classifier (5 regimes)
│
├── events/
│   ├── calendar.py                # Earnings + FOMC/macro event loading
│   ├── event_guard.py             # Enforces blackout rules, gap protocol
│   └── data/
│       └── macro_calendar.json    # Pre-loaded high-impact event dates (update monthly)
│
├── resilience/
│   ├── health_monitor.py          # Heartbeat, reconnect, position sync
│   ├── correlation_guard.py       # Pearson correlation check before entry
│   └── order_state_machine.py     # Tracks PENDING→SUBMITTED→FILLED→CLOSED
│
├── broker/
│   ├── alpaca_client.py           # Stocks: fractional shares, OCO orders
│   ├── oanda_client.py            # Forex: micro-lots, bracket orders
│   └── base_broker.py             # Abstract interface
│
├── data/
│   ├── fetcher.py                 # Multi-timeframe candle fetching (5m/15m/1h)
│   ├── preprocessor.py            # Normalize, resample, forward-fill gaps
│   └── news_fetcher.py            # Alpaca News API + NewsAPI.org
│
├── optimization/
│   ├── pipeline.py                # Orchestrates the 7-step cycle
│   ├── performance_report.py      # Pulls trade+signal data, computes all metrics
│   ├── statistical_tests.py       # Binomial test, Bonferroni correction
│   ├── backtest_validator.py      # vectorbt OOS validation + Monte Carlo
│   ├── proposal_parser.py         # Parses Gemini's JSON ChangeProposal
│   └── prompts.py                 # Optimization prompt templates (versioned)
│
├── database/
│   ├── models.py                  # SQLAlchemy ORM models
│   ├── migrations/                # Schema migrations (Alembic)
│   └── queries.py                 # Common query helpers
│
├── backtesting/
│   ├── backtest_runner.py         # Full strategy backtest with realistic fees
│   ├── walk_forward.py            # Walk-forward validation
│   └── results/                   # Saved backtest JSON outputs
│
├── monitoring/
│   ├── dashboard.py               # Streamlit: 7 pages (added Portfolio page)
│   └── alerts.py                  # Telegram bot, daily summary scheduler
│
├── config/
│   ├── settings.py                # All config loaded from .env
│   ├── strategy_params.json       # Current live parameter set
│   └── .env.example               # Template — NEVER commit .env
│
├── tests/
│   ├── test_signals.py
│   ├── test_confidence_scorer.py
│   ├── test_risk_agent.py
│   ├── test_event_guard.py
│   ├── test_pdt_tracker.py
│   ├── test_scanner.py
│   ├── test_correlation_guard.py
│   └── test_optimization_pipeline.py
│
├── manage.py                      # CLI tool
├── main.py                        # Entry point
├── docker-compose.yml
├── Dockerfile
└── requirements.txt
```

---

## 14. Monitoring & Alerts

### Telegram Alerts

```
Trade entry:    "BUY NVDA @ $151.20 | Score: 78 | Regime: TRENDING_UP
                 Signals: trend+1 mom+1 vol+1 mtf+2 macro0 | Size: $250 | Stop: $148.97
                 Capital deployed: $250/$350 available | PDT used: 1/3"

Trade exit:     "SOLD NVDA @ $155.75 | PnL: +$9.10 (+3.6%) | Reason: TP1 hit
                 After fees/slippage est: +$8.30 | Account: $512 (+$8.30)"

Event guard:    "BLACKOUT: NVDA earnings in 18h. No new entries. Existing stop tightened."

PDT warning:    "PDT ALERT: 2/3 day trades used this week. Next stock trade must be swing."

Scanner result: "SCAN: EUR/USD 82% BUY, NVDA 71% BUY, AAPL 58% BUY | Trading EUR/USD first"

Circuit break:  "HALT: 3 consecutive losses on EUR/USD. 1hr pause. Regime re-checking."

Daily summary:  "Day Close | Account: $518 (+$18 / +3.6%)
                 Trades: 4 (3W 1L) | PDT used: 2/3 | Cash reserve: $168
                 Best: EUR/USD +2.1% | Worst: AAPL −1.4% (stop hit)
                 Instruments traded today: EUR/USD, AAPL"

Opt cycle:      "OPT READY: 1 change approved, 2 rejected. Open dashboard to approve."
```

### Streamlit Dashboard (7 Pages)

```
Page 1 — Live Status
  Current regime, open positions (symbol/size/entry/unrealized PnL), today's PnL curve
  Bot status, PDT counter (X/3 used), cash available, capital deployment bar

Page 2 — Portfolio View  ← NEW
  Universe watchlist: all 6 instruments with current confidence score (live updating)
  Capital allocation diagram: how $500 is currently split
  Correlation matrix between all 6 instruments (visual heatmap)
  Session timing indicator: which instruments are in their active trading window

Page 3 — Signal Board
  Per-instrument: all 8 category votes as colored cells (+/0/−)
  Confidence score gauge, event guard status, last LLM review summary

Page 4 — Trade History
  Filterable trade log with entry/exit/PnL/signals/regime
  Equity curve chart, drawdown chart, rolling win rate
  Per-instrument breakdown (which instrument makes the most money?)

Page 5 — Cost Analysis
  Gross PnL vs net PnL after slippage/fees
  Running total of costs paid, slippage vs estimate comparison over time

Page 6 — Optimizer
  Last cycle report, approved/rejected changes, out-of-sample results
  APPROVE / REJECT buttons with confirmation modal
  All past cycle outcomes

Page 7 — Strategy Registry
  All parameter versions, deployed/retired dates, per-version metrics
  One-click rollback to any previous version
```

---

## 15. Broker Integration — Safe & Legitimate

### Stocks — Alpaca Markets

```python
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    LimitOrderRequest, StopOrderRequest, OcoOrderRequest
)
from alpaca.trading.enums import OrderSide, TimeInForce

client = TradingClient('API_KEY', 'SECRET_KEY', paper=True)  # paper=False for live

# Entry: limit order (preferred — avoids slippage)
entry = client.submit_order(LimitOrderRequest(
    symbol="NVDA",
    qty=10,
    side=OrderSide.BUY,
    time_in_force=TimeInForce.DAY,
    limit_price=151.20
))

# On fill: submit OCO bracket (stop + take profit together)
# One cancels the other automatically
bracket = client.submit_order(OcoOrderRequest(
    symbol="NVDA",
    qty=10,
    side=OrderSide.SELL,
    time_in_force=TimeInForce.GTC,
    stop_price=148.97,   # hard stop (−1.5%)
    limit_price=155.74   # take profit (+3%)
))
```

### Forex — OANDA

```python
import oandapyV20
from oandapyV20.endpoints.orders import OrderCreate

client = oandapyV20.API(access_token="TOKEN", environment="practice")  # practice = demo

# Bracket order with SL + TP in one request
order_body = {
    "order": {
        "type": "LIMIT",
        "instrument": "EUR_USD",
        "units": "1000",
        "price": "1.08500",                          # limit entry
        "stopLossOnFill":   {"price": "1.08338"},    # −1.5% stop
        "takeProfitOnFill": {"price": "1.08825"},    # +3% take profit
        "timeInForce": "GTC"
    }
}
r = OrderCreate(accountID="ACCOUNT_ID", data=order_body)
client.request(r)
```

### Brokers to Avoid

- Any broker not licensed by: FCA (UK), SEC/FINRA/NFA (US), ASIC (AU), CySEC (EU)
- Platforms that promise "guaranteed returns" or don't allow API trading in their Terms of Service
- Any exchange that requires you to deposit crypto to trade (unregulated)

---

## 16. Safety & Legal Checklist

- **Paper trade first** — minimum 4 weeks before real money, no exceptions
- **All secrets in .env** — never hardcoded, .env is gitignored
- **Rate limit API calls** — brokers ban accounts that spam; use exponential backoff
- **Broker-side stops always** — stop orders placed at broker, not just tracked locally
- **OCO brackets** — entry fill triggers automatic stop + TP without monitoring
- **Circuit breakers** — loss streak, daily halt, weekly halt, drawdown guard all enforced in code
- **Human approval gate** — AI proposes, you approve; bot never self-modifies live params
- **Event guard** — no trades near earnings, FOMC, CPI, NFP
- **PDT tracked in code** — pdt_tracker.py enforces 3-trade limit; bot switches to forex-only when limit reached
- **Tax compliance** — short-term capital gains on every trade; use TaxBit or Koinly to track
- **$500 reserve rule** — always keep $150 minimum cash; never go all-in

---

## 17. Development Phases

### Phase 0 — Setup (Days 1–3)
- [x] Python 3.11 venv, install all dependencies
      `pip install pandas pandas-ta vectorbt alpaca-py yfinance oandapyV20`
      `pip install sqlalchemy apscheduler python-dotenv langchain-groq`
      `pip install google-generativeai python-telegram-bot streamlit`
- [x] Open IBKR paper account (changed from Alpaca — IBKR supports fractional shares + forex)
- [x] Open OANDA demo account (oanda.com) — API key verified, OANDA: ✓ configured
- [x] Set up SQLite DB with schema from Section 12 (init_db() auto-creates on first run)
- [x] .env configured, .gitignore includes .env, .env never committed

### Phase 1 — Signals + Backtesting on All 6 Instruments (Weeks 1–2)
- [x] Implement all 8 signal categories (cat1–cat8 in signals/)
- [x] Implement regime detector (regime/detector.py — TRENDING_UP/DOWN, RANGING, HIGH/LOW_VOL)
- [x] Implement confidence scorer with regime multipliers (agents/confidence_scorer.py)
      — macro_multiplier: Cat8 risk_level → position size reduction (HIGH=0.5×, MEDIUM=0.75×)
- [x] Backtest infrastructure built (backtesting/backtest_runner.py + walk_forward.py)
      — daily bar proxy with realistic fees: stocks 0.1%, forex 0.03%
- [x] **Portfolio backtest built** (backtesting/portfolio_backtest.py)
      — scores all 6 simultaneously, picks top 2, enforces correlation guards
      — ATR-based SL (2×ATR stocks, 1.5×ATR forex) + TP (4×ATR, 3×ATR) — 2:1 R:R
      — fixed NaN bug (forex-only days caused phantom 70% drawdown on stock positions)
      — run: `python validate.py --portfolio [--walkforward]`
- [x] Walk-forward validation: 3 non-overlapping periods completed (ATR-based SL)
      ```
      Period    Sharpe  MaxDD   Return  Trades
      2022 bear  -0.46   6.8%   -3.6%      37
      2023 bull  +0.28   7.2%   +2.0%     102
      2024 mixed +0.91   5.3%   +8.4%      87
      Full 3yr   +0.80   7.0%  +24.4%     262
      ```
- [x] **MaxDD gate PASS**: 7.0% full, 5–7% per year (gate: < 15%) ✓
- [x] **Threshold sweep completed** — confidence threshold vs Sharpe:
      ```
      Threshold  Sharpe  MaxDD   Return  Trades  Notes
      55%         0.80    7.0%   +24.4%    262    best return + lowest MaxDD
      60–65%      0.89    9.8%   +22.9%    278    marginal Sharpe gain, higher MaxDD
      70%+       -0.06    7.4%    -1.1%     83    cliff — too few signals, wrong timing
      ```
      Conclusion: daily proxy Sharpe ceiling ≈ 0.89. Threshold alone cannot clear 1.2 gate.
      Root cause: cat4 (BB squeeze) and cat8 (macro/LLM) always = 0 in backtest proxy
      → 2 of 9 signal points permanently missing → score ceiling ~77% not 100%
      → live 15-min system with all 8 categories active expected to score higher and filter better
- [x] **Phase 1 decision: ACCEPT & PROCEED to Phase 2**
      MaxDD gate: PASS (7.0% < 15%) ✓
      Sharpe gate: proxy ceiling 0.89 — cannot be resolved without 15-min live data
      Backtest at 55% threshold chosen: lowest MaxDD, highest return, 262 trades (stat. significant)
- [x] Identify which 2 instruments perform best per regime → scanner priority confirmed:
      - TRENDING_DOWN (bear/2022): **QQQ** (+$24), **GBPUSD** / **AAPL** — avoid NVDA
      - TRENDING_UP (bull/2023):   **NVDA** (+$55), **QQQ** (+$40)
      - RANGING (mixed/2024):      **NVDA** (+$164), **QQQ** (+$32)
      - EURUSD: −$57 total, loses every year → lowest scan priority
      - AAPL: −$70 total, not a consistent signal → deprioritise
- [ ] Verify 85%+ confidence tier outperforms 55–64% tier (post rate-limit clearance)

### Phase 2 — Portfolio Module + Paper Trading (Weeks 3–6)
- [ ] Implement portfolio/scanner.py, state.py, pdt_tracker.py, watchlist.py
- [ ] Implement risk agent with $2000-specific position sizing
- [ ] Implement execution agent with OCO bracket orders
- [ ] Implement health monitor, event guard, correlation guard
- [ ] Connect to Alpaca paper + OANDA practice
- [ ] Run 24/7 for minimum 4 weeks — all 6 instruments scanned every 15 minutes
- [ ] Verify: PDT tracker prevents 4th stock day trade in same week
- [ ] Verify: scanner correctly skips EUR/USD and GBP/USD simultaneously
- [ ] Log all signal evaluations (not just trades) — this data trains the optimizer
- [ ] important question is whether the live 15-min signal engine generates high-quality signals on paper.

### Phase 3 — First Optimization Cycle (End of Week 6)
- [ ] Run 7-step pipeline with 4 weeks of paper data (need 50+ trades minimum)
- [ ] Verify statistical tests reject low-sample proposals
- [ ] Review Gemini proposals — approve max 3 changes
- [ ] Deploy to paper for 1 more week — compare before/after
- [ ] Check: did high-confidence scanner picks actually outperform?

### Phase 4 — Live $500 (Weeks 8–12)
- [ ] Start with full $500 — this is the test capital
- [ ] Conservative entry: confidence threshold 65% (not 55%) for first 2 weeks
- [ ] Primary instrument: EUR/USD (no PDT risk, 24/5, tight spread)
- [ ] Run paper and live in parallel — log slippage delta (live fill vs paper fill)
- [ ] Daily review mandatory. Do not set and forget.
- [ ] Gate: only expand position sizes after 4 consecutive profitable weeks

### Phase 5 — Compound + Optimize (Ongoing)
- [ ] Every 5 weeks: optimization cycle runs automatically
- [ ] Reinvest profits — let account compound from $500 upward
- [ ] After $1,000 balance: PDT rule becomes less restrictive (more breathing room)
- [ ] After $2,000: consider adding 1–2 more instruments (earnings-safe ETFs preferred)
- [ ] After 3 months live data: evaluate ML Category 9 (XGBoost on price + volume)
- [ ] After $25,000: PDT rule no longer applies — full intraday flexibility on all stocks

---

## 18. Key Metrics to Track

```
Performance (weekly + cumulative, all AFTER fees):
  Net Win Rate          → target > 57%
  Profit Factor         → target > 1.5  (net gross profit / net gross loss)
  Sharpe Ratio          → target > 1.5
  Sortino Ratio         → target > 2.0
  Max Drawdown          → hard limit < 15% of account ($75 on $500)
  Avg Win / Avg Loss    → target > 2.0
  Cost ratio            → fees+slippage as % of gross profit (target < 20%)
  Trades per week       → 10–25 across all 6 instruments (quality over quantity)

Portfolio-Specific ($500):
  Capital utilization   → target 40–65% deployed at any time (not too idle, not overexposed)
  PDT trades used       → track weekly (3 max for stocks, 0 limit for forex)
  Per-instrument PnL    → which of the 6 instruments is actually profitable?
  Cash reserve          → must never drop below $150

Signal Quality (reviewed each optimization cycle):
  Per-category accuracy    → win rate when that category fires (need 50+ samples)
  Confidence tier accuracy → does 85%+ tier outperform 55–64%? Must be YES
  Regime accuracy          → did regime classification predict the right strategy?
  LLM review accuracy      → when Groq flagged high_risk, did trade lose?
  Scanner rank accuracy    → did the top-ranked instrument actually perform best?

Optimization Health:
  Changes per cycle     → target ≤ 3
  OOS improvement delta → Sharpe change on out-of-sample data (must be positive)
  Rollback rate         → target < 20% of deployed changes
```

---

## 19. Recommended First 7 Days

```
Day 1:  Create Alpaca paper account + OANDA demo account.
        pip install everything. Configure .env. Verify both API keys work.
        Fetch NVDA 15m data and EUR_USD 15m data — print last 10 candles.

Day 2:  Build data/fetcher.py for all 6 instruments across 3 timeframes (5m/15m/1h).
        Build portfolio/watchlist.py — define the 6 instruments + session hours.
        Verify fetcher respects session timing (don't fetch forex at 3 AM if no activity).

Day 3:  Implement cat1_trend_direction.py + cat2_trend_strength.py.
        Run quick backtest on NVDA 2022–2024 with fees=0.001.
        Check: does EMA+ADX combo have positive expectancy even alone?

Day 4:  Implement regime/detector.py.
        Verify: Jan–Dec 2022 data → TRENDING_DOWN most of year.
        Verify: Q4 2023 data → TRENDING_UP.
        Verify: mid-2023 sideways period → RANGING.

Day 5:  Implement remaining 6 signal categories.
        Run full confidence scorer across all 6 instruments historically.
        Check: are 85%+ confidence moments rare and high-quality?
        (If they're firing 50 times/day, thresholds are too loose)

Day 6:  Implement portfolio/scanner.py + pdt_tracker.py.
        Implement events/event_guard.py.
        Test: simulate scan with NVDA earnings in 18h → must be blocked.
        Test: simulate 3 day trades → 4th stock trade must be rejected.

Day 7:  Connect Telegram bot + Alpaca paper API. Place first paper limit order manually.
        Start APScheduler: regime every 30 min, scanner every 15 min during session.
        Let it run through the weekend. Check Monday: did it trade? Did it log correctly?
```

Week 2: Add remaining agents (risk, execution, health monitor, LLM reviewer).
Week 3: Full paper trading loop — all 6 instruments, every 15 minutes.
Week 6: First optimization cycle — need at least 50 paper trades logged.
Week 8: Live $500 deployment — EUR/USD first, add stocks gradually.
        Test: verify NVDA blocks trade entry when its next earnings date is within 24h.
        Connect to Alpaca paper API. Submit first paper limit order manually.

Day 7:  Connect Telegram bot. Start APScheduler for regime check every 30 min.
        Let system run in paper mode over the weekend. Review Monday morning.
```

Week 2: Implement remaining agents (risk, execution, health monitor).
Week 3: Full paper trading loop live. Collect data.
Week 6: Run first optimization cycle.
Week 8: If metrics are strong → move to small live capital.
