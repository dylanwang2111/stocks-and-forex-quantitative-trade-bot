"""
portfolio/pdt_tracker.py
Pattern Day Trader (PDT) rule tracker.

PDT Rule: A US stock account with < $25k equity may execute at most 3 day trades
in any rolling 5-business-day window. A "day trade" is defined as opening and
closing a stock position on the same calendar day.

Only IBKR (stock) trades count toward PDT. OANDA (forex) trades are exempt.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from database.models import Trade, get_session
from portfolio.watchlist import UNIVERSE_BY_SYMBOL
from config.settings import settings

logger = logging.getLogger(__name__)

# Brokers whose trades are subject to PDT rules
_PDT_BROKERS: tuple[str, ...] = ("ibkr",)


def _subtract_business_days(ref_date: date, n: int) -> date:
    """
    Return the date that is exactly `n` business days before `ref_date`.

    Iterates backward through calendar days, counting only Mon-Fri.
    The result is the *start* of the business-day window — any trade whose
    exit_time.date() >= result falls inside the rolling window.
    """
    current = ref_date
    counted = 0
    while counted < n:
        current -= timedelta(days=1)
        if current.weekday() < 5:  # 0=Mon … 4=Fri
            counted += 1
    return current


class PDTTracker:
    """Tracks and enforces the FINRA Pattern Day Trader (PDT) rule."""

    PDT_LIMIT = 3  # max day trades in rolling 5-business-day window

    def __init__(self, database_url: str | None = None) -> None:
        self._database_url: str = database_url or settings.bot.database_url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def count_day_trades_rolling(self) -> int:
        """
        Count same-day-closed stock trades in the past 5 business days.

        A trade qualifies when ALL of:
        - broker is in _PDT_BROKERS (i.e. "ibkr")
        - status == "closed"
        - exit_time >= cutoff (5 business days ago from today, inclusive)
        - entry_time.date() == exit_time.date()  (same calendar day)
        """
        today = datetime.utcnow().date()
        cutoff_date = _subtract_business_days(today, self.PDT_LIMIT)
        # Convert to datetime at midnight so the DB filter is precise
        cutoff_dt = datetime(cutoff_date.year, cutoff_date.month, cutoff_date.day, 0, 0, 0)

        with get_session(self._database_url) as session:
            candidates = (
                session.query(Trade)
                .filter(
                    Trade.broker.in_(list(_PDT_BROKERS)),
                    Trade.status == "closed",
                    Trade.exit_time >= cutoff_dt,
                )
                .all()
            )

        count = sum(
            1
            for t in candidates
            if t.entry_time is not None
            and t.exit_time is not None
            and t.entry_time.date() == t.exit_time.date()
        )

        logger.debug(
            "PDT rolling count: %d day trade(s) since %s (cutoff %s)",
            count,
            cutoff_date,
            cutoff_dt,
        )
        return count

    def can_day_trade(self) -> bool:
        """
        Return True if another day trade is allowed under PDT rules.

        Returns False when the rolling 5-business-day count has already
        reached or exceeded PDT_LIMIT (3).
        """
        count = self.count_day_trades_rolling()
        allowed = count < self.PDT_LIMIT
        if not allowed:
            logger.warning(
                "PDT limit reached: %d/%d day trades used in the past 5 business days. "
                "No further stock day trades permitted.",
                count,
                self.PDT_LIMIT,
            )
        return allowed

    def is_day_trade(self, symbol: str, entry_time: datetime) -> bool:
        """
        Determine whether closing a position for `symbol` RIGHT NOW counts as a day trade.

        Returns True when:
        1. The instrument is a stock (asset_type == "stock" in UNIVERSE_BY_SYMBOL), AND
        2. entry_time.date() == datetime.utcnow().date()  (entered today, closing today)

        Forex instruments (OANDA) are never day trades for PDT purposes.
        Unknown symbols are treated conservatively as non-stocks (returns False with a warning).
        """
        instrument = UNIVERSE_BY_SYMBOL.get(symbol)
        if instrument is None:
            logger.warning(
                "is_day_trade: unknown symbol '%s' — treating as non-stock (PDT-exempt).",
                symbol,
            )
            return False

        if instrument.asset_type != "stock":
            return False

        today = datetime.utcnow().date()
        result = entry_time.date() == today

        logger.debug(
            "is_day_trade(%s): entry_date=%s today=%s → %s",
            symbol,
            entry_time.date(),
            today,
            result,
        )
        return result

    def record_day_trade(
        self,
        symbol: str,
        entry_time: datetime,
        exit_time: datetime,
        pnl_usd: float,
    ) -> None:
        """
        Log a completed day trade.

        The authoritative Trade record is already written to the database by
        PortfolioStateManager. This method only emits a structured log entry so
        that the PDT event is easily searchable in application logs.
        """
        logger.info(
            "DAY_TRADE recorded | symbol=%s | entry=%s | exit=%s | pnl=%.2f USD",
            symbol,
            entry_time.strftime("%Y-%m-%d %H:%M:%S"),
            exit_time.strftime("%Y-%m-%d %H:%M:%S"),
            pnl_usd,
        )

    def days_until_reset(self) -> int:
        """
        Return the number of business days until the oldest qualifying day trade
        rolls out of the 5-business-day window.

        Returns 0 if the account is currently under the PDT limit (no urgency).

        Algorithm:
        - If count < PDT_LIMIT → return 0
        - Otherwise query the oldest exit_time among day trades in the window,
          and calculate how many business days from today until that trade's
          exit_date exits the 5-business-day rolling window.
        """
        if self.count_day_trades_rolling() < self.PDT_LIMIT:
            return 0

        today = datetime.utcnow().date()
        cutoff_date = _subtract_business_days(today, self.PDT_LIMIT)
        cutoff_dt = datetime(cutoff_date.year, cutoff_date.month, cutoff_date.day, 0, 0, 0)

        with get_session(self._database_url) as session:
            candidates = (
                session.query(Trade)
                .filter(
                    Trade.broker.in_(list(_PDT_BROKERS)),
                    Trade.status == "closed",
                    Trade.exit_time >= cutoff_dt,
                )
                .all()
            )

        day_trades = [
            t
            for t in candidates
            if t.entry_time is not None
            and t.exit_time is not None
            and t.entry_time.date() == t.exit_time.date()
        ]

        if not day_trades:
            return 0

        # The oldest day trade's exit date
        oldest_exit_date: date = min(t.exit_time.date() for t in day_trades)

        # That trade rolls off once today is 5 business days past oldest_exit_date.
        # Count forward from oldest_exit_date until we accumulate 5 business days
        # to find the first day the trade is no longer in the window.
        roll_off_date = oldest_exit_date
        bdays_counted = 0
        while bdays_counted < self.PDT_LIMIT:
            roll_off_date += timedelta(days=1)
            if roll_off_date.weekday() < 5:
                bdays_counted += 1

        # Now count business days from today to roll_off_date
        if roll_off_date <= today:
            return 0

        remaining = 0
        cursor = today
        while cursor < roll_off_date:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                remaining += 1

        logger.debug(
            "days_until_reset: oldest_exit=%s roll_off=%s remaining=%d business day(s)",
            oldest_exit_date,
            roll_off_date,
            remaining,
        )
        return remaining


# ---------------------------------------------------------------------------
# Self-test (run with: python -m portfolio.pdt_tracker)
# ---------------------------------------------------------------------------

def test_pdt_tracker() -> None:
    """
    Lightweight in-memory unit tests — no live DB required.

    Tests covered:
    1. count=2 → can_day_trade() is True
    2. count=3 → can_day_trade() is False
    3. Forex symbol → is_day_trade() is False regardless of entry date
    4. Swing trade (entry yesterday, close today) → NOT a day trade
    5. Same-day stock trade → IS a day trade
    6. days_until_reset() returns 0 when under limit
    """
    import sqlite3
    import tempfile
    import os
    from database.models import init_db

    print("Running PDTTracker self-tests …")
    failures: list[str] = []

    def assert_eq(label: str, actual, expected) -> None:
        if actual != expected:
            failures.append(f"FAIL [{label}]: expected {expected!r}, got {actual!r}")
        else:
            print(f"  PASS  {label}")

    # ── Helpers ────────────────────────────────────────────────────────────────

    def make_db_with_trades(trade_rows: list[dict]) -> str:
        """Create a temp SQLite DB, insert Trade rows, return database_url."""
        tmp = tempfile.mktemp(suffix=".db")
        db_url = f"sqlite:///{tmp}"
        init_db(db_url)

        engine_conn = sqlite3.connect(tmp)
        cur = engine_conn.cursor()
        for row in trade_rows:
            cur.execute(
                """
                INSERT INTO trades
                    (symbol, broker, direction, entry_price, exit_price, quantity,
                     confidence, position_tier, pnl_usd, entry_time, exit_time, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["symbol"],
                    row["broker"],
                    row.get("direction", "long"),
                    row.get("entry_price", 100.0),
                    row.get("exit_price", 101.0),
                    row.get("quantity", 1.0),
                    row.get("confidence", 70.0),
                    row.get("position_tier", "medium"),
                    row.get("pnl_usd", 1.0),
                    row["entry_time"],
                    row["exit_time"],
                    row.get("status", "closed"),
                ),
            )
        engine_conn.commit()
        engine_conn.close()
        return db_url, tmp

    today = datetime.utcnow().date()
    yesterday = today - timedelta(days=1)

    def dt(d: date, hour: int = 10) -> str:
        return datetime(d.year, d.month, d.day, hour, 0, 0).strftime("%Y-%m-%d %H:%M:%S")

    # ── Test 1 & 2: count=2 → can_trade True; count=3 → can_trade False ────────
    # Build 2 day trades in the window
    two_day_trades = [
        dict(symbol="SPY", broker="ibkr", entry_time=dt(today, 9), exit_time=dt(today, 15)),
        dict(symbol="AAPL", broker="ibkr", entry_time=dt(today, 9), exit_time=dt(today, 14)),
    ]
    db_url2, tmp2 = make_db_with_trades(two_day_trades)
    tracker2 = PDTTracker(database_url=db_url2)
    assert_eq("count=2 → can_day_trade() True", tracker2.can_day_trade(), True)
    assert_eq("count=2 → count_day_trades_rolling()==2", tracker2.count_day_trades_rolling(), 2)
    assert_eq("count=2 → days_until_reset()==0", tracker2.days_until_reset(), 0)
    os.unlink(tmp2)

    # Build 3 day trades in the window
    three_day_trades = two_day_trades + [
        dict(symbol="NVDA", broker="ibkr", entry_time=dt(today, 9), exit_time=dt(today, 13)),
    ]
    db_url3, tmp3 = make_db_with_trades(three_day_trades)
    tracker3 = PDTTracker(database_url=db_url3)
    assert_eq("count=3 → can_day_trade() False", tracker3.can_day_trade(), False)
    assert_eq("count=3 → count_day_trades_rolling()==3", tracker3.count_day_trades_rolling(), 3)
    # days_until_reset should be > 0 when at limit with trades from today
    dur = tracker3.days_until_reset()
    if dur <= 0:
        failures.append(f"FAIL [days_until_reset > 0 when at limit]: got {dur}")
    else:
        print(f"  PASS  days_until_reset > 0 when at limit (got {dur})")
    os.unlink(tmp3)

    # ── Test 3: Forex symbol → is_day_trade() always False ─────────────────────
    empty_db_url, tmp_empty = make_db_with_trades([])
    tracker_empty = PDTTracker(database_url=empty_db_url)
    forex_entry = datetime(today.year, today.month, today.day, 8, 0, 0)
    assert_eq(
        "EURUSD (forex) → is_day_trade() False",
        tracker_empty.is_day_trade("EURUSD", forex_entry),
        False,
    )
    assert_eq(
        "GBPUSD (forex) → is_day_trade() False",
        tracker_empty.is_day_trade("GBPUSD", forex_entry),
        False,
    )

    # ── Test 4: Swing trade (entry yesterday, close today) → NOT counted ────────
    swing_trades = [
        dict(
            symbol="SPY",
            broker="ibkr",
            entry_time=dt(yesterday, 14),  # entered YESTERDAY
            exit_time=dt(today, 10),        # closed TODAY — different dates → NOT a day trade
        ),
    ]
    db_url_swing, tmp_swing = make_db_with_trades(swing_trades)
    tracker_swing = PDTTracker(database_url=db_url_swing)
    assert_eq(
        "Swing trade (entry yesterday → close today) not counted as day trade",
        tracker_swing.count_day_trades_rolling(),
        0,
    )
    os.unlink(tmp_swing)

    # ── Test 5: Same-day stock trade → is_day_trade() True ─────────────────────
    same_day_entry = datetime(today.year, today.month, today.day, 9, 30, 0)
    assert_eq(
        "SPY same-day entry → is_day_trade() True",
        tracker_empty.is_day_trade("SPY", same_day_entry),
        True,
    )
    # Previous-day entry → False
    prev_entry = datetime(yesterday.year, yesterday.month, yesterday.day, 9, 30, 0)
    assert_eq(
        "SPY previous-day entry → is_day_trade() False",
        tracker_empty.is_day_trade("SPY", prev_entry),
        False,
    )

    os.unlink(tmp_empty)

    # ── Summary ────────────────────────────────────────────────────────────────
    if failures:
        print("\nTest failures:")
        for f in failures:
            print(f"  {f}")
        raise AssertionError(f"{len(failures)} test(s) failed.")
    else:
        print("\nAll PDTTracker tests passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_pdt_tracker()
