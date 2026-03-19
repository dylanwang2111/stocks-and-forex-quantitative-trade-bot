"""
optimization/performance_report.py
Generates a performance report from the trade database for the optimization pipeline.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import ClassVar, Optional

from config.settings import settings
from database.models import EventLog, Trade, get_session, init_db

logger = logging.getLogger(__name__)


@dataclass
class CategoryMetrics:
    category: str       # "cat1", "cat2", ... "cat8"
    total_votes: int
    bull_votes: int
    bear_votes: int
    accuracy: float     # % of time vote direction matched trade outcome


@dataclass
class TierMetrics:
    tier: str           # "SMALL" | "MEDIUM" | "LARGE" | "FULL"
    trade_count: int
    win_rate: float
    avg_pnl_usd: float
    avg_pnl_pct: float
    total_pnl_usd: float


@dataclass
class RegimeMetrics:
    regime: str
    trade_count: int
    win_rate: float
    avg_pnl_usd: float
    total_pnl_usd: float


@dataclass
class PerformanceReport:
    period_start: datetime
    period_end: datetime
    total_trades: int
    closed_trades: int
    win_rate: float
    avg_pnl_usd: float
    total_pnl_usd: float
    sharpe_ratio: float         # simplified: mean_pnl / std_pnl * sqrt(52)
    max_drawdown: float         # max consecutive loss streak in USD
    by_tier: list[TierMetrics]
    by_regime: list[RegimeMetrics]
    meets_min_trades: bool      # total_trades >= MIN_TRADES_THRESHOLD

    # ClassVar excludes this from __init__ and keeps it as a real class attribute.
    MIN_TRADES_THRESHOLD: ClassVar[int] = 50

    def to_dict(self) -> dict:
        """Return a flat dict suitable for JSON serialization."""
        return {
            "period_start": self.period_start.isoformat(),
            "period_end": self.period_end.isoformat(),
            "total_trades": self.total_trades,
            "closed_trades": self.closed_trades,
            "win_rate": self.win_rate,
            "avg_pnl_usd": self.avg_pnl_usd,
            "total_pnl_usd": self.total_pnl_usd,
            "sharpe_ratio": self.sharpe_ratio,
            "max_drawdown": self.max_drawdown,
            "meets_min_trades": self.meets_min_trades,
            "by_tier": [m.__dict__ for m in self.by_tier],
            "by_regime": [m.__dict__ for m in self.by_regime],
        }

    def summary_text(self) -> str:
        """Return a human-readable multi-line summary of the report."""
        tier_parts = " ".join(
            f"{m.tier}({m.trade_count} trades, {m.win_rate * 100:.0f}% WR)"
            for m in self.by_tier
        )
        regime_parts = " ".join(
            f"{m.regime}({m.trade_count} trades, {m.win_rate * 100:.0f}% WR)"
            for m in self.by_regime
        )
        min_trades_label = "YES" if self.meets_min_trades else "NO"
        lines = [
            f"Performance Report ({self.period_start.date()} to {self.period_end.date()})",
            (
                f"Total trades: {self.total_trades} | "
                f"Closed: {self.closed_trades} | "
                f"Win rate: {self.win_rate * 100:.1f}%"
            ),
            (
                f"Total P&L: ${self.total_pnl_usd:.2f} | "
                f"Avg P&L: ${self.avg_pnl_usd:.2f} | "
                f"Sharpe: {self.sharpe_ratio:.2f}"
            ),
            f"Tier breakdown: {tier_parts}" if tier_parts else "Tier breakdown: (none)",
            f"Regime: {regime_parts}" if regime_parts else "Regime: (none)",
            f"Min trades met: {min_trades_label}",
        ]
        return "\n".join(lines)


class PerformanceReportGenerator:
    """Generates PerformanceReport from the trade database."""

    def __init__(self, database_url: str | None = None) -> None:
        self._db = database_url or settings.bot.database_url

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def generate(self, weeks: int = 5) -> PerformanceReport:
        """
        Generate a performance report for the last N weeks of closed trades.

        Steps:
        1. period_start = utcnow - timedelta(weeks=weeks)
        2. Query all closed Trade rows where exit_time >= period_start
        3. Compute aggregate stats (win_rate, avg_pnl, total_pnl)
        4. Compute simplified Sharpe: mean(pnl_usd) / std(pnl_usd) * sqrt(52)
           (weekly scaling); if std == 0 → sharpe = 0
        5. Compute max_drawdown as max consecutive negative pnl streak (sum of losses)
        6. Group by position_tier → TierMetrics list
        7. Group by regime → RegimeMetrics list
        8. Return PerformanceReport
        """
        period_start = datetime.utcnow() - timedelta(weeks=weeks)
        period_end = datetime.utcnow()

        logger.info(
            "Generating performance report from %s to %s",
            period_start.date(),
            period_end.date(),
        )

        trades = self._fetch_closed_trades(period_start)
        partial_count = self._count_partial_closes(period_start)

        total_trades = len(trades) + partial_count
        closed_trades = total_trades

        if len(trades) == 0:
            logger.warning("No closed trades found in the requested period.")
            return PerformanceReport(
                period_start=period_start,
                period_end=period_end,
                total_trades=0,
                closed_trades=0,
                win_rate=0.0,
                avg_pnl_usd=0.0,
                total_pnl_usd=0.0,
                sharpe_ratio=0.0,
                max_drawdown=0.0,
                by_tier=[],
                by_regime=[],
                meets_min_trades=False,
            )

        pnl_values = [t.pnl_usd for t in trades if t.pnl_usd is not None]
        wins = [p for p in pnl_values if p > 0]

        win_rate = len(wins) / len(pnl_values) if pnl_values else 0.0
        total_pnl_usd = sum(pnl_values)
        avg_pnl_usd = total_pnl_usd / len(pnl_values) if pnl_values else 0.0
        sharpe_ratio = self._compute_sharpe(pnl_values)
        max_drawdown = self._compute_max_drawdown(pnl_values)

        by_tier = self._group_by_tier(trades)
        by_regime = self._group_by_regime(trades)

        meets_min_trades = total_trades >= PerformanceReport.MIN_TRADES_THRESHOLD

        return PerformanceReport(
            period_start=period_start,
            period_end=period_end,
            total_trades=total_trades,
            closed_trades=closed_trades,
            win_rate=win_rate,
            avg_pnl_usd=avg_pnl_usd,
            total_pnl_usd=total_pnl_usd,
            sharpe_ratio=sharpe_ratio,
            max_drawdown=max_drawdown,
            by_tier=by_tier,
            by_regime=by_regime,
            meets_min_trades=meets_min_trades,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_closed_trades(self, period_start: datetime) -> list[Trade]:
        """Fetch all closed trades with exit_time >= period_start."""
        session = get_session(self._db)
        try:
            rows = (
                session.query(Trade)
                .filter(
                    Trade.status == "closed",
                    Trade.exit_time >= period_start,
                )
                .order_by(Trade.exit_time)
                .all()
            )
            # Detach objects from session so they can be used after close
            session.expunge_all()
            return rows
        except Exception:
            logger.exception("Error fetching closed trades from database.")
            return []
        finally:
            session.close()

    def _count_partial_closes(self, period_start: datetime) -> int:
        """Count partial_close EventLog entries since period_start."""
        session = get_session(self._db)
        try:
            return (
                session.query(EventLog)
                .filter(
                    EventLog.event_type == "partial_close",
                    EventLog.timestamp >= period_start,
                )
                .count()
            )
        except Exception:
            return 0
        finally:
            session.close()

    def _compute_sharpe(self, pnl_values: list[float]) -> float:
        """Simplified Sharpe: mean(pnl) / std(pnl) * sqrt(52)."""
        if not pnl_values:
            return 0.0
        n = len(pnl_values)
        mean = sum(pnl_values) / n
        if n < 2:
            return 0.0
        variance = sum((x - mean) ** 2 for x in pnl_values) / (n - 1)
        std = math.sqrt(variance)
        if std == 0.0:
            return 0.0
        return (mean / std) * math.sqrt(52)

    def _compute_max_drawdown(self, pnl_values: list[float]) -> float:
        """
        Max consecutive loss streak in USD.
        Walk through pnl_values; accumulate losses while in a losing streak.
        Track the worst (most negative) streak total.
        """
        if not pnl_values:
            return 0.0
        max_dd = 0.0
        current_streak = 0.0
        for pnl in pnl_values:
            if pnl < 0:
                current_streak += pnl
                if current_streak < max_dd:
                    max_dd = current_streak
            else:
                current_streak = 0.0
        return max_dd  # negative number (or 0); caller may abs() as needed

    def _group_by_tier(self, trades: list[Trade]) -> list[TierMetrics]:
        """Group trades by position_tier and compute per-tier metrics."""
        tier_map: dict[str, list[Trade]] = {}
        for trade in trades:
            tier = trade.position_tier or "UNKNOWN"
            tier_map.setdefault(tier, []).append(trade)

        result: list[TierMetrics] = []
        for tier, tier_trades in sorted(tier_map.items()):
            pnl_usds = [t.pnl_usd for t in tier_trades if t.pnl_usd is not None]
            pnl_pcts = [t.pnl_pct for t in tier_trades if t.pnl_pct is not None]
            count = len(tier_trades)
            wins = sum(1 for p in pnl_usds if p > 0)
            win_rate = wins / len(pnl_usds) if pnl_usds else 0.0
            total_pnl = sum(pnl_usds)
            avg_pnl_usd = total_pnl / len(pnl_usds) if pnl_usds else 0.0
            avg_pnl_pct = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0
            result.append(
                TierMetrics(
                    tier=tier,
                    trade_count=count,
                    win_rate=win_rate,
                    avg_pnl_usd=avg_pnl_usd,
                    avg_pnl_pct=avg_pnl_pct,
                    total_pnl_usd=total_pnl,
                )
            )
        return result

    def _group_by_regime(self, trades: list[Trade]) -> list[RegimeMetrics]:
        """Group trades by regime and compute per-regime metrics."""
        regime_map: dict[str, list[Trade]] = {}
        for trade in trades:
            regime = trade.regime or "UNKNOWN"
            regime_map.setdefault(regime, []).append(trade)

        result: list[RegimeMetrics] = []
        for regime, regime_trades in sorted(regime_map.items()):
            pnl_usds = [t.pnl_usd for t in regime_trades if t.pnl_usd is not None]
            count = len(regime_trades)
            wins = sum(1 for p in pnl_usds if p > 0)
            win_rate = wins / len(pnl_usds) if pnl_usds else 0.0
            total_pnl = sum(pnl_usds)
            avg_pnl_usd = total_pnl / len(pnl_usds) if pnl_usds else 0.0
            result.append(
                RegimeMetrics(
                    regime=regime,
                    trade_count=count,
                    win_rate=win_rate,
                    avg_pnl_usd=avg_pnl_usd,
                    total_pnl_usd=total_pnl,
                )
            )
        return result


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_performance_report() -> None:
    """
    Integration test using a temporary SQLite database.
    - Creates 5 closed trades (3 wins, 2 losses)
    - Verifies win_rate = 0.60
    - Verifies total_pnl = sum of pnl_usd values
    - Verifies meets_min_trades = False (< 50)
    """
    import tempfile
    import os

    print("Running test_performance_report...")

    # --- Setup temp DB ---
    tmp_file = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp_file.close()
    db_url = f"sqlite:///{tmp_file.name}"

    init_db(db_url)
    session = get_session(db_url)

    now = datetime.utcnow()
    trade_data = [
        # (pnl_usd, pnl_pct, position_tier, regime, direction)
        (5.00,  0.015, "SMALL",  "TRENDING_UP",   "long"),   # win
        (3.20,  0.010, "MEDIUM", "TRENDING_UP",   "long"),   # win
        (-2.50, -0.008, "SMALL", "RANGING",        "short"),  # loss
        (1.80,  0.006, "LARGE",  "TRENDING_DOWN",  "short"),  # win
        (-1.00, -0.003, "MEDIUM","RANGING",         "long"),  # loss
    ]
    expected_total_pnl = sum(t[0] for t in trade_data)

    for i, (pnl_usd, pnl_pct, tier, regime, direction) in enumerate(trade_data):
        trade = Trade(
            symbol="SPY",
            broker="alpaca",
            direction=direction,
            entry_price=400.0,
            exit_price=400.0 + (pnl_usd / 1.0),
            quantity=1.0,
            confidence=65.0,
            position_tier=tier,
            regime=regime,
            pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            entry_time=now - timedelta(days=i + 1),
            exit_time=now - timedelta(hours=i),
            status="closed",
        )
        session.add(trade)

    session.commit()
    session.close()

    # --- Run generator ---
    gen = PerformanceReportGenerator(database_url=db_url)
    report = gen.generate(weeks=5)

    # --- Assertions ---
    assert report.closed_trades == 5, f"Expected 5 closed trades, got {report.closed_trades}"
    assert abs(report.win_rate - 0.60) < 1e-9, f"Expected win_rate=0.60, got {report.win_rate}"
    assert abs(report.total_pnl_usd - expected_total_pnl) < 1e-9, (
        f"Expected total_pnl={expected_total_pnl}, got {report.total_pnl_usd}"
    )
    assert report.meets_min_trades is False, (
        "Expected meets_min_trades=False for 5 trades (< 50 threshold)"
    )

    # Spot-check summary text contains key fields
    summary = report.summary_text()
    assert "Win rate: 60.0%" in summary, f"Summary missing win rate: {summary}"
    assert "Min trades met: NO" in summary, f"Summary missing min trades: {summary}"

    print(summary)
    print("All assertions passed.")

    # --- Cleanup ---
    os.unlink(tmp_file.name)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_performance_report()
