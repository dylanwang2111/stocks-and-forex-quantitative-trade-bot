"""
agents/pre_screen_agent.py
Daily pre-screen agent: expands the live universe from the full CANDIDATE_POOL.

Runs at 05:00 UTC daily (except Monday — weekly PortfolioAgent selection takes precedence).
Scores all instruments in CANDIDATE_POOL on 30 days of daily bars using:
  - Softer gate: EMA9 > EMA21 only (no EMA50 requirement — faster to react)
  - Same scoring layers as PortfolioAgent: technical + fundamental + macro context

Selects:
  - Top MAX_STOCKS stocks (correlation-aware greedy)
  - Top MAX_FOREX forex (by score)

Calls set_active_universe() to swap the live universe atomically.
"""
from __future__ import annotations

import logging
import math

import pandas as pd
import pandas_ta as ta
import yfinance as yf

from config.settings import settings
from data.fetcher import _SYMBOL_MAP, fetch_candles
from portfolio.watchlist import CANDIDATE_POOL, Instrument, set_active_universe
from agents.portfolio_agent import MacroContext, _SECTOR, _fetch_macro_context_shared

logger = logging.getLogger(__name__)

# Fundamental thresholds (same as PortfolioAgent)
_PE_GOOD_MAX  = 30
_PE_GREAT_MAX = 18
_ROA_GOOD     = 0.05
_MARGIN_GOOD  = 0.10
_EPS_GROWTH   = 0.10

# Macro thresholds (same as PortfolioAgent)
_VIX_FEAR   = 22
_VIX_STRESS = 30


