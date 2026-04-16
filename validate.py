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

# Fixed candidate pool with 10yr+ history for portfolio backtests.
# Used when --portfolio is active and no --symbol is specified,
# instead of active_symbols() (which reflects the live runtime universe).
# SPY excluded — consistently near-zero edge vs QQQ; they are too correlated.
_BT_CANDIDATE_POOL: list[str] = [
    "QQQ", "NVDA", "AAPL", "MSFT", "TSLA", "AMZN", "META",
    "XOM", "XLE", "JPM", "GLD", "BTCUSD", "ETHUSD",
]


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
        "--portfolio",
        action="store_true",
        help="Run portfolio-level backtest (all instruments simultaneously, max 2 positions).",
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
        default=7,
        help="Days to hold a position (default: 7)",
    )
    parser.add_argument(
        "--start",
        type=str,
        default="2015-01-01",
        help="Backtest start date YYYY-MM-DD (default: 2015-01-01)",
    )
    parser.add_argument(
        "--end",
        type=str,
        default="2024-12-31",
        help="Backtest end date YYYY-MM-DD (default: 2024-12-31)",
    )
    parser.add_argument(
        "--ibkr-capital",
        type=float,
        default=None,
        dest="ibkr_capital",
        help="Override IBKR broker capital for backtest (e.g. 15000.0).",
    )
    parser.add_argument(
        "--oanda-capital",
        type=float,
        default=None,
        dest="oanda_capital",
        help="Override OANDA broker capital for backtest (e.g. 5000.0).",
    )
    parser.add_argument(
        "--cash-reserve",
        type=float,
        default=None,
        dest="cash_reserve",
        help="Override cash reserve fraction for backtest (e.g. 0.0 = fully deploy).",
    )
    parser.add_argument(
        "--no-signal-exit",
        action="store_true",
        default=False,
        help="Disable signal-reversal exit (EMA9 cross below EMA21).",
    )
    parser.add_argument(
        "--no-two-phase-trail",
        action="store_true",
        default=False,
        help="Disable two-phase trailing stop (tightens after 50%% of TP reached).",
    )
    parser.add_argument(
        "--volume-confirmation",
        action="store_true",
        default=False,
        help="Require entry-day volume >= 0.8x 20d average (volume confirmation gate).",
    )
    parser.add_argument(
        "--sl-mult",
        type=float,
        default=None,
        dest="sl_mult",
        help="Override ATR stop-loss multiplier for all assets (e.g. 1.5).",
    )
    parser.add_argument(
        "--tp-scale",
        type=float,
        default=1.0,
        dest="tp_scale",
        help="Scale factor applied to all TP tiers (e.g. 1.5 = 50%% wider TP).",
    )
    return parser.parse_args()


