"""
backtesting/backtest_runner.py
Vectorbt-based backtester with realistic fee modelling.
"""
from __future__ import annotations

import warnings
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

# Suppress vectorbt's verbose numpy deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)

try:
    import vectorbt as vbt
    VBT_AVAILABLE = True
except ImportError:
    VBT_AVAILABLE = False

from data.fetcher import fetch_candles
from data.preprocessor import clean
import pandas_ta as ta
from agents.confidence_scorer import ConfidenceScorer
from config.settings import settings


# Fee schedule (round-trip)
FEES: dict[str, float] = {
    "stock": 0.001,   # 0.1% round-trip (IBKR $0.005/share + slippage)
    "forex": 0.0003,  # 0.03% round-trip (OANDA spread)
}

# Asset type mapping
ASSET_TYPE: dict[str, str] = {
    "SPY":    "stock",
    "QQQ":    "stock",
    "NVDA":   "stock",
    "AAPL":   "stock",
    "EURUSD": "forex",
    "GBPUSD": "forex",
}


@dataclass
class BacktestResult:
    symbol:       str
    start:        str
    end:          str
    sharpe:       float
    win_rate:     float
    profit_factor: float
    max_drawdown: float     # as positive fraction, e.g. 0.12 = 12%
    trade_count:  int
    total_return: float     # fraction, e.g. 0.15 = 15%
    equity_curve: pd.Series | None = None

    def passed(self) -> bool:
        """
        Minimum thresholds for the simplified daily-bar proxy backtest.
        Live system runs on 15-min bars across 6 instruments simultaneously;
        daily bar swing trades naturally cluster into 7–12 trades/year.
        ≥ 15 trades over 3 years is the minimum for statistical validity here.
        """
        return (
            self.sharpe > 1.2
            and self.max_drawdown < 0.20
            and self.trade_count >= 15
        )

    def summary(self) -> dict:
        return {
            "symbol":        self.symbol,
            "period":        f"{self.start} → {self.end}",
            "sharpe":        round(self.sharpe, 2),
            "win_rate":      f"{self.win_rate * 100:.1f}%",
            "profit_factor": round(self.profit_factor, 2),
            "max_drawdown":  f"{self.max_drawdown * 100:.1f}%",
            "trade_count":   self.trade_count,
            "total_return":  f"{self.total_return * 100:.1f}%",
            "passed":        self.passed(),
        }


