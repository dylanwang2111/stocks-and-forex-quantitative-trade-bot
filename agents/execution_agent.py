"""
agents/execution_agent.py
Routes trade orders to the correct broker (IBKR for stocks, OANDA for forex),
persists Trade rows to the database, and keeps PortfolioStateManager in sync.

Paper mode is the safe default — live mode requires ib_insync / oandapyV20 and
valid credentials.  All public methods swallow exceptions and return OrderResult
so callers never need to catch.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from config.settings import settings
from database.models import EventLog, Trade, get_session, init_db
from portfolio.state import Position, PortfolioStateManager
from portfolio.watchlist import get_instrument

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class RiskParams:
    """Sizing output produced by RiskAgent (agents/risk_agent.py)."""
    position_size_usd: float
    quantity: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    risk_dollars: float
    position_tier: str
    size_fraction: float


@dataclass
class OrderResult:
    success: bool
    order_id: str | None
    filled_price: float | None
    error: str | None
    broker: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    """Compact UTC timestamp string for synthetic order IDs."""
    return datetime.utcnow().strftime("%Y%m%d%H%M%S%f")


def _oanda_symbol(symbol: str) -> str:
    """Convert 'EURUSD' → 'EUR_USD' for OANDA REST API."""
    symbol = symbol.upper()
    if len(symbol) == 6 and "_" not in symbol:
        return f"{symbol[:3]}_{symbol[3:]}"
    return symbol


# ---------------------------------------------------------------------------
# ExecutionAgent
# ---------------------------------------------------------------------------

class ExecutionAgent:
    """
    Routes place_order / close_position calls to the correct broker,
    writes Trade rows in the database, and updates PortfolioStateManager.

    Never raises — all public methods return OrderResult and catch internally.
    """

    def __init__(
        self,
        state_manager: PortfolioStateManager,
        database_url: str | None = None,
    ) -> None:
        self._state = state_manager
        self._db_url: str = database_url or settings.bot.database_url

        logger.info(
            "ExecutionAgent ready | mode=%s db=%s",
            settings.bot.trading_mode,
            self._db_url,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def place_order(
        self,
        symbol: str,
        risk_params: RiskParams,
        direction: str,
        confidence_result,       # ConfidenceResult from agents/confidence_scorer.py
        regime,                  # RegimeContext from regime/detector.py
    ) -> OrderResult:
        """
        Place an entry order for *symbol*.

        Routes to _place_ibkr_order() (stocks) or _place_oanda_order() (forex).

        On success:
          1. Inserts a Trade row (status="open") in the database.
          2. Calls state_manager.add_position(...).

        On failure:
          - Logs an EventLog row.
          - Returns OrderResult(success=False, ...).

        Never raises.
        """
        try:
            instrument = get_instrument(symbol)
        except KeyError:
            error = f"Unknown symbol '{symbol}' — not in watchlist"
            logger.error("place_order: %s", error)
            self._log_event(
                event_type="order_error",
                symbol=symbol,
                description=error,
            )
            return OrderResult(
                success=False, order_id=None,
                filled_price=None, error=error,
                broker="unknown",
            )

        try:
            if instrument.asset_type == "stock":
                result = self._place_ibkr_order(symbol, risk_params, direction)
            else:
                result = self._place_oanda_order(symbol, risk_params, direction)

            if not result.success:
                self._log_event(
                    event_type="order_error",
                    symbol=symbol,
                    description=result.error or "Order placement failed",
                    metadata={"direction": direction, "broker": result.broker},
                )
                return result

            # ── DB: insert Trade row ────────────────────────────────────────
            # filled_price is always set on a successful result; fall back to
            # the target entry price as a safety net so downstream math never
            # receives None.
            filled_price: float = result.filled_price or risk_params.entry_price
            db_trade_id = self._insert_trade(
                symbol=symbol,
                broker=instrument.broker,
                direction=direction,
                filled_price=filled_price,
                risk_params=risk_params,
                confidence_result=confidence_result,
                regime=regime,
            )
            if db_trade_id is None:
                # DB write failed — still return success so position can be tracked
                # but log the issue; db_trade_id sentinel = -1
                db_trade_id = -1
                logger.error(
                    "place_order: DB insert failed for %s — position tracked with id=-1",
                    symbol,
                )

            # ── State: add position ─────────────────────────────────────────
            position = Position(
                symbol=symbol,
                broker=instrument.broker,
                direction=direction,
                quantity=risk_params.quantity,
                entry_price=filled_price,
                stop_price=risk_params.stop_price,
                take_profit_price=risk_params.take_profit_price,
                confidence=confidence_result.dominant_score,
                position_tier=confidence_result.position_tier.value,
                entry_time=datetime.utcnow(),
                db_trade_id=db_trade_id,
            )
            self._state.add_position(position)

            logger.info(
                "Order filled and position opened | %s %s dir=%s qty=%.4f "
                "fill=%.4f stop=%.4f tp=%.4f order_id=%s",
                instrument.broker.upper(),
                symbol,
                direction,
                risk_params.quantity,
                result.filled_price,
                risk_params.stop_price,
                risk_params.take_profit_price,
                result.order_id,
            )
            return result

        except Exception as exc:
            error = f"Unexpected error placing order for {symbol}: {exc}"
            logger.exception("place_order: %s", error)
            self._log_event(
                event_type="order_error",
                symbol=symbol,
                description=error,
                metadata={"direction": direction},
            )
            return OrderResult(
                success=False, order_id=None,
                filled_price=None, error=error,
                broker=instrument.broker if "instrument" in dir() else "unknown",
            )

    def close_position(
        self,
        position: Position,
        reason: str,
    ) -> OrderResult:
        """
        Close an existing position via the appropriate broker.

        On success: calls state_manager.close_position(...) which updates the
        Trade row and removes the position from memory.

        On failure: logs an EventLog row, returns OrderResult(success=False, ...).

        Never raises.
        """
        symbol = position.symbol
        try:
            instrument = get_instrument(symbol)

            if instrument.asset_type == "stock":
                result = self._close_ibkr_position(position, reason)
            else:
                result = self._close_oanda_position(position, reason)

            if not result.success:
                self._log_event(
                    event_type="close_error",
                    symbol=symbol,
                    description=result.error or "Position close failed",
                    metadata={"reason": reason, "broker": result.broker},
                )
                return result

            # Delegate DB update + memory removal to PortfolioStateManager
            exit_price = result.filled_price or position.entry_price
            self._state.close_position(
                symbol=symbol,
                exit_price=exit_price,
                exit_time=datetime.utcnow(),
            )

            logger.info(
                "Position closed | %s %s exit=%.4f reason=%s order_id=%s",
                symbol,
                position.direction,
                exit_price,
                reason,
                result.order_id,
            )
            return result

        except Exception as exc:
            error = f"Unexpected error closing position for {symbol}: {exc}"
            logger.exception("close_position: %s", error)
            self._log_event(
                event_type="close_error",
                symbol=symbol,
                description=error,
                metadata={"reason": reason},
            )
            return OrderResult(
                success=False, order_id=None,
                filled_price=None, error=error,
                broker=position.broker,
            )

    # ------------------------------------------------------------------
    # IBKR order placement (stocks)
    # ------------------------------------------------------------------

    def _place_ibkr_order(
        self,
        symbol: str,
        risk_params: RiskParams,
        direction: str,
    ) -> OrderResult:
        """Place an entry order on IBKR.

        Falls back to paper mode when:
          - settings.bot.trading_mode == "paper"
          - settings.ibkr is not enabled (no account_id)
          - ib_insync is not installed
        """
        is_paper = (
            settings.bot.trading_mode != "live"
            or not settings.ibkr.enabled
        )

        if not is_paper:
            # Try to import live library
            try:
                from ib_insync import IB, Stock, LimitOrder, StopOrder, Order  # noqa: F401
            except ImportError:
                logger.warning(
                    "_place_ibkr_order: ib_insync not installed — "
                    "falling back to paper mode for %s",
                    symbol,
                )
                is_paper = True

        if is_paper:
            return self._paper_ibkr_order(symbol, risk_params, direction)

        # ── Live IBKR execution ─────────────────────────────────────────────
        try:
            from ib_insync import IB, Stock, LimitOrder, StopOrder, Order

            ib = IB()
            ib.connect(
                host=settings.ibkr.host,
                port=settings.ibkr.port,
                clientId=settings.ibkr.client_id,
            )

            contract = Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            action = "BUY" if direction == "long" else "SELL"
            qty = risk_params.quantity

            # Entry: limit order at desired entry price
            entry_order = LimitOrder(
                action=action,
                totalQuantity=qty,
                lmtPrice=round(risk_params.entry_price, 2),
            )
            entry_trade = ib.placeOrder(contract, entry_order)
            ib.sleep(2)  # allow a short fill window

            # Retrieve fill price
            filled_price: float | None = None
            if entry_trade.fills:
                filled_price = entry_trade.fills[-1].execution.avgPrice
            else:
                filled_price = risk_params.entry_price  # unfilled limit — use target

            # Bracket: stop-loss
            stop_action = "SELL" if direction == "long" else "BUY"
            stop_order = StopOrder(
                action=stop_action,
                totalQuantity=qty,
                stopPrice=round(risk_params.stop_price, 2),
            )
            stop_order.parentId = entry_order.orderId
            stop_order.transmit = False

            # Bracket: take-profit (limit order on the other side)
            tp_order = LimitOrder(
                action=stop_action,
                totalQuantity=qty,
                lmtPrice=round(risk_params.take_profit_price, 2),
            )
            tp_order.parentId = entry_order.orderId
            tp_order.ocaGroup = f"TP_SL_{entry_order.orderId}"
            tp_order.ocaType = 2  # cancel remaining on partial fill
            tp_order.transmit = True

            ib.placeOrder(contract, stop_order)
            ib.placeOrder(contract, tp_order)
            ib.disconnect()

            order_id = str(entry_order.orderId)
            logger.info(
                "IBKR LIVE ORDER: %s %s %s qty=%.4f @ %.4f stop=%.4f tp=%.4f id=%s",
                action, qty, symbol,
                qty, filled_price,
                risk_params.stop_price, risk_params.take_profit_price,
                order_id,
            )
            return OrderResult(
                success=True,
                order_id=order_id,
                filled_price=filled_price,
                error=None,
                broker="ibkr",
            )

        except Exception as exc:
            error = f"IBKR live order failed for {symbol}: {exc}"
            logger.exception("_place_ibkr_order live: %s", error)
            return OrderResult(
                success=False, order_id=None,
                filled_price=None, error=error,
                broker="ibkr",
            )

    def _paper_ibkr_order(
        self,
        symbol: str,
        risk_params: RiskParams,
        direction: str,
    ) -> OrderResult:
        """Simulate an IBKR order fill at risk_params.entry_price."""
        order_id = f"PAPER-IBKR-{symbol}-{_ts()}"
        action = "BUY" if direction == "long" else "SELL"
        logger.info(
            "PAPER ORDER: %s %s %s @ %.4f  stop=%.4f  tp=%.4f  id=%s",
            action,
            risk_params.quantity,
            symbol,
            risk_params.entry_price,
            risk_params.stop_price,
            risk_params.take_profit_price,
            order_id,
        )
        return OrderResult(
            success=True,
            order_id=order_id,
            filled_price=risk_params.entry_price,
            error=None,
            broker="ibkr",
        )

    # ------------------------------------------------------------------
    # OANDA order placement (forex)
    # ------------------------------------------------------------------

    def _place_oanda_order(
        self,
        symbol: str,
        risk_params: RiskParams,
        direction: str,
    ) -> OrderResult:
        """Place a forex entry order on OANDA.

        Falls back to paper mode when:
          - settings.bot.trading_mode == "paper"
          - settings.oanda is not enabled
          - oandapyV20 is not installed
        """
        is_paper = (
            settings.bot.trading_mode != "live"
            or not settings.oanda.enabled
        )

        if not is_paper:
            try:
                import oandapyV20  # noqa: F401
                from oandapyV20.endpoints import orders as oanda_orders  # noqa: F401
            except ImportError:
                logger.warning(
                    "_place_oanda_order: oandapyV20 not installed — "
                    "falling back to paper mode for %s",
                    symbol,
                )
                is_paper = True

        if is_paper:
            return self._paper_oanda_order(symbol, risk_params, direction)

        # ── Live OANDA execution ────────────────────────────────────────────
        try:
            import oandapyV20
            from oandapyV20.endpoints import orders as oanda_orders

            client = oandapyV20.API(
                access_token=settings.oanda.api_key,
                environment=settings.oanda.environment,
            )

            oanda_sym = _oanda_symbol(symbol)
            # OANDA units: positive = buy (long), negative = sell (short)
            units = int(risk_params.quantity) if direction == "long" else -int(risk_params.quantity)

            order_data = {
                "order": {
                    "type": "MARKET",
                    "instrument": oanda_sym,
                    "units": str(units),
                    "stopLossOnFill": {
                        "price": f"{risk_params.stop_price:.5f}",
                    },
                    "takeProfitOnFill": {
                        "price": f"{risk_params.take_profit_price:.5f}",
                    },
                    "timeInForce": "FOK",  # fill-or-kill for deterministic execution
                    "positionFill": "DEFAULT",
                }
            }

            request = oanda_orders.OrderCreate(
                accountID=settings.oanda.account_id,
                data=order_data,
            )
            response = client.request(request)

            # Extract fill price from response
            order_fill = response.get("orderFillTransaction", {})
            filled_price_str = order_fill.get("price", str(risk_params.entry_price))
            filled_price = float(filled_price_str)
            order_id = order_fill.get("id", f"OANDA-{symbol}-{_ts()}")

            logger.info(
                "OANDA LIVE ORDER: %s %d units %s @ %.5f  stop=%.5f  tp=%.5f  id=%s",
                direction.upper(), abs(units), oanda_sym,
                filled_price,
                risk_params.stop_price, risk_params.take_profit_price,
                order_id,
            )
            return OrderResult(
                success=True,
                order_id=str(order_id),
                filled_price=filled_price,
                error=None,
                broker="oanda",
            )

        except Exception as exc:
            error = f"OANDA live order failed for {symbol}: {exc}"
            logger.exception("_place_oanda_order live: %s", error)
            return OrderResult(
                success=False, order_id=None,
                filled_price=None, error=error,
                broker="oanda",
            )

    def _paper_oanda_order(
        self,
        symbol: str,
        risk_params: RiskParams,
        direction: str,
    ) -> OrderResult:
        """Simulate an OANDA order fill at risk_params.entry_price."""
        order_id = f"PAPER-OANDA-{symbol}-{_ts()}"
        units = int(risk_params.quantity)
        logger.info(
            "PAPER ORDER: %s %d units %s @ %.5f  stop=%.5f  tp=%.5f  id=%s",
            direction.upper(),
            units,
            symbol,
            risk_params.entry_price,
            risk_params.stop_price,
            risk_params.take_profit_price,
            order_id,
        )
        return OrderResult(
            success=True,
            order_id=order_id,
            filled_price=risk_params.entry_price,
            error=None,
            broker="oanda",
        )

    # ------------------------------------------------------------------
    # IBKR position close (stocks)
    # ------------------------------------------------------------------

    def _close_ibkr_position(
        self,
        position: Position,
        reason: str,
    ) -> OrderResult:
        """Close an IBKR stock position.

        Paper mode simulates at entry_price (no live quote available here).
        Live mode cancels any outstanding bracket orders then submits a
        market close order.
        """
        is_paper = (
            settings.bot.trading_mode != "live"
            or not settings.ibkr.enabled
        )

        if not is_paper:
            try:
                from ib_insync import IB, Stock, MarketOrder  # noqa: F401
            except ImportError:
                logger.warning(
                    "_close_ibkr_position: ib_insync not installed — paper fallback"
                )
                is_paper = True

        if is_paper:
            order_id = f"PAPER-IBKR-CLOSE-{position.symbol}-{_ts()}"
            logger.info(
                "PAPER CLOSE: %s %s qty=%.4f reason=%s",
                position.symbol, position.direction, position.quantity, reason,
            )
            return OrderResult(
                success=True,
                order_id=order_id,
                filled_price=position.entry_price,  # best estimate without live quote
                error=None,
                broker="ibkr",
            )

        # ── Live close ──────────────────────────────────────────────────────
        try:
            from ib_insync import IB, Stock, MarketOrder

            ib = IB()
            ib.connect(
                host=settings.ibkr.host,
                port=settings.ibkr.port,
                clientId=settings.ibkr.client_id,
            )

            contract = Stock(position.symbol, "SMART", "USD")
            ib.qualifyContracts(contract)

            # Cancel any open orders for this contract (stop / tp bracket legs)
            open_orders = ib.reqAllOpenOrders()
            for o in open_orders:
                if o.contract.symbol == position.symbol:
                    ib.cancelOrder(o.order)
            ib.sleep(1)

            close_action = "SELL" if position.direction == "long" else "BUY"
            close_order = MarketOrder(action=close_action, totalQuantity=position.quantity)
            close_trade = ib.placeOrder(contract, close_order)
            ib.sleep(2)

            filled_price = (
                close_trade.fills[-1].execution.avgPrice
                if close_trade.fills
                else position.entry_price
            )
            ib.disconnect()

            order_id = str(close_order.orderId)
            logger.info(
                "IBKR LIVE CLOSE: %s %s qty=%.4f fill=%.4f reason=%s id=%s",
                position.symbol, close_action, position.quantity,
                filled_price, reason, order_id,
            )
            return OrderResult(
                success=True,
                order_id=order_id,
                filled_price=filled_price,
                error=None,
                broker="ibkr",
            )

        except Exception as exc:
            error = f"IBKR live close failed for {position.symbol}: {exc}"
            logger.exception("_close_ibkr_position live: %s", error)
            return OrderResult(
                success=False, order_id=None,
                filled_price=None, error=error,
                broker="ibkr",
            )

    # ------------------------------------------------------------------
    # OANDA position close (forex)
    # ------------------------------------------------------------------

    def _close_oanda_position(
        self,
        position: Position,
        reason: str,
    ) -> OrderResult:
        """Close an OANDA forex position."""
        is_paper = (
            settings.bot.trading_mode != "live"
            or not settings.oanda.enabled
        )

        if not is_paper:
            try:
                import oandapyV20  # noqa: F401
                from oandapyV20.endpoints import positions as oanda_positions  # noqa: F401
            except ImportError:
                logger.warning(
                    "_close_oanda_position: oandapyV20 not installed — paper fallback"
                )
                is_paper = True

        if is_paper:
            order_id = f"PAPER-OANDA-CLOSE-{position.symbol}-{_ts()}"
            logger.info(
                "PAPER CLOSE: %s %s units=%.4f reason=%s",
                position.symbol, position.direction, position.quantity, reason,
            )
            return OrderResult(
                success=True,
                order_id=order_id,
                filled_price=position.entry_price,
                error=None,
                broker="oanda",
            )

        # ── Live close ──────────────────────────────────────────────────────
        try:
            import oandapyV20
            from oandapyV20.endpoints import positions as oanda_positions

            client = oandapyV20.API(
                access_token=settings.oanda.api_key,
                environment=settings.oanda.environment,
            )

            oanda_sym = _oanda_symbol(position.symbol)

            # Close all units on the long or short side
            if position.direction == "long":
                close_data = {"longUnits": "ALL"}
            else:
                close_data = {"shortUnits": "ALL"}

            request = oanda_positions.PositionClose(
                accountID=settings.oanda.account_id,
                instrument=oanda_sym,
                data=close_data,
            )
            response = client.request(request)

            # Extract fill details
            if position.direction == "long":
                tx = response.get("longOrderFillTransaction", {})
            else:
                tx = response.get("shortOrderFillTransaction", {})

            filled_price = float(tx.get("price", position.entry_price))
            order_id = str(tx.get("id", f"OANDA-CLOSE-{position.symbol}-{_ts()}"))

            logger.info(
                "OANDA LIVE CLOSE: %s %s units=%.4f fill=%.5f reason=%s id=%s",
                oanda_sym, position.direction, position.quantity,
                filled_price, reason, order_id,
            )
            return OrderResult(
                success=True,
                order_id=order_id,
                filled_price=filled_price,
                error=None,
                broker="oanda",
            )

        except Exception as exc:
            error = f"OANDA live close failed for {position.symbol}: {exc}"
            logger.exception("_close_oanda_position live: %s", error)
            return OrderResult(
                success=False, order_id=None,
                filled_price=None, error=error,
                broker="oanda",
            )

    # ------------------------------------------------------------------
    # Database helpers
    # ------------------------------------------------------------------

    def _insert_trade(
        self,
        symbol: str,
        broker: str,
        direction: str,
        filled_price: float,
        risk_params: RiskParams,
        confidence_result,
        regime,
    ) -> Optional[int]:
        """Insert a Trade row with status="open". Returns the new trade id, or None on failure."""
        session = get_session(self._db_url)
        try:
            trade = Trade(
                symbol=symbol,
                broker=broker,
                direction=direction,
                entry_price=filled_price,
                quantity=risk_params.quantity,
                confidence=confidence_result.dominant_score,
                position_tier=confidence_result.position_tier.value,
                regime=regime.regime.value,
                entry_time=datetime.utcnow(),
                status="open",
                signal_breakdown=confidence_result.breakdown,
            )
            session.add(trade)
            session.commit()
            trade_id = trade.id
            logger.debug("Trade inserted | id=%d symbol=%s", trade_id, symbol)
            return trade_id
        except Exception:
            session.rollback()
            logger.exception("_insert_trade: DB commit failed for %s", symbol)
            return None
        finally:
            session.close()

    def _log_event(
        self,
        event_type: str,
        symbol: str,
        description: str,
        metadata: dict | None = None,
    ) -> None:
        """Append a row to the EventLog table. Silently swallows DB errors."""
        session = get_session(self._db_url)
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
            session.rollback()
            logger.exception("_log_event: failed to write EventLog for %s", symbol)
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Test function
# ---------------------------------------------------------------------------

def test_execution_agent() -> None:
    """
    Smoke-test ExecutionAgent with paper orders against a temp SQLite DB.

    Verifies:
    1. Paper IBKR order:  success=True, order_id starts with "PAPER-IBKR"
    2. Paper OANDA order: success=True, order_id starts with "PAPER-OANDA"
    3. DB record written: Trade row exists with status="open"
    4. close_position:    Trade row updated to status="closed", Position removed from state
    """
    import os
    import tempfile

    # ── Setup temp DB ───────────────────────────────────────────────────────
    db_path = os.path.join(tempfile.gettempdir(), "test_execution_agent.db")
    db_url = f"sqlite:///{db_path}"
    init_db(db_url)

    # ── Minimal stubs ───────────────────────────────────────────────────────
    from enum import Enum
    from dataclasses import dataclass as _dc

    class _Tier(Enum):
        MEDIUM = "MEDIUM"

        def size_fraction(self):
            return 0.5

    @_dc
    class _ConfidenceResult:
        direction: str = "long"
        dominant_score: float = 78.0
        position_tier: object = None
        breakdown: dict = None

        def __post_init__(self):
            if self.position_tier is None:
                self.position_tier = _Tier.MEDIUM
            if self.breakdown is None:
                self.breakdown = {"cat1": {"vote": 1}}

    class _Regime(Enum):
        TRENDING_UP = "TRENDING_UP"

    @_dc
    class _RegimeContext:
        regime: object = None

        def __post_init__(self):
            if self.regime is None:
                self.regime = _Regime.TRENDING_UP

    confidence = _ConfidenceResult()
    regime = _RegimeContext()

    # Risk params: SPY long
    spy_risk = RiskParams(
        position_size_usd=100.0,
        quantity=0.2,
        entry_price=500.0,
        stop_price=490.0,
        take_profit_price=520.0,
        risk_dollars=5.0,
        position_tier="MEDIUM",
        size_fraction=0.5,
    )

    # Risk params: EURUSD long (forex)
    eurusd_risk = RiskParams(
        position_size_usd=100.0,
        quantity=1000.0,
        entry_price=1.08500,
        stop_price=1.07500,
        take_profit_price=1.10500,
        risk_dollars=5.0,
        position_tier="MEDIUM",
        size_fraction=0.5,
    )

    # ── Instantiate agent ───────────────────────────────────────────────────
    state = PortfolioStateManager(database_url=db_url)
    agent = ExecutionAgent(state_manager=state, database_url=db_url)

    # ── Test 1: Paper IBKR order (SPY) ─────────────────────────────────────
    result_ibkr = agent.place_order(
        symbol="SPY",
        risk_params=spy_risk,
        direction="long",
        confidence_result=confidence,
        regime=regime,
    )
    assert result_ibkr.success, f"IBKR paper order failed: {result_ibkr.error}"
    assert result_ibkr.order_id is not None
    assert result_ibkr.order_id.startswith("PAPER-IBKR"), (
        f"Expected order_id starting with 'PAPER-IBKR', got '{result_ibkr.order_id}'"
    )
    assert result_ibkr.filled_price == spy_risk.entry_price
    assert result_ibkr.broker == "ibkr"
    print(f"  [PASS] IBKR paper order: {result_ibkr.order_id}")

    # ── Test 2: Paper OANDA order (EURUSD) ──────────────────────────────────
    result_oanda = agent.place_order(
        symbol="EURUSD",
        risk_params=eurusd_risk,
        direction="long",
        confidence_result=confidence,
        regime=regime,
    )
    assert result_oanda.success, f"OANDA paper order failed: {result_oanda.error}"
    assert result_oanda.order_id is not None
    assert result_oanda.order_id.startswith("PAPER-OANDA"), (
        f"Expected order_id starting with 'PAPER-OANDA', got '{result_oanda.order_id}'"
    )
    assert result_oanda.filled_price == eurusd_risk.entry_price
    assert result_oanda.broker == "oanda"
    print(f"  [PASS] OANDA paper order: {result_oanda.order_id}")

    # ── Test 3: DB records written with status="open" ───────────────────────
    session = get_session(db_url)
    try:
        spy_position = state.get_position("SPY")
        eur_position = state.get_position("EURUSD")

        assert spy_position is not None, "SPY position not in state"
        assert eur_position is not None, "EURUSD position not in state"

        spy_trade = session.get(Trade, spy_position.db_trade_id)
        eur_trade = session.get(Trade, eur_position.db_trade_id)

        assert spy_trade is not None, "SPY Trade row not found in DB"
        assert eur_trade is not None, "EURUSD Trade row not found in DB"

        assert spy_trade.status == "open", f"SPY trade status={spy_trade.status}"
        assert eur_trade.status == "open", f"EURUSD trade status={eur_trade.status}"
        assert spy_trade.symbol == "SPY"
        assert eur_trade.symbol == "EURUSD"
        assert spy_trade.direction == "long"
        assert eur_trade.direction == "long"
        assert spy_trade.entry_price == spy_risk.entry_price
        assert eur_trade.entry_price == eurusd_risk.entry_price
        print("  [PASS] DB records: both Trade rows exist with status='open'")
    finally:
        session.close()

    # ── Test 4: close_position — DB updated, Position removed ───────────────
    spy_pos = state.get_position("SPY")
    assert spy_pos is not None, "SPY position missing before close test"
    close_result = agent.close_position(position=spy_pos, reason="take_profit")

    assert close_result.success, f"Close SPY failed: {close_result.error}"
    assert state.get_position("SPY") is None, "SPY still in state after close"

    session = get_session(db_url)
    try:
        closed_trade = session.get(Trade, spy_pos.db_trade_id)
        assert closed_trade is not None
        assert closed_trade.status == "closed", (
            f"Trade status expected 'closed', got '{closed_trade.status}'"
        )
        assert closed_trade.exit_price is not None
        assert closed_trade.exit_time is not None
        print(
            f"  [PASS] close_position: Trade id={spy_pos.db_trade_id} "
            f"status=closed exit={closed_trade.exit_price:.4f}"
        )
    finally:
        session.close()

    # ── Test 5: Unknown symbol returns failure, never raises ─────────────────
    bad_result = agent.place_order(
        symbol="UNKNOWN_XYZ",
        risk_params=spy_risk,
        direction="long",
        confidence_result=confidence,
        regime=regime,
    )
    assert not bad_result.success, "Unknown symbol should return success=False"
    assert bad_result.error is not None
    print(f"  [PASS] Unknown symbol handled gracefully: {bad_result.error[:60]}")

    # ── Cleanup ──────────────────────────────────────────────────────────────
    os.remove(db_path)
    print("\ntest_execution_agent: ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_execution_agent()
