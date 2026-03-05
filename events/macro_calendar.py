from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
import logging

logger = logging.getLogger(__name__)


@dataclass
class MarketEvent:
    name: str                    # "Earnings: AAPL", "FOMC Meeting", "CPI Release"
    event_type: str              # "earnings" | "fomc" | "macro" | "options_expiry"
    symbol: Optional[str]        # None for market-wide events (FOMC, CPI)
    event_time: datetime         # UTC
    blackout_before_hours: float # hours before event to block trading
    blackout_after_hours: float  # hours after event to block trading

    @property
    def blackout_start(self) -> datetime:
        return self.event_time - timedelta(hours=self.blackout_before_hours)

    @property
    def blackout_end(self) -> datetime:
        return self.event_time + timedelta(hours=self.blackout_after_hours)

    def is_active(self, now: Optional[datetime] = None) -> bool:
        """True if we're currently inside the blackout window."""
        now = now or datetime.utcnow()
        return self.blackout_start <= now <= self.blackout_end

    def affects_symbol(self, symbol: str) -> bool:
        """True if this event affects the given symbol."""
        # Market-wide events affect all symbols
        # Earnings events only affect the specific symbol
        return self.symbol is None or self.symbol == symbol


class MacroCalendar:
    """
    Maintains a list of market events.

    Phase 3 note: In production this would call an earnings API.
    For Phase 2, it uses a manually-managed list + hardcoded recurring events.
    """

    # Blackout defaults by event type
    BLACKOUT_DEFAULTS = {
        "earnings":       {"before": 2.0, "after": 4.0},   # 2h before, 4h after
        "fomc":           {"before": 2.0, "after": 4.0},
        "macro":          {"before": 0.5, "after": 1.0},   # CPI, NFP
        "options_expiry": {"before": 0.0, "after": 2.0},
    }

    def __init__(self):
        self._events: list[MarketEvent] = []
        self._last_refresh: Optional[datetime] = None
        self._load_static_events()

    def _load_static_events(self) -> None:
        """Load known recurring/upcoming events. Extend this manually."""
        # This is the seed list — in production, call an earnings API here
        self._events = []

    def add_event(self, event: MarketEvent) -> None:
        self._events.append(event)

    def add_earnings(self, symbol: str, event_time: datetime) -> None:
        defaults = self.BLACKOUT_DEFAULTS["earnings"]
        self._events.append(MarketEvent(
            name=f"Earnings: {symbol}",
            event_type="earnings",
            symbol=symbol,
            event_time=event_time,
            blackout_before_hours=defaults["before"],
            blackout_after_hours=defaults["after"],
        ))

    def add_fomc(self, event_time: datetime) -> None:
        defaults = self.BLACKOUT_DEFAULTS["fomc"]
        self._events.append(MarketEvent(
            name="FOMC Meeting",
            event_type="fomc",
            symbol=None,
            event_time=event_time,
            blackout_before_hours=defaults["before"],
            blackout_after_hours=defaults["after"],
        ))

    def add_macro(self, name: str, event_time: datetime) -> None:
        """Add CPI, NFP, or similar macro event."""
        defaults = self.BLACKOUT_DEFAULTS["macro"]
        self._events.append(MarketEvent(
            name=name,
            event_type="macro",
            symbol=None,
            event_time=event_time,
            blackout_before_hours=defaults["before"],
            blackout_after_hours=defaults["after"],
        ))

    def active_events(self, symbol: str, now: Optional[datetime] = None) -> list[MarketEvent]:
        """Return all events currently in blackout that affect the given symbol."""
        now = now or datetime.utcnow()
        return [e for e in self._events if e.is_active(now) and e.affects_symbol(symbol)]

    def upcoming_events(self, symbol: str, hours_ahead: float = 24.0, now: Optional[datetime] = None) -> list[MarketEvent]:
        """Return events starting within the next N hours that affect the given symbol."""
        now = now or datetime.utcnow()
        cutoff = now + timedelta(hours=hours_ahead)
        return [
            e for e in self._events
            if e.affects_symbol(symbol) and now <= e.blackout_start <= cutoff
        ]

    def purge_old_events(self) -> None:
        """Remove events whose blackout_end is in the past."""
        now = datetime.utcnow()
        before = len(self._events)
        self._events = [e for e in self._events if e.blackout_end >= now]
        removed = before - len(self._events)
        if removed:
            logger.debug("MacroCalendar: purged %d expired event(s).", removed)

    @property
    def last_refresh(self) -> Optional[datetime]:
        return self._last_refresh

    def mark_refreshed(self) -> None:
        self._last_refresh = datetime.utcnow()
