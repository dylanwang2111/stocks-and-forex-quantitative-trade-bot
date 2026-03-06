"""
agents/pre_screen_agent.py
Daily pre-screen agent: expands the live universe from the full CANDIDATE_POOL.

Runs at 05:00 UTC daily (except Monday — weekly PortfolioAgent selection takes precedence).
Scores all 25+ instruments in CANDIDATE_POOL on 30 days of daily bars using a softer
gate (EMA9 > EMA21 only) and selects:
  - Top MAX_STOCKS stocks (correlation-aware greedy)
  - Top MAX_FOREX forex (EURUSD always force-included as anchor)

Calls set_active_universe() to swap the live universe atomically.
"""
from __future__ import annotations

import logging

import pandas as pd
import pandas_ta as ta

from data.fetcher import fetch_candles
from portfolio.watchlist import CANDIDATE_POOL, Instrument, set_active_universe

logger = logging.getLogger(__name__)

# EURUSD anchor — always included regardless of EMA gate
_EURUSD_ANCHOR = "EURUSD"


class PreScreenAgent:
    """
    Daily pre-screen: lighter than PortfolioAgent (30d data, softer EMA gate).

    Gate:  EMA9 > EMA21 (both stocks and forex — softer than weekly agent)
    Score: ADX>25, ADX>40, 20d return>2%, RSI[45,70], ATR/price[0.3%,6%]
    """

    MAX_STOCKS: int = 4
    MAX_FOREX: int  = 2
    MIN_BARS: int   = 20

    def screen(self) -> list[Instrument]:
        """
        Score all CANDIDATE_POOL instruments, select the best set,
        and update the active UNIVERSE via set_active_universe().

        Returns the selected instrument list, or [] if all fail (no change made).
        """
        logger.info(
            "PreScreenAgent.screen(): evaluating %d candidates",
            len(CANDIDATE_POOL),
        )

        scored_stocks: list[tuple[float, Instrument]] = []
        scored_forex:  list[tuple[float, Instrument]] = []

        for instrument in CANDIDATE_POOL:
            score = self._score_instrument(instrument)
            if score is None:
                logger.debug("  %s: skipped (gate failed or data unavailable)", instrument.symbol)
                continue
            logger.debug("  %s: score=%.1f", instrument.symbol, score)
            if instrument.asset_type == "stock":
                scored_stocks.append((score, instrument))
            else:
                scored_forex.append((score, instrument))

        scored_stocks.sort(key=lambda t: t[0], reverse=True)
        scored_forex.sort(key=lambda t: t[0], reverse=True)

        # Greedy correlation-aware stock selection
        selected_stocks: list[Instrument] = []
        selected_symbols: set[str] = set()

        for _score, inst in scored_stocks:
            if len(selected_stocks) >= self.MAX_STOCKS:
                break
            if any(corr in selected_symbols for corr in inst.correlated_with):
                logger.debug("  %s: skipped (correlated with already-selected)", inst.symbol)
                continue
            selected_stocks.append(inst)
            selected_symbols.add(inst.symbol)

        # Forex: force-include EURUSD anchor, then fill remaining slots by score
        eurusd_instrument: Instrument | None = None
        other_forex: list[Instrument] = []

        for _score, inst in scored_forex:
            if inst.symbol == _EURUSD_ANCHOR:
                eurusd_instrument = inst
            else:
                other_forex.append(inst)

        # If EURUSD didn't pass the gate, find it in CANDIDATE_POOL directly
        if eurusd_instrument is None:
            for inst in CANDIDATE_POOL:
                if inst.symbol == _EURUSD_ANCHOR:
                    eurusd_instrument = inst
                    logger.info(
                        "PreScreenAgent: EURUSD failed EMA gate — force-included as anchor"
                    )
                    break

        selected_forex: list[Instrument] = []
        if eurusd_instrument is not None:
            selected_forex.append(eurusd_instrument)

        # Fill remaining forex slots from scored candidates (skip EURUSD already added)
        for inst in other_forex:
            if len(selected_forex) >= self.MAX_FOREX:
                break
            selected_forex.append(inst)

        selected = selected_stocks + selected_forex

        if not selected:
            logger.warning(
                "PreScreenAgent: no instruments passed screening — keeping existing UNIVERSE"
            )
            return []

        logger.info(
            "PreScreenAgent selected %d instruments: stocks=[%s] forex=[%s]",
            len(selected),
            ", ".join(i.symbol for i in selected_stocks),
            ", ".join(i.symbol for i in selected_forex),
        )

        set_active_universe(selected)
        return selected

    # ------------------------------------------------------------------
    # Scoring (30d lookback, softer EMA gate)
    # ------------------------------------------------------------------

    def _score_instrument(self, instrument: Instrument) -> float | None:
        """
        Fetch 30 days of daily data, apply soft EMA gate (EMA9 > EMA21), then score.
        Returns None if gate fails or data is insufficient.
        """
        try:
            df = fetch_candles(instrument.symbol, "1d", period="30d", use_cache=False)
        except Exception as exc:
            logger.debug("  %s: fetch error — %s", instrument.symbol, exc)
            return None

        if df is None or len(df) < self.MIN_BARS:
            logger.debug(
                "  %s: insufficient data (%d bars)",
                instrument.symbol,
                len(df) if df is not None else 0,
            )
            return None

        close  = df["close"]
        volume = df.get("volume", pd.Series(dtype=float))

        ema9  = ta.ema(close, length=9)
        ema21 = ta.ema(close, length=21)
        rsi   = ta.rsi(close, length=14)
        adx_df = ta.adx(df["high"], df["low"], close, length=14)

        e9  = float(ema9.iloc[-1])  if ema9  is not None and not ema9.empty  else None
        e21 = float(ema21.iloc[-1]) if ema21 is not None and not ema21.empty else None
        rsi_val = float(rsi.iloc[-1]) if rsi is not None and not rsi.empty else None

        adx_val = None
        if adx_df is not None and not adx_df.empty:
            adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
            if adx_col:
                adx_val = float(adx_df[adx_col[0]].iloc[-1])

        # ── Soft gate: EMA9 > EMA21 (applies to both stocks and forex) ────
        if e9 is None or e21 is None:
            return None
        if not (e9 > e21):
            return None

        # ── Scoring ──────────────────────────────────────────────────────
        score = 0.0

        if adx_val is not None and adx_val > 25:
            score += 1.0
            if adx_val > 40:
                score += 1.0

        if len(close) >= 20:
            ret_20d = float(close.iloc[-1] / close.iloc[-20] - 1.0)
            if ret_20d > 0.02:
                score += 1.0

        if rsi_val is not None and 45 <= rsi_val <= 70:
            score += 1.0

        if instrument.asset_type == "stock" and not volume.empty:
            avg_vol = float(volume.rolling(20).mean().iloc[-1])
            if avg_vol > 500_000:
                score += 1.0

        if len(df) >= 14:
            atr_series = ta.atr(df["high"], df["low"], close, length=14)
            if atr_series is not None and not atr_series.empty:
                atr_val = float(atr_series.iloc[-1])
                atr_pct = atr_val / float(close.iloc[-1])
                if 0.003 <= atr_pct <= 0.06:
                    score += 1.0

        return score
