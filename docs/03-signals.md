# Signal System

The bot evaluates 8 independent signal categories per instrument every 15 minutes. Each category casts a vote of `+1` (bullish), `-1` (bearish), or `0` (neutral). Votes are weighted, summed, and normalized to a 0–100 confidence score.

---

## Overview

| Cat | Name | Timeframe | Max Vote | Indicator |
|-----|------|-----------|----------|-----------|
| 1 | Trend Direction | 15m | ±1 | EMA9/EMA21 crossover + MACD histogram |
| 2 | Trend Strength | 15m | ±1 | ADX(14) |
| 3 | Momentum | 1h | ±1 | MACD line vs signal line |
| 4 | Volatility Band | 1h | ±1 | Bollinger Band breakout |
| 5 | Volume | 5m or 1h | ±1 | OBV EMA crossover |
| 6 | Price Structure | 1h | ±1 | 5-bar price momentum (ROC) |
| 7 | Multi-Timeframe | 5m + 15m + 1h | **±2** | Consensus across all timeframes |
| 8 | Macro / News | Cached 15m | ±1 | LLM sentiment + macro risk level |

**Max raw score: 9** (cats 1–6 + cat8 = 7, cat7 = 2). Normalised to 0–100.

---

## Category Details

### Cat 1 — Trend Direction (`cat1_trend_direction.py`)
**Timeframe**: 15m
**Indicators**: EMA9, EMA21, MACD histogram

**Long vote (+1)** when:
- EMA9 > EMA21 (bullish crossover confirmed)
- MACD histogram > 0 (momentum aligns)

**Short vote (-1)** when:
- EMA9 < EMA21 (bearish crossover)
- MACD histogram < 0

**Neutral (0)**: Mixed signals (EMA bullish but MACD bearish, or vice versa).

---

### Cat 2 — Trend Strength (`cat2_trend_strength.py`)
**Timeframe**: 15m
**Indicator**: ADX(14)

**Long vote (+1)**: ADX > 25 AND price above EMA21 (trending up with strength)
**Short vote (-1)**: ADX > 25 AND price below EMA21 (trending down with strength)
**Neutral (0)**: ADX ≤ 25 (ranging, no clear trend)

ADX measures trend strength irrespective of direction. Values above 25 indicate a trending market; below 25 is ranging.

---

### Cat 3 — Momentum (`cat3_momentum.py`)
**Timeframe**: 1h
**Indicator**: MACD line vs signal line

**Long vote (+1)**: MACD line crosses above signal line (bullish momentum)
**Short vote (-1)**: MACD line crosses below signal line (bearish momentum)
**Neutral (0)**: No recent crossover or lines flat

---

### Cat 4 — Volatility Band (`cat4_volatility_band.py`)
**Timeframe**: 1h
**Indicator**: Bollinger Bands (20, 2σ)

**Long vote (+1)**: Price closes above upper band (bullish breakout)
**Short vote (-1)**: Price closes below lower band (bearish breakdown)
**Neutral (0)**: Price inside bands (consolidation)

Designed to be independent of trend — captures volatility expansions.

---

### Cat 5 — Volume (`cat5_volume.py`)
**Timeframe**: 5m (primary), 1h (fallback)
**Indicator**: OBV (On-Balance Volume) EMA crossover

**Long vote (+1)**: OBV EMA short crosses above OBV EMA long (buying pressure)
**Short vote (-1)**: OBV EMA short crosses below OBV EMA long (selling pressure)
**Neutral (0)**: No crossover or insufficient data

OBV accumulates volume on up-bars and subtracts on down-bars, revealing institutional flow.

---

### Cat 6 — Price Structure (`cat6_price_structure.py`)
**Timeframe**: 1h
**Indicator**: 5-bar Rate of Change (ROC)

**Long vote (+1)**: ROC > +0.5% over last 5 bars (upward price structure)
**Short vote (-1)**: ROC < -0.5% over last 5 bars (downward structure)
**Neutral (0)**: ROC within ±0.5% (flat structure)

Captures short-term price momentum independently of oscillators.

---

### Cat 7 — Multi-Timeframe Alignment (`cat7_multi_timeframe.py`)
**Timeframes**: 5m + 15m + 1h
**Vote weight**: **±2** (double weight)

Checks whether the trend direction is consistent across all three timeframes. Uses EMA9/EMA21 alignment on each.

**Long (+2)**: All three timeframes show EMA9 > EMA21 (full bullish alignment)
**Short (-2)**: All three timeframes show EMA9 < EMA21 (full bearish alignment)
**Partial (+1 or -1)**: 2 of 3 timeframes agree
**Neutral (0)**: Mixed — no clear multi-TF consensus

This is the highest-weighted category because multi-timeframe agreement is the strongest entry filter.

