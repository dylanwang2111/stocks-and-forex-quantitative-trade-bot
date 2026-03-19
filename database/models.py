"""
database/models.py
SQLAlchemy ORM models + init_db() helper.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
    create_engine,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Session


class Base(DeclarativeBase):
    pass


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(20), nullable=False, index=True)
    broker = Column(String(20), nullable=False)           # ibkr | oanda
    direction = Column(String(10), nullable=False)        # long | short
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    quantity = Column(Float, nullable=False)             # original entry quantity (never changes)
    remaining_quantity = Column(Float, nullable=True)    # after partial close(s); NULL = full qty still open
    confidence = Column(Float, nullable=False)
    position_tier = Column(String(20), nullable=False)
    regime = Column(String(30), nullable=True)
    pnl_usd = Column(Float, nullable=True)
    pnl_pct = Column(Float, nullable=True)
    entry_time = Column(DateTime, nullable=False, default=datetime.utcnow)
    exit_time = Column(DateTime, nullable=True)
    status = Column(String(20), nullable=False, default="open")  # open | closed | cancelled
    stop_price = Column(Float, nullable=True)
    take_profit_price = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)
    signal_breakdown = Column(JSON, nullable=True)


class SignalLog(Base):
    __tablename__ = "signal_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    symbol = Column(String(20), nullable=False, index=True)
    cat1_trend = Column(Integer, nullable=True)
    cat2_strength = Column(Integer, nullable=True)
    cat3_momentum = Column(Integer, nullable=True)
    cat4_volatility = Column(Integer, nullable=True)
    cat5_volume = Column(Integer, nullable=True)
    cat6_structure = Column(Integer, nullable=True)
    cat7_mtf = Column(Integer, nullable=True)
    cat8_macro = Column(Integer, nullable=True)
    bull_score = Column(Float, nullable=True)
    bear_score = Column(Float, nullable=True)
    direction = Column(String(10), nullable=True)
    dominant_score = Column(Float, nullable=True)
    regime = Column(String(30), nullable=True)
    position_tier = Column(String(20), nullable=True)
    raw_votes = Column(JSON, nullable=True)
    macro_risk_level = Column(String(10), nullable=True)


class StrategyRegistry(Base):
    __tablename__ = "strategy_registry"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(100), nullable=False, unique=True)
    version = Column(String(20), nullable=False)
    params = Column(JSON, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    is_active = Column(Boolean, nullable=False, default=True)
    description = Column(Text, nullable=True)


class OptimizationCycle(Base):
    __tablename__ = "optimization_cycles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    strategy_name = Column(String(100), nullable=False, index=True)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    in_sample_start = Column(DateTime, nullable=False)
    in_sample_end = Column(DateTime, nullable=False)
    oos_start = Column(DateTime, nullable=False)
    oos_end = Column(DateTime, nullable=False)
    in_sample_sharpe = Column(Float, nullable=True)
    oos_sharpe = Column(Float, nullable=True)
    in_sample_trades = Column(Integer, nullable=True)
    oos_trades = Column(Integer, nullable=True)
    params_before = Column(JSON, nullable=True)
    params_after = Column(JSON, nullable=True)
    accepted = Column(Boolean, nullable=True)
    p_value = Column(Float, nullable=True)
    notes = Column(Text, nullable=True)


class PortfolioSnapshot(Base):
    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    total_equity = Column(Float, nullable=False)
    cash = Column(Float, nullable=False)
    open_positions = Column(Integer, nullable=False, default=0)
    daily_pnl = Column(Float, nullable=True)
    weekly_pnl = Column(Float, nullable=True)
    drawdown_pct = Column(Float, nullable=True)
    positions_detail = Column(JSON, nullable=True)


class EventLog(Base):
    __tablename__ = "event_log"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    event_type = Column(String(50), nullable=False)   # earnings | fomc | macro | system
    symbol = Column(String(20), nullable=True)
    description = Column(Text, nullable=True)
    blackout_start = Column(DateTime, nullable=True)
    blackout_end = Column(DateTime, nullable=True)
    event_metadata = Column("metadata", JSON, nullable=True)


def init_db(database_url: str = "sqlite:///trade_bot.db") -> None:
    """Create all tables. Safe to call multiple times (no-op if tables exist)."""
    engine = create_engine(database_url, echo=False)
    Base.metadata.create_all(engine)
    # Migrate: add stop_price / take_profit_price columns if the table pre-dates them
    with engine.connect() as conn:
        for col in ("stop_price", "take_profit_price", "remaining_quantity"):
            try:
                conn.execute(text(f"ALTER TABLE trades ADD COLUMN {col} FLOAT"))
                conn.commit()
            except Exception:
                pass  # column already exists — safe to ignore
    return engine


def get_session(database_url: str = "sqlite:///trade_bot.db") -> Session:
    """Return a new SQLAlchemy session."""
    engine = create_engine(database_url, echo=False)
    return Session(engine)
