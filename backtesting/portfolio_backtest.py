"""
backtesting/portfolio_backtest.py

Portfolio-level backtester: runs all 6 instruments simultaneously,
picks the top positions by confidence every day, enforces correlation guards,
and tracks a single combined equity curve.

Strategy improvements over v1:
  - cat4: BB breakout signal (price vs BB bands) — truly independent of cat1
  - cat5: OBV EMA crossover (true buying/selling pressure) — truly independent
  - cat6: 5-bar price momentum (more robust than 1-bar)
  - Regime: EMA50 vs EMA200 golden/death cross (more robust than EMA50 slope)
  - Short selling: when regime is bearish and bear conviction is high
  - Trailing stops: peak/trough tracking per position
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
import pandas_ta as ta

warnings.filterwarnings("ignore")

from agents.confidence_scorer import ConfidenceScorer
from config.settings import settings
from portfolio.watchlist import CORRELATION_BLACKLIST, UNIVERSE

# ── Fee schedule (round-trip) ──────────────────────────────────────────────────
_FEES: dict[str, float] = {
    "stock":  0.001,   # 0.1% round-trip
    "crypto": 0.002,   # 0.2% round-trip (OANDA spread)
}

# ATR-based stop loss / take-profit multipliers
_ATR_SL_MULT: dict[str, float] = {
    "stock":  2.0,
    "crypto": 2.0,
}
_ATR_TP_MULT: dict[str, float] = {
    "stock":  4.0,    # R:R 2.0:1 — tighter target improves WR; more realistic for
    "crypto": 4.0,    # daily-bar proxy (live 15m signals often exit well before 6×ATR)
}
# Trailing stop: exit when close pulls back N*ATR from the position's best price
_ATR_TRAIL_MULT: dict[str, float] = {
    "stock":  2.0,
    "crypto": 2.0,
}
_FALLBACK_SL: dict[str, float] = {"stock": 0.015, "crypto": 0.015}
_FALLBACK_TP: dict[str, float] = {"stock": 0.030, "crypto": 0.030}

_ASSET_TYPE: dict[str, str] = {
    inst.symbol: inst.asset_type for inst in UNIVERSE
}

# Sector tags for macro context gating (mirrors portfolio_agent._SECTOR)
_SECTOR: dict[str, str] = {
    "XOM": "energy", "CVX": "energy", "COP": "energy",
    "OXY": "energy", "SLB": "energy", "HAL": "energy",
    "MPC": "energy", "VLO": "energy", "XLE": "energy",
    "GLD": "gold",   "GOLD": "gold",  "NEM": "gold",
    "GDX": "gold",   "GDXJ": "gold",
}

# Short entry requires a higher confidence threshold than long entry
# to compensate for the additional risk (unlimited upside for the stock)
_SHORT_THRESHOLD_PREMIUM = 5.0   # e.g. if long threshold=55, short threshold=60
# Short entries only allowed when regime is strongly bearish (EMA50 well below EMA200)
_SHORT_REGIME_REQUIRED = True


@dataclass
class ClosedTrade:
    symbol:     str
    direction:  int      # +1 long, -1 short
    entry_date: object
    exit_date:  object
    entry_px:   float
    exit_px:    float
    pnl_usd:    float


@dataclass
class PortfolioResult:
    start:        str
    end:          str
    symbols:      list[str]
    sharpe:       float
    win_rate:     float
    profit_factor: float
    max_drawdown: float
    total_return: float
    trade_count:  int
    avg_positions: float
    equity_curve: pd.Series
    trades:       list[ClosedTrade] = field(default_factory=list)

    def passed(self) -> bool:
        return (
            self.sharpe > 1.2
            and self.max_drawdown < 0.20
            and self.trade_count >= 15
        )

    def print_table(self) -> None:
        print("\n" + "=" * 70)
        print("  PORTFOLIO BACKTEST — Full Period")
        print("=" * 70)
        status = "PASS ✓" if self.passed() else "FAIL ✗"
        print(f"  Period       : {self.start} → {self.end}")
        print(f"  Instruments  : {', '.join(self.symbols)}")
        print(f"  Sharpe       : {self.sharpe:.2f}")
        print(f"  Win Rate     : {self.win_rate * 100:.1f}%")
        print(f"  Max Drawdown : {self.max_drawdown * 100:.1f}%")
        print(f"  Profit Factor: {self.profit_factor:.2f}")
        print(f"  Total Return : {self.total_return * 100:.1f}%")
        print(f"  Total Trades : {self.trade_count}")
        print(f"  Avg Positions: {self.avg_positions:.1f}")
        print(f"  Status       : {status}")
        print("=" * 70)

        if self.trades:
            print("\n  Per-symbol trade count:")
            sym_counts: dict[str, int] = {}
            sym_pnl: dict[str, float] = {}
            for t in self.trades:
                sym_counts[t.symbol] = sym_counts.get(t.symbol, 0) + 1
                sym_pnl[t.symbol] = sym_pnl.get(t.symbol, 0.0) + t.pnl_usd
            for sym in sorted(sym_counts):
                n_long  = sum(1 for t in self.trades if t.symbol == sym and t.direction == 1)
                n_short = sum(1 for t in self.trades if t.symbol == sym and t.direction == -1)
                print(
                    f"    {sym:<8} {sym_counts[sym]:>3} trades  "
                    f"(L:{n_long} S:{n_short})  PnL ${sym_pnl[sym]:>+8.2f}"
                )
        print()


class PortfolioBacktestRunner:
    """
    Simulates the full portfolio strategy on historical daily bars.

    At each bar:
    1. Score all instruments (8-category proxy, improved independence)
    2. Identify long candidates (bull regime, score >= threshold)
       and short candidates (bear regime, score >= threshold + premium)
    3. Select top N (≤ MAX_POSITIONS) respecting correlation guards
    4. Manage existing positions: SL/TP/trailing stop/signal exit
    5. Track equity = cash + Σ mark-to-market position values
    """

    MAX_POSITIONS     = 2
    CASH_RESERVE_PCT  = float(getattr(getattr(settings, "bot", None), "cash_reserve_pct", 0.30))
    TOTAL_CAPITAL     = float(getattr(getattr(settings, "bot", None), "total_capital", 2000.0))

    def __init__(self, confidence_threshold: float = 62.0, holding_days: int = 3):
        self.threshold    = confidence_threshold
        self.holding_days = holding_days
        self.scorer       = ConfidenceScorer()

    # ── Public API ─────────────────────────────────────────────────────────────

    def run(
        self,
        symbols: list[str],
        start: str,
        end: str,
        prefetched_dfs: dict[str, pd.DataFrame],
    ) -> PortfolioResult:
        score_data = self._compute_all_scores(symbols, prefetched_dfs, start, end)
        if score_data.empty:
            return self._empty_result(symbols, start, end)
        macro_ctx = self._build_macro_context(prefetched_dfs, start, end)
        return self._simulate(symbols, prefetched_dfs, score_data, start, end, macro_ctx)

    # ── Signal scoring ─────────────────────────────────────────────────────────

    def _compute_all_scores(
        self,
        symbols: list[str],
        dfs: dict[str, pd.DataFrame],
        start: str,
        end: str,
    ) -> pd.DataFrame:
        """
        Returns DataFrame indexed by date with columns:
          {sym}_score, {sym}_direction, {sym}_atr, {sym}_regime, {sym}_ema200
        """
        frames: dict[str, pd.Series] = {}
        for sym in symbols:
            df = dfs.get(sym)
            if df is None or df.empty or len(df) < 210:
                continue
            score_s, dir_s, atr_s, regime_s, ema200_s, ema50_s, roll_high_s, roll_low_s, adx_s = self._score_series(df)
            frames[f"{sym}_score"]     = score_s.loc[start:end]
            frames[f"{sym}_dir"]       = dir_s.loc[start:end]
            frames[f"{sym}_atr"]       = atr_s.loc[start:end]
            frames[f"{sym}_regime"]    = regime_s.loc[start:end]
            frames[f"{sym}_ema200"]    = ema200_s.loc[start:end]
            frames[f"{sym}_ema50"]     = ema50_s.loc[start:end]
            frames[f"{sym}_roll_high"] = roll_high_s.loc[start:end]
            frames[f"{sym}_roll_low"]  = roll_low_s.loc[start:end]
            frames[f"{sym}_adx"]       = adx_s.loc[start:end]

        if not frames:
            return pd.DataFrame()

        result = pd.DataFrame(frames).fillna(0)
        return result

    def _score_series(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        Compute (score, direction, atr, regime) for a single instrument.

        Signal categories:
          cat1 – EMA9/21 crossover confirmed by MACD histogram (trend direction)
          cat2 – EMA21 vs EMA50 alignment (medium-term trend strength)
          cat3 – RSI momentum: >55 bull, <45 bear (tighter deadband = less noise)
          cat4 – BB breakout: price ABOVE upper band = momentum surge (bull),
                 price BELOW lower band = breakdown (bear). INDEPENDENT of cat1.
          cat5 – OBV EMA5 vs EMA20 crossover: rising OBV = buying pressure (bull),
                 falling OBV = selling pressure (bear). INDEPENDENT of cat1.
          cat6 – 5-bar price momentum: 5-day return > +1% = bull, < -1% = bear.
                 More robust than single-bar return.
          cat7 – MTF proxy: cat1 × 2 when cat1 == cat2, else cat1 (double weight)
          cat8 – macro/news: always 0 in backtest (cannot replay news fairly)

        Regime (returned separately, not in score):
          +1 = EMA50 > EMA200 (bull / golden cross) → long entries allowed
          -1 = EMA50 < EMA200 (bear / death cross)  → short entries allowed
        """
        close  = df["close"]
        volume = df.get("volume", pd.Series(1.0, index=close.index))

        # ── Indicators ────────────────────────────────────────────────────────
        ema9   = ta.ema(close, length=9)
        ema21  = ta.ema(close, length=21)
        ema50  = ta.ema(close, length=50)
        ema200 = ta.ema(close, length=200)   # for regime detection
        rsi    = ta.rsi(close, length=14)
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)

        if macd_df is not None:
            hist_col  = [c for c in macd_df.columns if "MACDh" in c][0]
            macd_hist = macd_df[hist_col]
        else:
            macd_hist = pd.Series(0.0, index=close.index)

        # ATR for adaptive stops
        atr_series = ta.atr(df["high"], df["low"], close, length=14)
        if atr_series is None:
            atr_series = close * 0.015

        # cat4: Bollinger Bands (for breakout signal — price position vs bands)
        bb = ta.bbands(close, length=20, std=2.0)
        if bb is not None:
            bbu_col  = [c for c in bb.columns if "BBU" in c][0]
            bbl_col  = [c for c in bb.columns if "BBL" in c][0]
            bb_upper = bb[bbu_col]
            bb_lower = bb[bbl_col]
        else:
            bb_upper = close * 1.02
            bb_lower = close * 0.98

        # cat5: On-Balance Volume EMA crossover (true buying/selling pressure)
        obv = ta.obv(close, volume)
        if obv is not None:
            obv_fast = ta.ema(obv, length=5)
            obv_slow = ta.ema(obv, length=20)
        else:
            obv_fast = pd.Series(0.0, index=close.index)
            obv_slow = pd.Series(0.0, index=close.index)

        # ADX(14) — trend quality filter: only enter when a trend is present
        adx_df = ta.adx(df["high"].astype(np.float64), df["low"].astype(np.float64),
                        close.astype(np.float64), length=14)
        if adx_df is not None:
            adx_col = [c for c in adx_df.columns if c.startswith("ADX_")][0]
            adx_s = adx_df[adx_col]
        else:
            adx_s = pd.Series(25.0, index=close.index)  # neutral fallback

        # ── Output series ─────────────────────────────────────────────────────
        scores     = pd.Series(0.0, index=close.index)
        directions = pd.Series(0,   index=close.index, dtype=int)
        regime_s   = pd.Series(0,   index=close.index, dtype=int)

        for i in range(200, len(df)):
            votes: dict[str, int] = {}

            # ── cat1: EMA9/21 crossover + MACD histogram confirmation ─────────
            if pd.notna(ema9.iloc[i]) and pd.notna(ema21.iloc[i]):
                ema_bull  = ema9.iloc[i] > ema21.iloc[i]
                ema_bear  = ema9.iloc[i] < ema21.iloc[i]
                macd_bull = float(macd_hist.iloc[i]) > 0
                macd_bear = float(macd_hist.iloc[i]) < 0
                if ema_bull and macd_bull:
                    votes["cat1"] = 1
                elif ema_bear and macd_bear:
                    votes["cat1"] = -1
                else:
                    votes["cat1"] = 0
            else:
                votes["cat1"] = 0

            # ── cat2: EMA21 vs EMA50 (medium-term trend alignment) ────────────
            if pd.notna(ema21.iloc[i]) and pd.notna(ema50.iloc[i]):
                votes["cat2"] = (
                    1  if ema21.iloc[i] > ema50.iloc[i] else
                    -1 if ema21.iloc[i] < ema50.iloc[i] else 0
                )
            else:
                votes["cat2"] = 0

            # ── cat3: RSI momentum (tighter 45/55 deadband, less noise) ──────
            # Overbought cap: RSI > 75 returns 0 (not +1) — avoids entering at
            # momentum extremes where reversal risk is high (e.g., TSLA Jan 2022
            # at RSI=85 just before the crash).
            if pd.notna(rsi.iloc[i]):
                rsi_val = float(rsi.iloc[i])
                votes["cat3"] = (1 if 55 < rsi_val < 75 else
                                 (-1 if rsi_val < 45 else 0))
            else:
                votes["cat3"] = 0

            # ── cat4: BB breakout/breakdown — INDEPENDENT of cat1 ─────────────
            # Price closing ABOVE upper band = bullish momentum surge
            # Price closing BELOW lower band = bearish breakdown
            if pd.notna(bb_upper.iloc[i]) and pd.notna(bb_lower.iloc[i]):
                if float(close.iloc[i]) > float(bb_upper.iloc[i]):
                    votes["cat4"] = 1
                elif float(close.iloc[i]) < float(bb_lower.iloc[i]):
                    votes["cat4"] = -1
                else:
                    votes["cat4"] = 0
            else:
                votes["cat4"] = 0

            # ── cat5: OBV EMA crossover — INDEPENDENT of cat1 ─────────────────
            # OBV fast EMA rising above slow EMA = buying pressure accumulating
            if (obv_fast is not None and obv_slow is not None
                    and pd.notna(obv_fast.iloc[i]) and pd.notna(obv_slow.iloc[i])):
                if float(obv_fast.iloc[i]) > float(obv_slow.iloc[i]):
                    votes["cat5"] = 1
                elif float(obv_fast.iloc[i]) < float(obv_slow.iloc[i]):
                    votes["cat5"] = -1
                else:
                    votes["cat5"] = 0
            else:
                votes["cat5"] = 0

            # ── cat6: 5-bar price momentum — more robust than 1-bar ───────────
            if i >= 5 and pd.notna(close.iloc[i - 5]) and float(close.iloc[i - 5]) > 0:
                ret_5 = float(close.iloc[i]) / float(close.iloc[i - 5]) - 1.0
                votes["cat6"] = 1 if ret_5 > 0.01 else (-1 if ret_5 < -0.01 else 0)
            else:
                votes["cat6"] = 0

            # ── cat7: MTF proxy — cat1 × 2 when cat1 agrees with cat2 ─────────
            c1, c2 = votes["cat1"], votes["cat2"]
            if c1 != 0 and c1 == c2:
                votes["cat7"] = c1 * 2
            elif c1 != 0:
                votes["cat7"] = c1
            else:
                votes["cat7"] = 0

            # ── cat8: macro/news — always neutral in backtest ─────────────────
            votes["cat8"] = 0

            # ── Regime: EMA50 vs EMA200 (golden cross / death cross) ──────────
            # EMA50 > EMA200 → bull regime (long entries allowed)
            # EMA50 < EMA200 → bear regime (short entries allowed)
            if pd.notna(ema50.iloc[i]) and pd.notna(ema200.iloc[i]):
                regime = 1 if float(ema50.iloc[i]) > float(ema200.iloc[i]) else -1
            else:
                regime = 0
            regime_s.iloc[i] = regime

            direction, score = self.scorer.simple_signal(votes, self.threshold)

            scores.iloc[i]     = score if direction != 0 else 0.0
            directions.iloc[i] = direction

        # ── EMA200, EMA50 and 52-week proximity filter columns ───────────────────
        ema200_s   = pd.Series(np.nan, index=close.index)
        ema50_s    = pd.Series(np.nan, index=close.index)
        # Rolling 252-day high/low for 52-week proximity filter
        roll_high  = close.rolling(252, min_periods=20).max()
        roll_low   = close.rolling(252, min_periods=20).min()
        for i in range(50, len(df)):
            if pd.notna(ema50.iloc[i]):
                ema50_s.iloc[i] = float(ema50.iloc[i])
        for i in range(200, len(df)):
            if pd.notna(ema200.iloc[i]):
                ema200_s.iloc[i] = float(ema200.iloc[i])

        # adx_s already computed above — returned for per-asset-type gate in _simulate
        return scores, directions, atr_series, regime_s, ema200_s, ema50_s, roll_high, roll_low, adx_s

    # ── Macro context (sector trend gates) ────────────────────────────────────

    def _build_macro_context(
        self,
        prefetched_dfs: dict[str, pd.DataFrame],
        start: str,
        end: str,
    ) -> dict[str, pd.Series]:
        """
        Build a dict of {macro_key: pd.Series[bool]} for sector trend gates.

        Currently computes:
          "oil_uptrend" — USO EMA20 > EMA60 (mirrors live MacroContext.oil_uptrend)

        Uses "USO" from prefetched_dfs if available, otherwise tries yfinance.
        Falls back to all-True (neutral / allow all) if data unavailable.

        Returns a dict so future macro signals (gold_uptrend, vix_stress) can
        be added without changing the _simulate signature.
        """
        import pandas_ta as _ta
        import pandas as _pd

        # ── Fetch USO ─────────────────────────────────────────────────────
        uso_df = prefetched_dfs.get("USO")
        if uso_df is None or uso_df.empty:
            try:
                import yfinance as yf
                # Fetch with extra warmup (60 bars) so EMAs are warm at start
                import datetime
                fetch_dt = datetime.date.fromisoformat(start) - datetime.timedelta(days=120)
                uso_raw = yf.download(
                    "USO",
                    start=fetch_dt.strftime("%Y-%m-%d"),
                    end=end,
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                )
                if uso_raw is not None and not uso_raw.empty:
                    uso_raw.columns = [c.lower() for c in uso_raw.columns]
                    if uso_raw.index.tz is None:
                        uso_raw.index = uso_raw.index.tz_localize("UTC")
                    else:
                        uso_raw.index = uso_raw.index.tz_convert("UTC")
                    uso_df = uso_raw
            except Exception:
                uso_df = None

        result: dict[str, pd.Series] = {}

        if uso_df is not None and not uso_df.empty and "close" in uso_df.columns:
            close = uso_df["close"].dropna()
            ema_fast = _ta.ema(close, length=20)
            ema_slow = _ta.ema(close, length=60)
            if ema_fast is not None and ema_slow is not None:
                oil_up = (ema_fast > ema_slow).reindex(close.index).fillna(False)
                result["oil_uptrend"] = oil_up
        # If fetch failed, "oil_uptrend" key absent → _simulate defaults to True (allow)

        return result

    # ── Portfolio simulation ───────────────────────────────────────────────────

    def _simulate(
        self,
        symbols: list[str],
        dfs: dict[str, pd.DataFrame],
        score_data: pd.DataFrame,
        start: str,
        end: str,
        macro_ctx: "dict[str, pd.Series] | None" = None,
    ) -> PortfolioResult:
        if macro_ctx is None:
            macro_ctx = {}
        # Build aligned OHLC matrices
        close_frames, high_frames, low_frames = {}, {}, {}
        for sym in symbols:
            df = dfs.get(sym)
            if df is not None and not df.empty:
                sl = df.loc[start:end]
                close_frames[sym] = sl["close"]
                high_frames[sym]  = sl["high"]
                low_frames[sym]   = sl["low"]

        close_matrix = pd.DataFrame(close_frames).sort_index()
        high_matrix  = pd.DataFrame(high_frames).reindex(close_matrix.index)
        low_matrix   = pd.DataFrame(low_frames).reindex(close_matrix.index)
        score_data   = score_data.reindex(close_matrix.index).fillna(0)

        deployable = self.TOTAL_CAPITAL * (1 - self.CASH_RESERVE_PCT)
        per_slot   = deployable / self.MAX_POSITIONS

        cash = self.TOTAL_CAPITAL
        equity_vals:   list[float] = []
        closed_trades: list[ClosedTrade] = []

        # Active positions: {sym: {direction, entry_px, shares, alloc, entry_date,
        #                         hold_count, sl_level, tp_level, best_price}}
        positions: dict[str, dict] = {}
        position_counts: list[int] = []

        # Stop-loss cooldown: after SL hit, skip re-entry for N bars
        sl_cooldown: dict[str, int] = {}
        _SL_COOLDOWN_BARS = 5

        for date, row in close_matrix.iterrows():
            high_row = high_matrix.loc[date] if date in high_matrix.index else row
            low_row  = low_matrix.loc[date]  if date in low_matrix.index  else row

            # Tick down cooldowns
            for sym in list(sl_cooldown.keys()):
                sl_cooldown[sym] -= 1
                if sl_cooldown[sym] <= 0:
                    del sl_cooldown[sym]

            # ── Collect today's candidates ─────────────────────────────────
            long_candidates:  list[tuple[str, float, int]] = []
            short_candidates: list[tuple[str, float, int]] = []

            for sym in symbols:
                score_col  = f"{sym}_score"
                dir_col    = f"{sym}_dir"
                regime_col = f"{sym}_regime"
                ema200_col    = f"{sym}_ema200"
                roll_high_col = f"{sym}_roll_high"
                roll_low_col  = f"{sym}_roll_low"
                if score_col not in score_data.columns:
                    continue
                adx_col    = f"{sym}_adx"
                score      = float(score_data.loc[date, score_col])       if date in score_data.index else 0.0
                direction  = int(score_data.loc[date, dir_col])            if date in score_data.index else 0
                regime     = int(score_data.loc[date, regime_col])         if regime_col     in score_data.columns and date in score_data.index else 1
                ema200_val = float(score_data.loc[date, ema200_col])       if ema200_col     in score_data.columns and date in score_data.index else np.nan
                roll_high  = float(score_data.loc[date, roll_high_col])    if roll_high_col  in score_data.columns and date in score_data.index else np.nan
                roll_low   = float(score_data.loc[date, roll_low_col])     if roll_low_col   in score_data.columns and date in score_data.index else np.nan
                adx_val    = float(score_data.loc[date, adx_col])          if adx_col        in score_data.columns and date in score_data.index else 25.0

                close_now = row.get(sym)
                close_now = float(close_now) if close_now is not None and pd.notna(close_now) else None

                # ADX quality gate: require minimum trend strength before entry.
                # Crypto uses a higher bar (> 25) — BTC/ETH generate many false
                # EMA crossovers in sideways markets. Stocks use a soft floor (> 20)
                # to filter true sideways chop while allowing moderate-trend entries
                # (e.g., QQQ/TSLA momentum trades have ADX 22-35).
                asset_type_now = _ASSET_TYPE.get(sym, "stock")
                adx_min = 25.0 if asset_type_now == "crypto" else 20.0
                if adx_val < adx_min:
                    continue

                # Long filter: close > EMA200 AND within 8% of 52-week high
                # (avoids longs on mean-reversion bounces in declining sectors;
                #  only trades instruments in confirmed uptrends near recent highs)
                _HIGH_PROXIMITY = 0.92   # close must be > 92% of 252-day high (within 8% of year high)
                above_ema200 = (close_now is not None and not np.isnan(ema200_val)
                                and close_now > ema200_val)
                near_52w_high = (close_now is not None and not np.isnan(roll_high)
                                 and roll_high > 0 and close_now > _HIGH_PROXIMITY * roll_high)
                # Short filter: close < EMA200 AND near 52-week low (confirmed downtrend)
                _LOW_PROXIMITY = 1.15    # close must be < 115% of 252-day low
                below_ema200 = (close_now is not None and not np.isnan(ema200_val)
                                and close_now < ema200_val)
                near_52w_low = (close_now is not None and not np.isnan(roll_low)
                                and roll_low > 0 and close_now < _LOW_PROXIMITY * roll_low)

                # Sector macro gate (mirrors live MacroContext logic):
                #   Energy stocks: require oil uptrend (USO EMA20 > EMA60) for longs,
                #   oil downtrend for shorts. Falls through to True if data unavailable.
                sector = _SECTOR.get(sym, "")
                oil_up_series = macro_ctx.get("oil_uptrend")
                oil_uptrend: bool = True   # default: allow (neutral macro)
                if oil_up_series is not None:
                    try:
                        # Find nearest available date ≤ current date
                        available = oil_up_series.index[oil_up_series.index <= date]
                        if len(available) > 0:
                            oil_uptrend = bool(oil_up_series.loc[available[-1]])
                    except Exception:
                        pass

                if sector == "energy":
                    if direction == 1 and not oil_uptrend:
                        continue   # skip energy long when oil is in downtrend
                    if direction == -1 and oil_uptrend:
                        continue   # skip energy short when oil is in uptrend

                if (direction == 1 and regime >= 0 and above_ema200 and near_52w_high
                        and score >= self.threshold):
                    long_candidates.append((sym, score, 1))
                elif (direction == -1 and regime < 0 and below_ema200 and near_52w_low
                      and score >= self.threshold + _SHORT_THRESHOLD_PREMIUM):
                    short_candidates.append((sym, score, -1))

            long_candidates.sort(key=lambda x: x[1], reverse=True)
            short_candidates.sort(key=lambda x: x[1], reverse=True)

            # Longs first, then shorts (fill remaining slots with shorts)
            all_candidates = long_candidates + short_candidates
            target_positions: dict[str, int] = self._select_top(all_candidates)

            # ── Manage existing positions ─────────────────────────────────
            for sym, pos in list(positions.items()):
                pos["hold_count"] += 1
                close_px = row.get(sym)
                high_px  = high_row.get(sym)
                low_px   = low_row.get(sym)

                if close_px is None or pd.isna(close_px):
                    continue

                close_px = float(close_px)
                asset_type = _ASSET_TYPE.get(sym, "stock")
                fee        = _FEES.get(asset_type, 0.001)
                direction  = pos["direction"]
                entry_px   = pos["entry_px"]
                sl_level   = pos["sl_level"]
                tp_level   = pos["tp_level"]
                trail_mult = _ATR_TRAIL_MULT.get(asset_type, 0.0)
                atr_col    = f"{sym}_atr"
                atr_val    = (
                    float(score_data.loc[date, atr_col])
                    if atr_col in score_data.columns and date in score_data.index
                       and pd.notna(score_data.loc[date, atr_col])
                    else entry_px * _FALLBACK_SL[asset_type]
                )
                if atr_val <= 0:
                    atr_val = entry_px * _FALLBACK_SL[asset_type]

                exit_px = None
                sl_hit  = False

                if direction == 1:
                    # Long position
                    # Update trailing best price
                    pos["best_price"] = max(pos.get("best_price", entry_px), close_px)
                    trail_level = (pos["best_price"] - trail_mult * atr_val
                                   if trail_mult > 0 else -np.inf)

                    low_px_f = float(low_px) if pd.notna(low_px) else close_px
                    high_px_f = float(high_px) if pd.notna(high_px) else close_px

                    if low_px_f <= sl_level:
                        exit_px = sl_level * (1 - fee / 2)
                        sl_hit  = True
                    elif high_px_f >= tp_level:
                        exit_px = tp_level * (1 - fee / 2)
                    elif trail_mult > 0 and close_px < trail_level:
                        exit_px = close_px * (1 - fee / 2)
                    elif sym not in target_positions or target_positions.get(sym) != 1:
                        exit_px = close_px * (1 - fee / 2)

                else:
                    # Short position
                    # Update trailing best price (for short, best = lowest price seen)
                    pos["best_price"] = min(pos.get("best_price", entry_px), close_px)
                    trail_level = (pos["best_price"] + trail_mult * atr_val
                                   if trail_mult > 0 else np.inf)

                    low_px_f  = float(low_px)  if pd.notna(low_px)  else close_px
                    high_px_f = float(high_px) if pd.notna(high_px) else close_px

                    if high_px_f >= sl_level:
                        exit_px = sl_level * (1 + fee / 2)   # buy back at SL (higher)
                        sl_hit  = True
                    elif low_px_f <= tp_level:
                        exit_px = tp_level * (1 + fee / 2)   # buy back at TP (lower)
                    elif trail_mult > 0 and close_px > trail_level:
                        exit_px = close_px * (1 + fee / 2)
                    elif sym not in target_positions or target_positions.get(sym) != -1:
                        exit_px = close_px * (1 + fee / 2)

                if exit_px is not None:
                    if sl_hit:
                        sl_cooldown[sym] = _SL_COOLDOWN_BARS

                    alloc   = pos["alloc"]
                    shares  = pos["shares"]
                    if direction == 1:
                        pnl = shares * (exit_px - entry_px)
                    else:
                        pnl = shares * (entry_px - exit_px)

                    cash += alloc + pnl   # return margin + profit/loss
                    closed_trades.append(ClosedTrade(
                        symbol=sym,
                        direction=direction,
                        entry_date=pos["entry_date"],
                        exit_date=date,
                        entry_px=entry_px,
                        exit_px=exit_px,
                        pnl_usd=pnl,
                    ))
                    del positions[sym]

            # ── Open new positions ─────────────────────────────────────────
            for sym, direction in target_positions.items():
                if sym in positions:
                    continue
                if sl_cooldown.get(sym, 0) > 0:
                    continue
                if len(positions) >= self.MAX_POSITIONS:
                    break

                px = row.get(sym)
                if px is None or pd.isna(px):
                    continue

                asset_type = _ASSET_TYPE.get(sym, "stock")
                fee        = _FEES.get(asset_type, 0.001)

                if direction == 1:
                    entry_px = float(px) * (1 + fee / 2)
                else:
                    entry_px = float(px) * (1 - fee / 2)   # short entry: sell at slightly lower

                atr_col = f"{sym}_atr"
                atr_val = (
                    float(score_data.loc[date, atr_col])
                    if atr_col in score_data.columns and date in score_data.index
                       and pd.notna(score_data.loc[date, atr_col])
                    else entry_px * _FALLBACK_SL[asset_type]
                )
                if atr_val <= 0:
                    atr_val = entry_px * _FALLBACK_SL[asset_type]

                # ── Volatility-adjusted sizing (target 2% ATR per slot) ────────
                # High-vol stocks (TSLA ~5% ATR, NVDA ~3%) get proportionally smaller
                # allocations so one volatile instrument can't dominate risk.
                _TARGET_VOL_PCT = 0.02
                atr_pct = atr_val / entry_px if entry_px > 0 else _TARGET_VOL_PCT
                vol_scale = min(1.0, _TARGET_VOL_PCT / atr_pct) if atr_pct > 0 else 1.0
                vol_scale = max(0.35, vol_scale)   # floor at 35% of slot
                reserve    = self.TOTAL_CAPITAL * self.CASH_RESERVE_PCT
                alloc      = min(per_slot * vol_scale, max(0.0, cash - reserve))
                if alloc < 10:
                    continue

                sl_mult = _ATR_SL_MULT[asset_type]
                tp_mult = _ATR_TP_MULT[asset_type]

                if direction == 1:
                    sl_level = entry_px - sl_mult * atr_val
                    tp_level = entry_px + tp_mult * atr_val
                else:
                    sl_level = entry_px + sl_mult * atr_val   # SL above entry for shorts
                    tp_level = entry_px - tp_mult * atr_val   # TP below entry for shorts

                shares = alloc / entry_px
                cash  -= alloc   # deduct margin/cost

                positions[sym] = {
                    "direction":  direction,
                    "entry_px":   entry_px,
                    "shares":     shares,
                    "alloc":      alloc,
                    "entry_date": date,
                    "hold_count": 0,
                    "sl_level":   sl_level,
                    "tp_level":   tp_level,
                    "best_price": entry_px,
                }

            # ── Mark total equity ─────────────────────────────────────────
            pos_value = 0.0
            for sym, pos in positions.items():
                px = row.get(sym)
                if px is None or pd.isna(px):
                    pos_value += pos["alloc"]   # hold at cost if no price
                    continue
                close_px = float(px)
                if pos["direction"] == 1:
                    # Long: value = shares * current price
                    pos_value += pos["shares"] * close_px
                else:
                    # Short: value = alloc + unrealised P&L
                    # P&L = shares * (entry_px - close_px); positive when price fell
                    pos_value += pos["alloc"] + pos["shares"] * (pos["entry_px"] - close_px)

            equity_vals.append(cash + pos_value)
            position_counts.append(len(positions))

        # Close any remaining open positions at last price
        last_row = close_matrix.iloc[-1]
        for sym, pos in positions.items():
            px = last_row.get(sym)
            if px is not None and not pd.isna(px):
                asset_type = _ASSET_TYPE.get(sym, "stock")
                fee        = _FEES.get(asset_type, 0.001)
                direction  = pos["direction"]
                if direction == 1:
                    exit_px = float(px) * (1 - fee / 2)
                    pnl     = pos["shares"] * (exit_px - pos["entry_px"])
                else:
                    exit_px = float(px) * (1 + fee / 2)
                    pnl     = pos["shares"] * (pos["entry_px"] - exit_px)
                cash += pos["alloc"] + pnl
                closed_trades.append(ClosedTrade(
                    symbol=sym,
                    direction=direction,
                    entry_date=pos["entry_date"],
                    exit_date=close_matrix.index[-1],
                    entry_px=pos["entry_px"],
                    exit_px=exit_px,
                    pnl_usd=pnl,
                ))

        # ── Compute metrics ────────────────────────────────────────────────
        equity        = pd.Series(equity_vals, index=close_matrix.index)
        daily_returns = equity.pct_change().dropna()

        sharpe = (
            float(daily_returns.mean() / daily_returns.std() * np.sqrt(252))
            if daily_returns.std() > 0 else 0.0
        )
        max_dd       = self._max_drawdown(equity)
        total_return = (equity.iloc[-1] - self.TOTAL_CAPITAL) / self.TOTAL_CAPITAL

        wins         = [t for t in closed_trades if t.pnl_usd > 0]
        win_rate     = len(wins) / len(closed_trades) if closed_trades else 0.0
        gross_profit = sum(t.pnl_usd for t in wins)
        gross_loss   = sum(abs(t.pnl_usd) for t in closed_trades if t.pnl_usd < 0)
        pf           = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        avg_pos      = float(np.mean(position_counts)) if position_counts else 0.0

        return PortfolioResult(
            start=start,
            end=end,
            symbols=symbols,
            sharpe=sharpe,
            win_rate=win_rate,
            profit_factor=min(pf, 99.0),
            max_drawdown=max_dd,
            total_return=total_return,
            trade_count=len(closed_trades),
            avg_positions=avg_pos,
            equity_curve=equity,
            trades=closed_trades,
        )

    def _select_top(self, ranked: list[tuple[str, float, int]]) -> dict[str, int]:
        """
        Greedily pick up to MAX_POSITIONS non-correlated symbols.
        Returns {symbol: direction} where direction = +1 (long) or -1 (short).
        """
        selected: dict[str, int] = {}
        for sym, score, direction in ranked:
            if len(selected) >= self.MAX_POSITIONS:
                break
            correlated = any(
                frozenset({sym, s}) in CORRELATION_BLACKLIST for s in selected
            )
            if not correlated:
                selected[sym] = direction
        return selected

    @staticmethod
    def _max_drawdown(equity: pd.Series) -> float:
        roll_max = equity.cummax()
        dd = (equity - roll_max) / roll_max
        return float(abs(dd.min()))

    @staticmethod
    def _empty_result(symbols, start, end) -> PortfolioResult:
        return PortfolioResult(
            start=start, end=end, symbols=symbols,
            sharpe=0.0, win_rate=0.0, profit_factor=0.0,
            max_drawdown=0.0, total_return=0.0, trade_count=0,
            avg_positions=0.0, equity_curve=pd.Series(dtype=float),
        )