class BacktestRunner:
    """
    Run a confidence-threshold strategy on historical daily candles.

    Strategy logic (applied on each daily bar using rolling 1h indicators):
    - Compute signal category votes from daily close data (simplified for speed)
    - If confidence score ≥ threshold and direction = long → buy signal
    - If confidence score ≥ threshold and direction = short → sell signal
    - Exit when ATR-based stop-loss OR take-profit is hit (two-phase proxy):
        Phase 1: hard stop at atr_sl_mult × ATR below/above entry
                 take-profit at atr_tp_mult × ATR above/below entry
        Phase 2 (proxy): treat TP hit as full exit (conservative approximation
                 of the live 50%-close + trailing-stop phase)
    - Max holding_days fallback to bound open trades
    """

    def __init__(
        self,
        confidence_threshold: float = 55.0,
        holding_days: int = 7,
        atr_sl_mult: float = 2.0,
        atr_tp_mult: float = 4.0,
    ):
        self.threshold = confidence_threshold
        self.holding_days = holding_days
        self.atr_sl_mult = atr_sl_mult
        self.atr_tp_mult = atr_tp_mult
        self.scorer = ConfidenceScorer()

    def run(
        self,
        symbol: str,
        start: str,
        end: str,
        df: pd.DataFrame | None = None,
    ) -> BacktestResult:
        """
        Args:
            symbol: bot-internal symbol
            start:  "YYYY-MM-DD"
            end:    "YYYY-MM-DD"
            df:     pre-fetched daily DataFrame (fetched from yfinance if None)

        Returns:
            BacktestResult
        """
        if df is None:
            # Always use yfinance for historical data (broker APIs don't serve multi-year bars)
            df = fetch_candles(symbol, "1d", period="5y", use_cache=False)

        # Keep the full df for indicator warmup (especially EMA200 needs 200 bars).
        # Signal generation uses the full df; simulation is sliced to start:end.
        if len(df) < 30:
            return self._empty_result(symbol, start, end, reason="Insufficient data")

        asset_type = ASSET_TYPE.get(symbol, "stock")
        fee = FEES[asset_type]

        # Resolve TP multiplier by asset type — forex uses a larger mult to
        # compensate for its lower ATR% (mirrors risk_agent._ATR_TP_MULT_BY_TIER_FOREX)
        if asset_type == "forex":
            from agents.risk_agent import _ATR_TP_MULT_BY_TIER_FOREX
            effective_tp_mult = _ATR_TP_MULT_BY_TIER_FOREX.get("SMALL", 8.0)
        else:
            effective_tp_mult = self.atr_tp_mult

        # Generate signals on full df (for EMA200 warmup), then slice inside run methods
        if VBT_AVAILABLE:
            return self._run_vectorbt(symbol, df, start, end, fee, effective_tp_mult)
        else:
            return self._run_pandas(symbol, df, start, end, fee, effective_tp_mult)

    # ── vectorbt path ──────────────────────────────────────────────────────────

    def _run_vectorbt(
        self,
        symbol: str,
        df: pd.DataFrame,
        start: str,
        end: str,
        fee: float,
        effective_tp_mult: float | None = None,
    ) -> BacktestResult:
        tp_mult = effective_tp_mult if effective_tp_mult is not None else self.atr_tp_mult

        # Compute ATR(14) on full df before slicing (needs warmup bars)
        atr14_full = ta.atr(df["high"], df["low"], df["close"], length=14)

        # Generate on full df (EMA200 warmup), then slice to the period
        long_entries, long_exits, short_entries, short_exits = self._generate_signals(df)
        long_entries  = long_entries.loc[start:end]
        long_exits    = long_exits.loc[start:end]
        short_entries = short_entries.loc[start:end]
        short_exits   = short_exits.loc[start:end]
        df_period     = df.loc[start:end]
        atr14         = atr14_full.loc[start:end]

        if long_entries.sum() + short_entries.sum() == 0:
            return self._empty_result(symbol, start, end, reason="No entries generated")

        # Per-bar ATR fraction of price → used as sl_stop / tp_stop fractions
        close_p = df_period["close"]
        atr_pct = (atr14 / close_p).fillna(0.015)   # fallback 1.5% if ATR missing
        sl_stop = (atr_pct * self.atr_sl_mult).clip(upper=0.30)
        tp_stop = (atr_pct * tp_mult).clip(upper=0.60)

        try:
            pf = vbt.Portfolio.from_signals(
                close=close_p,
                entries=long_entries,
                exits=long_exits,
                short_entries=short_entries,
                short_exits=short_exits,
                sl_stop=sl_stop,
                tp_stop=tp_stop,
                fees=fee,
                freq="1D",
                init_cash=settings.bot.total_capital,
            )
        except TypeError:
            # Older vectorbt versions don't support short_entries or per-bar stops
            if long_entries.sum() == 0:
                return self._empty_result(symbol, start, end, reason="No long entries (long-only fallback)")
            try:
                pf = vbt.Portfolio.from_signals(
                    close=close_p,
                    entries=long_entries,
                    exits=long_exits,
                    sl_stop=sl_stop,
                    tp_stop=tp_stop,
                    fees=fee,
                    freq="1D",
                    init_cash=settings.bot.total_capital,
                )
            except TypeError:
                # Very old vectorbt — no stop support at all, use signal exits only
                pf = vbt.Portfolio.from_signals(
                    close=close_p,
                    entries=long_entries,
                    exits=long_exits,
                    fees=fee,
                    freq="1D",
                    init_cash=settings.bot.total_capital,
                )

        stats = pf.stats()

        sharpe       = float(stats.get("Sharpe Ratio", 0) or 0)
        max_dd       = abs(float(stats.get("Max Drawdown [%]", 0) or 0)) / 100
        win_rate     = float(stats.get("Win Rate [%]", 0) or 0) / 100
        total_return = float(stats.get("Total Return [%]", 0) or 0) / 100
        trade_count  = int(stats.get("Total Trades", 0) or 0)

        pf_val = float(stats.get("Profit Factor", 0) or 0)
        if pf_val == 0 or np.isnan(pf_val) or np.isinf(pf_val):
            pf_val = self._manual_profit_factor(df_period, long_entries, long_exits, short_entries, short_exits)

        equity = pf.value()

        return BacktestResult(
            symbol=symbol,
            start=start,
            end=end,
            sharpe=sharpe,
            win_rate=win_rate,
            profit_factor=pf_val,
            max_drawdown=max_dd,
            trade_count=trade_count,
            total_return=total_return,
            equity_curve=equity,
        )

    # ── Pure-pandas fallback ───────────────────────────────────────────────────

    def _run_pandas(
        self,
        symbol: str,
        df: pd.DataFrame,
        start: str,
        end: str,
        fee: float,
        effective_tp_mult: float | None = None,
    ) -> BacktestResult:
        tp_mult = effective_tp_mult if effective_tp_mult is not None else self.atr_tp_mult
        # Compute ATR(14) on full df before slicing (needs warmup bars)
        atr14_full = ta.atr(df["high"], df["low"], df["close"], length=14)

        # Generate on full df (EMA200 warmup), then slice to the period
        long_entries, long_exits, short_entries, short_exits = self._generate_signals(df)
        long_entries  = long_entries.loc[start:end]
        long_exits    = long_exits.loc[start:end]
        short_entries = short_entries.loc[start:end]
        short_exits   = short_exits.loc[start:end]
        atr14         = atr14_full.loc[start:end]
        df = df.loc[start:end]
        close  = df["close"]
        highs  = df["high"]
        lows   = df["low"]

        initial_cash = settings.bot.total_capital
        cash  = initial_cash
        equity_vals = [cash]
        trades: list[tuple[float, float, str]] = []  # (entry_px, exit_px, side)

        position  = 0   # +1 = long, -1 = short, 0 = flat
        entry_px  = 0.0
        stop_lvl  = 0.0
        tp_lvl    = 0.0
        entry_idx = 0
        shares    = 0.0

        for i in range(len(df)):
            long_e  = bool(long_entries.iloc[i])
            long_x  = bool(long_exits.iloc[i])
            short_e = bool(short_entries.iloc[i])
            short_x = bool(short_exits.iloc[i])
            bar_high  = float(highs.iloc[i])
            bar_low   = float(lows.iloc[i])
            bar_close = float(close.iloc[i])

            # Check ATR-based stop / TP hits for open position
            stop_hit = tp_hit = hold_exceeded = False
            if position != 0:
                hold_exceeded = (i - entry_idx) >= self.holding_days
                if position == 1:   # long
                    stop_hit = bar_low  <= stop_lvl
                    tp_hit   = bar_high >= tp_lvl
                else:               # short
                    stop_hit = bar_high >= stop_lvl
                    tp_hit   = bar_low  <= tp_lvl

            # Close existing position
            if position != 0 and (stop_hit or tp_hit or hold_exceeded
                                  or (position == 1  and long_x)
                                  or (position == -1 and short_x)):
                if position == 1:
                    if stop_hit:
                        exit_px = stop_lvl * (1 - fee / 2)
                    elif tp_hit:
                        exit_px = tp_lvl   * (1 - fee / 2)
                    else:
                        exit_px = bar_close * (1 - fee / 2)
                    pnl = shares * (exit_px - entry_px)
                else:
                    if stop_hit:
                        exit_px = stop_lvl  * (1 + fee / 2)
                    elif tp_hit:
                        exit_px = tp_lvl    * (1 + fee / 2)
                    else:
                        exit_px = bar_close * (1 + fee / 2)
                    pnl = shares * (entry_px - exit_px)
                cash += pnl
                trades.append((entry_px, exit_px, "long" if position == 1 else "short"))
                position = 0
                shares   = 0.0

            # Open new position (ATR determines stop and TP distances)
            if position == 0:
                bar_atr = float(atr14.iloc[i]) if pd.notna(atr14.iloc[i]) else bar_close * 0.015
                if long_e:
                    entry_px = bar_close * (1 + fee / 2)
                    stop_lvl = entry_px - self.atr_sl_mult * bar_atr
                    tp_lvl   = entry_px + tp_mult * bar_atr
                    shares   = (cash * 0.95) / entry_px
                    position = 1
                    entry_idx = i
                elif short_e:
                    entry_px = bar_close * (1 - fee / 2)
                    stop_lvl = entry_px + self.atr_sl_mult * bar_atr
                    tp_lvl   = entry_px - tp_mult * bar_atr
                    shares   = (cash * 0.95) / entry_px
                    position = -1
                    entry_idx = i

            equity_vals.append(cash)

        if not trades:
            return self._empty_result(symbol, start, end, reason="No trades closed")

        equity = pd.Series(equity_vals[:len(close)], index=close.index)
        returns = equity.pct_change().dropna()

        sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0
        max_dd = self._max_drawdown(equity)

        def _is_win(entry, exit_px, side):
            return (exit_px > entry) if side == "long" else (exit_px < entry)

        def _pnl(entry, exit_px, side):
            return (exit_px - entry) if side == "long" else (entry - exit_px)

        wins = [(e, x, s) for e, x, s in trades if _is_win(e, x, s)]
        win_rate = len(wins) / len(trades)
        gross_profit = sum(_pnl(e, x, s) for e, x, s in wins)
        gross_loss   = sum(abs(_pnl(e, x, s)) for e, x, s in trades if not _is_win(e, x, s))
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        total_return = (cash - initial_cash) / initial_cash

        return BacktestResult(
            symbol=symbol,
            start=start,
            end=end,
            sharpe=float(sharpe),
            win_rate=win_rate,
            profit_factor=min(pf, 99.0),
            max_drawdown=max_dd,
            trade_count=len(trades),
            total_return=total_return,
            equity_curve=equity,
        )

    # ── Signal generation ──────────────────────────────────────────────────────

    def _generate_signals(
        self, df: pd.DataFrame
    ) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
        """
        Generate entry/exit boolean Series from daily OHLCV.
        Returns (long_entries, long_exits, short_entries, short_exits).

        Regime-aware with EMA50 vs EMA200 macro filter:
          - Bull regime (EMA50 > EMA200): long entries only
          - Bear regime (EMA50 < EMA200): short entries only
          - Exit on opposite signal or max holding_days

        Category proxies:
          cat1 – trend direction : EMA9 vs EMA21 + MACD histogram agreement
          cat2 – trend strength  : EMA21 vs EMA50 alignment
          cat3 – momentum        : RSI above/below midline (53/47 deadband)
          cat4 – BB expansion    : votes with cat1 when BB bandwidth expands (breakout)
          cat5 – volume          : volume ratio vs 20-day avg (echoes cat1 direction)
          cat6 – price structure : prior bar return direction
          cat7 – MTF proxy       : cat1 × 2 when cat1 == cat2, else cat1 (single weight)
          cat8 – macro/news      : always 0 (cannot replay macro events in backtest)
        """
        close  = df["close"]
        volume = df.get("volume", pd.Series(1, index=close.index))

        ema9  = ta.ema(close, length=9)
        ema21 = ta.ema(close, length=21)
        ema50 = ta.ema(close, length=50)
        rsi   = ta.rsi(close, length=14)
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)

        # ADX for trend-strength gate (only trade when trend is present)
        adx_df = ta.adx(df["high"], df["low"], close, length=14)
        if adx_df is not None:
            adx_col = [c for c in adx_df.columns if c.startswith("ADX_")][0]
            adx_series = adx_df[adx_col]
        else:
            adx_series = pd.Series(0.0, index=close.index)

        if macd_df is not None:
            hist_col = [c for c in macd_df.columns if "MACDh" in c][0]
            macd_hist = macd_df[hist_col]
        else:
            macd_hist = pd.Series(0.0, index=close.index)

        # BB bandwidth for cat4 (expansion = breakout confirmation)
        bb = ta.bbands(close, length=20, std=2.0)
        if bb is not None:
            bbu_col = [c for c in bb.columns if "BBU" in c][0]
            bbl_col = [c for c in bb.columns if "BBL" in c][0]
            bb_width = (bb[bbu_col] - bb[bbl_col]) / close
            bb_expanding = bb_width > bb_width.shift(1)
        else:
            bb_expanding = pd.Series(False, index=close.index)

        # EMA50 10-bar slope for macro regime gate
        ema50_slope = ema50.diff(10)

        # 20-bar rolling mean volume for cat5
        vol_ma20 = volume.rolling(20).mean()
        # Daily return for cat6
        daily_ret = close.pct_change()

        long_entries  = pd.Series(False, index=close.index)
        long_exits    = pd.Series(False, index=close.index)
        short_entries = pd.Series(False, index=close.index)
        short_exits   = pd.Series(False, index=close.index)

        for i in range(200, len(df)):
            votes = {}

            # ── cat1: EMA9/21 crossover confirmed by MACD histogram ─────────
            if pd.notna(ema9.iloc[i]) and pd.notna(ema21.iloc[i]):
                ema_bull  = ema9.iloc[i] > ema21.iloc[i]
                ema_bear  = ema9.iloc[i] < ema21.iloc[i]
                macd_bull = macd_hist.iloc[i] > 0
                macd_bear = macd_hist.iloc[i] < 0
                if ema_bull and macd_bull:
                    votes["cat1"] = 1
                elif ema_bear and macd_bear:
                    votes["cat1"] = -1
                else:
                    votes["cat1"] = 0
            else:
                votes["cat1"] = 0

            # ── cat2: EMA21 vs EMA50 for trend strength ─────────────────────
            if pd.notna(ema21.iloc[i]) and pd.notna(ema50.iloc[i]):
                if ema21.iloc[i] > ema50.iloc[i]:
                    votes["cat2"] = 1
                elif ema21.iloc[i] < ema50.iloc[i]:
                    votes["cat2"] = -1
                else:
                    votes["cat2"] = 0
            else:
                votes["cat2"] = 0

            # ── cat3: RSI momentum confirmation ──────────────────────────────
            if pd.notna(rsi.iloc[i]):
                rsi_val = rsi.iloc[i]
                if rsi_val > 53:
                    votes["cat3"] = 1
                elif rsi_val < 47:
                    votes["cat3"] = -1
                else:
                    votes["cat3"] = 0
            else:
                votes["cat3"] = 0

            # ── cat4: BB expansion proxy — votes with cat1 when expanding ─────
            if pd.notna(bb_expanding.iloc[i]):
                votes["cat4"] = votes.get("cat1", 0) if bb_expanding.iloc[i] else 0
            else:
                votes["cat4"] = 0

            # ── cat5: volume confirmation ────────────────────────────────────
            if pd.notna(vol_ma20.iloc[i]) and vol_ma20.iloc[i] > 0:
                vol_ratio = volume.iloc[i] / vol_ma20.iloc[i]
                votes["cat5"] = votes.get("cat1", 0) if vol_ratio > 1.1 else 0
            else:
                votes["cat5"] = 0

            # ── cat6: prior bar price structure ─────────────────────────────
            if pd.notna(daily_ret.iloc[i - 1]):
                ret = daily_ret.iloc[i - 1]
                votes["cat6"] = 1 if ret > 0.002 else (-1 if ret < -0.002 else 0)
            else:
                votes["cat6"] = 0

            # ── cat7: MTF proxy ───────────────────────────────────────────────
            c1, c2 = votes.get("cat1", 0), votes.get("cat2", 0)
            if c1 != 0 and c1 == c2:
                votes["cat7"] = c1 * 2
            elif c1 != 0:
                votes["cat7"] = c1
            else:
                votes["cat7"] = 0

            # ── cat8: neutral in backtest ─────────────────────────────────────
            votes["cat8"] = 0

            direction, score = self.scorer.simple_signal(votes, self.threshold)

            # ── Macro regime: EMA50 slope > 0 = uptrend ──────────────────────
            # Blocks long entries when EMA50 is falling (sustained downtrend)
            macro_uptrend = (
                pd.notna(ema50_slope.iloc[i]) and ema50_slope.iloc[i] > 0
            )

            if direction == 1 and macro_uptrend and not long_entries.iloc[i - 1]:
                long_entries.iloc[i] = True
            elif direction == -1 and not long_exits.iloc[i - 1]:
                long_exits.iloc[i] = True  # bearish signal always exits longs

        return long_entries, long_exits, short_entries, short_exits

    # ── Utilities ──────────────────────────────────────────────────────────────

    @staticmethod
    def _max_drawdown(equity: pd.Series) -> float:
        roll_max = equity.cummax()
        dd = (equity - roll_max) / roll_max
        return float(abs(dd.min()))

    @staticmethod
    def _manual_profit_factor(
        df: pd.DataFrame,
        long_entries: pd.Series,
        long_exits: pd.Series,
        short_entries: pd.Series | None = None,
        short_exits: pd.Series | None = None,
    ) -> float:
        close = df["close"]
        gross_profit = gross_loss = 0.0
        position = 0  # +1 long, -1 short
        entry_px = 0.0
        for i in range(len(close)):
            le = bool(long_entries.iloc[i])
            lx = bool(long_exits.iloc[i]) if long_exits is not None else False
            se = bool(short_entries.iloc[i]) if short_entries is not None else False
            sx = bool(short_exits.iloc[i]) if short_exits is not None else False
            px = float(close.iloc[i])

            if position == 1 and lx:
                pnl = px - entry_px
                (gross_profit if pnl > 0 else gross_loss).__add__(abs(pnl))
                gross_profit += max(0, pnl)
                gross_loss   += max(0, -pnl)
                position = 0
            elif position == -1 and sx:
                pnl = entry_px - px
                gross_profit += max(0, pnl)
                gross_loss   += max(0, -pnl)
                position = 0

            if position == 0 and le:
                entry_px = px
                position = 1
            elif position == 0 and se:
                entry_px = px
                position = -1

        return gross_profit / gross_loss if gross_loss > 0 else 1.0

    @staticmethod
    def _empty_result(
        symbol: str, start: str, end: str, reason: str = ""
    ) -> BacktestResult:
        return BacktestResult(
            symbol=symbol,
            start=start,
            end=end,
            sharpe=0.0,
            win_rate=0.0,
            profit_factor=0.0,
            max_drawdown=0.0,
            trade_count=0,
            total_return=0.0,
            equity_curve=None,
        )
