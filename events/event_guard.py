from __future__ import annotations
from datetime import datetime, timedelta
from typing import Optional
import logging

from events.macro_calendar import MacroCalendar

logger = logging.getLogger(__name__)


class EventGuard:
    """
    Checks whether trading a symbol is currently blocked due to a market event.
    Acts as a thin wrapper around MacroCalendar with refresh logic.
    """
    REFRESH_INTERVAL_HOURS = 24.0

    def __init__(self, calendar: Optional[MacroCalendar] = None):
        self._calendar = calendar or MacroCalendar()

    def is_blocked(self, symbol: str, asset_type: str) -> tuple[bool, str]:
        """
        Returns (blocked: bool, reason: str).

        Logic:
        - Refresh calendar if stale (> 24h since last refresh)
        - Check active blackout events for symbol
        - If any active: return (True, "<event name> blackout active until <time>")
        - Else: return (False, "")
        """
        self.refresh_if_stale()
        active = self._calendar.active_events(symbol)
        if active:
            event = active[0]
            reason = f"{event.name} blackout active until {event.blackout_end.strftime('%Y-%m-%d %H:%M')} UTC"
            return True, reason
        return False, ""

    def refresh_if_stale(self) -> None:
        """Refresh calendar if > 24h since last refresh."""
        last = self._calendar.last_refresh
        if last is None or (datetime.utcnow() - last) > timedelta(hours=self.REFRESH_INTERVAL_HOURS):
            self._calendar.purge_old_events()
            self._calendar.mark_refreshed()

    @property
    def calendar(self) -> MacroCalendar:
        return self._calendar


def test_event_guard() -> None:
    """
    Standalone tests for EventGuard.

    Test 1: Earnings event happening right now -> is_blocked returns True.
    Test 2: Earnings event from 2 weeks ago (expired blackout) -> is_blocked returns False.
    Test 3: Different symbol from the earnings event -> not blocked.
    """
    print("Running EventGuard tests...")

    # ------------------------------------------------------------------ #
    # Test 1: Active earnings event for AAPL -> blocked
    # ------------------------------------------------------------------ #
    cal1 = MacroCalendar()
    # Event time is "now", so we are squarely inside the blackout window
    # (2h before + 4h after the event_time = 6h total window).
    cal1.add_earnings("AAPL", datetime.utcnow())
    guard1 = EventGuard(calendar=cal1)

    blocked, reason = guard1.is_blocked("AAPL", "stock")
    assert blocked, f"Test 1 FAILED: expected blocked=True, got blocked={blocked}"
    assert "AAPL" in reason, f"Test 1 FAILED: reason should mention AAPL, got: {reason}"
    print(f"  Test 1 PASSED: AAPL blocked — {reason}")

    # ------------------------------------------------------------------ #
    # Test 2: Earnings event 2 weeks ago -> blackout long expired -> not blocked
    # ------------------------------------------------------------------ #
    cal2 = MacroCalendar()
    two_weeks_ago = datetime.utcnow() - timedelta(weeks=2)
    cal2.add_earnings("AAPL", two_weeks_ago)
    guard2 = EventGuard(calendar=cal2)

    blocked, reason = guard2.is_blocked("AAPL", "stock")
    assert not blocked, f"Test 2 FAILED: expected blocked=False, got blocked={blocked}, reason={reason}"
    print("  Test 2 PASSED: expired AAPL earnings — not blocked")

    # ------------------------------------------------------------------ #
    # Test 3: Active AAPL earnings event but querying a different symbol -> not blocked
    # ------------------------------------------------------------------ #
    cal3 = MacroCalendar()
    cal3.add_earnings("AAPL", datetime.utcnow())
    guard3 = EventGuard(calendar=cal3)

    blocked, reason = guard3.is_blocked("SPY", "stock")
    assert not blocked, f"Test 3 FAILED: expected SPY not blocked, got blocked={blocked}, reason={reason}"
    print("  Test 3 PASSED: AAPL earnings does not block SPY")

    print("All EventGuard tests passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_event_guard()
