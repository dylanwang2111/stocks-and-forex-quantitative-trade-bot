"""
main.py
Trade Bot entry point.

Usage:
    python main.py                    # defaults to paper mode
    python main.py --mode paper       # paper trading (simulated orders)
    python main.py --mode live        # live trading (real orders)
    python main.py --mode validate    # run backtests
    python main.py --mode optimize    # run optimization pipeline
    python main.py --mode dashboard   # launch Streamlit monitoring dashboard
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys

from config.settings import settings
from database.models import init_db
from portfolio.watchlist import UNIVERSE


def _setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _print_banner(mode: str) -> None:
    print("=" * 60)
    print(f"  Trade Bot — {mode.upper()} MODE")
    print("=" * 60)
    print(f"  Database  : {settings.bot.database_url}")
    print(f"  Capital   : ${settings.bot.total_capital:.0f} total | ${settings.bot.cash_reserve:.0f} reserve | ${settings.bot.deployable_capital:.0f} deployable")
    print(f"  Risk/trade: {settings.bot.risk_per_trade*100:.1f}% (${settings.bot.total_capital * settings.bot.risk_per_trade:.2f})")
    print(f"  Min conf  : {settings.bot.min_confidence:.0f}%")
    print(f"  Max pos   : {settings.bot.max_positions}")
    print(f"\n  API Keys:")
    print(f"    IBKR  : {'✓ configured' if settings.ibkr.enabled else '✗ not set (stub)'}")
    print(f"    OANDA : {'✓ configured' if settings.oanda.enabled else '✗ not set (stub)'}")
    print(f"    Groq  : {'✓ configured' if settings.groq.enabled else '✗ not set'}")
    print(f"    Gemini: {'✓ configured' if settings.gemini.enabled else '✗ not set'}")
    print(f"\n  Universe ({len(UNIVERSE)} instruments):")
    for inst in UNIVERSE:
        print(f"    {inst.symbol:<8} [{inst.broker:<6}] {inst.asset_type}")
    print("=" * 60)


def run_paper(args) -> None:
    """Start paper trading orchestrator."""
    _print_banner("paper")
    init_db(settings.bot.database_url)

    try:
        from agents.orchestrator import Orchestrator
    except ImportError as e:
        print(f"ERROR: Could not import Orchestrator: {e}")
        sys.exit(1)

    orch = Orchestrator(database_url=settings.bot.database_url, trading_mode="paper")
    print("\n  Starting paper trading orchestrator...")
    print("  Scans every 15 minutes. Press Ctrl+C to stop.\n")
    try:
        orch.start()
    except KeyboardInterrupt:
        print("\n  Stopping orchestrator...")
        orch.stop()
        print("  Stopped cleanly.")


def run_live(args) -> None:
    """Start live trading orchestrator."""
    if not settings.ibkr.enabled and not settings.oanda.enabled:
        print("ERROR: Live mode requires at least one broker configured.")
        print("  Set IBKR_ACCOUNT_ID and/or OANDA_API_KEY in .env")
        sys.exit(1)

    _print_banner("live")
    init_db(settings.bot.database_url)

    # Confirmation gate
    print("\n  WARNING: Live trading will place REAL orders with REAL money!")
    confirm = input("  Type 'CONFIRM' to proceed: ").strip()
    if confirm != "CONFIRM":
        print("  Aborted.")
        sys.exit(0)

    try:
        from agents.orchestrator import Orchestrator
    except ImportError as e:
        print(f"ERROR: Could not import Orchestrator: {e}")
        sys.exit(1)

    orch = Orchestrator(database_url=settings.bot.database_url, trading_mode="live")
    print("\n  Starting LIVE trading orchestrator...")
    try:
        orch.start()
    except KeyboardInterrupt:
        print("\n  Stopping orchestrator...")
        orch.stop()
        print("  Stopped cleanly.")


def run_validate(args) -> None:
    """Run backtests (existing validate.py behaviour)."""
    _print_banner("validate")
    # Strip main.py's own args before delegating so validate.py's argparser
    # doesn't see --mode / --log-level / --auto-approve.
    import sys as _sys
    _sys.argv = [_sys.argv[0]]
    try:
        import validate
        validate.main()
    except Exception as e:
        print(f"ERROR during validation: {e}")
        raise


def run_dashboard(args) -> None:
    """Launch the FastAPI dashboard (dashboard_v2)."""
    print("  Launching dashboard at http://localhost:8050")
    print("  Press Ctrl+C to stop.\n")
    try:
        subprocess.run(
            ["uvicorn", "dashboard_v2:app", "--host", "0.0.0.0", "--port", "8050"],
            check=True,
        )
    except FileNotFoundError:
        print("ERROR: 'uvicorn' not found. Run: pip install uvicorn[standard]")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n  Dashboard stopped.")


def run_optimize(args) -> None:
    """Run the optimization pipeline."""
    _print_banner("optimize")
    init_db(settings.bot.database_url)

    try:
        from optimization.pipeline import OptimizationPipeline
    except ImportError as e:
        print(f"ERROR: Could not import OptimizationPipeline: {e}")
        sys.exit(1)

    require_approval = not args.auto_approve
    pipeline = OptimizationPipeline(database_url=settings.bot.database_url)
    pipeline.run(require_human_approval=require_approval)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Trade Bot — automated trading system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  paper      Paper trading with simulated orders (default)
  live       Live trading with real orders (requires broker API keys)
  validate   Run backtests against historical data
  optimize   Run the Gemini-powered optimization pipeline
  dashboard  Launch the Streamlit monitoring dashboard (http://localhost:8501)
        """,
    )
    parser.add_argument(
        "--mode",
        choices=["paper", "live", "validate", "optimize", "dashboard"],
        default="paper",
        help="Trading mode (default: paper)",
    )
    parser.add_argument(
        "--log-level",
        default=settings.bot.log_level,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help="Skip human approval gate in optimize mode",
    )

    args = parser.parse_args()
    _setup_logging(args.log_level)

    mode_handlers = {
        "paper":     run_paper,
        "live":      run_live,
        "validate":  run_validate,
        "optimize":  run_optimize,
        "dashboard": run_dashboard,
    }
    mode_handlers[args.mode](args)


if __name__ == "__main__":
    main()
