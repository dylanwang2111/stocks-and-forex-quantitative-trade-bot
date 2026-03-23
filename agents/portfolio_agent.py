"""
agents/portfolio_agent.py
Weekly portfolio selection agent.

Evaluates all instruments in CANDIDATE_POOL on 60 days of daily data.
Selects the top MAX_STOCKS stocks + MAX_FOREX forex pairs, then calls
set_active_universe() to update the live UNIVERSE.

Selection runs on startup and every Monday at 00:00 UTC.

Scoring layers (additive):
  1. Technical (trend / momentum / liquidity / volatility) — 0–6 pts
  2. Fundamental (P/E, ROA, profit margin, EPS growth)     — 0–4 pts
  3. Macro context (gold trend, oil trend, VIX regime)      — 0–3 pts

Instruments with strong fundamentals AND aligned macro regime receive a
significant boost, ensuring the portfolio rotates into sectors that are
actually benefiting from the current macro environment (e.g. gold/energy
during geopolitical stress, tech during low-VIX risk-on periods).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd
import pandas_ta as ta
import yfinance as yf

from config.settings import settings
from data.fetcher import _SYMBOL_MAP, fetch_candles
from portfolio.watchlist import CANDIDATE_POOL, Instrument, set_active_universe

logger = logging.getLogger(__name__)

_BTC_ANCHOR    = "BTCUSD"   # always force-included in crypto selection

# ── Sector taxonomy ───────────────────────────────────────────────────────────
# Maps symbol → sector tag for macro-context bonuses.
# "energy"  → benefits when crude oil is in an uptrend
# "gold"    → benefits when gold price is rising / VIX is elevated (safe haven)
# "tech"    → benefits in low-VIX risk-on regimes
# "finance" → neutral (no specific macro bonus)
# "other"   → neutral
_SECTOR: dict[str, str] = {
    # Energy / oil
    "XOM": "energy", "CVX": "energy", "OXY": "energy", "COP": "energy",
    "SLB": "energy", "HAL": "energy", "MPC": "energy", "VLO": "energy",
    "XLE": "energy",
    # Gold & precious metals
    "GLD":  "gold",  "GDX":  "gold",  "GDXJ": "gold",
    "GOLD": "gold",  "NEM":  "gold",
    # Technology / growth
    "AAPL": "tech",  "MSFT": "tech",  "NVDA": "tech",  "GOOGL": "tech",
    "AMZN": "tech",  "META": "tech",  "TSLA": "tech",
    "QQQ":  "tech",  "XLK":  "tech",
    # ETF / diversified
    "SPY": "broad",  "IWM": "broad",
    # Financials
    "JPM": "finance", "GS": "finance", "BAC": "finance", "XLF": "finance",
    # Healthcare
    "JNJ": "health", "UNH": "health",
    # Consumer / Industrial
    "WMT": "consumer", "COST": "consumer", "CAT": "industrial",
}

# Fundamental score caps per metric (prevents one extreme value from dominating)
_PE_GOOD_MAX   = 30    # PE ≤ 30 → good valuation (+1)
_PE_GREAT_MAX  = 18    # PE ≤ 18 → great valuation (+1 extra)
_ROA_GOOD      = 0.05  # ROA ≥ 5% → solid asset efficiency (+1)
_MARGIN_GOOD   = 0.10  # net margin ≥ 10% → profitable (+1)
_EPS_GROWTH    = 0.10  # EPS growth ≥ 10% YoY → momentum in earnings (+1)

# Macro thresholds
_VIX_FEAR      = 22    # VIX ≥ 22 → elevated fear → favours gold, penalises tech growth
_VIX_STRESS    = 30    # VIX ≥ 30 → crisis → gold/energy strongly favoured
_OIL_MA_FAST   = 20    # oil short-term MA period
_OIL_MA_SLOW   = 60    # oil long-term MA period (uptrend = fast > slow)
_GOLD_MA_FAST  = 20
_GOLD_MA_SLOW  = 60


@dataclass
class MacroContext:
    """Macro market state fetched once per selection cycle."""
    vix:           float = 15.0   # current VIX level
    oil_uptrend:   bool  = False  # crude oil (USO) EMA20 > EMA60
    gold_uptrend:  bool  = False  # gold (GLD) EMA20 > EMA60
    gold_price:    float = 0.0    # latest GLD price
    oil_price:     float = 0.0    # latest USO price
    evz:           float = 7.0    # CBOE Euro Currency Volatility Index (^EVZ)


def _fetch_macro_context_shared() -> "MacroContext":
    """
    Module-level helper so PreScreenAgent can reuse the same macro fetch
    without importing PortfolioAgent (avoids circular imports).
    Delegates to PortfolioAgent._fetch_macro_context().
    """
    return PortfolioAgent()._fetch_macro_context()


class PortfolioAgent:
    """
    Selects the best trending instruments from CANDIDATE_POOL.

    Long-bias gate (hard filter before scoring):
      - Stocks : EMA9 > EMA21 AND EMA21 > EMA50 required
      - Forex  : EMA9 > EMA21 required (softer — forex can reverse quickly)

    Technical scoring (0–6 pts):
      +1  ADX(14) > 25          — trending
      +1  ADX(14) > 40          — strong trend bonus
      +1  20-day return > 2%    — real upward momentum
      +1  RSI(14) in [45, 70]   — healthy bullish range
      +1  avg_volume_20d > 500k — stocks only: adequate liquidity
      +1  ATR/price in [0.3%,6%] — tradeable volatility

    Fundamental scoring (0–4 pts, stocks only):
      +1  Trailing P/E ≤ 30     — not overvalued
      +1  Trailing P/E ≤ 18     — great valuation (bonus on top)
      +1  ROA ≥ 5%              — solid asset efficiency
      +1  Net profit margin ≥ 10% — profitable business
      +1  EPS growth ≥ 10% YoY  — earnings momentum

    Macro context bonus (0–3 pts, sector-specific):
      Gold sector:   +1 if gold uptrend; +1 if VIX ≥ 22 (fear bid)
      Energy sector: +1 if oil uptrend;  +1 if VIX ≥ 22 (inflation hedge bid)
      Tech sector:   +1 if VIX < 22 (risk-on);  −1 if VIX ≥ 30 (growth selloff)
      Broad ETFs:    +0.5 if gold or oil uptrend (diversification value)
    """

    MAX_STOCKS: int  = settings.bot.max_stocks
    MAX_FOREX: int   = settings.bot.max_forex
    MAX_CRYPTO: int  = settings.bot.max_crypto
    MIN_BARS: int    = 20
    MAX_PER_SECTOR: int = 3   # max stocks from any single sector

    def _bulk_fetch(self) -> dict[str, pd.DataFrame]:
        result = self._bulk_fetch_yfinance()
        if not result:
            logger.info("PortfolioAgent: yfinance failed — falling back to IBKR")
            result = self._bulk_fetch_ibkr()
        return result

    def _bulk_fetch_yfinance(self) -> dict[str, pd.DataFrame]:
        """Single yf.download() for all CANDIDATE_POOL symbols (60d, 1d)."""
        yf_syms = [_SYMBOL_MAP.get(i.symbol.upper(), i.symbol.upper()) for i in CANDIDATE_POOL]
        yf_to_sym = {
            _SYMBOL_MAP.get(i.symbol.upper(), i.symbol.upper()): i.symbol
            for i in CANDIDATE_POOL
        }
        logger.info("PortfolioAgent: bulk-fetching %d symbols via yfinance (60d, 1d)…", len(yf_syms))
        try:
            raw = yf.download(
                yf_syms,
                period="60d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                multi_level_index=True,
            )
        except Exception as exc:
            logger.warning("PortfolioAgent yfinance bulk fetch failed: %s", exc)
            return {}

        if raw is None or raw.empty:
            logger.warning("PortfolioAgent yfinance: empty response")
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
                logger.debug("PortfolioAgent: could not slice %s: %s", yf_sym, exc)

        logger.info("PortfolioAgent yfinance: got data for %d/%d symbols", len(result), len(yf_syms))
        return result

    def _bulk_fetch_ibkr(self) -> dict[str, pd.DataFrame]:
        """Fetch 60d daily bars via IBKR — single session, all symbols sequential."""
        import asyncio
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB, Forex, Stock, util
        from config.settings import settings

        if not settings.ibkr.enabled:
            logger.warning("PortfolioAgent IBKR fallback: IBKR not configured")
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
                        contract = Stock(sym.upper(), "SMART", "USD")
                        what = "TRADES"
                    bars = ib.reqHistoricalData(
                        contract, endDateTime="", durationStr="60 D",
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
                    logger.debug("PortfolioAgent IBKR: %s failed — %s", sym, exc)
        except Exception as exc:
            logger.warning("PortfolioAgent IBKR connect failed: %s", exc)
        finally:
            if ib.isConnected():
                ib.disconnect()

        logger.info("PortfolioAgent IBKR: got data for %d/%d symbols", len(result), len(CANDIDATE_POOL))
        return result

    # ── Macro context ──────────────────────────────────────────────────────────

    def _fetch_macro_context(self) -> MacroContext:
        """
        Fetch VIX, gold (GLD), and oil (USO) to build the macro context.
        Uses yfinance 90-day daily bars. Returns a neutral MacroContext on failure.
        """
        ctx = MacroContext()
        try:
            raw = yf.download(
                ["^VIX", "GLD", "USO", "^EVZ"],
                period="90d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                multi_level_index=True,
            )
            if raw is None or raw.empty:
                return ctx

            def _get_close(sym: str) -> pd.Series | None:
                try:
                    if isinstance(raw.columns, pd.MultiIndex):
                        s = raw.xs(sym, level=1, axis=1)["Close"]
                    else:
                        s = raw["Close"]
                    return s.dropna()
                except Exception:
                    return None

            # VIX
            vix_s = _get_close("^VIX")
            if vix_s is not None and len(vix_s) > 0:
                ctx.vix = float(vix_s.iloc[-1])

            # Gold (GLD)
            gld_s = _get_close("GLD")
            if gld_s is not None and len(gld_s) >= _GOLD_MA_SLOW:
                ctx.gold_price = float(gld_s.iloc[-1])
                gld_fast = float(ta.ema(gld_s, length=_GOLD_MA_FAST).iloc[-1])
                gld_slow = float(ta.ema(gld_s, length=_GOLD_MA_SLOW).iloc[-1])
                ctx.gold_uptrend = gld_fast > gld_slow

            # Oil (USO)
            uso_s = _get_close("USO")
            if uso_s is not None and len(uso_s) >= _OIL_MA_SLOW:
                ctx.oil_price = float(uso_s.iloc[-1])
                uso_fast = float(ta.ema(uso_s, length=_OIL_MA_FAST).iloc[-1])
                uso_slow = float(ta.ema(uso_s, length=_OIL_MA_SLOW).iloc[-1])
                ctx.oil_uptrend = uso_fast > uso_slow

            # EVZ (CBOE Euro Currency Volatility Index)
            evz_s = _get_close("^EVZ")
            if evz_s is not None and len(evz_s) > 0:
                ctx.evz = float(evz_s.iloc[-1])

        except Exception as exc:
            logger.warning("PortfolioAgent: macro context fetch failed: %s", exc)

        logger.info(
            "MacroContext: VIX=%.1f  EVZ=%.2f  gold_uptrend=%s  oil_uptrend=%s",
            ctx.vix, ctx.evz, ctx.gold_uptrend, ctx.oil_uptrend,
        )
        return ctx

    def _fetch_fundamentals(self, symbol: str) -> dict:
        """
        Fetch key fundamental metrics via yfinance Ticker.info.
        Returns a dict with keys: pe, roa, margin, eps_growth.
        All values default to None on failure.
        """
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
            logger.debug("PortfolioAgent fundamentals %s: %s", symbol, exc)
        return result

    # ── Main selection ─────────────────────────────────────────────────────────

    def select(self) -> list[Instrument]:
        """
        Score all CANDIDATE_POOL instruments, select the best ones,
        and update the active UNIVERSE via set_active_universe().
        """
        logger.info(
            "PortfolioAgent.select(): evaluating %d candidates",
            len(CANDIDATE_POOL),
        )

        prefetched = self._bulk_fetch()
        bulk_failed = not prefetched

        # Fetch macro context once for the whole cycle
        macro = self._fetch_macro_context()

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

        # Log top candidates for transparency
        logger.info("Top stock candidates:")
        for s, i in scored_stocks[:10]:
            sector = _SECTOR.get(i.symbol, "other")
            logger.info("  %-6s  score=%.2f  sector=%s", i.symbol, s, sector)

        # Sector-capped selection: at most MAX_PER_SECTOR stocks from any one sector.
        selected_stocks: list[Instrument] = []
        sector_counts: dict[str, int] = {}

        for _score, inst in scored_stocks:
            if len(selected_stocks) >= self.MAX_STOCKS:
                break
            sector = _SECTOR.get(inst.symbol, "other")
            if sector_counts.get(sector, 0) >= self.MAX_PER_SECTOR:
                logger.debug("  %s: skipped (sector cap reached for '%s')", inst.symbol, sector)
                continue
            selected_stocks.append(inst)
            sector_counts[sector] = sector_counts.get(sector, 0) + 1

        # Forex: select top MAX_FOREX by score
        selected_forex: list[Instrument] = []
        for _s, inst in scored_forex:
            if len(selected_forex) >= self.MAX_FOREX:
                break
            selected_forex.append(inst)

        # Crypto: force-include BTC anchor, fill remaining slots by score
        btc_inst = next((i for _s, i in scored_crypto if i.symbol == _BTC_ANCHOR), None)
        if btc_inst is None:
            btc_inst = next((i for i in CANDIDATE_POOL if i.symbol == _BTC_ANCHOR), None)
            if btc_inst:
                logger.info("PortfolioAgent: %s force-included as anchor", _BTC_ANCHOR)
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
            logger.warning("PortfolioAgent: no instruments passed — keeping existing UNIVERSE")
            return []

        logger.info(
            "PortfolioAgent selected %d: stocks=[%s] forex=[%s] crypto=[%s]",
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
        Apply long-bias gate, then compute a composite score.
        Returns None if the gate fails or data is insufficient.

        Score = technical (0-6) + fundamental (0-4) + macro_context (0-3)
        """
        if df is None:
            if skip_fallback:
                return None
            try:
                df = fetch_candles(instrument.symbol, "1d", period="60d", use_cache=False)
            except Exception as exc:
                logger.debug("  %s: fetch error — %s", instrument.symbol, exc)
                return None

        if df is None or len(df) < self.MIN_BARS:
            return None

        close  = df["close"]
        volume = df.get("volume", pd.Series(dtype=float))

        ema9  = ta.ema(close, length=9)
        ema21 = ta.ema(close, length=21)
        ema50 = ta.ema(close, length=50)
        rsi   = ta.rsi(close, length=14)
        adx_df = ta.adx(df["high"], df["low"], close, length=14)

        e9  = float(ema9.iloc[-1])  if ema9  is not None and not ema9.empty  else None
        e21 = float(ema21.iloc[-1]) if ema21 is not None and not ema21.empty else None
        e50 = float(ema50.iloc[-1]) if ema50 is not None and not ema50.empty else None
        rsi_val = float(rsi.iloc[-1]) if rsi is not None and not rsi.empty else None

        adx_val = None
        if adx_df is not None and not adx_df.empty:
            adx_col = [c for c in adx_df.columns if c.startswith("ADX_")]
            if adx_col:
                adx_val = float(adx_df[adx_col[0]].iloc[-1])

        # ── Direction gate ──────────────────────────────────────────────────────
        # Supports both long (uptrend) and short (downtrend) candidates.
        # Stocks require full EMA stack alignment (EMA9/21/50) for either direction.
        # Forex uses the softer EMA9 vs EMA21 gate only.
        if e9 is None or e21 is None:
            return None

        is_long = e9 > e21
        is_bear = e9 < e21

        if instrument.asset_type == "stock":
            if e50 is None:
                return None
            if is_long and not (e21 > e50):
                return None   # long stocks: need full bull stack EMA9 > EMA21 > EMA50
            if is_bear and not (e21 < e50):
                return None   # short stocks: need full bear stack EMA9 < EMA21 < EMA50

        if not (is_long or is_bear):
            return None

        # ── 1. Technical score (0–6) ───────────────────────────────────────────
        tech_score = 0.0

        # ADX: direction-agnostic trend strength
        if adx_val is not None and adx_val > 25:
            tech_score += 1.0
            if adx_val > 40:
                tech_score += 1.0

        if len(close) >= 20:
            ret_20d = float(close.iloc[-1] / close.iloc[-20] - 1.0)
            if is_long and ret_20d > 0.02:
                tech_score += 1.0
            elif is_bear and ret_20d < -0.02:
                tech_score += 1.0

        if rsi_val is not None:
            if is_long and 45 <= rsi_val <= 70:
                tech_score += 1.0
            elif is_bear and 30 <= rsi_val <= 55:
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

        # ── 2. Fundamental score (0–4, stocks only, long candidates only) ──────
        # Fundamentals reward quality longs. For shorts we skip — a downtrend in a
        # fundamentally good stock is still worth trading.
        fund_score = 0.0
        if instrument.asset_type == "stock" and is_long:
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

        # ── 3. Macro context bonus (0–3, sector-specific, direction-aware) ─────
        macro_score = 0.0
        sector = _SECTOR.get(instrument.symbol, "other")

        if is_long:
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
        else:  # bear
            if sector == "gold":
                if not macro.gold_uptrend:
                    macro_score += 1.5   # gold downtrend favours short gold names
                if macro.vix < _VIX_FEAR:
                    macro_score += 0.5   # risk-on = no safe-haven bid
            elif sector == "energy":
                if not macro.oil_uptrend:
                    macro_score += 1.5   # oil downtrend favours short energy names
                if macro.vix < _VIX_FEAR:
                    macro_score += 0.5
            elif sector == "tech":
                if macro.vix >= _VIX_FEAR:
                    macro_score += 1.0   # fear = tech selloff
                if macro.vix >= _VIX_STRESS:
                    macro_score += 0.5
            elif sector == "broad":
                if not macro.gold_uptrend and not macro.oil_uptrend:
                    macro_score += 0.5

        total_score = tech_score + fund_score + macro_score
        direction_tag = "long" if is_long else "short"
        logger.debug(
            "  %s [%s]: tech=%.1f fund=%.1f macro=%.1f total=%.2f [%s]",
            instrument.symbol, direction_tag, tech_score, fund_score, macro_score, total_score, sector,
        )
        return total_score


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def test_portfolio_agent() -> None:
    """
    Smoke-test PortfolioAgent.select() end-to-end.

    When real market data is unavailable (yfinance rate-limited), runs a
    unit test of the scoring logic with synthetic data instead.
    """
    from portfolio.watchlist import UNIVERSE
    import logging as _log
    _log.basicConfig(level=_log.DEBUG)

    print("=== test_portfolio_agent ===\n")

    # ── Unit test: scoring logic with synthetic data ───────────────────────
    print("--- Unit test: scoring logic ---")
    _run_scoring_unit_test()

    # ── Integration test: real selection ──────────────────────────────────
    print("\n--- Integration test: full select() ---")
    agent = PortfolioAgent()
    selected = agent.select()

    if not selected:
        print("WARNING: no instruments selected (market data may be unavailable)")
        print("Integration test: SKIPPED")
        return

    max_possible = PortfolioAgent.MAX_STOCKS + PortfolioAgent.MAX_FOREX
    assert 1 <= len(selected) <= max_possible, (
        f"Expected 1–{max_possible} instruments, got {len(selected)}"
    )
    print(f"PASS: selected {len(selected)} instruments")

    for inst in selected:
        assert inst.asset_type in ("stock", "forex", "crypto")
    print("PASS: all asset types valid")

    assert len(UNIVERSE) == len(selected)
    print(f"PASS: UNIVERSE updated to {len(UNIVERSE)} instruments")

    # Portfolio agent enforces MAX_PER_SECTOR (not correlated_with exclusion —
    # that's the scanner's CorrelationGuard). Verify sector cap is respected.
    sector_counts: dict[str, int] = {}
    for inst in selected:
        if inst.asset_type != "stock":
            continue
        sector = _SECTOR.get(inst.symbol, "other")
        sector_counts[sector] = sector_counts.get(sector, 0) + 1
    for sector, count in sector_counts.items():
        assert count <= PortfolioAgent.MAX_PER_SECTOR, (
            f"Sector cap violated: {count} '{sector}' stocks selected "
            f"(max={PortfolioAgent.MAX_PER_SECTOR})"
        )
    print("PASS: sector cap respected")

    print("\nSelected universe:")
    for inst in selected:
        sector = _SECTOR.get(inst.symbol, "other")
        print(f"  {inst.symbol:<8} [{inst.broker:<5}] {inst.asset_type:<6}  sector={sector}")

    print("\ntest_portfolio_agent: ALL ASSERTIONS PASSED")


