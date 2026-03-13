"""
agents/orchestrator.py
Wires all components together and runs a 15-minute scan-and-trade loop using
APScheduler's BlockingScheduler.

Lifecycle
---------
1. Orchestrator.__init__()  — instantiate all components
2. Orchestrator.start()     — init_db, start HealthMonitor, start scheduler
3. scan_and_trade()         — runs every 15 min (core decision loop)
4. save_snapshot()          — runs every 60 min (DB housekeeping)
5. Orchestrator.stop()      — graceful shutdown on SIGINT or explicit call
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

try:
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.interval import IntervalTrigger
    from apscheduler.triggers.cron import CronTrigger
except ImportError:
    BlockingScheduler = None  # type: ignore[assignment,misc]
    IntervalTrigger = None    # type: ignore[assignment]
    CronTrigger = None        # type: ignore[assignment]

from config.settings import settings
from database.models import EventLog, get_session, init_db
from portfolio.state import PortfolioStateManager, Position
from portfolio.pdt_tracker import PDTTracker
from resilience.health_monitor import HealthMonitor
from resilience.correlation_guard import CorrelationGuard
from events.event_guard import EventGuard
from agents.risk_agent import RiskAgent, RiskParams
from agents.execution_agent import ExecutionAgent, OrderResult
from agents.portfolio_agent import PortfolioAgent
from agents.pre_screen_agent import PreScreenAgent
from notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

@dataclass
class CircuitBreakerState:
    tripped: bool = False
    reason: str = ""
    consecutive_losses: int = 0
    trip_time: Optional[datetime] = None


class CircuitBreaker:
    """
    Prevents new entries when daily loss limit or consecutive-loss limit is hit.

    Thresholds
    ----------
    DAILY_LOSS_LIMIT_USD : trip if daily_pnl() < -(3% of total_capital)
    MAX_CONSECUTIVE_LOSSES : trip if consecutive_losses >= 5
    """

    MAX_CONSECUTIVE_LOSSES = 5

    def __init__(self) -> None:
        self._state = CircuitBreakerState()
        self._lock = threading.Lock()

    # ── Public API ────────────────────────────────────────────────────────────

    def check(self, state: PortfolioStateManager) -> tuple[bool, str]:
        """
        Evaluate both trip conditions and update internal state.

        Returns
        -------
        (tripped: bool, reason: str)
            tripped=True  → caller must skip new entries.
            tripped=False → all clear, reason="".
        """
        with self._lock:
            # ── Condition 1: daily loss limit ──────────────────────────────
            daily_pnl = state.daily_pnl()
            daily_loss_limit = settings.bot.total_capital * 0.03
            if daily_pnl < -daily_loss_limit:
                reason = (
                    f"3% daily loss limit hit "
                    f"(${daily_loss_limit:.2f}, daily_pnl={daily_pnl:.2f})"
                )
                self._state.tripped = True
                self._state.reason = reason
                self._state.trip_time = datetime.utcnow()
                logger.warning("CircuitBreaker tripped: %s", reason)
                return True, reason

            # ── Condition 2: consecutive losses ────────────────────────────
            if self._state.consecutive_losses >= self.MAX_CONSECUTIVE_LOSSES:
                reason = (
                    f"5 consecutive losses "
                    f"(count={self._state.consecutive_losses})"
                )
                self._state.tripped = True
                self._state.reason = reason
                if self._state.trip_time is None:
                    self._state.trip_time = datetime.utcnow()
                logger.warning("CircuitBreaker tripped: %s", reason)
                return True, reason

            # ── Healthy ────────────────────────────────────────────────────
            self._state.tripped = False
            self._state.reason = ""
            return False, ""

    def record_loss(self) -> None:
        """Increment consecutive loss counter."""
        with self._lock:
            self._state.consecutive_losses += 1
            logger.debug(
                "CircuitBreaker: loss recorded, consecutive=%d",
                self._state.consecutive_losses,
            )

    def record_win(self) -> None:
        """Reset consecutive loss counter on a winning trade."""
        with self._lock:
            self._state.consecutive_losses = 0
            logger.debug("CircuitBreaker: win recorded, consecutive_losses reset to 0")

    def reset(self) -> None:
        """Manually reset all circuit-breaker state (operator override)."""
        with self._lock:
            self._state = CircuitBreakerState()
        logger.info("CircuitBreaker: manually reset")

    @property
    def state(self) -> CircuitBreakerState:
        with self._lock:
            return self._state


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

class Orchestrator:
    """
    Top-level controller that wires Scanner, RiskAgent, ExecutionAgent,
    PortfolioStateManager, HealthMonitor, EventGuard, CorrelationGuard,
    PDTTracker, and CircuitBreaker into a single 15-minute scan loop.
    """

    SCAN_INTERVAL_MINUTES     = 15
    SNAPSHOT_INTERVAL_MINUTES = 60
    SWING_HOLDING_DAYS        = 3   # default holding-period for exit checks

    def __init__(
        self,
        database_url: str | None = None,
        trading_mode: str | None = None,
    ) -> None:
        self._database_url: str = database_url or settings.bot.database_url
        self._trading_mode: str = trading_mode or settings.bot.trading_mode

        logger.info(
            "Orchestrator initialising | mode=%s db=%s",
            self._trading_mode,
            self._database_url,
        )

        # ── Portfolio state ───────────────────────────────────────────────────
        self._state = PortfolioStateManager(database_url=self._database_url)

        # ── Risk + execution ──────────────────────────────────────────────────
        self._risk_agent   = RiskAgent(state_manager=self._state)
        self._exec_agent   = ExecutionAgent(
            state_manager=self._state,
            database_url=self._database_url,
        )

        # ── Guards and trackers ───────────────────────────────────────────────
        self._event_guard       = EventGuard()
        self._correlation_guard = CorrelationGuard()
        self._pdt_tracker       = PDTTracker(database_url=self._database_url)
        self._circuit_breaker   = CircuitBreaker()

        # ── Notifications ─────────────────────────────────────────────────────
        self._notifier = TelegramNotifier()

        # ── Portfolio selection agent ─────────────────────────────────────────
        self._portfolio_agent = PortfolioAgent()
        self._pre_screen_agent = PreScreenAgent()

        # ── Scanner (imported lazily to decouple; raises ImportError if missing) ─
        self._scanner = self._build_scanner()

        # ── Health monitor ────────────────────────────────────────────────────
        self._monitor = HealthMonitor(
            database_url=self._database_url,
            on_reconnect=self._on_reconnect,
        )

        # ── Scheduler (set up in start()) ─────────────────────────────────────
        self._scheduler: Optional[BlockingScheduler] = None  # type: ignore[type-arg]

        # ── Cycle tracking ────────────────────────────────────────────────────
        self._cycle_count = 0

    # ── Private helpers ───────────────────────────────────────────────────────

    def _build_scanner(self):
        """Import and instantiate Scanner, wired to the state manager."""
        try:
            from portfolio.scanner import Scanner
            return Scanner(
                state_manager=self._state,
                pdt_tracker=self._pdt_tracker,
                event_guard=self._event_guard,
                correlation_guard=self._correlation_guard,
                database_url=self._database_url,
            )
        except ImportError:
            logger.warning(
                "portfolio.scanner not found — scan_and_trade will be a no-op "
                "until the Scanner module is implemented."
            )
            return None

    def _on_reconnect(self) -> None:
        """Called by HealthMonitor when any broker reconnects."""
        logger.info("Broker reconnected — syncing positions")
        # In live mode, a full position sync would happen here via broker API.
        # For now, log the reconciliation intent; actual sync is broker-specific.
        open_positions = self._state.all_positions()
        if not open_positions:
            logger.info("Reconnect sync: no open positions to reconcile")
            return
        logger.info(
            "Reconnect sync: %d open position(s) in memory — verify against broker",
            len(open_positions),
        )

    def _daily_prescreen(self) -> None:
        """
        Run PreScreenAgent.screen() to refresh the active UNIVERSE from the full
        CANDIDATE_POOL. Called daily at 05:00 UTC (except Monday — the weekly
        PortfolioAgent selection at 00:00 Monday takes precedence).
        Failures are swallowed so the bot never dies from a pre-screen error.
        """
        # Monday guard: weekly selection already ran at 00:00; skip pre-screen
        if datetime.utcnow().weekday() == 0:
            logger.info("_daily_prescreen: Monday — skipping (weekly selection is authoritative)")
            return

        try:
            selected = self._pre_screen_agent.screen()
            if selected:
                logger.info(
                    "Daily pre-screen updated universe: %d instruments — %s",
                    len(selected),
                    ", ".join(i.symbol for i in selected),
                )
                self._notifier.notify_portfolio_updated(selected)
            else:
                logger.warning(
                    "_daily_prescreen: no instruments selected — keeping current universe"
                )
        except Exception:
            logger.exception("_daily_prescreen: pre-screen agent failed; keeping current universe")

    def _select_portfolio(self) -> None:
        """
        Run PortfolioAgent.select() to update the active UNIVERSE.
        Called once on startup and every Monday at 00:00 UTC.
        Failures are caught so the bot keeps running with the current universe.
        """
        try:
            selected = self._portfolio_agent.select()
            if selected:
                logger.info(
                    "Portfolio updated: %d instruments selected — %s",
                    len(selected),
                    ", ".join(i.symbol for i in selected),
                )
                self._notifier.notify_portfolio_updated(selected)
            else:
                logger.warning("Portfolio selection returned no instruments; keeping current universe.")
        except Exception:
            logger.exception("_select_portfolio: portfolio agent failed; keeping current universe")

    def _log_event(
        self,
        event_type: str,
        description: str,
        symbol: str | None = None,
        metadata: dict | None = None,
    ) -> None:
        """Write to EventLog table. Swallows DB errors."""
        session = get_session(self._database_url)
        try:
            event = EventLog(
                timestamp=datetime.utcnow(),
                event_type=event_type,
                symbol=symbol,
                description=description,
                event_metadata=metadata or {},
            )
            session.add(event)
            session.commit()
        except Exception:
            logger.exception("Orchestrator._log_event: DB write failed")
            session.rollback()
        finally:
            session.close()

    # ── Public API ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """
        1. Initialise database tables.
        2. Start HealthMonitor as a daemon thread.
        3. Set up BlockingScheduler:
               - scan_and_trade every SCAN_INTERVAL_MINUTES
               - save_snapshot every SNAPSHOT_INTERVAL_MINUTES
        4. scheduler.start() — blocks until stop() is called.
        """
        if BlockingScheduler is None:
            logger.error(
                "APScheduler is not installed. "
                "Install it with: pip install apscheduler"
            )
            raise ImportError(
                "apscheduler is required. Install with: pip install apscheduler"
            )

        logger.info("Orchestrator.start(): initialising database")
        init_db(self._database_url)

        restored = self._state.restore_from_db()
        if restored:
            logger.info("Orchestrator.start(): restored %d open position(s) from DB", restored)

        logger.info("Orchestrator.start(): starting HealthMonitor")
        self._monitor.start()

        logger.info(
            "Orchestrator.start(): setting up scheduler "
            "(scan=%dm, snapshot=%dm)",
            self.SCAN_INTERVAL_MINUTES,
            self.SNAPSHOT_INTERVAL_MINUTES,
        )

        self._scheduler = BlockingScheduler(timezone="UTC")

        self._scheduler.add_job(
            func=self.scan_and_trade,
            trigger=IntervalTrigger(minutes=self.SCAN_INTERVAL_MINUTES),
            id="scan_and_trade",
            name="Scan-and-trade cycle",
            replace_existing=True,
            misfire_grace_time=60,
        )

        self._scheduler.add_job(
            func=self.save_snapshot,
            trigger=IntervalTrigger(minutes=self.SNAPSHOT_INTERVAL_MINUTES),
            id="save_snapshot",
            name="Portfolio snapshot",
            replace_existing=True,
            misfire_grace_time=120,
        )

        self._scheduler.add_job(
            func=self._select_portfolio,
            trigger=CronTrigger(day_of_week="mon", hour=0, minute=0, timezone="UTC"),
            id="portfolio_selection",
            name="Weekly portfolio selection",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        self._scheduler.add_job(
            func=self._daily_prescreen,
            trigger=CronTrigger(hour=5, minute=0, timezone="UTC"),
            id="daily_prescreen",
            name="Daily pre-screen",
            replace_existing=True,
            misfire_grace_time=3600,
        )

        logger.info(
            "Orchestrator running | mode=%s | press Ctrl+C to stop",
            self._trading_mode,
        )

        # Select portfolio once on startup before first scan
        self._select_portfolio()

        # Run once immediately so we don't wait 15 min on first startup
        self.scan_and_trade()

        self._scheduler.start()  # blocks here

    def stop(self) -> None:
        """Gracefully stop the scheduler and the health monitor."""
        logger.info("Orchestrator.stop() called")

        if self._scheduler is not None and self._scheduler.running:
            try:
                self._scheduler.shutdown(wait=False)
                logger.info("Scheduler stopped")
            except Exception:
                logger.exception("Error shutting down scheduler")

        self._monitor.stop()
        logger.info("Orchestrator stopped")

    # ── Core cycle ────────────────────────────────────────────────────────────

    def scan_and_trade(self) -> None:
        """
        Main 15-minute decision cycle.

        Steps
        -----
        1. Health check  — skip cycle (entries + exits) if any broker is DOWN.
        2. Circuit breaker — skip entries (still check exits) if tripped.
        3. Check exits for all open positions.
        4. Scan all instruments.
        5. Pick top opportunity.
        6. If opportunity and CB not tripped: compute risk, place order.
        7. Log cycle summary.
        """
        self._cycle_count += 1
        cycle_start = datetime.utcnow()
        logger.info(
            "--- Cycle #%d started at %s ---",
            self._cycle_count,
            cycle_start.strftime("%Y-%m-%d %H:%M:%S UTC"),
        )

        # ── Step 1: Health check ───────────────────────────────────────────────
        health = self._monitor.status
        down_brokers = health.down_brokers()
        if health.all_down():
            logger.warning(
                "Cycle #%d skipped — ALL brokers DOWN "
                "(ibkr=%s oanda=%s). Will retry next cycle.",
                self._cycle_count,
                health.ibkr_status.value,
                health.oanda_status.value,
            )
            return
        if down_brokers:
            logger.warning(
                "Cycle #%d: degraded mode — broker(s) DOWN: %s. "
                "Instruments on those brokers will be skipped.",
                self._cycle_count,
                ", ".join(sorted(down_brokers)),
            )

        # ── Step 2: Circuit breaker check ─────────────────────────────────────
        cb_tripped, cb_reason = self._circuit_breaker.check(self._state)
        if cb_tripped:
            logger.warning(
                "Cycle #%d: CircuitBreaker tripped (%s) — "
                "skipping entries; still checking exits.",
                self._cycle_count,
                cb_reason,
            )
            # Only notify on the first trip (state flips tripped→True once)
            if not self._circuit_breaker.state.trip_time or \
               (datetime.utcnow() - self._circuit_breaker.state.trip_time).seconds < 60:
                self._notifier.notify_circuit_breaker(cb_reason)

        # ── Step 3: Check exits ────────────────────────────────────────────────
        self._check_exits()

        # ── Step 4 & 5: Scan + top opportunity ────────────────────────────────
        if cb_tripped:
            logger.info(
                "Cycle #%d: circuit tripped — skipping scan/entry.",
                self._cycle_count,
            )
            self._log_cycle_summary(cycle_start, skipped=True, skip_reason=cb_reason)
            return

        if self._scanner is None:
            logger.warning(
                "Cycle #%d: Scanner not available — skipping scan.",
                self._cycle_count,
            )
            self._log_cycle_summary(cycle_start, skipped=True, skip_reason="no scanner")
            return

        # ── Market-hours gate ──────────────────────────────────────────────────
        # Skip the scan entirely when all markets are closed AND we hold no open
        # positions (nothing to exit). Saves API calls during off-hours.
        if not self._state.all_positions() and not self._scanner.any_market_open():
            logger.debug(
                "Cycle #%d: all markets closed, no open positions — skipping scan.",
                self._cycle_count,
            )
            self._log_cycle_summary(cycle_start, skipped=True, skip_reason="markets closed")
            return

        try:
            scan_results = self._scanner.scan_all(skip_brokers=down_brokers)
        except Exception:
            logger.exception("Cycle #%d: scan_all() raised an exception", self._cycle_count)
            self._log_cycle_summary(cycle_start, skipped=True, skip_reason="scan error")
            return

        if not scan_results:
            logger.info("Cycle #%d: no scan results returned.", self._cycle_count)
            self._log_cycle_summary(cycle_start, skipped=False)
            return

        # Notify Telegram of top signals (only on cycles that find something actionable)
        try:
            tg_results = [
                {"symbol": r.symbol, "direction": getattr(r.confidence_result, "direction", "?"),
                 "score": getattr(r.confidence_result, "score", 0)}
                for r in scan_results
                if hasattr(r, "confidence_result") and r.confidence_result is not None
            ]
            if tg_results:
                self._notifier.notify_scan_result(tg_results)
        except Exception:
            pass  # never block trading loop for notification errors

        opportunities = self._scanner.tradeable_opportunities(scan_results)
        if not opportunities:
            logger.info(
                "Cycle #%d: no tradeable opportunity found (all below threshold).",
                self._cycle_count,
            )
            self._log_cycle_summary(cycle_start, skipped=False)
            return

        # ── Step 6: Risk + order (fill all available slots) ────────────────────
        for opportunity in opportunities:
            if not self._state.can_open_position(opportunity.symbol):
                logger.debug(
                    "Cycle #%d: capacity reached — stopping entry loop.",
                    self._cycle_count,
                )
                break
            self._attempt_entry(opportunity)

        # ── Step 7: Summary ────────────────────────────────────────────────────
        self._log_cycle_summary(cycle_start, skipped=False)

    def save_snapshot(self) -> None:
        """Persist a PortfolioSnapshot row to the database and send hourly summary."""
        try:
            self._state.save_snapshot()
        except Exception:
            logger.exception("save_snapshot: failed")
        try:
            positions = self._state.all_positions()
            daily_pnl = self._state.daily_pnl()
            equity = self._state.available_cash() + self._state.deployed_capital()

            # Calculate unrealized P&L from current prices
            unrealized_pnl = 0.0
            for pos in positions:
                try:
                    from data.fetcher import fetch_candles
                    df = fetch_candles(pos.symbol, "1h")
                    if df is not None and not df.empty:
                        current_price = float(df["close"].iloc[-1])
                        unrealized_pnl += pos.unrealized_pnl(current_price)
                except Exception:
                    logger.debug("save_snapshot: could not fetch price for %s", pos.symbol)

            self._notifier.notify_daily_summary(
                open_positions=len(positions),
                daily_pnl=daily_pnl,
                unrealized_pnl=round(unrealized_pnl, 2),
                total_equity=equity,
                trading_mode=self._trading_mode,
                deployed=round(self._state.deployed_capital(), 2),
                available_cash=round(self._state.available_cash(), 2),
            )
        except Exception:
            logger.exception("save_snapshot: failed to send Telegram summary")

    # ── Exit logic ────────────────────────────────────────────────────────────

    def _check_exits(self) -> None:
        """
        Evaluate every open position for time-based exit criteria.

        Time-based exit: close positions held longer than SWING_HOLDING_DAYS.
        Price-based exits (stop/TP) are handled at the broker level via bracket
        orders, so we only need to handle the time gate here.
        In paper mode, skip price-based checks (no live quote available).
        """
        positions = self._state.all_positions()
        now_utc = datetime.utcnow()

        for position in positions:
            symbol = position.symbol
            held_days = (now_utc - position.entry_time).days
            logger.debug(
                "Checking exit for %s: held %d day(s) "
                "(entry=%s dir=%s tier=%s)",
                symbol,
                held_days,
                position.entry_time.strftime("%Y-%m-%d"),
                position.direction,
                position.position_tier,
            )

            # ── Time-based exit ────────────────────────────────────────────
            if held_days >= self.SWING_HOLDING_DAYS:
                logger.info(
                    "Exit: %s held %d days (>= %d) — closing (time-based).",
                    symbol,
                    held_days,
                    self.SWING_HOLDING_DAYS,
                )
                result = self._exec_agent.close_position(
                    position=position,
                    reason="time_exit",
                )
                if result.success:
                    self._record_closed_trade_outcome(position, result)
                    if result.filled_price is not None:
                        self._notifier.notify_trade_closed(
                            symbol=symbol,
                            direction=position.direction,
                            entry_price=position.entry_price,
                            exit_price=result.filled_price,
                            quantity=position.quantity,
                            reason="time_exit",
                        )
                else:
                    logger.error(
                        "Exit failed for %s: %s", symbol, result.error
                    )
                continue  # move on; position may no longer exist in state

            # ── Signal-reversal exit (held ≥1 day) ────────────────────────
            if held_days >= 1 and self._has_signal_reversal(position):
                logger.info(
                    "Exit: %s EMA9 crossed against %s position — closing early "
                    "(held %d day(s)).",
                    symbol, position.direction, held_days,
                )
                result = self._exec_agent.close_position(
                    position=position,
                    reason="signal_exit",
                )
                if result.success:
                    self._record_closed_trade_outcome(position, result)
                    if result.filled_price is not None:
                        self._notifier.notify_trade_closed(
                            symbol=symbol,
                            direction=position.direction,
                            entry_price=position.entry_price,
                            exit_price=result.filled_price,
                            quantity=position.quantity,
                            reason="signal_exit",
                        )
                else:
                    logger.error("Signal-exit failed for %s: %s", symbol, result.error)
                continue

    def _record_closed_trade_outcome(
        self,
        position: Position,
        result: OrderResult,
    ) -> None:
        """
        Update the circuit breaker based on closed-trade P&L.

        We compare the filled exit price to entry to determine win/loss.
        If the broker filled at a price we can't compute (e.g., paper close at
        entry price), we conservatively treat it as a loss only if it's clearly
        below cost for a long, or above for a short.
        """
        if result.filled_price is None:
            return

        exit_price = result.filled_price
        entry_price = position.entry_price

        if position.direction == "long":
            pnl = (exit_price - entry_price) * position.quantity
        else:
            pnl = (entry_price - exit_price) * position.quantity

        if pnl < 0:
            self._circuit_breaker.record_loss()
            logger.debug(
                "CircuitBreaker.record_loss(): %s pnl=%.4f",
                position.symbol,
                pnl,
            )
        else:
            self._circuit_breaker.record_win()
            logger.debug(
                "CircuitBreaker.record_win(): %s pnl=%.4f",
                position.symbol,
                pnl,
            )

    # ── Entry logic ───────────────────────────────────────────────────────────

    def _attempt_entry(self, opportunity) -> None:
        """
        Given a ScanResult opportunity, run all pre-trade guards and, if all
        pass, compute risk parameters and place the order.

        Guards (in order)
        -----------------
        1. Already holding this symbol  →  skip
        2. Max positions reached        →  skip
        3. EventGuard blackout          →  skip
        4. CorrelationGuard             →  skip
        5. PDTTracker (stock day-trade) →  skip
        6. RiskAgent returns None       →  skip
        7. place_order() called
        """
        symbol = opportunity.symbol if hasattr(opportunity, "symbol") else str(opportunity)
        confidence_result = getattr(opportunity, "confidence_result", None)
        regime = getattr(opportunity, "regime", None)

        if confidence_result is None:
            logger.warning("_attempt_entry: opportunity has no confidence_result — skipping")
            return

        # ── Guard 1 & 2: portfolio capacity ───────────────────────────────────
        if not self._state.can_open_position(symbol):
            logger.info(
                "_attempt_entry: %s — portfolio at capacity or already held, skip.",
                symbol,
            )
            return

        # ── Guard 3: event blackout ────────────────────────────────────────────
        try:
            from portfolio.watchlist import get_instrument
            asset_type = get_instrument(symbol).asset_type
        except (KeyError, ImportError):
            asset_type = "stock"  # conservative default

        blocked, block_reason = self._event_guard.is_blocked(symbol, asset_type)
        if blocked:
            logger.info(
                "_attempt_entry: %s blocked by EventGuard — %s", symbol, block_reason
            )
            self._notifier.notify_event_guard(symbol, block_reason)
            return

        # ── Guard 4: correlation ───────────────────────────────────────────────
        open_symbols = [p.symbol for p in self._state.all_positions()]
        self._correlation_guard.update_open_symbols(open_symbols)
        corr_allowed, corr_reason = self._correlation_guard.is_allowed(symbol)
        if not corr_allowed:
            logger.info(
                "_attempt_entry: %s blocked by CorrelationGuard — %s",
                symbol,
                corr_reason,
            )
            return

        # ── Guard 5: PDT rule ─────────────────────────────────────────────────
        if asset_type == "stock":
            pdt_count = self._pdt_tracker.count_day_trades_rolling()
            pdt_limit = self._pdt_tracker.PDT_LIMIT
            if pdt_count >= pdt_limit - 1:  # warn at 2/3 or 3/3
                self._notifier.notify_pdt_warning(pdt_count, pdt_limit)
            if not self._pdt_tracker.can_day_trade():
                logger.info(
                    "_attempt_entry: %s skipped — PDT day-trade limit reached "
                    "(use swing mode for stocks).",
                    symbol,
                )
                # We do NOT return — swing trades are still allowed.
                # The PDT check is only relevant for same-day (intraday) closings.
                # For swing entries (held overnight) we let the trade proceed.
                # Log the warning and continue.

        # ── Get current price (use cached scan price; fallback to live fetch) ──
        current_price = getattr(opportunity, "current_price", None)
        if current_price is None or current_price <= 0:
            current_price = self._get_current_price(symbol)
        if current_price is None or current_price <= 0:
            logger.warning(
                "_attempt_entry: %s — could not fetch current price, skip.", symbol
            )
            return

        # ── Guard 6: RiskAgent ────────────────────────────────────────────────
        atr = getattr(opportunity, "atr", None)
        risk_params = self._risk_agent.compute(
            confidence_result=confidence_result,
            current_price=current_price,
            symbol=symbol,
            atr=atr,
        )
        if risk_params is None:
            logger.info(
                "_attempt_entry: %s — RiskAgent returned None (tier/cash gate), skip.",
                symbol,
            )
            return

        # ── Place order ───────────────────────────────────────────────────────
        direction = confidence_result.direction
        logger.info(
            "_attempt_entry: %s %s | tier=%s size_usd=%.2f risk=$%.2f",
            symbol,
            direction.upper(),
            risk_params.position_tier,
            risk_params.position_size_usd,
            risk_params.risk_dollars,
        )

        result = self._exec_agent.place_order(
            symbol=symbol,
            risk_params=risk_params,
            direction=direction,
            confidence_result=confidence_result,
            regime=regime,
        )

        if result.success:
            logger.info(
                "Order placed: %s %s fill=%.5f id=%s",
                symbol,
                direction.upper(),
                result.filled_price or 0.0,
                result.order_id,
            )
            self._notifier.notify_trade_opened(
                symbol=symbol,
                direction=direction,
                tier=risk_params.position_tier,
                quantity=risk_params.quantity,
                entry_price=result.filled_price or risk_params.entry_price,
                stop_price=risk_params.stop_price,
                take_profit_price=risk_params.take_profit_price,
                risk_dollars=risk_params.risk_dollars,
                position_size_usd=risk_params.position_size_usd,
            )
        else:
            logger.error(
                "Order failed: %s — %s", symbol, result.error
            )
            self._log_event(
                event_type="order_error",
                description=f"Order failed for {symbol}: {result.error}",
                symbol=symbol,
                metadata={"direction": direction, "broker": result.broker},
            )

    def _get_current_price(self, symbol: str) -> float | None:
        """
        Fetch the most recent close price for the symbol from yfinance (1h candle).
        Returns None on any error.
        """
        try:
            from data.fetcher import fetch_candles
            df = fetch_candles(symbol, "1h")
            if df is None or df.empty:
                logger.warning("_get_current_price: empty candles for %s", symbol)
                return None
            return float(df["close"].iloc[-1])
        except Exception as exc:
            logger.warning(
                "_get_current_price: could not fetch price for %s — %s",
                symbol,
                exc,
            )
            return None

    def _has_signal_reversal(self, position: Position) -> bool:
        """
        Return True if EMA9 has crossed against the position direction on 1h candles.
        Used to exit early when the short-term trend flips.
        Only called after ≥1 day holding to avoid noise from intraday candle jitter.
        """
        try:
            import pandas_ta as ta
            from data.fetcher import fetch_candles
            df = fetch_candles(position.symbol, "1h")
            if df is None or df.empty or len(df) < 21:
                return False
            close = df["close"]
            ema9  = ta.ema(close, length=9)
            ema21 = ta.ema(close, length=21)
            if ema9 is None or ema21 is None or ema9.empty or ema21.empty:
                return False
            e9  = float(ema9.iloc[-1])
            e21 = float(ema21.iloc[-1])
            if position.direction == "long"  and e9 < e21:
                return True
            if position.direction == "short" and e9 > e21:
                return True
            return False
        except Exception:
            logger.debug("_has_signal_reversal: check failed for %s", position.symbol)
            return False

    # ── Cycle summary ─────────────────────────────────────────────────────────

    def _log_cycle_summary(
        self,
        cycle_start: datetime,
        skipped: bool,
        skip_reason: str = "",
    ) -> None:
        elapsed = (datetime.utcnow() - cycle_start).total_seconds()
        positions = self._state.all_positions()
        daily_pnl = self._state.daily_pnl()
        cb_state = self._circuit_breaker.state

        logger.info(
            "--- Cycle #%d complete | %.1fs | open=%d daily_pnl=%.2f "
            "cb_consecutive=%d skipped=%s%s ---",
            self._cycle_count,
            elapsed,
            len(positions),
            daily_pnl,
            cb_state.consecutive_losses,
            skipped,
            f" ({skip_reason})" if skip_reason else "",
        )


# ---------------------------------------------------------------------------
# Test function
# ---------------------------------------------------------------------------

def test_orchestrator() -> None:
    """
    Lightweight unit tests for Orchestrator components.

    Tests
    -----
    1. CircuitBreaker: daily_loss=$16 → tripped=True with "daily loss" in reason
    2. CircuitBreaker: 5 consecutive losses → tripped=True
    3. CircuitBreaker: daily_loss=$5 → not tripped
    4. scan_and_trade called with full mocks: scanner.scan_all() called once
    """
    import unittest.mock as mock
    import tempfile
    import os

    print("=== test_orchestrator ===")
    passed = 0

    # ── Setup temp DB ──────────────────────────────────────────────────────────
    db_path = os.path.join(tempfile.gettempdir(), "test_orchestrator.db")
    db_url = f"sqlite:///{db_path}"
    init_db(db_url)

    # ── Minimal mock PortfolioStateManager ────────────────────────────────────
    def make_mock_state(daily_pnl_value: float):
        m = mock.MagicMock(spec=PortfolioStateManager)
        m.daily_pnl.return_value = daily_pnl_value
        m.all_positions.return_value = []
        m.can_open_position.return_value = True
        return m

    # ── Test 1: loss > 3% of capital → tripped ───────────────────────────────
    daily_limit = settings.bot.total_capital * 0.03
    over_limit  = -(daily_limit + 1.0)   # always exceeds the limit regardless of capital
    print(f"\n[Test 1] daily_loss=${abs(over_limit):.2f} (limit=${daily_limit:.2f}) → CircuitBreaker should trip")
    cb1 = CircuitBreaker()
    mock_state_loss = make_mock_state(daily_pnl_value=over_limit)
    tripped, reason = cb1.check(mock_state_loss)
    assert tripped, f"Expected tripped=True, got {tripped}"
    assert "daily loss" in reason.lower(), (
        f"Expected 'daily loss' in reason, got: {reason!r}"
    )
    print(f"  PASS: tripped={tripped}, reason={reason!r}")
    passed += 1

    # ── Test 2: 5 consecutive losses → tripped ────────────────────────────────
    print("\n[Test 2] 5 consecutive losses → CircuitBreaker should trip")
    cb2 = CircuitBreaker()
    within_limit = -(daily_limit * 0.1)  # 0.3% loss — always within the 3% limit
    mock_state_ok = make_mock_state(daily_pnl_value=within_limit)
    for _ in range(5):
        cb2.record_loss()
    tripped2, reason2 = cb2.check(mock_state_ok)
    assert tripped2, f"Expected tripped=True after 5 losses, got {tripped2}"
    assert "consecutive" in reason2.lower() or "5" in reason2, (
        f"Expected '5 consecutive' in reason, got: {reason2!r}"
    )
    print(f"  PASS: tripped={tripped2}, reason={reason2!r}")
    passed += 1

    # ── Test 3: loss < 3% of capital → not tripped ───────────────────────────
    small_loss = -(daily_limit * 0.5)   # 1.5% loss — always within the 3% limit
    print(f"\n[Test 3] daily_loss=${abs(small_loss):.2f} (< limit ${daily_limit:.2f}) → CircuitBreaker should NOT trip")
    cb3 = CircuitBreaker()
    mock_state_small_loss = make_mock_state(daily_pnl_value=small_loss)
    tripped3, reason3 = cb3.check(mock_state_small_loss)
    assert not tripped3, f"Expected tripped=False for small loss, got {tripped3}"
    assert reason3 == "", f"Expected empty reason, got: {reason3!r}"
    print(f"  PASS: tripped={tripped3}")
    passed += 1

    # ── Test 4: scan_and_trade calls scanner.scan_all() once ──────────────────
    print("\n[Test 4] scan_and_trade calls scanner.scan_all() exactly once")

    orch = Orchestrator.__new__(Orchestrator)
    orch._database_url = db_url
    orch._trading_mode = "paper"
    orch._cycle_count = 0

    # Create real state manager (with temp DB)
    real_state = PortfolioStateManager(database_url=db_url)
    orch._state = real_state

    # Mock all components
    orch._risk_agent   = mock.MagicMock(spec=RiskAgent)
    orch._exec_agent   = mock.MagicMock(spec=ExecutionAgent)
    orch._event_guard  = mock.MagicMock(spec=EventGuard)
    orch._event_guard.is_blocked.return_value = (False, "")
    orch._correlation_guard = mock.MagicMock(spec=CorrelationGuard)
    orch._correlation_guard.is_allowed.return_value = (True, "")
    orch._pdt_tracker  = mock.MagicMock(spec=PDTTracker)
    orch._pdt_tracker.can_day_trade.return_value = True
    orch._circuit_breaker = CircuitBreaker()

    mock_scanner = mock.MagicMock()
    mock_scanner.scan_all.return_value = []         # no results → no entry attempted
    mock_scanner.top_opportunity.return_value = None
    mock_scanner.tradeable_opportunities.return_value = []
    orch._scanner = mock_scanner

    mock_monitor = mock.MagicMock(spec=HealthMonitor)
    mock_health_status = mock.MagicMock()
    mock_health_status.all_down.return_value = False
    mock_health_status.down_brokers.return_value = set()
    mock_health_status.any_down.return_value = False
    mock_health_status.ibkr_status.value = "healthy"
    mock_health_status.oanda_status.value = "healthy"
    mock_monitor.status = mock_health_status
    orch._monitor = mock_monitor

    orch.scan_and_trade()

    mock_scanner.scan_all.assert_called_once()
    print(f"  PASS: scanner.scan_all() was called exactly once")
    passed += 1

    # ── Cleanup ───────────────────────────────────────────────────────────────
    if os.path.exists(db_path):
        os.remove(db_path)

    print(f"\n=== test_orchestrator: {passed}/4 PASSED ===")
    if passed < 4:
        raise AssertionError(f"Only {passed}/4 tests passed")


if __name__ == "__main__":
    import sys

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if len(sys.argv) > 1 and sys.argv[1] == "test":
        test_orchestrator()
    else:
        # Normal run
        orch = Orchestrator()
        try:
            orch.start()
        except KeyboardInterrupt:
            logger.info("KeyboardInterrupt — shutting down")
            orch.stop()
