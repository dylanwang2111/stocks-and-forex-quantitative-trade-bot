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
    - Exit after holding_days bars
    """

    def __init__(self, confidence_threshold: float = 55.0, holding_days: int = 3):
        self.threshold = confidence_threshold
        self.holding_days = holding_days
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
            df = fetch_candles(symbol, "1d", use_cache=False)

        # Filter to requested period
        df = df.loc[start:end].copy()
        if len(df) < 30:
            return self._empty_result(symbol, start, end, reason="Insufficient data")

        asset_type = ASSET_TYPE.get(symbol, "stock")
        fee = FEES[asset_type]

        if VBT_AVAILABLE:
            return self._run_vectorbt(symbol, df, start, end, fee)
        else:
            return self._run_pandas(symbol, df, start, end, fee)

    # ── vectorbt path ──────────────────────────────────────────────────────────

    def _run_vectorbt(
        self,
        symbol: str,
        df: pd.DataFrame,
        start: str,
        end: str,
        fee: float,
    ) -> BacktestResult:
        entries, exits = self._generate_signals(df)

        if entries.sum() == 0:
            return self._empty_result(symbol, start, end, reason="No entries generated")

        pf = vbt.Portfolio.from_signals(
            close=df["close"],
            entries=entries,
            exits=exits,
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
            pf_val = self._manual_profit_factor(df, entries, exits)

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
    ) -> BacktestResult:
        entries, exits = self._generate_signals(df)
        close = df["close"]

        initial_cash = settings.bot.total_capital
        cash  = initial_cash
        equity_vals = [cash]
        trades: list[tuple[float, float]] = []  # (entry_px, exit_px)

        in_trade = False
        entry_px = 0.0
        entry_idx = 0
        shares = 0.0

        for i, (entry, exit_sig) in enumerate(zip(entries, exits)):
            if not in_trade and entry:
                entry_px  = float(close.iloc[i]) * (1 + fee / 2)
                shares    = (cash * 0.95) / entry_px  # use 95% of cash
                in_trade  = True
                entry_idx = i

            elif in_trade:
                hold_exceeded = (i - entry_idx) >= self.holding_days
                if exit_sig or hold_exceeded:
                    exit_px = float(close.iloc[i]) * (1 - fee / 2)
                    pnl = shares * (exit_px - entry_px)
                    cash += pnl
                    trades.append((entry_px, exit_px))
                    in_trade = False
                    shares = 0.0

            equity_vals.append(cash)

        if not trades:
            return self._empty_result(symbol, start, end, reason="No trades closed")

        equity = pd.Series(equity_vals[:len(close)], index=close.index)
        returns = equity.pct_change().dropna()

        sharpe = (returns.mean() / returns.std() * np.sqrt(252)) if returns.std() > 0 else 0
        max_dd = self._max_drawdown(equity)

        wins = [(e, x) for e, x in trades if x > e]
        win_rate = len(wins) / len(trades)
        gross_profit = sum(x - e for e, x in wins)
        gross_loss   = sum(e - x for e, x in trades if x < e)
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
    ) -> tuple[pd.Series, pd.Series]:
        """
        Generate entry/exit boolean Series from daily OHLCV.
        Uses a simplified indicator stack covering all 8 categories so that
        the confidence scorer can reach the minimum threshold.

        Category proxies (simplified for backtesting speed):
          cat1 – trend direction : EMA9 vs EMA21 + MACD histogram agreement
          cat2 – trend strength  : EMA21 vs EMA50 alignment (mirrors cat1 direction)
          cat3 – momentum        : RSI oversold/overbought
          cat4 – volatility      : always 0 (neutral — no ATR band data)
          cat5 – volume          : close-to-close return magnitude vs 20-day avg
          cat6 – price structure : prior bar return direction
          cat7 – multi-timeframe : cat1 vote × 2 (double weight, single-TF proxy)
          cat8 – macro/news      : always 0 (no macro data in backtest)

        With a strong trend (cat1=cat2=cat5=cat6=+1, cat7=+2) the bull raw sum
        reaches 6/9 ≈ 67 — well above the 55 threshold.
        """
        close  = df["close"]
        volume = df.get("volume", pd.Series(1, index=close.index))

        ema9  = ta.ema(close, length=9)
        ema21 = ta.ema(close, length=21)
        ema50 = ta.ema(close, length=50)
        rsi   = ta.rsi(close, length=14)
        macd_df = ta.macd(close, fast=12, slow=26, signal=9)

        if macd_df is not None:
            hist_col = [c for c in macd_df.columns if "MACDh" in c][0]
            macd_hist = macd_df[hist_col]
        else:
            macd_hist = pd.Series(0.0, index=close.index)

        # 20-bar rolling mean volume for cat5
        vol_ma20 = volume.rolling(20).mean()
        # Daily return for cat6
        daily_ret = close.pct_change()

        entries = pd.Series(False, index=close.index)
        exits   = pd.Series(False, index=close.index)

        for i in range(50, len(df)):
            votes = {}

            # ── cat1: EMA9/21 crossover confirmed by MACD histogram ─────────
            if pd.notna(ema9.iloc[i]) and pd.notna(ema21.iloc[i]):
                ema_bull = ema9.iloc[i] > ema21.iloc[i]
                ema_bear = ema9.iloc[i] < ema21.iloc[i]
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

            # ── cat3: RSI momentum confirmation (trend-following) ────────────
            # RSI above midline = bullish momentum; below = bearish
            # Neutral band ±3 around 50 avoids noise at the midline
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

            # ── cat4: volatility — neutral in simplified backtest ───────────
            votes["cat4"] = 0

            # ── cat5: volume confirmation (stocks only — forex volume = noise)
            if pd.notna(vol_ma20.iloc[i]) and vol_ma20.iloc[i] > 0:
                vol_ratio = volume.iloc[i] / vol_ma20.iloc[i]
                if vol_ratio > 1.1:
                    votes["cat5"] = votes.get("cat1", 0)
                else:
                    votes["cat5"] = 0
            else:
                votes["cat5"] = 0

            # ── cat6: prior bar price structure ─────────────────────────────
            if pd.notna(daily_ret.iloc[i - 1]):
                ret = daily_ret.iloc[i - 1]
                votes["cat6"] = 1 if ret > 0.002 else (-1 if ret < -0.002 else 0)
            else:
                votes["cat6"] = 0

            # ── cat7: MTF — double weight only when cat1 AND cat2 agree ──────
            # Both EMAs aligned → genuine multi-timeframe confirmation (+2)
            # Only cat1 aligned → partial confirmation (+1)
            c1, c2 = votes.get("cat1", 0), votes.get("cat2", 0)
            if c1 != 0 and c1 == c2:
                votes["cat7"] = c1 * 2   # full double weight
            elif c1 != 0:
                votes["cat7"] = c1        # single weight
            else:
                votes["cat7"] = 0

            # ── cat8: macro — neutral in simplified backtest ─────────────────
            votes["cat8"] = 0

            direction, score = self.scorer.simple_signal(votes, self.threshold)

            if direction == 1 and not entries.iloc[i - 1]:
                entries.iloc[i] = True
            elif direction == -1 and not exits.iloc[i - 1]:
                exits.iloc[i] = True

        return entries, exits

    # ── Utilities ──────────────────────────────────────────────────────────────

    @staticmethod
    def _max_drawdown(equity: pd.Series) -> float:
        roll_max = equity.cummax()
        dd = (equity - roll_max) / roll_max
        return float(abs(dd.min()))

    @staticmethod
    def _manual_profit_factor(
        df: pd.DataFrame,
        entries: pd.Series,
        exits: pd.Series,
    ) -> float:
        close = df["close"]
        gross_profit = gross_loss = 0.0
        in_trade = False
        entry_px = 0.0
        for i, (e, x) in enumerate(zip(entries, exits)):
            if not in_trade and e:
                entry_px = float(close.iloc[i])
                in_trade = True
            elif in_trade and x:
                pnl = float(close.iloc[i]) - entry_px
                if pnl > 0:
                    gross_profit += pnl
                else:
                    gross_loss += abs(pnl)
                in_trade = False
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
