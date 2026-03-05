"""
backtesting/walk_forward.py
Walk-forward validation across 3 non-overlapping annual periods (2022, 2023, 2024).
Prints consistency table and statistical check.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

import numpy as np

from backtesting.backtest_runner import BacktestResult, BacktestRunner


# 3 non-overlapping test windows
PERIODS = [
    ("2022-01-01", "2022-12-31", "2022"),
    ("2023-01-01", "2023-12-31", "2023"),
    ("2024-01-01", "2024-12-31", "2024"),
]


@dataclass
class WalkForwardReport:
    symbol: str
    results: List[BacktestResult]
    consistent: bool       # strategy profitable in all 3 periods
    avg_sharpe: float
    avg_drawdown: float
    avg_win_rate: float
    total_trades: int
    recommendation: str

    def print_table(self) -> None:
        print(f"\n{'='*70}")
        print(f"  Walk-Forward Validation — {self.symbol}")
        print(f"{'='*70}")
        header = f"{'Period':<12} {'Sharpe':>8} {'WinRate':>9} {'MaxDD':>8} {'PF':>7} {'Trades':>7} {'Pass':>6}"
        print(header)
        print("-" * 70)
        for r in self.results:
            period_label = r.start[:4]
            passed_str = "✓" if r.passed() else "✗"
            print(
                f"{period_label:<12} "
                f"{r.sharpe:>8.2f} "
                f"{r.win_rate*100:>8.1f}% "
                f"{r.max_drawdown*100:>7.1f}% "
                f"{r.profit_factor:>7.2f} "
                f"{r.trade_count:>7} "
                f"{passed_str:>6}"
            )
        print("-" * 70)
        print(
            f"{'AVERAGE':<12} "
            f"{self.avg_sharpe:>8.2f} "
            f"{self.avg_win_rate*100:>8.1f}% "
            f"{self.avg_drawdown*100:>7.1f}% "
            f"{'N/A':>7} "
            f"{self.total_trades:>7}"
        )
        print(f"\nConsistent: {'YES' if self.consistent else 'NO'}")
        print(f"Recommendation: {self.recommendation}")
        print(f"{'='*70}\n")


class WalkForwardValidator:
    """
    Runs backtest across 3 annual periods and analyses consistency.
    """

    def __init__(self, confidence_threshold: float = 55.0, holding_days: int = 3):
        self.runner = BacktestRunner(
            confidence_threshold=confidence_threshold,
            holding_days=holding_days,
        )

    def validate(self, symbol: str) -> WalkForwardReport:
        """
        Run 3 independent backtests and compile a WalkForwardReport.

        Args:
            symbol: bot-internal symbol

        Returns:
            WalkForwardReport with per-period results and consistency check
        """
        print(f"  Running walk-forward validation for {symbol}...")
        results: list[BacktestResult] = []

        # Fetch full daily history once (reused across periods)
        from data.fetcher import fetch_candles
        try:
            df_all = fetch_candles(symbol, "1d", period="5y", use_cache=False)
        except Exception as e:
            print(f"  [WARN] Could not fetch {symbol} data: {e}")
            df_all = None

        for start, end, label in PERIODS:
            print(f"    Testing {label}...", end=" ", flush=True)
            try:
                if df_all is not None:
                    df_period = df_all.loc[start:end].copy()
                    result = self.runner.run(symbol, start, end, df=df_period)
                else:
                    result = self.runner.run(symbol, start, end)
                print(f"Sharpe={result.sharpe:.2f}, Trades={result.trade_count}")
            except Exception as e:
                print(f"ERROR: {e}")
                result = BacktestRunner._empty_result(symbol, start, end, str(e))
            results.append(result)

        return self._compile_report(symbol, results)

    @staticmethod
    def _compile_report(symbol: str, results: list[BacktestResult]) -> WalkForwardReport:
        valid = [r for r in results if r.trade_count > 0]

        if not valid:
            return WalkForwardReport(
                symbol=symbol,
                results=results,
                consistent=False,
                avg_sharpe=0.0,
                avg_drawdown=0.0,
                avg_win_rate=0.0,
                total_trades=0,
                recommendation="SKIP — no trades generated in any period",
            )

        avg_sharpe   = float(np.mean([r.sharpe for r in valid]))
        avg_drawdown = float(np.mean([r.max_drawdown for r in valid]))
        avg_win_rate = float(np.mean([r.win_rate for r in valid]))
        total_trades = sum(r.trade_count for r in valid)

        # Consistent = profitable (positive Sharpe) in ALL tested periods
        consistent = all(r.sharpe > 0 and r.total_return > 0 for r in results)

        # Recommendation logic
        if avg_sharpe >= 1.5 and avg_drawdown < 0.15 and consistent and total_trades >= 30:
            rec = "DEPLOY — passes all thresholds across all periods"
        elif avg_sharpe >= 1.0 and consistent:
            rec = "WATCH — promising but below Sharpe 1.5 target"
        elif not consistent:
            rec = "REJECT — inconsistent performance across periods (overfitting risk)"
        else:
            rec = "REJECT — insufficient performance metrics"

        return WalkForwardReport(
            symbol=symbol,
            results=results,
            consistent=consistent,
            avg_sharpe=avg_sharpe,
            avg_drawdown=avg_drawdown,
            avg_win_rate=avg_win_rate,
            total_trades=total_trades,
            recommendation=rec,
        )
