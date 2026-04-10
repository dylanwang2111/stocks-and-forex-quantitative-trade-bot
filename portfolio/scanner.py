"""
portfolio/scanner.py
Scans all instruments in UNIVERSE every cycle and ranks them by confidence.

Flow per symbol:
  1. Skip if already holding this symbol (open position)
  2. Check market hours (skip if outside active window)
  3. Fetch OHLCV data for "5m", "15m", "1h"
  4. Detect market regime from 1h candles
  5. Run signal engine across all timeframes
  6. Score confidence (regime-adjusted)
  7. Log signal to DB
  8. Check EventGuard (earnings / FOMC blackouts)
  9. Check CorrelationGuard (open position correlation)
 10. Check PDTTracker soft gate (stocks only)

Returns ALL ScanResult objects (blocked + unblocked).
Caller decides what to trade via top_opportunity().
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd
import pandas_ta as ta

from agents.confidence_scorer import ConfidenceResult, ConfidenceScorer
from agents.signal_engine import SignalBundle, SignalEngine
from agents.portfolio_agent import _SECTOR, _fetch_macro_context_shared, MacroContext
from data.fetcher import fetch_candles
from database.models import SignalLog, get_session
from events.event_guard import EventGuard
from config.settings import settings
from portfolio.pdt_tracker import PDTTracker
from portfolio.state import PortfolioStateManager
from portfolio.watchlist import get_universe_snapshot, Instrument
from regime.detector import RegimeContext, RegimeDetector
from resilience.correlation_guard import CorrelationGuard

# How long (seconds) to cache the macro context between refreshes.
# 4 h matches PortfolioAgent's selection cadence — USO EMA trend doesn't change minute-to-minute.
_MACRO_CACHE_TTL = 4 * 3600

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ScanResult
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """Holds the full evaluation result for one instrument in one scan cycle."""

    symbol: str
    confidence_result: ConfidenceResult
    regime: RegimeContext
    bundle: SignalBundle
    blocked: bool
    block_reason: str
    scan_time: datetime = field(default_factory=datetime.utcnow)
    atr: Optional[float] = None          # ATR(14) from 1h data — for adaptive stops
    ema50: Optional[float] = None        # EMA50 from 1h data — short-term trend filter
    current_price: Optional[float] = None  # cached close price from 1h data
    volume_ratio: Optional[float] = None   # current vol / 20-bar avg vol — stock entry gate
    ema50_1d: Optional[float] = None       # EMA50 from 1d data — daily trend alignment filter
    adx: Optional[float] = None            # ADX(14) from 1h — trend quality gate
    asset_type: str = "stock"              # "stock" | "crypto" — drives ADX threshold


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class Scanner:
    """
    Scans all 6 instruments in UNIVERSE and ranks them by dominant_score.

    Parameters
    ----------
    state_manager:
        Live portfolio state; used to skip symbols already held and to feed
        the CorrelationGuard with current open positions.
    pdt_tracker:
        Optional PDT rule enforcer.  Created internally if not supplied.
    event_guard:
        Optional event/earnings blackout guard.  Created internally if not supplied.
    correlation_guard:
        Optional correlation guard.  Created internally (with empty open_symbols)
        if not supplied — the guard is refreshed before each symbol check.
    database_url:
        SQLAlchemy URL for SignalLog persistence.  Falls back to the URL used
        by state_manager's internal DB if None (passed straight to get_session).
    """

    def __init__(
        self,
        state_manager: PortfolioStateManager,
        pdt_tracker: Optional[PDTTracker] = None,
        event_guard: Optional[EventGuard] = None,
        correlation_guard: Optional[CorrelationGuard] = None,
        database_url: Optional[str] = None,
    ) -> None:
        self._state = state_manager
        self._pdt = pdt_tracker or PDTTracker()
        self._event_guard = event_guard or EventGuard()
        self._corr_guard = correlation_guard or CorrelationGuard()
        self._database_url = database_url  # None → get_session default

        self._engine = SignalEngine()
        self._scorer = ConfidenceScorer()
        self._regime_detector = RegimeDetector()

        # Macro context cache — refreshed at most every _MACRO_CACHE_TTL seconds
        self._macro_ctx: Optional[MacroContext] = None
        self._macro_fetched_at: Optional[datetime] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan_all(self, skip_brokers: set[str] | None = None) -> list[ScanResult]:
        """
        Scan every instrument in UNIVERSE.

        Parameters
        ----------
        skip_brokers:
            Optional set of broker names (e.g. {"ibkr"}) whose instruments
            should be skipped this cycle. Used for degraded-mode operation
            when one broker is down but the other is healthy.

        Returns a list of ScanResult objects sorted by
        confidence_result.dominant_score DESC (best first).
        Blocked results are included so the caller has full visibility.
        Never raises — individual symbol failures are caught and logged.
        """
        skip_brokers = skip_brokers or set()

        # Snapshot current open positions once so we're consistent
        open_positions = self._state.all_positions()
        open_symbols: set[str] = {p.symbol for p in open_positions}
        partial_exit_symbols = {p.symbol for p in open_positions if p.partial_exit_done}

        # Refresh CorrelationGuard with live portfolio symbols
        self._corr_guard.update_open_symbols(list(open_symbols), partial_exit_symbols)

        results: list[ScanResult] = []

        for instrument in get_universe_snapshot():
            if instrument.broker in skip_brokers:
                logger.debug(
                    "scan_all: skipping %s — broker '%s' is down",
                    instrument.symbol,
                    instrument.broker,
                )
                continue
            try:
                result = self._scan_symbol(instrument, open_symbols)
                if result is not None:
                    results.append(result)
            except Exception:
                logger.exception(
                    "scan_all: unexpected error scanning %s — skipping",
                    instrument.symbol,
                )

        # Sort best opportunity first
        results.sort(key=lambda r: r.confidence_result.dominant_score, reverse=True)

        logger.info(
            "scan_all complete: %d result(s) (%d unblocked)",
            len(results),
            sum(1 for r in results if not r.blocked),
        )
        return results

    def top_opportunity(self, results: list[ScanResult]) -> Optional[ScanResult]:
        """
        Return the single best unblocked, tradeable opportunity.
        Applies EMA50(1h) trend filter: long only above EMA50, short only below.
        """
        candidates = self._filter_tradeable(results)
        if not candidates:
            logger.info("top_opportunity: no tradeable unblocked results")
            return None
        best = candidates[0]
        logger.info(
            "top_opportunity: %s | direction=%s | score=%.1f | tier=%s",
            best.symbol,
            best.confidence_result.direction,
            best.confidence_result.dominant_score,
            best.confidence_result.position_tier.value,
        )
        return best

    def tradeable_opportunities(self, results: list[ScanResult]) -> list[ScanResult]:
        """
        Return ALL tradeable, unblocked opportunities sorted by score DESC.
        Allows the orchestrator to fill multiple position slots in one cycle.
        """
        return self._filter_tradeable(results)

    def _filter_tradeable(self, results: list[ScanResult]) -> list[ScanResult]:
        """
        Shared filter: unblocked + tradeable tier + EMA50(1h) trend confirmation.
        Returns list sorted by dominant_score DESC.
        """
        min_score = settings.bot.min_confidence
        candidates: list[ScanResult] = []
        for r in results:
            if r.blocked or not r.confidence_result.tradeable():
                continue
            if r.confidence_result.dominant_score < min_score:
                logger.debug(
                    "_filter_tradeable: %s skipped — score %.1f < min_confidence %.1f",
                    r.symbol, r.confidence_result.dominant_score, min_score,
                )
                continue
            direction = r.confidence_result.direction
            # EMA50(1h) trend filter: long only above EMA50, short only below
            if r.ema50 is not None and r.current_price is not None:
                if direction == "long" and r.current_price < r.ema50:
                    logger.debug(
                        "_filter_tradeable: %s skipped — price %.4f below EMA50(1h) %.4f",
                        r.symbol, r.current_price, r.ema50,
                    )
                    continue
                if direction == "short" and r.current_price > r.ema50:
                    logger.debug(
                        "_filter_tradeable: %s skipped — price %.4f above EMA50(1h) %.4f",
                        r.symbol, r.current_price, r.ema50,
                    )
                    continue
            # EMA50(1d) daily trend alignment: long only above, short only below
            if r.ema50_1d is not None and r.current_price is not None:
                if direction == "long" and r.current_price < r.ema50_1d:
                    logger.debug(
                        "_filter_tradeable: %s skipped — price %.4f below EMA50(1d) %.4f",
                        r.symbol, r.current_price, r.ema50_1d,
                    )
                    continue
                if direction == "short" and r.current_price > r.ema50_1d:
                    logger.debug(
                        "_filter_tradeable: %s skipped — price %.4f above EMA50(1d) %.4f",
                        r.symbol, r.current_price, r.ema50_1d,
                    )
                    continue
            # Volume confirmation for stocks: require above-average volume on entry
            if r.volume_ratio is not None and r.volume_ratio < 0.5:
                logger.debug(
                    "_filter_tradeable: %s skipped — volume_ratio=%.2f below 0.5",
                    r.symbol, r.volume_ratio,
                )
                continue
            # ADX quality gate: require directional trend before entry (crypto>25, stocks>20)
            if r.adx is not None:
                adx_min = 25.0 if r.asset_type == "crypto" else 20.0
                if r.adx < adx_min:
                    logger.debug(
                        "_filter_tradeable: %s skipped — ADX=%.1f < %.1f (%s threshold)",
                        r.symbol, r.adx, adx_min, r.asset_type,
                    )
                    continue
            # Macro oil gate: block energy stock longs when oil is in downtrend
            sector = _SECTOR.get(r.symbol, "")
            if sector == "energy":
                macro = self._get_macro_context()
                if macro is not None:
                    if direction == "long" and not macro.oil_uptrend:
                        logger.debug(
                            "_filter_tradeable: %s skipped — energy long blocked, oil downtrend",
                            r.symbol,
                        )
                        continue
                    if direction == "short" and macro.oil_uptrend:
                        logger.debug(
                            "_filter_tradeable: %s skipped — energy short blocked, oil uptrend",
                            r.symbol,
                        )
                        continue
            candidates.append(r)
        candidates.sort(key=lambda r: r.confidence_result.dominant_score, reverse=True)
        return candidates

    def _get_macro_context(self) -> Optional[MacroContext]:
        """
        Return a cached MacroContext, refreshing at most every _MACRO_CACHE_TTL seconds.
        Returns None (neutral / allow all) on fetch failure so gates fail open.
        """
        now = datetime.utcnow()
        stale = (
            self._macro_ctx is None
            or self._macro_fetched_at is None
            or (now - self._macro_fetched_at).total_seconds() > _MACRO_CACHE_TTL
        )
        if stale:
            try:
                self._macro_ctx = _fetch_macro_context_shared()
                self._macro_fetched_at = now
                logger.info(
                    "scanner: macro context refreshed — oil_uptrend=%s gold_uptrend=%s vix=%.1f",
                    self._macro_ctx.oil_uptrend,
                    self._macro_ctx.gold_uptrend,
                    self._macro_ctx.vix,
                )
            except Exception:
                logger.warning("scanner: macro context fetch failed — using stale/neutral", exc_info=True)
        return self._macro_ctx

    # ------------------------------------------------------------------
    # Internal per-symbol logic
    # ------------------------------------------------------------------

    def _scan_symbol(
        self,
        instrument: Instrument,
        open_symbols: set[str],
    ) -> Optional[ScanResult]:
        """
        Evaluate a single instrument.  Returns None if the symbol is skipped
        before signal evaluation (already held, market closed).
        """
        symbol = instrument.symbol

        # Step 1: Skip if we already hold this symbol
        if symbol in open_symbols:
            logger.debug("scan: skipping %s — open position exists", symbol)
            return None

        # Step 2: Market-hours check (global weekend guard built in)
        if not self._is_market_open(instrument):
            logger.debug("scan: skipping %s — outside market hours", symbol)
            return None

        # Step 3: Fetch multi-timeframe OHLCV data
        dfs: dict[str, pd.DataFrame] = {}
        for tf in ("5m", "15m", "1h"):
            try:
                df = fetch_candles(symbol, tf)
                dfs[tf] = df
            except Exception:
                logger.warning(
                    "scan: failed to fetch %s [%s] — skipping symbol", symbol, tf
                )
                return None  # cannot proceed without data

        # Fetch 1d data separately (optional — fail open if unavailable)
        try:
            dfs["1d"] = fetch_candles(symbol, "1d")
        except Exception:
            logger.debug("scan: failed to fetch %s [1d] — proceeding without", symbol)

        # Validate we actually got required data (5m, 15m, 1h); 1d is optional
        required_tfs = ("5m", "15m", "1h")
        if not dfs or any(dfs.get(tf, pd.DataFrame()).empty for tf in required_tfs):
            logger.warning("scan: empty data for %s — skipping", symbol)
            return None
        # Remove 1d from dfs if it's empty (fail-open: don't skip symbol for missing daily data)
        if "1d" in dfs and dfs["1d"].empty:
            del dfs["1d"]

        # Step 4: Regime detection on 1h data
        try:
            regime: RegimeContext = self._regime_detector.detect(dfs["1h"])
        except Exception:
            logger.warning(
                "scan: regime detection failed for %s — skipping", symbol, exc_info=True
            )
            return None

        # Compute ATR and EMA50 from 1h data for downstream use
        atr_val: Optional[float] = None
        ema50_val: Optional[float] = None
        current_price_val: Optional[float] = None
        try:
            df_1h = dfs["1h"]
            close_1h = df_1h["close"]
            current_price_val = float(close_1h.iloc[-1])
            if len(df_1h) >= 14:
                atr_s = ta.atr(df_1h["high"], df_1h["low"], close_1h, length=14)
                if atr_s is not None and not atr_s.empty and pd.notna(atr_s.iloc[-1]):
                    atr_val = float(atr_s.iloc[-1])
            if len(df_1h) >= 30:
                ema50_s = ta.ema(close_1h, length=50)
                if ema50_s is not None and not ema50_s.empty and pd.notna(ema50_s.iloc[-1]):
                    ema50_val = float(ema50_s.iloc[-1])
        except Exception:
            logger.debug("scan: ATR/EMA50 fetch failed for %s — proceeding without", symbol, exc_info=True)

        # Volume ratio (stocks only): current vol / 20-bar avg — for entry gate
        volume_ratio_val: Optional[float] = None
        if instrument.asset_type == "stock":
            try:
                df_1h = dfs.get("1h")
                if df_1h is not None and "volume" in df_1h.columns and len(df_1h) >= 20:
                    vol_ma = float(df_1h["volume"].rolling(20).mean().iloc[-1])
                    if vol_ma > 0:
                        volume_ratio_val = float(df_1h["volume"].iloc[-1]) / vol_ma
            except Exception:
                logger.debug("scan: volume_ratio failed for %s", symbol)

        # EMA50(1d): daily trend alignment — fails open if 1d data unavailable
        ema50_1d_val: Optional[float] = None
        try:
            df_1d = dfs.get("1d")
            if df_1d is not None and len(df_1d) >= 30:
                ema50_1d_s = ta.ema(df_1d["close"], length=50)
                if ema50_1d_s is not None and not ema50_1d_s.empty and pd.notna(ema50_1d_s.iloc[-1]):
                    ema50_1d_val = float(ema50_1d_s.iloc[-1])
        except Exception:
            logger.debug("scan: EMA50(1d) failed for %s", symbol)

        # ADX(14) from 1h: trend quality gate — crypto>25, stocks>20
        adx_val: Optional[float] = None
        try:
            df_1h = dfs["1h"]
            if len(df_1h) >= 14:
                adx_df = ta.adx(
                    df_1h["high"].astype(np.float64),
                    df_1h["low"].astype(np.float64),
                    df_1h["close"].astype(np.float64),
                    length=14,
                )
                if adx_df is not None and not adx_df.empty:
                    adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
                    if adx_col and pd.notna(adx_df[adx_col[0]].iloc[-1]):
                        adx_val = float(adx_df[adx_col[0]].iloc[-1])
        except Exception:
            logger.debug("scan: ADX computation failed for %s", symbol)

        # Step 5: Signal engine
        try:
            bundle: SignalBundle = self._engine.evaluate(symbol, dfs)
        except Exception:
            logger.warning(
                "scan: signal engine failed for %s — skipping", symbol, exc_info=True
            )
            return None

        # Step 6: Confidence scoring
        try:
            confidence: ConfidenceResult = self._scorer.score(bundle, regime)
        except Exception:
            logger.warning(
                "scan: confidence scoring failed for %s — skipping", symbol, exc_info=True
            )
            return None

        # Step 6b: Log confidence score for visibility
        logger.info(
            "scan: %s | dir=%-5s bull=%4.1f bear=%4.1f score=%4.1f tier=%s",
            symbol,
            confidence.direction,
            confidence.bull_score,
            confidence.bear_score,
            confidence.dominant_score,
            confidence.position_tier.value,
        )

        # Step 7: Persist signal to DB
        try:
            self._log_signal(symbol, bundle, confidence, regime)
        except Exception:
            # DB logging failure must not abort the scan
            logger.warning("scan: DB signal log failed for %s", symbol, exc_info=True)

        # Step 8: EventGuard — earnings / FOMC blackouts
        blocked = False
        block_reason = ""
        try:
            event_blocked, event_reason = self._event_guard.is_blocked(
                symbol, instrument.asset_type
            )
            if event_blocked:
                blocked = True
                block_reason = event_reason
                logger.info("scan: %s blocked by EventGuard — %s", symbol, event_reason)
        except Exception:
            logger.warning(
                "scan: EventGuard check failed for %s — treating as unblocked",
                symbol,
                exc_info=True,
            )

        # Step 9: CorrelationGuard
        if not blocked:
            try:
                corr_allowed, corr_reason = self._corr_guard.is_allowed(symbol)
                if not corr_allowed:
                    blocked = True
                    block_reason = corr_reason
                    logger.info(
                        "scan: %s blocked by CorrelationGuard — %s", symbol, corr_reason
                    )
            except Exception:
                logger.warning(
                    "scan: CorrelationGuard check failed for %s — treating as allowed",
                    symbol,
                    exc_info=True,
                )

        # Step 10: PDT soft gate (stocks only)
        if not blocked and instrument.asset_type == "stock":
            try:
                if not self._pdt.can_day_trade():
                    blocked = True
                    block_reason = (
                        "PDT limit reached: 3 day trades used in the past 5 business days"
                    )
                    logger.info("scan: %s blocked by PDTTracker — PDT limit reached", symbol)
            except Exception:
                logger.warning(
                    "scan: PDTTracker check failed for %s — treating as allowed",
                    symbol,
                    exc_info=True,
                )

        return ScanResult(
            symbol=symbol,
            confidence_result=confidence,
            regime=regime,
            bundle=bundle,
            blocked=blocked,
            block_reason=block_reason,
            atr=atr_val,
            ema50=ema50_val,
            current_price=current_price_val,
            volume_ratio=volume_ratio_val,
            ema50_1d=ema50_1d_val,
            adx=adx_val,
            asset_type=instrument.asset_type,
        )

    # ------------------------------------------------------------------
    # Market hours
    # ------------------------------------------------------------------

    def any_market_open(self) -> bool:
        """Return True if at least one instrument in the current universe has an open market."""
        for instrument in get_universe_snapshot():
            if self._is_market_open(instrument):
                return True
        return False

    def _is_market_open(self, instrument: Instrument) -> bool:
        """
        Return True if the market is currently open for *instrument*.

        Rules:
        - All instruments: return False on Saturday or Sunday.
        - Parse active_hours_utc (e.g. "13:30–20:00") on the em-dash U+2013.
        - Return True if current UTC time falls inside [open, close).
        - On parse error: log a warning and return True (fail open).
        """
        now_utc = datetime.utcnow()

        # Weekend guard — applies to ALL instruments (forex is 24/5, not 24/7)
        if now_utc.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
            return False

        hours_str = instrument.active_hours_utc
        # Split on the em-dash U+2013 as documented in watchlist.py
        try:
            open_str, close_str = hours_str.split("\u2013")
            open_h, open_m = (int(x) for x in open_str.strip().split(":"))
            close_h, close_m = (int(x) for x in close_str.strip().split(":"))
        except Exception:
            logger.warning(
                "_is_market_open: could not parse active_hours_utc=%r for %s — defaulting to open",
                hours_str,
                instrument.symbol,
            )
            return True

        open_minutes = open_h * 60 + open_m
        close_minutes = close_h * 60 + close_m
        now_minutes = now_utc.hour * 60 + now_utc.minute

        return open_minutes <= now_minutes < close_minutes

    # ------------------------------------------------------------------
    # DB logging
    # ------------------------------------------------------------------

    def _log_signal(
        self,
        symbol: str,
        bundle: SignalBundle,
        result: ConfidenceResult,
        regime: RegimeContext,
    ) -> None:
        """Write a SignalLog row to the database."""
        log = SignalLog(
            timestamp=datetime.utcnow(),
            symbol=symbol,
            cat1_trend=bundle.cat1.vote,
            cat2_strength=bundle.cat2.vote,
            cat3_momentum=bundle.cat3.vote,
            cat4_volatility=bundle.cat4.vote,
            cat5_volume=bundle.cat5.vote,
            cat6_structure=bundle.cat6.vote,
            cat7_mtf=bundle.cat7.vote,
            cat8_macro=bundle.cat8.vote,
            bull_score=result.bull_score,
            bear_score=result.bear_score,
            direction=result.direction,
            dominant_score=result.dominant_score,
            regime=regime.regime.value,
            position_tier=result.position_tier.value,
            raw_votes=bundle.votes(),
            macro_risk_level=bundle.cat8.params.get("risk_level"),
        )

        kwargs = {}
        if self._database_url:
            kwargs["database_url"] = self._database_url

        session = get_session(**kwargs) if kwargs else get_session()
        try:
            session.add(log)
            session.commit()
            logger.debug(
                "_log_signal: %s | dir=%s score=%.1f tier=%s",
                symbol,
                result.direction,
                result.dominant_score,
                result.position_tier.value,
            )
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------


def test_scanner() -> None:
    """
    Unit tests for Scanner.  No real network calls are made.

    Tests:
    1. _is_market_open: stock instrument at 14:00 UTC → True
    2. _is_market_open: stock instrument at 22:00 UTC → False
    3. _is_market_open: any instrument on Saturday → False
    4. top_opportunity: mixed blocked/unblocked list → highest unblocked score
    5. scan_all with mocked fetch_candles raising an exception → returns []
    """
    import unittest.mock as mock
    from dataclasses import dataclass as dc
    from agents.confidence_scorer import PositionTier
    from regime.detector import Regime

    print("Running Scanner tests…")
    failures: list[str] = []

    def check(label: str, actual, expected) -> None:
        if actual != expected:
            failures.append(f"FAIL [{label}]: expected {expected!r}, got {actual!r}")
        else:
            print(f"  PASS  {label}")

    # ── Build a minimal PortfolioStateManager mock ──────────────────────────────

    mock_state = mock.MagicMock(spec=PortfolioStateManager)
    mock_state.all_positions.return_value = []
    mock_state.get_position.return_value = None

    # ── Instrument fixtures ─────────────────────────────────────────────────────

    from portfolio.watchlist import get_instrument
    spy    = get_instrument("SPY")       # active_hours_utc = "13:30–20:00"
    btcusd = get_instrument("BTCUSD")    # active_hours_utc = "00:00–23:59"

    # ── Test 1 & 2: _is_market_open for stock ──────────────────────────────────

    scanner = Scanner(state_manager=mock_state)

    # patch target: __name__ is '__main__' when run directly, 'portfolio.scanner' via pytest
    _patch_target = f"{__name__}.datetime"

    # Patch datetime.utcnow() to return 14:00 UTC on a Monday (weekday=0)
    monday_14 = datetime(2026, 3, 2, 14, 0, 0)   # 2026-03-02 is a Monday
    with mock.patch(_patch_target) as mock_dt:
        mock_dt.utcnow.return_value = monday_14
        result_open = scanner._is_market_open(spy)
    check("SPY at 14:00 UTC Monday → open", result_open, True)

    # 22:00 UTC — after market close
    monday_22 = datetime(2026, 3, 2, 22, 0, 0)
    with mock.patch(_patch_target) as mock_dt:
        mock_dt.utcnow.return_value = monday_22
        result_closed = scanner._is_market_open(spy)
    check("SPY at 22:00 UTC Monday → closed", result_closed, False)

    # ── Test 3: Weekend guard ───────────────────────────────────────────────────

    saturday_14 = datetime(2026, 3, 7, 14, 0, 0)  # 2026-03-07 is a Saturday
    with mock.patch(_patch_target) as mock_dt:
        mock_dt.utcnow.return_value = saturday_14
        result_weekend = scanner._is_market_open(btcusd)
    check("BTCUSD at 14:00 UTC Saturday → closed (weekend)", result_weekend, False)

    # ── Test 4: top_opportunity ─────────────────────────────────────────────────

    def _make_confidence(score: float, tradeable: bool) -> ConfidenceResult:
        """Stub ConfidenceResult."""
        tier = PositionTier.SMALL if tradeable else PositionTier.NO_TRADE
        return ConfidenceResult(
            bull_score=score,
            bear_score=0.0,
            direction="long",
            dominant_score=score,
            position_tier=tier,
            breakdown={},
        )

    def _make_regime() -> RegimeContext:
        return RegimeContext(
            regime=Regime.TRENDING_UP,
            adx=25.0,
            ema20_above_ema50=True,
            atr=0.01,
            atr_ratio=1.0,
            bb_width=0.02,
            bb_width_pct_rank=0.5,
            description="stub",
        )

    def _make_bundle(symbol: str) -> SignalBundle:
        return SignalBundle(symbol=symbol)

    blocked_high = ScanResult(
        symbol="SPY",
        confidence_result=_make_confidence(95.0, True),
        regime=_make_regime(),
        bundle=_make_bundle("SPY"),
        blocked=True,
        block_reason="earnings blackout",
    )
    unblocked_low = ScanResult(
        symbol="AAPL",
        confidence_result=_make_confidence(68.0, True),
        regime=_make_regime(),
        bundle=_make_bundle("AAPL"),
        blocked=False,
        block_reason="",
    )
    unblocked_high = ScanResult(
        symbol="BTCUSD",
        confidence_result=_make_confidence(82.0, True),
        regime=_make_regime(),
        bundle=_make_bundle("BTCUSD"),
        blocked=False,
        block_reason="",
    )
    no_trade = ScanResult(
        symbol="QQQ",
        confidence_result=_make_confidence(40.0, False),
        regime=_make_regime(),
        bundle=_make_bundle("QQQ"),
        blocked=False,
        block_reason="",
    )

    mixed = [blocked_high, unblocked_low, unblocked_high, no_trade]
    best = scanner.top_opportunity(mixed)
    if best is None:
        failures.append("FAIL [top_opportunity returns result]: got None")
    else:
        check("top_opportunity returns BTCUSD (highest unblocked score)", best.symbol, "BTCUSD")
        check("top_opportunity score is 82.0", best.confidence_result.dominant_score, 82.0)

    # Edge: all blocked → None
    all_blocked = [blocked_high]
    result_none = scanner.top_opportunity(all_blocked)
    check("top_opportunity all blocked → None", result_none, None)

    # Edge: empty list → None
    result_empty = scanner.top_opportunity([])
    check("top_opportunity empty list → None", result_empty, None)

    # ── Test 5: scan_all with mocked fetch that raises → returns [] ─────────────

    with mock.patch(f"{__name__}.fetch_candles", side_effect=RuntimeError("network error")):
        scan_results = scanner.scan_all()

    check("scan_all with fetch error → empty list", len(scan_results), 0)

    # ── Summary ────────────────────────────────────────────────────────────────

    if failures:
        print("\nTest failures:")
        for f in failures:
            print(f"  {f}")
        raise AssertionError(f"{len(failures)} test(s) failed.")
    else:
        print("\nAll Scanner tests passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_scanner()