def _cache_path(symbols: list[str], start: str, end: str) -> "Path":
    """Return a deterministic cache file path for a given query."""
    from pathlib import Path
    import hashlib
    key = f"{'_'.join(sorted(symbols))}_{start}_{end}"
    h = hashlib.md5(key.encode()).hexdigest()[:8]
    cache_dir = Path("tasks/data_cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / f"daily_{h}.pkl"


def _ibkr_fetch_daily(symbol: str, start: str, end: str) -> "pd.DataFrame | None":
    """Fetch daily bars for one symbol from IBKR (fallback when yfinance is rate-limited).
    Uses the same infrastructure as data/fetcher._fetch_ibkr."""
    try:
        import asyncio
        asyncio.set_event_loop(asyncio.new_event_loop())
        import pandas as pd
        from ib_insync import IB, Stock, util
        from config.settings import settings
        from data.fetcher import _next_ibkr_client_id

        host = settings.ibkr.host
        port = settings.ibkr.port
        clientId = _next_ibkr_client_id()

        contract = Stock(symbol, "SMART", "USD")
        ib = IB()
        try:
            ib.connect(host, port, clientId=clientId, timeout=10, readonly=True)
            bars = ib.reqHistoricalData(
                contract,
                endDateTime="",
                durationStr="5 Y",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
            )
        finally:
            if ib.isConnected():
                ib.disconnect()

        if not bars:
            return None

        df = util.df(bars)[["date", "open", "high", "low", "close", "volume"]].copy()
        df = df.rename(columns={"date": "time"})
        df["time"] = pd.to_datetime(df["time"], utc=True)
        df = df.set_index("time").sort_index()
        df = df[(df.index >= pd.Timestamp(start, tz="UTC")) &
                (df.index <= pd.Timestamp(end, tz="UTC"))]
        return df if not df.empty else None
    except Exception as exc:
        print(f"  IBKR fallback failed for {symbol}: {exc}")
        return None


def bulk_fetch_daily(
    symbols: list[str],
    start: str,
    end: str,
) -> dict[str, "pd.DataFrame"]:
    """
    Download daily OHLCV for all symbols in a single yfinance API call.
    Results are cached to disk so repeated runs don't re-hit yfinance rate limits.
    Falls back to individual yfinance downloads, then IBKR, on rate-limit failure.
    Returns {symbol: DataFrame} mapping. Symbols that fail are omitted.
    """
    import pickle
    from pathlib import Path
    import pandas as pd
    import yfinance as yf
    from data.fetcher import _SYMBOL_MAP

    cache_file = _cache_path(symbols, start, end)
    if cache_file.exists():
        try:
            with open(cache_file, "rb") as f:
                cached = pickle.load(f)
            print(f"  Loaded cached data ({len(cached)} symbols, {cache_file.name})", flush=True)
            return cached
        except Exception:
            cache_file.unlink(missing_ok=True)

    yf_symbols = [_SYMBOL_MAP.get(s.upper(), s.upper()) for s in symbols]
    sym_to_yf   = {s.upper(): _SYMBOL_MAP.get(s.upper(), s.upper()) for s in symbols}
    yf_to_sym   = {v: k for k, v in sym_to_yf.items()}

    import time

    print(f"  Bulk-fetching {len(yf_symbols)} symbols from {start} to {end}…", flush=True)
    raw = None
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

    if raw is None or raw.empty:
        # Fallback: individual downloads with small delays to avoid rate limits
        print("  Falling back to individual symbol downloads…", flush=True)
        result: dict[str, pd.DataFrame] = {}
        for i, (yf_sym, orig_sym) in enumerate(yf_to_sym.items()):
            if i > 0:
                time.sleep(1.5)
            try:
                df = yf.download(yf_sym, start=start, end=end, interval="1d",
                                 auto_adjust=True, progress=False)
                if df.empty:
                    raise ValueError("empty response")
                df.columns = [c.lower() for c in df.columns]
                df = df[["open", "high", "low", "close", "volume"]].copy()
                if df.index.tz is None:
                    df.index = df.index.tz_localize("UTC")
                else:
                    df.index = df.index.tz_convert("UTC")
                df = df.dropna(subset=["close"])
                if not df.empty:
                    result[orig_sym] = df
                    print(f"  {orig_sym}: {len(df)} rows", flush=True)
            except Exception as exc2:
                # Final fallback: IBKR (skipped for crypto since IBKR doesn't have BTC/ETH daily)
                from portfolio.watchlist import CANDIDATE_POOL_BY_SYMBOL
                inst = CANDIDATE_POOL_BY_SYMBOL.get(orig_sym)
                if inst and inst.asset_type == "stock":
                    print(f"  {orig_sym}: yfinance failed ({type(exc2).__name__}), trying IBKR…", flush=True)
                    df = _ibkr_fetch_daily(orig_sym, start, end)
                    if df is not None and not df.empty:
                        result[orig_sym] = df
                        print(f"  {orig_sym}: {len(df)} rows (IBKR)", flush=True)
                    else:
                        print(f"  Warning: {orig_sym} failed all sources")
                else:
                    print(f"  Warning: {orig_sym} skipped (crypto, yfinance failed: {type(exc2).__name__})")
        if result:
            try:
                with open(cache_file, "wb") as f:
                    pickle.dump(result, f)
                print(f"  Cached to {cache_file.name}", flush=True)
            except Exception:
                pass
        return result

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

    if result:
        try:
            with open(cache_file, "wb") as f:
                pickle.dump(result, f)
            print(f"  Cached to {cache_file.name}", flush=True)
        except Exception:
            pass  # cache write failure is non-fatal

    return result


def print_summary_table(results: list) -> None:
    """Print a formatted summary table of BacktestResult objects."""
    if not results:
        print("No results to display.")
        return

    print("\n" + "=" * 80)
    print("  BACKTEST SUMMARY — Individual Instruments")
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
    """Run and print a single-period backtest."""
    from backtesting.backtest_runner import BacktestRunner

    runner = BacktestRunner(
        confidence_threshold=args.threshold,
        holding_days=args.holding_days,
    )

    start = getattr(args, "start", "2020-01-01")
    end   = getattr(args, "end",   "2025-03-01")
    print(f"  Fetching data for {symbol}...")
    t0 = time.time()
    try:
        result = runner.run(symbol, start=start, end=end)
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


def run_portfolio_backtest(args: argparse.Namespace, walkforward: bool = False) -> None:
    """Run portfolio-level backtest (all instruments simultaneously)."""
    from backtesting.portfolio_backtest import PortfolioBacktestRunner
    from portfolio.watchlist import active_symbols, UNIVERSE_BY_SYMBOL

    if args.symbol:
        all_syms = [args.symbol.upper()]
    else:
        # Use fixed 10yr+ history pool for portfolio mode instead of live active universe
        all_syms = _BT_CANDIDATE_POOL
    # Daily bar signals are not representative for forex (live bot uses 15m/1h for forex).
    # Exclude forex from the portfolio daily-bar backtest to avoid misleading results.
    symbols = [s for s in all_syms
               if not (UNIVERSE_BY_SYMBOL.get(s) and UNIVERSE_BY_SYMBOL[s].asset_type == "forex")]
    if not symbols:
        symbols = all_syms  # fallback: keep all if filter removed everything

    runner = PortfolioBacktestRunner(
        confidence_threshold=args.threshold,
        holding_days=args.holding_days,
        ibkr_capital=args.ibkr_capital,
        oanda_capital=args.oanda_capital,
        cash_reserve_pct=args.cash_reserve,
        signal_reversal_exit=not args.no_signal_exit,
        two_phase_trail=not args.no_two_phase_trail,
        volume_confirmation=args.volume_confirmation,
    )
    if args.sl_mult is not None:
        runner.sl_mult_override = args.sl_mult
    runner.tp_scale = args.tp_scale

    bt_start = getattr(args, "start", "2020-01-01")
    bt_end   = getattr(args, "end",   "2025-03-01")

    # Fetch with a 2-year lookback before start for EMA200 warmup (200 trading days ~ 10 months,
    # but we use 2 years to be safe and handle any gaps).
    import datetime
    fetch_start_dt = datetime.date.fromisoformat(bt_start) - datetime.timedelta(days=730)
    FETCH_START = fetch_start_dt.strftime("%Y-%m-%d")
    FULL_END    = bt_end

    print(f"\n  Bulk-fetching {len(symbols)} symbols for portfolio backtest…")
    print(f"  Period: {bt_start} → {bt_end}  (warmup from {FETCH_START})")
    prefetched = bulk_fetch_daily(symbols, FETCH_START, FULL_END)

    if walkforward:
        # Split the requested range into annual periods
        import datetime as dt
        start_yr = int(bt_start[:4])
        end_yr   = int(bt_end[:4])
        periods = []
        for yr in range(start_yr, end_yr + 1):
            yr_start = f"{yr}-01-01"
            yr_end   = f"{yr}-12-31"
            # Don't exceed the requested end
            if yr_end > bt_end:
                yr_end = bt_end
            label = f"{yr}"
            periods.append((yr_start, yr_end, label))

        print(f"\n  Portfolio walk-forward — {', '.join(symbols)}")
        for start, end, label in periods:
            result = runner.run(symbols, start=start, end=end, prefetched_dfs=prefetched)
            print(f"\n  ── {label} ──")
            result.print_table()
    else:
        result = runner.run(symbols, start=bt_start, end=FULL_END, prefetched_dfs=prefetched)
        result.print_table()


def main() -> None:
    args = parse_args()

    from portfolio.watchlist import active_symbols
    symbols = [args.symbol.upper()] if args.symbol else active_symbols()

    start = getattr(args, "start", "2020-01-01")
    end   = getattr(args, "end",   "2025-03-01")
    mode = (
        f"Portfolio walk-forward ({start[:4]}–{end[:4]})" if (args.portfolio and args.walkforward)
        else f"Portfolio full period ({start} → {end})" if args.portfolio
        else "Walk-forward (per-year periods)" if args.walkforward
        else f"Full period ({start} → {end})"
    )

    print("=" * 60)
    print(f"  Trade Bot — Backtest Validator")
    print(f"  Symbols    : {', '.join(symbols)}")
    print(f"  Mode       : {mode}")
    print(f"  Threshold  : {args.threshold}%")
    print(f"  Hold days  : {args.holding_days}")
    print("=" * 60)

    if args.portfolio:
        run_portfolio_backtest(args, walkforward=args.walkforward)
        return

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

            START, END = start, end
            prefetched = bulk_fetch_daily(symbols, START, END)

            all_results = []
            for sym in symbols:
                print(f"  Testing {sym}...", end=" ", flush=True)
                t0 = time.time()
                try:
                    df = prefetched.get(sym)
                    result = runner.run(sym, start=start, end=end, df=df)
                    elapsed = time.time() - t0
                    print(f"Sharpe={result.sharpe:.2f}, Trades={result.trade_count} ({elapsed:.1f}s)")
                    all_results.append(result)
                except Exception as e:
                    print(f"ERROR: {e}")

            print_summary_table(all_results)


if __name__ == "__main__":
    main()
