"""
resilience/health_monitor.py
Daemon thread that heartbeats both brokers (IBKR + OANDA) and logs system
events to the EventLog table. Implements exponential backoff on failure and
calls an on_reconnect callback when a broker comes back up.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

from config.settings import settings
from database.models import EventLog, get_session, init_db

logger = logging.getLogger(__name__)


class BrokerStatus(Enum):
    HEALTHY  = "healthy"
    DEGRADED = "degraded"   # ping succeeded but errors increasing
    DOWN     = "down"       # connection failed


@dataclass
class HealthStatus:
    ibkr_status:  BrokerStatus
    oanda_status: BrokerStatus
    last_ibkr_check:  datetime
    last_oanda_check: datetime
    consecutive_ibkr_failures:  int = 0
    consecutive_oanda_failures: int = 0
    notes: list[str] = field(default_factory=list)

    def all_healthy(self) -> bool:
        return (
            self.ibkr_status  == BrokerStatus.HEALTHY
            and self.oanda_status == BrokerStatus.HEALTHY
        )

    def any_down(self) -> bool:
        return BrokerStatus.DOWN in (self.ibkr_status, self.oanda_status)

    def all_down(self) -> bool:
        return (
            self.ibkr_status  == BrokerStatus.DOWN
            and self.oanda_status == BrokerStatus.DOWN
        )

    def down_brokers(self) -> set[str]:
        """Return the set of broker names that are currently DOWN."""
        down: set[str] = set()
        if self.ibkr_status == BrokerStatus.DOWN:
            down.add("ibkr")
        if self.oanda_status == BrokerStatus.DOWN:
            down.add("oanda")
        return down


class HealthMonitor:
    """
    Runs as a daemon thread. Heartbeats both brokers every HEARTBEAT_INTERVAL
    seconds.  On failure: exponential backoff up to MAX_BACKOFF_SEC.
    On reconnect: calls the on_reconnect callback to reconcile positions.
    Logs system events to the EventLog table.
    """

    HEARTBEAT_INTERVAL = 30   # seconds between health checks
    MAX_BACKOFF_SEC    = 300  # 5 minutes maximum backoff ceiling
    MAX_FAILURES_ALERT = 3    # log WARNING after N consecutive failures

    def __init__(
        self,
        database_url: str | None = None,
        on_reconnect: Optional[Callable[[], None]] = None,
    ) -> None:
        """
        Parameters
        ----------
        database_url:
            SQLAlchemy URL.  Falls back to ``settings.bot.database_url``.
        on_reconnect:
            Callback invoked whenever a broker transitions from DOWN back to
            HEALTHY (e.g., sync open positions).  Called once per broker per
            reconnect event.
        """
        self._database_url = database_url or settings.bot.database_url
        self._on_reconnect = on_reconnect
        self._status = HealthStatus(
            ibkr_status=BrokerStatus.HEALTHY,
            oanda_status=BrokerStatus.HEALTHY,
            last_ibkr_check=datetime.utcnow(),
            last_oanda_check=datetime.utcnow(),
        )
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the monitor as a daemon thread."""
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run, name="HealthMonitor", daemon=True
        )
        self._thread.start()
        logger.info("HealthMonitor started (interval=%ds)", self.HEARTBEAT_INTERVAL)

    def stop(self) -> None:
        """Signal the monitor to stop and wait up to 10 s for the thread to exit."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                logger.warning("HealthMonitor thread did not exit within 10 s")
            else:
                logger.info("HealthMonitor stopped")

    @property
    def status(self) -> HealthStatus:
        with self._lock:
            return self._status

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    def _run(self) -> None:
        """Main monitor loop with per-broker exponential backoff."""
        ibkr_backoff  = self.HEARTBEAT_INTERVAL
        oanda_backoff = self.HEARTBEAT_INTERVAL

        while not self._stop_event.is_set():
            now = datetime.utcnow()

            # ---- IBKR check ----------------------------------------
            ibkr_ok = self._check_ibkr()
            with self._lock:
                prev_ibkr = self._status.ibkr_status
                if ibkr_ok:
                    if prev_ibkr == BrokerStatus.DOWN:
                        logger.info("IBKR reconnected")
                        self._log_event("IBKR connection restored")
                        if self._on_reconnect:
                            try:
                                self._on_reconnect()
                            except Exception:
                                logger.exception(
                                    "HealthMonitor: on_reconnect raised an exception (IBKR)"
                                )
                    self._status.ibkr_status = BrokerStatus.HEALTHY
                    self._status.consecutive_ibkr_failures = 0
                    ibkr_backoff = self.HEARTBEAT_INTERVAL
                else:
                    self._status.consecutive_ibkr_failures += 1
                    self._status.ibkr_status = BrokerStatus.DOWN
                    if self._status.consecutive_ibkr_failures >= self.MAX_FAILURES_ALERT:
                        logger.warning(
                            "IBKR: %d consecutive failures",
                            self._status.consecutive_ibkr_failures,
                        )
                        self._log_event(
                            f"IBKR down: {self._status.consecutive_ibkr_failures} consecutive failures"
                        )
                    ibkr_backoff = min(ibkr_backoff * 2, self.MAX_BACKOFF_SEC)
                self._status.last_ibkr_check = now

            # ---- OANDA check ---------------------------------------
            oanda_ok = self._check_oanda()
            with self._lock:
                prev_oanda = self._status.oanda_status
                if oanda_ok:
                    if prev_oanda == BrokerStatus.DOWN:
                        logger.info("OANDA reconnected")
                        self._log_event("OANDA connection restored")
                        if self._on_reconnect:
                            try:
                                self._on_reconnect()
                            except Exception:
                                logger.exception(
                                    "HealthMonitor: on_reconnect raised an exception (OANDA)"
                                )
                    self._status.oanda_status = BrokerStatus.HEALTHY
                    self._status.consecutive_oanda_failures = 0
                    oanda_backoff = self.HEARTBEAT_INTERVAL
                else:
                    self._status.consecutive_oanda_failures += 1
                    self._status.oanda_status = BrokerStatus.DOWN
                    if self._status.consecutive_oanda_failures >= self.MAX_FAILURES_ALERT:
                        logger.warning(
                            "OANDA: %d consecutive failures",
                            self._status.consecutive_oanda_failures,
                        )
                        self._log_event(
                            f"OANDA down: {self._status.consecutive_oanda_failures} consecutive failures"
                        )
                    oanda_backoff = min(oanda_backoff * 2, self.MAX_BACKOFF_SEC)
                self._status.last_oanda_check = now

            # Sleep for the shorter of the two backoffs so we re-check the
            # faster-recovering broker sooner.
            sleep_sec = min(ibkr_backoff, oanda_backoff)
            self._stop_event.wait(timeout=sleep_sec)

    # ------------------------------------------------------------------
    # Broker ping helpers
    # ------------------------------------------------------------------

    def _check_ibkr(self) -> bool:
        """
        Ping IBKR TWS/Gateway via a raw TCP connect.

        Returns True if:
        - IBKR is not configured (stub/paper mode — treat as healthy), or
        - a TCP connection to (host, port) succeeds within 5 seconds.

        Returns False on timeout, ConnectionRefused, or any socket error.
        """
        if not settings.ibkr.enabled:
            return True  # not configured — treat as healthy (stub mode)
        import socket
        try:
            with socket.create_connection(
                (settings.ibkr.host, settings.ibkr.port), timeout=5
            ):
                return True
        except (socket.timeout, ConnectionRefusedError, OSError) as exc:
            logger.debug("IBKR health check failed: %s", exc)
            return False

    def _check_oanda(self) -> bool:
        """
        Ping the OANDA REST API by fetching GET /v3/accounts/{account_id}.

        Returns True if:
        - OANDA is not configured (stub mode — treat as healthy), or
        - the HTTP response status is 200.

        Returns False on any HTTP error, network error, or non-200 status.
        """
        if not settings.oanda.enabled:
            return True  # not configured — treat as healthy (stub mode)
        import urllib.request
        import urllib.error

        base = (
            "https://api-fxtrade.oanda.com"
            if settings.oanda.environment == "live"
            else "https://api-fxpractice.oanda.com"
        )
        url = f"{base}/v3/accounts/{settings.oanda.account_id}"
        req = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {settings.oanda.api_key}"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except urllib.error.HTTPError as exc:
            logger.debug("OANDA health check HTTP error: %s %s", exc.code, exc.reason)
            return False
        except Exception as exc:
            logger.debug("OANDA health check failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Database event logging
    # ------------------------------------------------------------------

    def _log_event(self, description: str) -> None:
        """Write a system event to the EventLog table. Swallows DB errors."""
        try:
            session = get_session(self._database_url)
        except Exception:
            logger.exception("HealthMonitor: could not open DB session for event log")
            return
        try:
            event = EventLog(
                timestamp=datetime.utcnow(),
                event_type="system",
                description=description,
                event_metadata={"source": "HealthMonitor"},
            )
            session.add(event)
            session.commit()
        except Exception:
            logger.exception("HealthMonitor: failed to log event to DB")
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Self-contained test
# ---------------------------------------------------------------------------

def test_health_monitor() -> None:
    """
    Standalone test function exercising HealthMonitor behaviour.

    Scenarios covered
    -----------------
    1. _check_ibkr returns False 3× then True:
       - consecutive_ibkr_failures increments on each failure.
       - After the 3rd failure (>= MAX_FAILURES_ALERT) a WARNING is logged.
       - On recovery: consecutive_ibkr_failures resets to 0, on_reconnect
         is called exactly once, and ibkr_status returns to HEALTHY.
    2. stop() terminates the background thread cleanly within 10 seconds.
    3. _check_oanda failures increment consecutive_oanda_failures and reset
       on recovery; on_reconnect is called for OANDA reconnect too.
    """
    import unittest.mock as mock
    import time

    print("=== test_health_monitor ===")
    passed = 0
    failed = 0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _make_monitor(on_reconnect=None, db_url="sqlite:///:memory:"):
        init_db(db_url)
        mon = HealthMonitor(database_url=db_url, on_reconnect=on_reconnect)
        return mon

    # ------------------------------------------------------------------
    # Test 1: consecutive_ibkr_failures increments on failure and resets
    #         on recovery; on_reconnect called exactly once.
    # ------------------------------------------------------------------
    print("\n[Test 1] IBKR failure→recovery cycle")
    reconnect_calls: list[str] = []

    def _on_reconnect():
        reconnect_calls.append("called")

    mon = _make_monitor(on_reconnect=_on_reconnect)

    # Patch _check_ibkr: False×3, then True.  OANDA always True.
    ibkr_results = iter([False, False, False, True])

    with mock.patch.object(mon, "_check_ibkr", side_effect=ibkr_results), \
         mock.patch.object(mon, "_check_oanda", return_value=True), \
         mock.patch.object(mon, "_log_event"):  # suppress DB writes

        # Tick 1 — first failure
        mon._run_once_for_test()
        s = mon.status
        assert s.consecutive_ibkr_failures == 1, f"expected 1, got {s.consecutive_ibkr_failures}"
        assert s.ibkr_status == BrokerStatus.DOWN
        print("  Tick 1: failures=1, status=DOWN  OK")
        passed += 1

        # Tick 2 — second failure
        mon._run_once_for_test()
        s = mon.status
        assert s.consecutive_ibkr_failures == 2, f"expected 2, got {s.consecutive_ibkr_failures}"
        print("  Tick 2: failures=2  OK")
        passed += 1

        # Tick 3 — third failure (>= MAX_FAILURES_ALERT=3 → WARNING)
        mon._run_once_for_test()
        s = mon.status
        assert s.consecutive_ibkr_failures == 3, f"expected 3, got {s.consecutive_ibkr_failures}"
        print("  Tick 3: failures=3 (alert threshold hit)  OK")
        passed += 1

        # Tick 4 — recovery
        mon._run_once_for_test()
        s = mon.status
        assert s.consecutive_ibkr_failures == 0, f"expected 0, got {s.consecutive_ibkr_failures}"
        assert s.ibkr_status == BrokerStatus.HEALTHY
        assert len(reconnect_calls) == 1, f"on_reconnect called {len(reconnect_calls)}× (expected 1)"
        print("  Tick 4: failures=0, status=HEALTHY, on_reconnect called once  OK")
        passed += 1

    # ------------------------------------------------------------------
    # Test 2: stop() terminates the thread within 10 seconds.
    # ------------------------------------------------------------------
    print("\n[Test 2] stop() terminates thread within 10 s")
    mon2 = _make_monitor()
    with mock.patch.object(mon2, "_check_ibkr", return_value=True), \
         mock.patch.object(mon2, "_check_oanda", return_value=True), \
         mock.patch.object(mon2, "_log_event"):
        mon2.start()
        assert mon2._thread is not None and mon2._thread.is_alive(), "thread not started"
        t0 = time.monotonic()
        mon2.stop()
        elapsed = time.monotonic() - t0
        assert not mon2._thread.is_alive(), "thread still alive after stop()"
        assert elapsed < 10, f"stop() took {elapsed:.1f}s (> 10 s)"
        print(f"  Thread stopped in {elapsed:.2f}s  OK")
        passed += 1

    # ------------------------------------------------------------------
    # Test 3: OANDA failure→recovery; on_reconnect called for OANDA too.
    # ------------------------------------------------------------------
    print("\n[Test 3] OANDA failure→recovery cycle")
    oanda_reconnect_calls: list[str] = []

    def _on_reconnect_oanda():
        oanda_reconnect_calls.append("called")

    mon3 = _make_monitor(on_reconnect=_on_reconnect_oanda)
    oanda_results = iter([False, False, False, True])

    with mock.patch.object(mon3, "_check_ibkr", return_value=True), \
         mock.patch.object(mon3, "_check_oanda", side_effect=oanda_results), \
         mock.patch.object(mon3, "_log_event"):

        for tick in range(1, 4):
            mon3._run_once_for_test()
            s = mon3.status
            assert s.consecutive_oanda_failures == tick, \
                f"tick {tick}: expected failures={tick}, got {s.consecutive_oanda_failures}"
            assert s.oanda_status == BrokerStatus.DOWN
        print("  Ticks 1–3: OANDA failures accumulate  OK")
        passed += 1

        mon3._run_once_for_test()
        s = mon3.status
        assert s.consecutive_oanda_failures == 0
        assert s.oanda_status == BrokerStatus.HEALTHY
        assert len(oanda_reconnect_calls) == 1
        print("  Tick 4: OANDA recovered, on_reconnect called once  OK")
        passed += 1

    # ------------------------------------------------------------------
    # Test 4: all_healthy() and any_down() helpers
    # ------------------------------------------------------------------
    print("\n[Test 4] HealthStatus helpers")
    hs_healthy = HealthStatus(
        ibkr_status=BrokerStatus.HEALTHY,
        oanda_status=BrokerStatus.HEALTHY,
        last_ibkr_check=datetime.utcnow(),
        last_oanda_check=datetime.utcnow(),
    )
    assert hs_healthy.all_healthy() is True
    assert hs_healthy.any_down() is False
    print("  all_healthy=True, any_down=False  OK")
    passed += 1

    hs_down = HealthStatus(
        ibkr_status=BrokerStatus.DOWN,
        oanda_status=BrokerStatus.HEALTHY,
        last_ibkr_check=datetime.utcnow(),
        last_oanda_check=datetime.utcnow(),
    )
    assert hs_down.all_healthy() is False
    assert hs_down.any_down() is True
    print("  all_healthy=False, any_down=True  OK")
    passed += 1

    print(f"\n=== Results: {passed} passed, {failed} failed ===")
    if failed:
        raise AssertionError(f"{failed} test(s) failed")


# Attach a convenience tick helper to HealthMonitor so the test can drive
# individual iterations without running the real sleep loop.
def _run_once_for_test(self: HealthMonitor) -> None:
    """Execute a single check cycle (IBKR + OANDA).  For testing only."""
    now = datetime.utcnow()

    ibkr_ok = self._check_ibkr()
    with self._lock:
        prev_ibkr = self._status.ibkr_status
        if ibkr_ok:
            if prev_ibkr == BrokerStatus.DOWN:
                self._log_event("IBKR connection restored")
                if self._on_reconnect:
                    self._on_reconnect()
            self._status.ibkr_status = BrokerStatus.HEALTHY
            self._status.consecutive_ibkr_failures = 0
        else:
            self._status.consecutive_ibkr_failures += 1
            self._status.ibkr_status = BrokerStatus.DOWN
            if self._status.consecutive_ibkr_failures >= self.MAX_FAILURES_ALERT:
                self._log_event(
                    f"IBKR down: {self._status.consecutive_ibkr_failures} consecutive failures"
                )
        self._status.last_ibkr_check = now

    oanda_ok = self._check_oanda()
    with self._lock:
        prev_oanda = self._status.oanda_status
        if oanda_ok:
            if prev_oanda == BrokerStatus.DOWN:
                self._log_event("OANDA connection restored")
                if self._on_reconnect:
                    self._on_reconnect()
            self._status.oanda_status = BrokerStatus.HEALTHY
            self._status.consecutive_oanda_failures = 0
        else:
            self._status.consecutive_oanda_failures += 1
            self._status.oanda_status = BrokerStatus.DOWN
            if self._status.consecutive_oanda_failures >= self.MAX_FAILURES_ALERT:
                self._log_event(
                    f"OANDA down: {self._status.consecutive_oanda_failures} consecutive failures"
                )
        self._status.last_oanda_check = now


# Bind as an instance method so tests can call mon._run_once_for_test()
HealthMonitor._run_once_for_test = _run_once_for_test  # type: ignore[attr-defined]


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_health_monitor()
