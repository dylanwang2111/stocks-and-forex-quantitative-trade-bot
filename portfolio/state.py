"""
portfolio/state.py
In-memory portfolio state manager with SQLite persistence.

Tracks open positions, computes available cash, handles close lifecycle,
reconciles against broker-reported positions, and emits PortfolioSnapshot rows.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from database.models import EventLog, PortfolioSnapshot, Trade, get_session
from config.settings import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Position dataclass
# ---------------------------------------------------------------------------

@dataclass
class Position:
    """Represents a single open position held in memory."""

    symbol: str
    broker: str
    direction: str           # "long" | "short"
    quantity: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    confidence: float
    position_tier: str
    entry_time: datetime
    db_trade_id: int

    # Exit state — persisted in PortfolioSnapshot.positions_detail
    tp_breach_streak: int = 0      # consecutive cycles price has been past TP
    partial_exit_done: bool = False # True once 50% has been closed at TP confirmation

    # ------------------------------------------------------------------
    # Computed properties
    # ------------------------------------------------------------------

    def current_value(self, current_price: float) -> float:
        """Market value of the position at *current_price* (always positive)."""
        return current_price * self.quantity

    def unrealized_pnl(self, current_price: float) -> float:
        """Unrealized P&L in USD.

        Long:  (current - entry) * qty
        Short: (entry - current) * qty

        For USD-base forex pairs (USDJPY, USDCHF, USDCAD): P&L is in the quote
        currency (JPY/CHF/CAD), so divide by current_price to convert to USD.
        """
        _is_usd_base = False
        try:
            from portfolio.watchlist import get_instrument
            instr = get_instrument(self.symbol)
            if instr.asset_type == "forex" and self.symbol.upper().startswith("USD"):
                _is_usd_base = True
        except Exception:
            pass
        if _is_usd_base:
            if self.direction == "long":
                return (current_price - self.entry_price) / current_price * self.quantity
            else:
                return (self.entry_price - current_price) / current_price * self.quantity
        if self.direction == "long":
            return (current_price - self.entry_price) * self.quantity
        else:  # short
            return (self.entry_price - current_price) * self.quantity

    # Convenience --------------------------------------------------------

    def cost_basis(self) -> float:
        """Capital deployed for this position in USD.

        For USD-base forex pairs (USDJPY, USDCAD, USDCHF): 1 OANDA unit = 1 USD,
        so deployed = quantity. For all others: deployed = entry_price * quantity.
        """
        try:
            from portfolio.watchlist import get_instrument
            instr = get_instrument(self.symbol)
            if instr.asset_type == "forex" and self.symbol.upper().startswith("USD"):
                return self.quantity
        except Exception:
            pass
        return self.entry_price * self.quantity

    def __repr__(self) -> str:
        return (
            f"Position({self.symbol} {self.direction} qty={self.quantity} "
            f"entry={self.entry_price:.4f} tier={self.position_tier})"
        )


# ---------------------------------------------------------------------------
def _calc_pnl_usd(symbol: str, direction: str, entry: float, exit_price: float, qty: float) -> float:
    """Compute realised P&L in USD, applying quote-currency conversion for USD-base forex pairs.

    USDJPY/USDCHF/USDCAD: price is quote-currency per 1 USD.  Raw P&L is in that
    quote currency, so divide by exit_price to convert back to USD.
    All other instruments: raw P&L is already in USD.
    """
    try:
        from portfolio.watchlist import get_instrument
        instr = get_instrument(symbol)
        is_usd_base = instr.asset_type == "forex" and symbol.upper().startswith("USD")
    except Exception:
        is_usd_base = False

    if direction == "long":
        raw = (exit_price - entry) * qty
    else:
        raw = (entry - exit_price) * qty

    if is_usd_base and exit_price:
        return raw / exit_price
    return raw


# PortfolioStateManager
# ---------------------------------------------------------------------------

class PortfolioStateManager:
    """Thread-safe in-memory portfolio tracker backed by SQLite.

    Capital model
    -------------
    total_capital   = configured (settings.bot.total_capital)
    cash_reserve    = 30% of total_capital (settings.bot.cash_reserve)  — never deployed
    deployable      = 70% of total_capital (total_capital - cash_reserve)
    deployed        = sum(entry_price * quantity) for open positions
    available_cash  = deployable - deployed
                    = total_capital - cash_reserve - deployed_capital()
    """

    def __init__(self, database_url: str | None = None) -> None:
        self._database_url: str = database_url or settings.bot.database_url
        self._positions: dict[str, Position] = {}
        self._lock = threading.Lock()

        self._total_capital: float = settings.bot.total_capital
        self._cash_reserve: float = settings.bot.cash_reserve
        self._max_positions: int = settings.bot.max_positions

        logger.info(
            "PortfolioStateManager initialised | capital=%.2f reserve=%.2f max_pos=%d db=%s",
            self._total_capital,
            self._cash_reserve,
            self._max_positions,
            self._database_url,
        )

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def deployed_capital(self, broker: str | None = None) -> float:
        """Sum of (entry_price * quantity) for open positions.

        If *broker* is given, only count positions held at that broker.
        """
        with self._lock:
            positions = self._positions.values()
            if broker is not None:
                positions = [p for p in positions if p.broker == broker]
            return sum(p.cost_basis() for p in positions)

    def available_cash(self, broker: str | None = None) -> float:
        """Capital available to open new positions.

        Compounds realized P&L into the pool so closed profits increase
        the capital available for new trades.

        If *broker* is given, uses that broker's configured capital pool
        (IBKR_CAPITAL / OANDA_CAPITAL) plus realized P&L for that broker,
        minus what's already deployed there.
        """
        realized = self.realized_pnl_by_broker(broker)
        if broker is not None:
            broker_cap = settings.bot.broker_capital(broker)
            reserve = broker_cap * settings.bot.cash_reserve_pct
            avail = broker_cap + realized - reserve - self.deployed_capital(broker)
        else:
            avail = self._total_capital + realized - self._cash_reserve - self.deployed_capital()
        return max(avail, 0.0)

    def position_count(self) -> int:
        """Number of currently open positions."""
        with self._lock:
            return len(self._positions)

    def get_position(self, symbol: str) -> Optional[Position]:
        """Return the open Position for *symbol*, or None if not held."""
        with self._lock:
            return self._positions.get(symbol)

    def all_positions(self) -> list[Position]:
        """Return a snapshot list of all open positions."""
        with self._lock:
            return list(self._positions.values())

    def can_open_position(self, symbol: str) -> bool:
        """Return True only if both conditions are met:

        1. We do not already hold *symbol*.
        2. We have not reached *max_positions*.
        """
        with self._lock:
            if symbol in self._positions:
                logger.debug("can_open_position(%s) -> False: already held", symbol)
                return False
            if len(self._positions) >= self._max_positions:
                logger.debug(
                    "can_open_position(%s) -> False: at max_positions (%d)",
                    symbol,
                    self._max_positions,
                )
                return False
        return True

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def add_position(self, position: Position) -> None:
        """Register a newly opened position in memory.

        Does NOT write to the database — the caller is expected to have
        already inserted the Trade row and obtained the db_trade_id before
        calling this method.
        """
        with self._lock:
            if position.symbol in self._positions:
                raise ValueError(
                    f"add_position: {position.symbol} is already tracked. "
                    "Close the existing position before adding a new one."
                )
            self._positions[position.symbol] = position

        logger.info("Position added: %r", position)

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_time: datetime,
        reason: str | None = None,
    ) -> None:
        """Close an open position and persist the outcome to the database.

        Computes P&L:
            Long:  pnl_usd = (exit_price - entry_price) * quantity
            Short: pnl_usd = (entry_price - exit_price) * quantity
            pnl_pct = pnl_usd / (entry_price * quantity)

        Updates the Trade row: exit_price, exit_time, pnl_usd, pnl_pct,
        status="closed".  Removes the position from the in-memory dict.
        """
        with self._lock:
            position = self._positions.get(symbol)
            if position is None:
                raise KeyError(f"close_position: no open position found for '{symbol}'")

        # Compute P&L outside the lock (pure arithmetic)
        pnl_usd = _calc_pnl_usd(position.symbol, position.direction,
                                 position.entry_price, exit_price, position.quantity)

        cost_basis = position.cost_basis()
        pnl_pct = pnl_usd / cost_basis if cost_basis != 0.0 else 0.0

        # Persist to DB
        session = get_session(self._database_url)
        try:
            trade = session.get(Trade, position.db_trade_id)
            if trade is None:
                logger.error(
                    "close_position: Trade id=%d not found in DB for %s",
                    position.db_trade_id,
                    symbol,
                )
            else:
                trade.exit_price = exit_price
                trade.exit_time = exit_time
                trade.pnl_usd = round(pnl_usd, 6)
                trade.pnl_pct = round(pnl_pct, 6)
                trade.status = "closed"
                if reason is not None:
                    trade.notes = reason
                session.commit()
                logger.info(
                    "Trade id=%d closed | %s %s exit=%.4f pnl=%.2f (%.2f%%)",
                    position.db_trade_id,
                    symbol,
                    position.direction,
                    exit_price,
                    pnl_usd,
                    pnl_pct * 100,
                )
        except Exception:
            session.rollback()
            logger.exception("close_position: DB update failed for %s", symbol)
            raise
        finally:
            session.close()

        # Remove from memory after successful DB write
        with self._lock:
            self._positions.pop(symbol, None)

    def partial_close_position(
        self,
        symbol: str,
        close_fraction: float,
        exit_price: float,
        exit_time: datetime,
    ) -> float:
        """Close *close_fraction* (0 < f < 1) of a position in-place.

        Reduces position.quantity, marks partial_exit_done=True, updates the
        Trade row's quantity, and returns the realised P&L for the closed slice.
        Does NOT remove the position from memory.
        """
        with self._lock:
            position = self._positions.get(symbol)
            if position is None:
                raise KeyError(f"partial_close_position: no open position for '{symbol}'")
            close_qty  = position.quantity * close_fraction
            remain_qty = position.quantity - close_qty

        # P&L for the closed slice
        pnl_usd = _calc_pnl_usd(position.symbol, position.direction,
                                 position.entry_price, exit_price, close_qty)

        # Persist remaining quantity to Trade row (original quantity is preserved)
        session = get_session(self._database_url)
        try:
            trade = session.get(Trade, position.db_trade_id)
            if trade is not None:
                trade.remaining_quantity = round(remain_qty, 8)
                session.commit()
            else:
                logger.warning(
                    "partial_close_position: Trade id=%d not found for %s",
                    position.db_trade_id, symbol,
                )
        except Exception:
            session.rollback()
            logger.exception("partial_close_position: DB update failed for %s", symbol)
            raise
        finally:
            session.close()

        # Update in-memory position
        with self._lock:
            position.quantity = remain_qty
            position.partial_exit_done = True

        logger.info(
            "Partial close: %s %s closed %.4f (%.0f%%) @ %.4f  pnl=%.2f  remaining=%.4f",
            symbol, position.direction, close_qty, close_fraction * 100,
            exit_price, pnl_usd, remain_qty,
        )
        return pnl_usd

    def restore_from_db(self) -> int:
        """Reload open positions from DB into memory on startup.

        Queries the trades table for status='open' rows and the latest
        portfolio snapshot for stop/TP prices, then reconstructs the
        in-memory _positions dict.  Returns the number of positions restored.
        """
        session = get_session(self._database_url)
        try:
            open_trades = (
                session.query(Trade)
                .filter(Trade.status == "open")
                .all()
            )
            if not open_trades:
                return 0

            # Build stop/TP lookup from latest snapshot's positions_detail
            stop_tp: dict[str, dict] = {}
            latest_snap = (
                session.query(PortfolioSnapshot)
                .order_by(PortfolioSnapshot.timestamp.desc())
                .first()
            )
            if latest_snap and latest_snap.positions_detail:
                details = latest_snap.positions_detail
                if isinstance(details, str):
                    import json
                    details = json.loads(details)
                for d in details:
                    stop_tp[d["symbol"]] = d

            restored = 0
            for trade in open_trades:
                detail = stop_tp.get(trade.symbol, {})
                # Priority: snapshot detail → Trade table columns → hardcoded fallback
                stop_price = (
                    detail.get("stop_price")
                    or trade.stop_price
                    or trade.entry_price * (0.98 if trade.direction == "long" else 1.02)
                )
                tp_price = (
                    detail.get("take_profit_price")
                    or trade.take_profit_price
                    or trade.entry_price * (1.04 if trade.direction == "long" else 0.96)
                )
                # Only restore phase state if the snapshot entry belongs to THIS trade
                same_trade = detail.get("db_trade_id") == trade.id
                # Use remaining_quantity if a partial close has occurred, else original quantity
                current_qty = (
                    trade.remaining_quantity
                    if trade.remaining_quantity is not None
                    else trade.quantity
                )
                pos = Position(
                    symbol=trade.symbol,
                    broker=trade.broker,
                    direction=trade.direction,
                    quantity=current_qty,
                    entry_price=trade.entry_price,
                    stop_price=stop_price,
                    take_profit_price=tp_price,
                    confidence=trade.confidence,
                    position_tier=trade.position_tier,
                    entry_time=trade.entry_time,
                    db_trade_id=trade.id,
                    tp_breach_streak=detail.get("tp_breach_streak", 0) if same_trade else 0,
                    partial_exit_done=detail.get("partial_exit_done", False) if same_trade else False,
                )
                with self._lock:
                    self._positions[trade.symbol] = pos
                restored += 1
                logger.info(
                    "restore_from_db: reloaded %s %s entry=%.4f stop=%.4f tp=%.4f",
                    trade.symbol, trade.direction, trade.entry_price, stop_price, tp_price,
                )
            return restored
        except Exception:
            logger.exception("restore_from_db: failed to restore positions")
            return 0
        finally:
            session.close()

    def sync_from_broker(self, broker_positions: list[dict]) -> None:
        """Reconcile in-memory state with broker-reported positions.

        Each element of *broker_positions* must contain:
            { "symbol": str, "quantity": float, "avg_price": float, "direction": str }

        Behaviour:
        - Warns when an in-memory position is absent from the broker list
          (position may have been closed externally or hit a stop).
        - Warns when the broker reports a position we do not have in memory
          (could indicate a manual trade or a missed open event).
        - Does NOT auto-mutate positions to avoid silent data corruption;
          the caller should act on warnings and call add_position /
          close_position explicitly if reconciliation is needed.
        """
        broker_symbols: set[str] = {p["symbol"] for p in broker_positions}

        with self._lock:
            memory_symbols: set[str] = set(self._positions.keys())

        missing_from_broker = memory_symbols - broker_symbols
        missing_from_memory = broker_symbols - memory_symbols

        for sym in missing_from_broker:
            logger.warning(
                "sync_from_broker: '%s' is tracked in memory but NOT reported by broker. "
                "Possible external close or stop hit. Investigate and call close_position() if needed.",
                sym,
            )

        for sym in missing_from_memory:
            logger.warning(
                "sync_from_broker: '%s' is reported by broker but NOT in memory. "
                "Possible manual trade or missed open event. Investigate and call add_position() if needed.",
                sym,
            )

        if not missing_from_broker and not missing_from_memory:
            logger.debug("sync_from_broker: in-memory state matches broker (%d positions).", len(memory_symbols))

    # ------------------------------------------------------------------
    # Reporting
    # ------------------------------------------------------------------

    def daily_pnl(self) -> float:
        """Realized P&L for today (local calendar day): closed trades + partial close events."""
        today_start_naive = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

        session = get_session(self._database_url)
        try:
            # Full closes
            trades_today = (
                session.query(Trade)
                .filter(
                    Trade.status == "closed",
                    Trade.exit_time >= today_start_naive,
                )
                .all()
            )
            total = sum(t.pnl_usd for t in trades_today if t.pnl_usd is not None)

            # Partial closes (P&L stored in EventLog.metadata["pnl_usd"])
            partial_events = (
                session.query(EventLog)
                .filter(
                    EventLog.event_type == "partial_close",
                    EventLog.timestamp >= today_start_naive,
                )
                .all()
            )
            for evt in partial_events:
                meta = evt.event_metadata or {}
                if isinstance(meta, str):
                    import json
                    meta = json.loads(meta)
                total += meta.get("pnl_usd", 0.0)

            return round(total, 6)
        except Exception:
            logger.exception("daily_pnl: DB query failed")
            return 0.0
        finally:
            session.close()

    def total_realized_pnl(self) -> float:
        """All-time realized P&L: all closed trades + all partial close events."""
        return self.realized_pnl_by_broker(broker=None)

    def realized_pnl_by_broker(self, broker: str | None = None) -> float:
        """Realized P&L for a specific broker, or all brokers when broker=None.

        Sums:
        - Closed Trade rows (filtered by broker when given)
        - partial_close EventLog rows, matched to Trade via db_trade_id
          (events without db_trade_id are attributed proportionally by initial
          capital weight when broker is given, or included in full when broker=None)
        """
        session = get_session(self._database_url)
        try:
            import json as _json

            # Closed trades
            q = session.query(Trade).filter(Trade.status == "closed")
            if broker is not None:
                q = q.filter(Trade.broker == broker)
            total = sum(t.pnl_usd for t in q.all() if t.pnl_usd is not None)

            # Partial close events
            partial_events = (
                session.query(EventLog)
                .filter(EventLog.event_type == "partial_close")
                .all()
            )
            for evt in partial_events:
                meta = evt.event_metadata or {}
                if isinstance(meta, str):
                    meta = _json.loads(meta)
                pnl = meta.get("pnl_usd", 0.0)
                if broker is None:
                    total += pnl
                    continue
                # Attribute to broker via db_trade_id when available
                trade_id = meta.get("db_trade_id")
                if trade_id is not None:
                    trade = session.get(Trade, trade_id)
                    if trade is not None and trade.broker == broker:
                        total += pnl
                else:
                    # Older events without db_trade_id: split by initial capital weight
                    total_initial = settings.bot.total_capital or 1.0
                    broker_initial = settings.bot.broker_capital(broker)
                    total += pnl * (broker_initial / total_initial)

            return round(total, 6)
        except Exception:
            logger.exception("realized_pnl_by_broker: DB query failed")
            return 0.0
        finally:
            session.close()

    def snapshot(self) -> dict:
        """Return current portfolio state as a plain dict.

        Matches the columns of the PortfolioSnapshot model so the caller
        can pass this directly to save_snapshot() or log it.
        """
        with self._lock:
            positions_detail = [
                {
                    "symbol": p.symbol,
                    "broker": p.broker,
                    "direction": p.direction,
                    "quantity": p.quantity,
                    "entry_price": p.entry_price,
                    "stop_price": p.stop_price,
                    "take_profit_price": p.take_profit_price,
                    "confidence": p.confidence,
                    "position_tier": p.position_tier,
                    "entry_time": p.entry_time.isoformat(),
                    "db_trade_id": p.db_trade_id,
                    "cost_basis": round(p.cost_basis(), 4),
                    "tp_breach_streak": p.tp_breach_streak,
                    "partial_exit_done": p.partial_exit_done,
                }
                for p in self._positions.values()
            ]
            open_count = len(self._positions)

        deployed = self.deployed_capital()
        cash = self.available_cash()
        total_equity = self._total_capital  # unrealized P&L not included without live prices
        d_pnl = self.daily_pnl()

        return {
            "timestamp": datetime.utcnow(),
            "total_equity": round(total_equity, 4),
            "cash": round(cash, 4),
            "open_positions": open_count,
            "daily_pnl": round(d_pnl, 6),
            "weekly_pnl": None,       # populated by caller if needed
            "drawdown_pct": None,     # populated by caller if needed
            "positions_detail": positions_detail,
        }

    def update_snapshot_prices(self, prices: dict) -> None:
        """Patch the most recent snapshot's positions_detail with current market prices.

        Called by the orchestrator after it fetches live prices for the Telegram
        summary, so the dashboard can display unrealized P&L without re-fetching.
        """
        if not prices:
            return
        import json as _json
        session = get_session(self._database_url)
        try:
            snap = (
                session.query(PortfolioSnapshot)
                .order_by(PortfolioSnapshot.timestamp.desc())
                .first()
            )
            if snap and snap.positions_detail:
                details = snap.positions_detail
                if isinstance(details, str):
                    details = _json.loads(details)
                for d in details:
                    sym = d.get("symbol")
                    if sym and sym in prices:
                        d["current_price"] = round(float(prices[sym]), 6)
                snap.positions_detail = details
                session.commit()
                logger.debug("update_snapshot_prices: patched %d symbol(s)", len(prices))
        except Exception:
            session.rollback()
            logger.exception("update_snapshot_prices: DB write failed")
        finally:
            session.close()

    def save_snapshot(self) -> None:
        """Write a PortfolioSnapshot row and sync stop/tp back to Trade rows."""
        data = self.snapshot()
        # Equity = initial capital + all realized P&L so the equity curve actually moves
        data["total_equity"] = round(self._total_capital + self.total_realized_pnl(), 4)
        session = get_session(self._database_url)
        try:
            snap = PortfolioSnapshot(
                timestamp=data["timestamp"],
                total_equity=data["total_equity"],
                cash=data["cash"],
                open_positions=data["open_positions"],
                daily_pnl=data["daily_pnl"],
                weekly_pnl=data["weekly_pnl"],
                drawdown_pct=data["drawdown_pct"],
                positions_detail=data["positions_detail"],
            )
            session.add(snap)
            # Sync in-memory stop/tp (may have moved due to trailing stop) back to Trade rows
            for detail in data["positions_detail"]:
                trade = session.get(Trade, detail.get("db_trade_id"))
                if trade is not None:
                    trade.stop_price = detail.get("stop_price")
                    trade.take_profit_price = detail.get("take_profit_price")
            session.commit()
            logger.info(
                "Snapshot saved | equity=%.2f cash=%.2f positions=%d daily_pnl=%.4f",
                data["total_equity"],
                data["cash"],
                data["open_positions"],
                data["daily_pnl"],
            )
        except Exception:
            session.rollback()
            logger.exception("save_snapshot: DB write failed")
            raise
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def test_portfolio_state() -> None:
    """Smoke-test PortfolioStateManager with two positions.

    Verifies:
    - available_cash() after opening 2 positions
    - can_open_position() returns False when at max_positions limit
    - can_open_position() returns False for a symbol already held
    """
    import tempfile, os

    db_path = os.path.join(tempfile.gettempdir(), "test_portfolio_state.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    db_url = f"sqlite:///{db_path}"

    # Initialise tables
    from database.models import init_db
    init_db(db_url)

    mgr = PortfolioStateManager(database_url=db_url)

    # Confirm clean slate — available cash = total_capital (no positions, no reserve)
    total_cap = settings.bot.total_capital
    assert mgr.available_cash() == total_cap, (
        f"Expected {total_cap}, got {mgr.available_cash()}"
    )
    assert mgr.can_open_position("SPY"), "Should be able to open SPY on clean slate"

    # Insert two Trade rows so we have valid db_trade_id values
    session = get_session(db_url)
    try:
        now = datetime.utcnow()
        t1 = Trade(
            symbol="SPY", broker="alpaca", direction="long",
            entry_price=500.0, quantity=0.2, confidence=70.0,
            position_tier="tier2", entry_time=now, status="open",
        )
        t2 = Trade(
            symbol="EURUSD", broker="oanda", direction="short",
            entry_price=1.08, quantity=1000.0, confidence=65.0,
            position_tier="tier1", entry_time=now, status="open",
        )
        session.add_all([t1, t2])
        session.commit()
        t1_id, t2_id = t1.id, t2.id
    finally:
        session.close()

    # Position 1: SPY long, cost = 500.0 * 0.2 = $100
    pos1 = Position(
        symbol="SPY",
        broker="alpaca",
        direction="long",
        quantity=0.2,
        entry_price=500.0,
        stop_price=490.0,
        take_profit_price=520.0,
        confidence=70.0,
        position_tier="tier2",
        entry_time=datetime.utcnow(),
        db_trade_id=t1_id,
    )
    mgr.add_position(pos1)

    # Position 2: EUR/USD short, cost = 1.08 * 1000 = $1080 (over capital but
    #   for forex the notional is leveraged; in our model we track margin not
    #   notional, so use a small margin-equivalent qty for the test)
    pos2 = Position(
        symbol="EURUSD",
        broker="oanda",
        direction="short",
        quantity=10.0,       # $10.80 cost basis for test purposes
        entry_price=1.08,
        stop_price=1.09,
        take_profit_price=1.06,
        confidence=65.0,
        position_tier="tier1",
        entry_time=datetime.utcnow(),
        db_trade_id=t2_id,
    )
    mgr.add_position(pos2)

    # deployed = SPY cost_basis + EURUSD cost_basis
    # SPY: stock → entry * qty = 500*0.2 = $100
    # EURUSD: non-USD-base forex → entry * qty = 1.08*10 = $10.80
    reserve = total_cap * settings.bot.cash_reserve_pct
    expected_cash = round(total_cap - reserve - (500.0 * 0.2) - (1.08 * 10.0), 6)
    actual_cash = mgr.available_cash()
    assert abs(actual_cash - expected_cash) < 0.0001, (
        f"available_cash mismatch: expected {expected_cash}, got {actual_cash}"
    )

    # Already-held symbol blocked
    assert not mgr.can_open_position("SPY"), (
        "can_open_position should be False for already-held symbol"
    )

    # Verify position_count
    assert mgr.position_count() == 2, f"Expected 2, got {mgr.position_count()}"

    # Close SPY position
    exit_time = datetime.utcnow()
    mgr.close_position("SPY", exit_price=510.0, exit_time=exit_time)
    assert mgr.get_position("SPY") is None, "SPY should be removed after close"
    assert mgr.position_count() == 1

    # After closing SPY: available_cash = total_cap + realized_pnl - remaining_deployed
    spy_pnl = (510.0 - 500.0) * 0.2  # $2 realized
    expected_cash_after = round(total_cap - reserve + spy_pnl - (1.08 * 10.0), 6)
    actual_cash_after = mgr.available_cash()
    assert abs(actual_cash_after - expected_cash_after) < 0.0001, (
        f"available_cash after close mismatch: expected {expected_cash_after}, got {actual_cash_after}"
    )

    # Can open new position (slot freed)
    assert mgr.can_open_position("QQQ"), (
        "can_open_position should be True after closing one position"
    )

    # Force max_positions to test the cap
    mgr._max_positions = 1
    assert not mgr.can_open_position("QQQ"), (
        "can_open_position should be False when at max_positions"
    )
    mgr._max_positions = settings.bot.max_positions  # restore

    # snapshot() returns correct structure
    snap = mgr.snapshot()
    assert snap["open_positions"] == 1
    assert snap["cash"] == round(actual_cash_after, 4)
    assert len(snap["positions_detail"]) == 1
    assert snap["positions_detail"][0]["symbol"] == "EURUSD"

    # Verify DB trade was updated correctly
    session = get_session(db_url)
    try:
        closed_trade = session.get(Trade, t1_id)
        assert closed_trade.status == "closed"
        assert closed_trade.exit_price == 510.0
        # long pnl = (510 - 500) * 0.2 = 2.0
        assert abs(closed_trade.pnl_usd - 2.0) < 0.0001, (
            f"pnl_usd expected 2.0, got {closed_trade.pnl_usd}"
        )
        # pnl_pct = 2.0 / (500 * 0.2) = 0.02
        assert abs(closed_trade.pnl_pct - 0.02) < 0.0001, (
            f"pnl_pct expected 0.02, got {closed_trade.pnl_pct}"
        )
    finally:
        session.close()

    # Cleanup
    os.remove(db_path)

    print("test_portfolio_state: ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_portfolio_state()