def _run_scoring_unit_test() -> None:
    """
    Unit-test the scoring layers with synthetic OHLCV data.
    Verifies that:
    1. Gate correctly blocks downtrending instruments
    2. Technical score adds up correctly
    3. Macro bonus correctly routes by sector
    4. Fundamentals score correctly
    """
    import numpy as np
    from portfolio.watchlist import CANDIDATE_POOL

    agent = PortfolioAgent()

    # ── Build synthetic uptrending OHLCV (60 bars) ──────────────────────
    def _make_df(trend: float = 0.003, vol: float = 0.01, n: int = 65) -> pd.DataFrame:
        """trend > 0 = uptrend, trend < 0 = downtrend."""
        np.random.seed(42)
        prices = [100.0]
        for _ in range(n - 1):
            r = trend + np.random.randn() * vol
            prices.append(max(prices[-1] * (1 + r), 1.0))
        idx = pd.bdate_range(end=pd.Timestamp("2025-01-31"), periods=n)
        close_arr = np.array(prices, dtype=np.float64)
        atr_arr   = close_arr * 0.01
        return pd.DataFrame({
            "open":   close_arr.copy(),
            "high":   (close_arr + atr_arr).astype(np.float64),
            "low":    (close_arr - atr_arr).astype(np.float64),
            "close":  close_arr.copy(),
            "volume": np.full(n, 2_000_000.0, dtype=np.float64),
        }, index=idx)

    # Find a representative stock instrument from CANDIDATE_POOL
    test_stock = next((i for i in CANDIDATE_POOL if i.symbol == "AAPL"), CANDIDATE_POOL[0])
    test_gold  = next((i for i in CANDIDATE_POOL if i.symbol == "GOLD"), None)
    test_energy = next((i for i in CANDIDATE_POOL if i.symbol == "XOM"), None)

    neutral_macro = MacroContext(vix=15.0, oil_uptrend=False, gold_uptrend=False)
    fear_macro    = MacroContext(vix=28.0, oil_uptrend=True,  gold_uptrend=True)

    # ── Test 1: gate blocks downtrend ─────────────────────────────────────
    down_df = _make_df(trend=-0.005)
    score = agent._score_instrument(test_stock, neutral_macro, df=down_df)
    assert score is None, f"Downtrend should be blocked by gate, got score={score}"
    print("PASS: gate blocks downtrend")

    # ── Test 2: uptrend passes gate and scores > 0 ────────────────────────
    up_df = _make_df(trend=0.003)
    score = agent._score_instrument(test_stock, neutral_macro, df=up_df)
    assert score is not None and score > 0, f"Uptrend should score > 0, got {score}"
    print(f"PASS: uptrend passes gate (score={score:.2f})")

    # ── Test 3: gold sector gets macro bonus in fear/gold-uptrend env ─────
    if test_gold is not None:
        score_neutral = agent._score_instrument(test_gold, neutral_macro, df=up_df)
        score_fear    = agent._score_instrument(test_gold, fear_macro,    df=up_df)
        assert score_fear is not None and score_neutral is not None
        assert score_fear > score_neutral, (
            f"Gold in fear macro should score higher: fear={score_fear:.2f} vs neutral={score_neutral:.2f}"
        )
        print(f"PASS: gold macro bonus works (neutral={score_neutral:.2f} → fear={score_fear:.2f})")

    # ── Test 4: energy gets macro bonus when oil is in uptrend ────────────
    if test_energy is not None:
        score_neutral = agent._score_instrument(test_energy, neutral_macro, df=up_df)
        oil_macro     = MacroContext(vix=15.0, oil_uptrend=True, gold_uptrend=False)
        score_oil_up  = agent._score_instrument(test_energy, oil_macro, df=up_df)
        assert score_oil_up is not None and score_neutral is not None
        assert score_oil_up > score_neutral, (
            f"Energy in oil uptrend should score higher: {score_oil_up:.2f} vs {score_neutral:.2f}"
        )
        print(f"PASS: energy macro bonus works (neutral={score_neutral:.2f} → oil_up={score_oil_up:.2f})")

    # ── Test 5: tech gets penalised in crisis (VIX ≥ 30) ─────────────────
    crisis_macro = MacroContext(vix=35.0, oil_uptrend=False, gold_uptrend=True)
    score_normal = agent._score_instrument(test_stock, neutral_macro, df=up_df)
    score_crisis = agent._score_instrument(test_stock, crisis_macro,  df=up_df)
    if score_normal is not None and score_crisis is not None:
        assert score_crisis < score_normal, (
            f"Tech should score lower in crisis: crisis={score_crisis:.2f} vs normal={score_normal:.2f}"
        )
        print(f"PASS: tech penalised in crisis (normal={score_normal:.2f} → crisis={score_crisis:.2f})")

    # ── Test 6: fundamental mock ──────────────────────────────────────────
    # Inject synthetic fundamental data by monkey-patching the method
    original_fetch = agent._fetch_fundamentals

    def _good_fundamentals(sym):
        return {"pe": 15.0, "roa": 0.08, "margin": 0.15, "eps_growth": 0.20}

    def _bad_fundamentals(sym):
        return {"pe": 50.0, "roa": 0.01, "margin": 0.02, "eps_growth": -0.05}

    agent._fetch_fundamentals = _good_fundamentals
    score_good_fund = agent._score_instrument(test_stock, neutral_macro, df=up_df)

    agent._fetch_fundamentals = _bad_fundamentals
    score_bad_fund = agent._score_instrument(test_stock, neutral_macro, df=up_df)

    agent._fetch_fundamentals = original_fetch  # restore

    if score_good_fund is not None and score_bad_fund is not None:
        assert score_good_fund > score_bad_fund, (
            f"Good fundamentals should score higher: {score_good_fund:.2f} vs {score_bad_fund:.2f}"
        )
        print(f"PASS: fundamentals scoring works (bad={score_bad_fund:.2f} → good={score_good_fund:.2f})")

    print("\nAll scoring unit tests PASSED")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    test_portfolio_agent()
