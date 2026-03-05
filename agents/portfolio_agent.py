"""
agents/portfolio_agent.py
Weekly portfolio selection agent.

Evaluates all instruments in CANDIDATE_POOL on 60 days of daily data,
applies a long-bias gate and scores each on trend strength, momentum,
liquidity, and volatility. Selects the top MAX_STOCKS stocks and
MAX_FOREX forex pairs, then calls set_active_universe() to update
the live UNIVERSE that the Scanner trades.

Selection runs on startup and every Monday at 00:00 UTC.
"""
from __future__ import annotations

import logging
import math

import pandas as pd

import pandas_ta as ta

from data.fetcher import fetch_candles
from portfolio.watchlist import CANDIDATE_POOL, Instrument, set_active_universe

logger = logging.getLogger(__name__)


class PortfolioAgent:
    """
    Selects the best trending instruments from CANDIDATE_POOL.

    Long-bias gate (hard filter before scoring):
      - Stocks : EMA9 > EMA21 AND EMA21 > EMA50 required
      - Forex  : EMA9 > EMA21 required (softer — forex can reverse quickly)

    Scoring (applied only to candidates that pass the gate):
      +1  ADX(14) > 25          — instrument is trending
      +1  ADX(14) > 40          — strong trend bonus
      +1  20-day return > 2%    — real upward momentum
      +1  RSI(14) in [45, 70]   — healthy bullish range
      +1  avg_volume_20d > 500k — stocks only: adequate liquidity
      +1  ATR/price in [0.3%, 6%] — tradeable volatility

    Max possible score: 6 for stocks, 5 for forex.

    Correlation-aware greedy selection: after sorting stocks by score,
    iterate top-to-bottom and skip a candidate if any of its
    `correlated_with` symbols are already in the selected set.
    """

    MAX_STOCKS: int = 6
    MAX_FOREX: int  = 2
    MIN_BARS: int   = 20   # minimum daily bars required to score

    def select(self) -> list[Instrument]:
        """
        Score all CANDIDATE_POOL instruments, select the best ones,
        and update the active UNIVERSE via set_active_universe().

        Returns the selected instrument list (may be shorter than
        MAX_STOCKS + MAX_FOREX if few candidates pass the long-bias gate).
        Falls back to keeping the current UNIVERSE unchanged on total failure.
        """
        logger.info(
            "PortfolioAgent.select(): evaluating %d candidates",
            len(CANDIDATE_POOL),
        )

        scored_stocks: list[tuple[float, Instrument]] = []
        scored_forex:  list[tuple[float, Instrument]] = []

        for instrument in CANDIDATE_POOL:
            score = self._score_instrument(instrument)
            if score is None:
                logger.debug(
                    "  %s: skipped (gate failed or data unavailable)", instrument.symbol
                )
                continue
            logger.debug("  %s: score=%.1f", instrument.symbol, score)
            if instrument.asset_type == "stock":
                scored_stocks.append((score, instrument))
            else:
                scored_forex.append((score, instrument))

        # Sort descending by score
        scored_stocks.sort(key=lambda t: t[0], reverse=True)
        scored_forex.sort(key=lambda t: t[0], reverse=True)

        # Greedy correlation-aware stock selection
        selected_stocks: list[Instrument] = []
        selected_symbols: set[str] = set()

        for _score, inst in scored_stocks:
            if len(selected_stocks) >= self.MAX_STOCKS:
                break
            # Skip if correlated with any already-selected stock
            if any(corr in selected_symbols for corr in inst.correlated_with):
                logger.debug(
                    "  %s: skipped (correlated with already-selected symbol)",
                    inst.symbol,
                )
                continue
            selected_stocks.append(inst)
            selected_symbols.add(inst.symbol)

        # Forex: simple top-N (correlation guard handles pairs during live trading)
        selected_forex = [inst for _score, inst in scored_forex[: self.MAX_FOREX]]

        selected = selected_stocks + selected_forex

        if not selected:
            logger.warning(
                "PortfolioAgent: no instruments passed selection — keeping existing UNIVERSE"
            )
            return []

        logger.info(
            "PortfolioAgent selected %d instruments: stocks=[%s] forex=[%s]",
            len(selected),
            ", ".join(i.symbol for i in selected_stocks),
            ", ".join(i.symbol for i in selected_forex),
        )

        set_active_universe(selected)
        return selected

    # ------------------------------------------------------------------
    # Scoring
    # ------------------------------------------------------------------

    def _score_instrument(self, instrument: Instrument) -> float | None:
        """
        Fetch 60 days of daily data, apply long-bias gate, then score.
        Returns None if gate fails or data is insufficient.
        """
        try:
            df = fetch_candles(instrument.symbol, "1d", period="60d", use_cache=False)
        except Exception as exc:
            logger.debug("  %s: fetch error — %s", instrument.symbol, exc)
            return None

        if df is None or len(df) < self.MIN_BARS:
            logger.debug(
                "  %s: insufficient data (%d bars)", instrument.symbol, len(df) if df is not None else 0
            )
            return None

        close  = df["close"]
        volume = df.get("volume", pd.Series(dtype=float))

        # ── Indicators ────────────────────────────────────────────────────────
        ema9  = ta.ema(close, length=9)
        ema21 = ta.ema(close, length=21)
        ema50 = ta.ema(close, length=50)
        rsi   = ta.rsi(close, length=14)
        adx_df = ta.adx(df["high"], df["low"], close, length=14)

        # Latest values
        e9  = float(ema9.iloc[-1])  if ema9  is not None and not ema9.empty  else None
        e21 = float(ema21.iloc[-1]) if ema21 is not None and not ema21.empty else None
        e50 = float(ema50.iloc[-1]) if ema50 is not None and not ema50.empty else None
        rsi_val = float(rsi.iloc[-1]) if rsi is not None and not rsi.empty else None

        adx_val = None
        if adx_df is not None and not adx_df.empty:
            adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
            if adx_col:
                adx_val = float(adx_df[adx_col[0]].iloc[-1])

        # ── Long-bias gate ────────────────────────────────────────────────────
        if instrument.asset_type == "stock":
            # Require full bullish EMA stack
            if e9 is None or e21 is None or e50 is None:
                return None
            if not (e9 > e21 > e50):
                return None
        else:
            # Forex: softer gate — just EMA9 > EMA21
            if e9 is None or e21 is None:
                return None
            if not (e9 > e21):
                return None

        # ── Scoring ──────────────────────────────────────────────────────────
        score = 0.0

        # ADX > 25: trending
        if adx_val is not None and adx_val > 25:
            score += 1.0
            # ADX > 40: strong trend bonus
            if adx_val > 40:
                score += 1.0

        # 20-day return > 2%
        if len(close) >= 20:
            ret_20d = float(close.iloc[-1] / close.iloc[-20] - 1.0)
            if ret_20d > 0.02:
                score += 1.0

        # RSI in [45, 70]: healthy bullish range
        if rsi_val is not None and 45 <= rsi_val <= 70:
            score += 1.0

        # Stocks only: avg_volume_20d > 500k
        if instrument.asset_type == "stock" and not volume.empty:
            avg_vol = float(volume.rolling(20).mean().iloc[-1])
            if avg_vol > 500_000:
                score += 1.0

        # ATR/price in [0.3%, 6%]: tradeable volatility
        if len(df) >= 14:
            atr_series = ta.atr(df["high"], df["low"], close, length=14)
            if atr_series is not None and not atr_series.empty:
                atr_val = float(atr_series.iloc[-1])
                atr_pct = atr_val / float(close.iloc[-1])
                if 0.003 <= atr_pct <= 0.06:
                    score += 1.0

        return score


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def test_portfolio_agent() -> None:
    """
    Smoke-test PortfolioAgent.select() end-to-end.

    Verifies:
    1. select() runs without error
    2. Returns between 1 and (MAX_STOCKS + MAX_FOREX) instruments
    3. All selected instruments have asset_type in ["stock", "forex"]
    4. set_active_universe() was called (UNIVERSE length changed)
    5. No two selected stocks are correlated with each other
    """
    from portfolio.watchlist import UNIVERSE
    import logging as _log
    _log.basicConfig(level=_log.INFO)

    print("=== test_portfolio_agent ===")
    agent = PortfolioAgent()
    selected = agent.select()

    if not selected:
        print("WARNING: no instruments selected (market data may be unavailable)")
        print("test_portfolio_agent: SKIPPED (no data)")
        return

    # Test 1: valid count
    max_possible = PortfolioAgent.MAX_STOCKS + PortfolioAgent.MAX_FOREX
    assert 1 <= len(selected) <= max_possible, (
        f"Expected 1–{max_possible} instruments, got {len(selected)}"
    )
    print(f"Test 1 PASS: selected {len(selected)} instruments")

    # Test 2: valid asset types
    for inst in selected:
        assert inst.asset_type in ("stock", "forex"), (
            f"Unexpected asset_type '{inst.asset_type}' for {inst.symbol}"
        )
    print("Test 2 PASS: all asset types valid")

    # Test 3: UNIVERSE was updated
    assert len(UNIVERSE) == len(selected), (
        f"UNIVERSE length {len(UNIVERSE)} != selected {len(selected)}"
    )
    print(f"Test 3 PASS: UNIVERSE updated to {len(UNIVERSE)} instruments")

    # Test 4: no two selected stocks are correlated with each other
    stock_symbols = {i.symbol for i in selected if i.asset_type == "stock"}
    for inst in selected:
        if inst.asset_type != "stock":
            continue
        for corr_sym in inst.correlated_with:
            assert corr_sym not in stock_symbols or corr_sym == inst.symbol, (
                f"Correlation violation: {inst.symbol} and {corr_sym} both selected"
            )
    print("Test 4 PASS: no correlated stocks selected together")

    print("\nSelected universe:")
    for inst in selected:
        print(f"  {inst.symbol:<8} [{inst.broker:<5}] {inst.asset_type}")

    print("\ntest_portfolio_agent: ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    test_portfolio_agent()