class PreScreenAgent:
    """
    Daily pre-screen: lighter than PortfolioAgent (30d data, softer EMA gate).

    Gate:  EMA9 > EMA21 (both stocks and forex — softer than weekly agent)

    Score = technical (0-6) + fundamental (0-4) + macro context (0-3)
    Macro context is fetched once and shared across all instruments.
    """

    MAX_STOCKS: int  = settings.bot.max_stocks
    MAX_FOREX: int   = settings.bot.max_forex
    MAX_CRYPTO: int  = settings.bot.max_crypto
    MIN_BARS: int    = 20

    def _bulk_fetch(self) -> dict[str, pd.DataFrame]:
        result = self._bulk_fetch_yfinance()
        if not result:
            logger.info("PreScreenAgent: yfinance failed — falling back to IBKR")
            result = self._bulk_fetch_ibkr()
        return result

    def _bulk_fetch_yfinance(self) -> dict[str, pd.DataFrame]:
        """Single yf.download() for all CANDIDATE_POOL symbols (30d, 1d)."""
        yf_syms = [_SYMBOL_MAP.get(i.symbol.upper(), i.symbol.upper()) for i in CANDIDATE_POOL]
        yf_to_sym = {
            _SYMBOL_MAP.get(i.symbol.upper(), i.symbol.upper()): i.symbol
            for i in CANDIDATE_POOL
        }
        logger.info("PreScreenAgent: bulk-fetching %d symbols via yfinance (30d, 1d)…", len(yf_syms))
        try:
            raw = yf.download(
                yf_syms,
                period="30d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                multi_level_index=True,
            )
        except Exception as exc:
            logger.warning("PreScreenAgent yfinance bulk fetch failed: %s", exc)
            return {}

        if raw is None or raw.empty:
            return {}

        result: dict[str, pd.DataFrame] = {}
        for yf_sym in yf_syms:
            orig_sym = yf_to_sym.get(yf_sym, yf_sym)
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    df = raw.xs(yf_sym, level=1, axis=1).copy()
                else:
                    df = raw.copy()
                df.columns = [c.lower() for c in df.columns]
                df = df[[c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]].copy()
                df = df.dropna(subset=["close"])
                if not df.empty:
                    result[orig_sym] = df
            except Exception as exc:
                logger.debug("PreScreenAgent: could not slice %s: %s", yf_sym, exc)

        logger.info("PreScreenAgent yfinance: got data for %d/%d symbols", len(result), len(yf_syms))
        return result

    def _bulk_fetch_ibkr(self) -> dict[str, pd.DataFrame]:
        """Fetch 30d daily bars via IBKR — single session, all symbols sequential."""
        import asyncio
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB, Forex, Stock, util
        from config.settings import settings

        if not settings.ibkr.enabled:
            logger.warning("PreScreenAgent IBKR fallback: IBKR not configured")
            return {}

        host     = settings.ibkr.host
        port     = settings.ibkr.port
        clientId = settings.ibkr.client_id + 2

        ib = IB()
        result: dict[str, pd.DataFrame] = {}
        try:
            ib.connect(host, port, clientId=clientId, timeout=15, readonly=True)
            for instrument in CANDIDATE_POOL:
                sym = instrument.symbol
                if instrument.asset_type == "crypto":
                    continue  # crypto is OANDA-only — IBKR has no definition for these
                try:
                    if instrument.asset_type == "forex":
                        contract = Forex(sym.upper().replace("/", ""))
                        what = "MIDPOINT"
                    else:
                        from ib_insync import Stock
                        contract = Stock(sym.upper(), "SMART", "USD")
                        what = "TRADES"
                    bars = ib.reqHistoricalData(
                        contract, endDateTime="", durationStr="30 D",
                        barSizeSetting="1 day", whatToShow=what,
                        useRTH=True, formatDate=2,
                    )
                    if not bars:
                        continue
                    df = util.df(bars)[["date", "open", "high", "low", "close", "volume"]].copy()
                    df = df.rename(columns={"date": "time"})
                    df["time"] = pd.to_datetime(df["time"], utc=True)
                    df = df.set_index("time").sort_index().dropna(subset=["close"])
                    if not df.empty:
                        result[sym] = df
                except Exception as exc:
                    logger.debug("PreScreenAgent IBKR: %s failed — %s", sym, exc)
        except Exception as exc:
            logger.warning("PreScreenAgent IBKR connect failed: %s", exc)
        finally:
            if ib.isConnected():
                ib.disconnect()

        logger.info("PreScreenAgent IBKR: got data for %d/%d symbols", len(result), len(CANDIDATE_POOL))
        return result

    def _fetch_fundamentals(self, symbol: str) -> dict:
        """Fetch fundamental metrics via yfinance Ticker.info."""
        result = {"pe": None, "roa": None, "margin": None, "eps_growth": None}
        try:
            info = yf.Ticker(symbol).info
            pe = info.get("trailingPE") or info.get("forwardPE")
            if pe and math.isfinite(pe) and pe > 0:
                result["pe"] = float(pe)
            roa = info.get("returnOnAssets")
            if roa and math.isfinite(roa):
                result["roa"] = float(roa)
            margin = info.get("profitMargins")
            if margin and math.isfinite(margin):
                result["margin"] = float(margin)
            eps_growth = info.get("earningsGrowth") or info.get("earningsQuarterlyGrowth")
            if eps_growth and math.isfinite(eps_growth):
                result["eps_growth"] = float(eps_growth)
        except Exception as exc:
            logger.debug("PreScreenAgent fundamentals %s: %s", symbol, exc)
        return result

    # ── Main screen ────────────────────────────────────────────────────────────

    def screen(self) -> list[Instrument]:
        """
        Score all CANDIDATE_POOL instruments, select the best set,
        and update the active UNIVERSE via set_active_universe().
        """
        logger.info(
            "PreScreenAgent.screen(): evaluating %d candidates",
            len(CANDIDATE_POOL),
        )

        prefetched = self._bulk_fetch()
        bulk_failed = not prefetched

        # Fetch macro context once for all instruments
        macro = _fetch_macro_context_shared()

        scored_stocks: list[tuple[float, Instrument]] = []
        scored_forex:  list[tuple[float, Instrument]] = []
        scored_crypto: list[tuple[float, Instrument]] = []

        for instrument in CANDIDATE_POOL:
            df = prefetched.get(instrument.symbol)
            score = self._score_instrument(instrument, macro, df=df, skip_fallback=bulk_failed)
            if score is None:
                logger.debug("  %s: skipped (gate failed or insufficient data)", instrument.symbol)
                continue
            logger.debug("  %s: score=%.2f", instrument.symbol, score)
            if instrument.asset_type == "stock":
                scored_stocks.append((score, instrument))
            elif instrument.asset_type == "crypto":
                scored_crypto.append((score, instrument))
            else:
                scored_forex.append((score, instrument))

        scored_stocks.sort(key=lambda t: t[0], reverse=True)
        scored_forex.sort(key=lambda t: t[0], reverse=True)
        scored_crypto.sort(key=lambda t: t[0], reverse=True)

        # Greedy correlation-aware stock selection
        selected_stocks: list[Instrument] = []
        selected_symbols: set[str] = set()

        for _score, inst in scored_stocks:
            if len(selected_stocks) >= self.MAX_STOCKS:
                break
            if any(corr in selected_symbols for corr in inst.correlated_with):
                logger.debug("  %s: skipped (correlated)", inst.symbol)
                continue
            selected_stocks.append(inst)
            selected_symbols.add(inst.symbol)

        # Forex: select top MAX_FOREX by score
        selected_forex: list[Instrument] = []
        for _score, inst in scored_forex:
            if len(selected_forex) >= self.MAX_FOREX:
                break
            selected_forex.append(inst)

        # Crypto: force-include BTC anchor, fill remaining slots by score
        _BTC_ANCHOR = "BTCUSD"
        btc_inst = next((i for _s, i in scored_crypto if i.symbol == _BTC_ANCHOR), None)
        if btc_inst is None:
            btc_inst = next((i for i in CANDIDATE_POOL if i.symbol == _BTC_ANCHOR), None)
            if btc_inst:
                logger.info("PreScreenAgent: %s force-included as anchor", _BTC_ANCHOR)
        other_crypto = [i for _s, i in scored_crypto if i.symbol != _BTC_ANCHOR]
        selected_crypto: list[Instrument] = []
        if btc_inst and self.MAX_CRYPTO >= 1:
            selected_crypto.append(btc_inst)
        for inst in other_crypto:
            if len(selected_crypto) >= self.MAX_CRYPTO:
                break
            selected_crypto.append(inst)

        selected = selected_stocks + selected_forex + selected_crypto

        if not selected:
            logger.warning("PreScreenAgent: no instruments passed — keeping existing UNIVERSE")
            return []

        logger.info(
            "PreScreenAgent selected %d: stocks=[%s] forex=[%s] crypto=[%s]",
            len(selected),
            ", ".join(i.symbol for i in selected_stocks),
            ", ".join(i.symbol for i in selected_forex),
            ", ".join(i.symbol for i in selected_crypto),
        )
        set_active_universe(selected)
        return selected

    # ── Scoring ────────────────────────────────────────────────────────────────

    def _score_instrument(
        self,
        instrument: Instrument,
        macro: MacroContext,
        df: pd.DataFrame | None = None,
        skip_fallback: bool = False,
    ) -> float | None:
        """
        Soft EMA gate (EMA9 > EMA21), then composite score.
        Returns None if the gate fails or data is insufficient.
        """
        if df is None:
            if skip_fallback:
                return None
            try:
                df = fetch_candles(instrument.symbol, "1d", period="30d", use_cache=False)
            except Exception as exc:
                logger.debug("  %s: fetch error — %s", instrument.symbol, exc)
                return None

        if df is None or len(df) < self.MIN_BARS:
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

        # ── Soft gate: EMA9 > EMA21 ────────────────────────────────────────────
        if e9 is None or e21 is None or not (e9 > e21):
            return None

        # ── 1. Technical score (0–6) ───────────────────────────────────────────
        tech_score = 0.0

        if adx_val is not None and adx_val > 25:
            tech_score += 1.0
            if adx_val > 40:
                tech_score += 1.0

        if len(close) >= 20:
            ret_20d = float(close.iloc[-1] / close.iloc[-20] - 1.0)
            if ret_20d > 0.02:
                tech_score += 1.0

        if rsi_val is not None and 45 <= rsi_val <= 70:
            tech_score += 1.0

        if instrument.asset_type == "stock" and not volume.empty:
            avg_vol = float(volume.rolling(20).mean().iloc[-1])
            if avg_vol > 500_000:
                tech_score += 1.0

        if len(df) >= 14:
            atr_series = ta.atr(df["high"], df["low"], close, length=14)
            if atr_series is not None and not atr_series.empty:
                atr_pct = float(atr_series.iloc[-1]) / float(close.iloc[-1])
                if 0.003 <= atr_pct <= 0.06:
                    tech_score += 1.0

        # ── 2. Fundamental score (0–4, stocks only) ───────────────────────────
        fund_score = 0.0
        if instrument.asset_type == "stock":
            fund = self._fetch_fundamentals(instrument.symbol)

            pe = fund.get("pe")
            if pe is not None and 0 < pe <= _PE_GOOD_MAX:
                fund_score += 1.0
                if pe <= _PE_GREAT_MAX:
                    fund_score += 1.0

            roa = fund.get("roa")
            if roa is not None and roa >= _ROA_GOOD:
                fund_score += 1.0

            margin = fund.get("margin")
            if margin is not None and margin >= _MARGIN_GOOD:
                fund_score += 1.0

            eps_g = fund.get("eps_growth")
            if eps_g is not None and eps_g >= _EPS_GROWTH:
                fund_score += 0.5

        # ── 3. Macro context bonus (0–3, sector-specific) ─────────────────────
        macro_score = 0.0
        sector = _SECTOR.get(instrument.symbol, "other")

        if sector == "gold":
            if macro.gold_uptrend:
                macro_score += 1.5
            if macro.vix >= _VIX_FEAR:
                macro_score += 1.0
            if macro.vix >= _VIX_STRESS:
                macro_score += 0.5
        elif sector == "energy":
            if macro.oil_uptrend:
                macro_score += 1.5
            if macro.vix >= _VIX_FEAR:
                macro_score += 0.5
        elif sector == "tech":
            if macro.vix < _VIX_FEAR:
                macro_score += 1.0
            if macro.vix >= _VIX_STRESS:
                macro_score -= 1.0
        elif sector == "broad":
            if macro.gold_uptrend or macro.oil_uptrend:
                macro_score += 0.5

        return tech_score + fund_score + macro_score
