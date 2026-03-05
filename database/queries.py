"""
database/queries.py
Common query helpers for the trade bot.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from database.models import (
    EventLog,
    OptimizationCycle,
    PortfolioSnapshot,
    SignalLog,
    Trade,
    get_session,
    init_db,
)


# ── Trade helpers ──────────────────────────────────────────────────────────────

def get_recent_trades(
    session: Session,
    symbol: str | None = None,
    limit: int = 50,
    status: str | None = None,
) -> list[Trade]:
    q = session.query(Trade)
    if symbol:
        q = q.filter(Trade.symbol == symbol)
    if status:
        q = q.filter(Trade.status == status)
    return q.order_by(Trade.entry_time.desc()).limit(limit).all()


def get_open_trades(session: Session) -> list[Trade]:
    return session.query(Trade).filter(Trade.status == "open").all()


def count_day_trades(session: Session, window_days: int = 5) -> int:
    """Count stock day trades in the rolling PDT window."""
    cutoff = datetime.utcnow() - timedelta(days=window_days)
    return (
        session.query(Trade)
        .filter(
            Trade.broker == "ibkr",
            Trade.entry_time >= cutoff,
            Trade.exit_time.isnot(None),
            Trade.status == "closed",
        )
        .count()
    )


# ── Signal helpers ─────────────────────────────────────────────────────────────

def log_signal(
    session: Session,
    symbol: str,
    votes: dict[str, int],
    scores: dict[str, float],
    regime: str,
    position_tier: str,
) -> SignalLog:
    entry = SignalLog(
        symbol=symbol,
        cat1_trend=votes.get("cat1"),
        cat2_strength=votes.get("cat2"),
        cat3_momentum=votes.get("cat3"),
        cat4_volatility=votes.get("cat4"),
        cat5_volume=votes.get("cat5"),
        cat6_structure=votes.get("cat6"),
        cat7_mtf=votes.get("cat7"),
        cat8_macro=votes.get("cat8"),
        bull_score=scores.get("bull_score"),
        bear_score=scores.get("bear_score"),
        direction=scores.get("direction"),
        dominant_score=scores.get("dominant_score"),
        regime=regime,
        position_tier=position_tier,
        raw_votes=votes,
    )
    session.add(entry)
    session.commit()
    return entry


def get_recent_signals(
    session: Session,
    symbol: str,
    hours: int = 24,
) -> list[SignalLog]:
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    return (
        session.query(SignalLog)
        .filter(SignalLog.symbol == symbol, SignalLog.timestamp >= cutoff)
        .order_by(SignalLog.timestamp.desc())
        .all()
    )


# ── Portfolio snapshot helpers ─────────────────────────────────────────────────

def save_portfolio_snapshot(
    session: Session,
    total_equity: float,
    cash: float,
    open_positions: int,
    daily_pnl: float | None = None,
    drawdown_pct: float | None = None,
    positions_detail: dict | None = None,
) -> PortfolioSnapshot:
    snap = PortfolioSnapshot(
        total_equity=total_equity,
        cash=cash,
        open_positions=open_positions,
        daily_pnl=daily_pnl,
        drawdown_pct=drawdown_pct,
        positions_detail=positions_detail,
    )
    session.add(snap)
    session.commit()
    return snap


def get_latest_snapshot(session: Session) -> PortfolioSnapshot | None:
    return (
        session.query(PortfolioSnapshot)
        .order_by(PortfolioSnapshot.timestamp.desc())
        .first()
    )


# ── Event helpers ──────────────────────────────────────────────────────────────

def log_event(
    session: Session,
    event_type: str,
    description: str,
    symbol: str | None = None,
    blackout_start: datetime | None = None,
    blackout_end: datetime | None = None,
    metadata: dict | None = None,
) -> EventLog:
    ev = EventLog(
        event_type=event_type,
        symbol=symbol,
        description=description,
        blackout_start=blackout_start,
        blackout_end=blackout_end,
        metadata=metadata,
    )
    session.add(ev)
    session.commit()
    return ev


def is_in_blackout(session: Session, symbol: str) -> bool:
    """Return True if symbol is currently in an event blackout window."""
    now = datetime.utcnow()
    return (
        session.query(EventLog)
        .filter(
            EventLog.symbol == symbol,
            EventLog.blackout_start <= now,
            EventLog.blackout_end >= now,
        )
        .count()
        > 0
    )
