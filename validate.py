"""
validate.py
CLI script: runs backtest on all 6 instruments (or a specific one) and prints
a results table.  No broker API keys required — uses yfinance.

Usage:
    python validate.py                        # all 6 instruments
    python validate.py --symbol NVDA          # single instrument
    python validate.py --symbol NVDA --walkforward   # walk-forward (3 periods)
    python validate.py --walkforward          # walk-forward for ALL instruments
"""
from __future__ import annotations

import argparse
import sys
import time
import warnings

warnings.filterwarnings("ignore")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Trade Bot — Backtest Validator")
    parser.add_argument(
        "--symbol",
        type=str,
        default=None,
        help="Single instrument to test (e.g. NVDA, EURUSD). Default: all 6.",
    )
    parser.add_argument(
        "--walkforward",
        action="store_true",
        help="Run walk-forward validation (2022, 2023, 2024) instead of single period.",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=55.0,
        help="Confidence threshold for trade entry (default: 55.0)",
    )
    parser.add_argument(
        "--holding-days",
        type=int,
        default=3,
        help="Days to hold a position (default: 3)",
    )
    return parser.parse_args()


def bulk_fetch_daily(
    symbols: list[str],
    start: str,
    end: str,
) -> dict[str, "pd.DataFrame"]:
    """
    Download daily OHLCV for all symbols in a single yfinance API call.
    Returns {symbol: DataFrame} mapping. Symbols that fail are omitted.
    Falls back to an empty dict on total failure.
    """
    import pandas as pd
    import yfinance as yf
    from data.fetcher import _SYMBOL_MAP

    yf_symbols = [_SYMBOL_MAP.get(s.upper(), s.upper()) for s in symbols]
    sym_to_yf   = {s.upper(): _SYMBOL_MAP.get(s.upper(), s.upper()) for s in symbols}
    yf_to_sym   = {v: k for k, v in sym_to_yf.items()}

    print(f"  Bulk-fetching {len(yf_symbols)} symbols from {start} to {end}…", flush=True)
    try:
        raw = yf.download(
            yf_symbols,
            start=start,
            end=end,
            interval="1d",
            auto_adjust=True,
            progress=False,
            multi_level_index=True,
        )
    except Exception as exc:
        print(f"  Bulk fetch failed: {exc}")
        return {}

    if raw.empty:
        print("  Bulk fetch returned empty data.")
        return {}

    result: dict[str, pd.DataFrame] = {}
    for yf_sym in yf_symbols:
        orig_sym = yf_to_sym.get(yf_sym, yf_sym)
        try:
            if isinstance(raw.columns, pd.MultiIndex):
                df = raw.xs(yf_sym, level=1, axis=1).copy()
            else:
                df = raw.copy()

            df.columns = [c.lower() for c in df.columns]
            df = df[["open", "high", "low", "close", "volume"]].copy()

            if df.index.tz is None:
                df.index = df.index.tz_localize("UTC")
            else:
                df.index = df.index.tz_convert("UTC")

            df = df.dropna(subset=["close"])
            if not df.empty:
                result[orig_sym] = df
        except Exception as exc:
            print(f"  Warning: could not slice {yf_sym} ({orig_sym}): {exc}")

    return result


def print_summary_table(results: list) -> None:
    """Print a formatted summary table of BacktestResult objects."""
    if not results:
        print("No results to display.")
        return

    print("\n" + "=" * 80)
    print("  BACKTEST SUMMARY — Full Period (2022–2024)")
    print("=" * 80)
    header = (
        f"{'Symbol':<10} {'Sharpe':>8} {'WinRate':>9} {'MaxDD':>8} "
        f"{'PF':>7} {'Trades':>7} {'Return':>9} {'Status':>8}"
    )
    print(header)
    print("-" * 80)

    for r in results:
        status = "PASS ✓" if r.passed() else "FAIL ✗"
        print(
            f"{r.symbol:<10} "
            f"{r.sharpe:>8.2f} "
            f"{r.win_rate * 100:>8.1f}% "
            f"{r.max_drawdown * 100:>7.1f}% "
            f"{r.profit_factor:>7.2f} "
            f"{r.trade_count:>7} "
            f"{r.total_return * 100:>8.1f}% "
            f"{status:>8}"
        )

    print("-" * 80)
    passed = [r for r in results if r.passed()]
    print(f"\n  {len(passed)}/{len(results)} instruments passed minimum thresholds")
    print(f"  (Sharpe > 1.2, MaxDD < 20%, Trades ≥ 15 — daily-bar proxy thresholds)\n")


def run_single_backtest(symbol: str, args: argparse.Namespace) -> None:
    """Run and print a single-period (3-year) backtest."""
    from backtesting.backtest_runner import BacktestRunner

    runner = BacktestRunner(
        confidence_threshold=args.threshold,
        holding_days=args.holding_days,
    )

    print(f"  Fetching data for {symbol}...")
    t0 = time.time()
    try:
        result = runner.run(symbol, start="2022-01-01", end="2024-12-31")
        elapsed = time.time() - t0
        print(f"  Done in {elapsed:.1f}s")
        print_summary_table([result])
    except Exception as e:
        print(f"  [ERROR] {symbol}: {e}")
        import traceback
        traceback.print_exc()


def run_walkforward(symbol: str, args: argparse.Namespace) -> None:
    """Run walk-forward validation for a single symbol."""
    from backtesting.walk_forward import WalkForwardValidator

    validator = WalkForwardValidator(
        confidence_threshold=args.threshold,
        holding_days=args.holding_days,
    )
    report = validator.validate(symbol)
    report.print_table()


def main() -> None:
    args = parse_args()

    from portfolio.watchlist import active_symbols
    symbols = [args.symbol.upper()] if args.symbol else active_symbols()

    print("=" * 60)
    print(f"  Trade Bot — Backtest Validator")
    print(f"  Symbols    : {', '.join(symbols)}")
    print(f"  Mode       : {'Walk-forward (3 periods)' if args.walkforward else 'Full period (2022–2024)'}")
    print(f"  Threshold  : {args.threshold}%")
    print(f"  Hold days  : {args.holding_days}")
    print("=" * 60)

    if args.walkforward:
        for sym in symbols:
            run_walkforward(sym, args)
    else:
        if len(symbols) == 1:
            run_single_backtest(symbols[0], args)
        else:
            # Run all instruments, collect results
            from backtesting.backtest_runner import BacktestRunner
            runner = BacktestRunner(
                confidence_threshold=args.threshold,
                holding_days=args.holding_days,
            )

            START, END = "2022-01-01", "2024-12-31"
            prefetched = bulk_fetch_daily(symbols, START, END)

            all_results = []
            for sym in symbols:
                print(f"  Testing {sym}...", end=" ", flush=True)
                t0 = time.time()
                try:
                    df = prefetched.get(sym)
                    result = runner.run(sym, start=START, end=END, df=df)
                    elapsed = time.time() - t0
                    print(f"Sharpe={result.sharpe:.2f}, Trades={result.trade_count} ({elapsed:.1f}s)")
                    all_results.append(result)
                except Exception as e:
                    print(f"ERROR: {e}")

            print_summary_table(all_results)


if __name__ == "__main__":
    main()