---

### Cat 8 — Macro / News (`cat8_macro_news.py`)
**Timeframe**: Cached — refreshed every 15 minutes
**Source**: Yahoo Finance RSS headlines + LLM analysis (Groq primary, Gemini fallback)

**How it works**:
1. Fetches recent headlines for the instrument's sector from Yahoo Finance RSS
2. Sends headlines to LLM with a structured prompt
3. LLM returns: `vote` (±1 or 0) + `risk_level` (LOW / MEDIUM / HIGH) + `reason`

**Vote**:
- `+1`: Positive macro/news environment (e.g. rate cuts, strong earnings, geopolitical calm)
- `-1`: Negative macro/news (e.g. rate hikes, recession fears, war/conflict)
- `0`: Neutral or ambiguous

**Risk Level → Macro Multiplier** (applied to final position size):

| Risk Level | Position Multiplier |
|------------|---------------------|
| LOW | 1.0× (no change) |
| MEDIUM | 0.75× |
| HIGH | 0.50× |

This scales down size for FOMC, geopolitical shocks, or high-uncertainty events — without blocking the trade entirely.

**Cache TTL**: 15 minutes. Refreshed each scan cycle.

**Fallback**: If both LLM providers fail (rate limit, no key), returns neutral vote (0) and LOW risk level. The bot continues without macro intelligence.

---

## Regime Multipliers

Before aggregating votes, each category's contribution is scaled by a regime multiplier based on the current market regime (detected from 1h data):

| Category | TRENDING | RANGING | HIGH_VOL | LOW_VOL |
|----------|----------|---------|----------|---------|
| Cat 1 (Trend) | 1.2× | 0.7× | 0.7× | 0.9× |
| Cat 2 (ADX) | 1.2× | 0.7× | 0.7× | 0.9× |
| Cat 3 (MACD) | 1.0× | 1.2× | 0.7× | 1.1× |
| Cat 4 (BB) | 0.8× | 1.3× | 0.7× | 1.2× |
| Cat 5 (Volume) | 1.1× | 0.9× | 0.7× | 0.8× |
| Cat 6 (ROC) | 1.0× | 1.3× | 0.7× | 1.2× |
| Cat 7 (MTF) | 1.2× | 0.7× | 0.7× | 0.9× |
| Cat 8 (Macro) | 1.0× | 1.0× | 1.0× | 1.0× |

In a trending market, trend-following categories (1, 2, 7) are boosted. In a ranging market, mean-reversion categories (4, 6) and oscillators (3) are boosted. High volatility dampens everything.

---

## Scoring Pipeline

```
Raw votes (±1 or ±2 for cat7)
        │
        ▼
Regime multiplier applied per category
        │
        ▼
Separate bull / bear accumulators
  bull_raw = Σ(positive contributions)
  bear_raw = Σ(negative contributions)
        │
        ▼
Normalize to 0–100
  bull_score = (bull_raw / MAX_RAW) × 100
  bear_score = (bear_raw / MAX_RAW) × 100
  MAX_RAW = 9
        │
        ▼
Direction: dominant = max(bull_score, bear_score)
  Lead gap check: |bull - bear| ≥ MIN_LEAD_GAP (10 pts)
  → If gap < 10: direction = "neutral", tier = NO_TRADE
        │
        ▼
Position tier from dominant_score
```

---

## Position Tiers

| Score | Tier | Size Fraction | Action |
|-------|------|---------------|--------|
| < 45 | NO_TRADE | 0% | Skip |
| 45–54 | WATCH | 0% | Monitor only |
| 55–69 | SMALL | 25% of max | Enter (small) |
| 70–79 | MEDIUM | 50% of max | Enter (medium) |
| 80–89 | LARGE | 75% of max | Enter (large) |
| ≥ 90 | FULL | 100% of max | Enter (full) |

---

## EMA50 Entry Filter

Beyond the confidence score, the scanner enforces a short-term trend filter:

- **Longs**: Blocked if current price < EMA50(1h) — prevents buying into a short-term downtrend
- **Shorts**: Blocked if current price > EMA50(1h) — prevents shorting into an uptrend

This is applied after scoring, as a hard gate before risk sizing.

---

## ConfidenceResult Object

```python
@dataclass
class ConfidenceResult:
    bull_score: float         # 0–100
    bear_score: float         # 0–100
    direction: str            # "long" | "short" | "neutral"
    dominant_score: float     # max(bull_score, bear_score)
    position_tier: PositionTier
    breakdown: dict           # per-category: vote, multiplier, contribution
    macro_multiplier: float   # 0.5 | 0.75 | 1.0 (from Cat8 risk_level)
```

The `breakdown` dict is stored in the `signal_breakdown` column of the `Trade` table for post-trade analysis.
