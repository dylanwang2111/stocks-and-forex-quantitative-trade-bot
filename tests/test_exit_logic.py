"""
tests/test_exit_logic.py
Unit tests for the two-phase exit strategy:
  Phase 1 — hard stop-loss only (TP is a phase-trigger, not an exit)
  Phase 2 — partial close 50% on TP confirmation (streak ≥ 2), then 2.1×ATR trailing stop

Also tests:
  - portfolio.state.partial_close_position()
  - TP breach streak: increment / reset
  - partial_exit_done flag prevents double partial close
  - Snapshot persistence and restore of exit state

Run:
    python3 -m pytest tests/test_exit_logic.py -v
  or
    python3 tests/test_exit_logic.py
"""
from __future__ import annotations

import os
import sys
import logging
import tempfile
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call

# ── path setup ───────────────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database.models import init_db, Trade, get_session
from portfolio.state import Position, PortfolioStateManager
from agents.execution_agent import ExecutionAgent, OrderResult

logging.basicConfig(level=logging.WARNING)


# ── helpers ───────────────────────────────────────────────────────────────────

_db_counter = 0

def _make_db() -> str:
    """Create a unique temp SQLite DB per call and return its URL."""
    global _db_counter
    _db_counter += 1
    path = os.path.join(tempfile.gettempdir(), f"test_exit_{os.getpid()}_{_db_counter}.db")
    if os.path.exists(path):
        os.remove(path)
    url = f"sqlite:///{path}"
    init_db(url)
    return url


def _insert_trade(session, symbol: str, direction: str, qty: float, entry: float) -> int:
    trade = Trade(
        symbol=symbol,
        broker="ibkr" if symbol not in ("EURUSD", "GBPUSD", "USDJPY") else "oanda",
        direction=direction,
        entry_price=entry,
        quantity=qty,
        confidence=70.0,
        position_tier="MEDIUM",
        entry_time=datetime.utcnow(),
        status="open",
    )
    session.add(trade)
    session.commit()
    return trade.id


def _make_position(
    symbol="SPY",
    direction="long",
    qty=10.0,
    entry=100.0,
    stop=95.0,
    tp=110.0,
    db_trade_id=1,
    tp_breach_streak=0,
    partial_exit_done=False,
    days_held=2,
) -> Position:
    return Position(
        symbol=symbol,
        broker="ibkr",
        direction=direction,
        quantity=qty,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=tp,
        confidence=70.0,
        position_tier="MEDIUM",
        entry_time=datetime.utcnow() - timedelta(days=days_held),
        db_trade_id=db_trade_id,
        tp_breach_streak=tp_breach_streak,
        partial_exit_done=partial_exit_done,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. partial_close_position() — PortfolioStateManager
# ─────────────────────────────────────────────────────────────────────────────

def test_partial_close_reduces_quantity():
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade_id = _insert_trade(session, "SPY", "long", 10.0, 100.0)
    finally:
        session.close()

    mgr = PortfolioStateManager(database_url=db_url)
    pos = _make_position(qty=10.0, entry=100.0, db_trade_id=trade_id)
    mgr._positions["SPY"] = pos

    mgr.partial_close_position("SPY", close_fraction=0.5, exit_price=110.0, exit_time=datetime.utcnow())

    assert pos.quantity == 5.0, f"Expected 5.0, got {pos.quantity}"
    print("  [PASS] partial_close reduces quantity to 50%")


def test_partial_close_pnl_long():
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade_id = _insert_trade(session, "AAPL", "long", 10.0, 100.0)
    finally:
        session.close()

    mgr = PortfolioStateManager(database_url=db_url)
    pos = _make_position(symbol="AAPL", qty=10.0, entry=100.0, db_trade_id=trade_id)
    mgr._positions["AAPL"] = pos

    # close 50% at 110 — pnl = (110 - 100) * 5 = 50
    pnl = mgr.partial_close_position("AAPL", 0.5, exit_price=110.0, exit_time=datetime.utcnow())
    assert abs(pnl - 50.0) < 0.001, f"Expected pnl=50.0, got {pnl}"
    print("  [PASS] partial_close P&L correct for long")


def test_partial_close_pnl_short():
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade_id = _insert_trade(session, "GDXJ", "short", 8.0, 130.0)
    finally:
        session.close()

    mgr = PortfolioStateManager(database_url=db_url)
    pos = _make_position(
        symbol="GDXJ", direction="short", qty=8.0, entry=130.0,
        stop=136.0, tp=120.0, db_trade_id=trade_id,
    )
    mgr._positions["GDXJ"] = pos

    # close 50% at 120 — pnl = (130 - 120) * 4 = 40
    pnl = mgr.partial_close_position("GDXJ", 0.5, exit_price=120.0, exit_time=datetime.utcnow())
    assert abs(pnl - 40.0) < 0.001, f"Expected pnl=40.0, got {pnl}"
    print("  [PASS] partial_close P&L correct for short")


def test_partial_close_marks_partial_exit_done():
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade_id = _insert_trade(session, "XLE", "long", 20.0, 50.0)
    finally:
        session.close()

    mgr = PortfolioStateManager(database_url=db_url)
    pos = _make_position(symbol="XLE", qty=20.0, entry=50.0, db_trade_id=trade_id)
    mgr._positions["XLE"] = pos

    assert not pos.partial_exit_done
    mgr.partial_close_position("XLE", 0.5, exit_price=55.0, exit_time=datetime.utcnow())
    assert pos.partial_exit_done, "partial_exit_done should be True after partial close"
    print("  [PASS] partial_exit_done set to True")


def test_partial_close_updates_db_quantity():
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade_id = _insert_trade(session, "GLD", "short", 6.0, 460.0)
    finally:
        session.close()

    mgr = PortfolioStateManager(database_url=db_url)
    pos = _make_position(symbol="GLD", direction="short", qty=6.0, entry=460.0, db_trade_id=trade_id)
    mgr._positions["GLD"] = pos

    mgr.partial_close_position("GLD", 0.5, exit_price=450.0, exit_time=datetime.utcnow())

    session = get_session(db_url)
    try:
        trade = session.get(Trade, trade_id)
        assert abs(trade.quantity - 3.0) < 0.001, f"DB quantity expected 3.0, got {trade.quantity}"
    finally:
        session.close()
    print("  [PASS] DB Trade.quantity updated after partial close")


# ─────────────────────────────────────────────────────────────────────────────
# 2. Snapshot persistence of exit state
# ─────────────────────────────────────────────────────────────────────────────

def test_snapshot_includes_exit_state():
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade_id = _insert_trade(session, "SPY", "long", 10.0, 100.0)
    finally:
        session.close()

    mgr = PortfolioStateManager(database_url=db_url)
    pos = _make_position(
        qty=10.0, entry=100.0, db_trade_id=trade_id,
        tp_breach_streak=2, partial_exit_done=True,
    )
    mgr._positions["SPY"] = pos

    snap = mgr.snapshot()
    detail = snap["positions_detail"][0]
    assert detail["tp_breach_streak"] == 2, f"Expected 2, got {detail['tp_breach_streak']}"
    assert detail["partial_exit_done"] is True, "partial_exit_done should be True in snapshot"
    print("  [PASS] snapshot() includes tp_breach_streak and partial_exit_done")


def test_restore_from_db_restores_exit_state():
    from database.models import PortfolioSnapshot

    db_url = _make_db()
    now = datetime.utcnow()

    # Insert open Trade row
    session = get_session(db_url)
    try:
        trade = Trade(
            symbol="XOM", broker="ibkr", direction="long",
            entry_price=120.0, quantity=5.0, confidence=75.0,
            position_tier="MEDIUM", entry_time=now, status="open",
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id

        # Insert a snapshot with exit state
        detail = [{
            "symbol": "XOM",
            "stop_price": 115.0,
            "take_profit_price": 130.0,
            "tp_breach_streak": 2,
            "partial_exit_done": True,
        }]
        snap = PortfolioSnapshot(
            timestamp=now,
            total_equity=2000.0,
            cash=500.0,
            open_positions=1,
            daily_pnl=0.0,
            positions_detail=detail,  # JSON column — pass Python object directly
        )
        session.add(snap)
        session.commit()
    finally:
        session.close()

    mgr = PortfolioStateManager(database_url=db_url)
    count = mgr.restore_from_db()
    assert count == 1

    pos = mgr.get_position("XOM")
    assert pos is not None
    assert pos.tp_breach_streak == 2, f"Expected streak=2, got {pos.tp_breach_streak}"
    assert pos.partial_exit_done is True, "partial_exit_done should be restored as True"
    print("  [PASS] restore_from_db() restores tp_breach_streak and partial_exit_done")


# ─────────────────────────────────────────────────────────────────────────────
# 3. Orchestrator exit logic — via _check_exits with mocked dependencies
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_orchestrator(positions: list[Position]):
    """Build a minimal Orchestrator-like object with mocked dependencies."""
    from agents.orchestrator import Orchestrator
    from unittest.mock import MagicMock

    # Patch heavy constructors so __init__ doesn't actually connect to anything
    with patch("agents.orchestrator.HealthMonitor"), \
         patch("agents.orchestrator.PortfolioAgent"), \
         patch("agents.orchestrator.PreScreenAgent"), \
         patch("agents.orchestrator.TelegramNotifier"), \
         patch("agents.orchestrator.RiskAgent"), \
         patch("agents.orchestrator.ExecutionAgent"), \
         patch("agents.orchestrator.PortfolioStateManager"), \
         patch("agents.orchestrator.Scanner", create=True), \
         patch("agents.orchestrator.BlockingScheduler"):

        # Use a real-ish state mock
        state_mock = MagicMock()
        state_mock.all_positions.return_value = positions

        orch = object.__new__(Orchestrator)
        orch._state = state_mock
        orch._exec_agent = MagicMock()
        orch._notifier = MagicMock()
        orch._swing_holding_days = 5
        orch._cycle_count = 1
        orch._trading_mode = "paper"
        orch._database_url = "sqlite://"

    return orch


def _make_price_df(price: float):
    """Return a minimal one-row DataFrame that fetch_candles would return."""
    import pandas as pd
    return pd.DataFrame({"close": [price], "high": [price * 1.01], "low": [price * 0.99]})


# ── Phase 1: hard stop fires ──────────────────────────────────────────────────

def test_phase1_stop_loss_fires():
    """Phase 1: price below stop → close_position called with reason=stop_loss."""
    pos = _make_position(direction="long", entry=100.0, stop=95.0, tp=110.0)

    orch = _make_mock_orchestrator([pos])
    orch._exec_agent.close_position.return_value = OrderResult(
        success=True, order_id="P1", filled_price=95.0, error=None, broker="ibkr"
    )

    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(94.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_record_closed_trade_outcome"):
        orch._check_exits()

    orch._exec_agent.close_position.assert_called_once()
    kwargs = orch._exec_agent.close_position.call_args
    assert kwargs.kwargs["reason"] == "stop_loss", f"Expected stop_loss, got {kwargs}"
    assert kwargs.kwargs["fill_price"] == 95.0
    print("  [PASS] Phase 1: hard stop fires correctly")


def test_phase1_stop_fires_short():
    """Phase 1 short: price above stop → stop_loss."""
    pos = _make_position(direction="short", entry=100.0, stop=106.0, tp=90.0)

    orch = _make_mock_orchestrator([pos])
    orch._exec_agent.close_position.return_value = OrderResult(
        success=True, order_id="P1", filled_price=106.0, error=None, broker="ibkr"
    )

    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(107.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_record_closed_trade_outcome"):
        orch._check_exits()

    kwargs = orch._exec_agent.close_position.call_args.kwargs
    assert kwargs["reason"] == "stop_loss"
    print("  [PASS] Phase 1: short hard stop fires correctly")


# ── TP streak: increments and resets ─────────────────────────────────────────

def test_tp_streak_increments_no_close():
    """Price past TP in Phase 1: streak increments but NO close_position call."""
    pos = _make_position(direction="long", entry=100.0, stop=95.0, tp=110.0,
                         tp_breach_streak=0)

    orch = _make_mock_orchestrator([pos])

    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(112.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_update_trailing_stop"):
        orch._check_exits()

    # streak=1 → not yet phase 2 → no full close, no partial close
    assert pos.tp_breach_streak == 1, f"Expected streak=1, got {pos.tp_breach_streak}"
    orch._exec_agent.close_position.assert_not_called()
    orch._exec_agent.partial_close_position.assert_not_called()
    print("  [PASS] TP streak increments to 1, no close triggered")


def test_tp_streak_resets_when_price_falls_back():
    """Price falls back below TP: streak resets to 0."""
    pos = _make_position(direction="long", entry=100.0, stop=95.0, tp=110.0,
                         tp_breach_streak=1)

    orch = _make_mock_orchestrator([pos])

    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(108.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_update_trailing_stop"):
        orch._check_exits()

    assert pos.tp_breach_streak == 0, f"Expected streak=0, got {pos.tp_breach_streak}"
    orch._exec_agent.close_position.assert_not_called()
    print("  [PASS] TP streak resets to 0 when price falls back")


# ── Phase 2: partial close on streak=2 ───────────────────────────────────────

def test_phase2_partial_close_fires_at_streak_2():
    """At streak=2, partial_close_position is called once (50%)."""
    pos = _make_position(direction="long", entry=100.0, stop=95.0, tp=110.0,
                         tp_breach_streak=1, partial_exit_done=False)

    orch = _make_mock_orchestrator([pos])
    orch._exec_agent.partial_close_position.return_value = OrderResult(
        success=True, order_id="PART1", filled_price=112.0, error=None, broker="ibkr"
    )

    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(112.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_update_trailing_stop"):
        orch._check_exits()

    assert pos.tp_breach_streak == 2, f"Expected streak=2, got {pos.tp_breach_streak}"
    orch._exec_agent.partial_close_position.assert_called_once()
    kwargs = orch._exec_agent.partial_close_position.call_args.kwargs
    assert kwargs["close_fraction"] == 0.5
    assert kwargs["fill_price"] == 112.0
    print("  [PASS] Phase 2: partial close (50%) fires at streak=2")


def test_phase2_partial_close_only_once():
    """partial_exit_done=True prevents a second partial close."""
    pos = _make_position(direction="long", entry=100.0, stop=95.0, tp=110.0,
                         tp_breach_streak=5, partial_exit_done=True)

    orch = _make_mock_orchestrator([pos])

    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(115.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_update_trailing_stop"):
        orch._check_exits()

    orch._exec_agent.partial_close_position.assert_not_called()
    print("  [PASS] partial_exit_done=True prevents double partial close")


def test_phase2_trailing_stop_fires():
    """Phase 2: trailing stop fires when price reverses below updated stop."""
    # Position already in Phase 2: streak=2, partial done, stop now at 109
    pos = _make_position(direction="long", entry=100.0, stop=109.0, tp=110.0,
                         tp_breach_streak=2, partial_exit_done=True)

    orch = _make_mock_orchestrator([pos])
    orch._exec_agent.close_position.return_value = OrderResult(
        success=True, order_id="TS1", filled_price=109.0, error=None, broker="ibkr"
    )

    # Price has fallen to 108 — below the trailing stop at 109
    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(108.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_update_trailing_stop"),   \
         patch.object(orch, "_record_closed_trade_outcome"):
        orch._check_exits()

    orch._exec_agent.close_position.assert_called_once()
    kwargs = orch._exec_agent.close_position.call_args.kwargs
    assert kwargs["reason"] == "trailing_stop", f"Expected trailing_stop, got {kwargs['reason']}"
    assert kwargs["fill_price"] == 109.0
    print("  [PASS] Phase 2: trailing stop fires correctly")


def test_phase2_permanent_once_partial_done():
    """Once partial_exit_done=True, Phase 2 stays active even if price dips below TP."""
    # price fell back below TP (streak resets to 0), but partial already done
    pos = _make_position(direction="long", entry=100.0, stop=109.0, tp=110.0,
                         tp_breach_streak=0, partial_exit_done=True)

    orch = _make_mock_orchestrator([pos])
    orch._exec_agent.close_position.return_value = OrderResult(
        success=True, order_id="TS3", filled_price=109.0, error=None, broker="ibkr"
    )

    # Price at 108 — below trailing stop 109; partial_exit_done keeps us in Phase 2
    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(108.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_update_trailing_stop"), \
         patch.object(orch, "_record_closed_trade_outcome"):
        orch._check_exits()

    kwargs = orch._exec_agent.close_position.call_args.kwargs
    assert kwargs["reason"] == "trailing_stop", (
        f"Expected trailing_stop (Phase 2 permanent), got {kwargs['reason']}"
    )
    print("  [PASS] partial_exit_done keeps Phase 2 active even when streak resets")


def test_phase2_trailing_stop_fires_short():
    """Phase 2 short: trailing stop fires when price rises above stop."""
    pos = _make_position(direction="short", entry=130.0, stop=124.0, tp=120.0,
                         tp_breach_streak=3, partial_exit_done=True)

    orch = _make_mock_orchestrator([pos])
    orch._exec_agent.close_position.return_value = OrderResult(
        success=True, order_id="TS2", filled_price=124.0, error=None, broker="ibkr"
    )

    # Price at 125 — above the trailing stop at 124 → exit
    with patch("data.fetcher.fetch_candles", return_value=_make_price_df(125.0)), \
         patch.object(orch, "_has_signal_reversal", return_value=False), \
         patch.object(orch, "_update_trailing_stop"), \
         patch.object(orch, "_record_closed_trade_outcome"):
        orch._check_exits()

    kwargs = orch._exec_agent.close_position.call_args.kwargs
    assert kwargs["reason"] == "trailing_stop"
    assert kwargs["fill_price"] == 124.0
    print("  [PASS] Phase 2: short trailing stop fires correctly")


# ── ExecutionAgent.partial_close_position — paper mode ───────────────────────

def test_exec_agent_partial_close_paper_ibkr():
    """ExecutionAgent.partial_close_position returns success in paper mode, calls state."""
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade_id = _insert_trade(session, "SPY", "long", 10.0, 100.0)
    finally:
        session.close()

    state = PortfolioStateManager(database_url=db_url)
    pos = _make_position(qty=10.0, entry=100.0, db_trade_id=trade_id)
    state._positions["SPY"] = pos

    agent = ExecutionAgent(state_manager=state, database_url=db_url)
    result = agent.partial_close_position(position=pos, close_fraction=0.5, fill_price=108.0)

    assert result.success, f"Paper partial close should succeed: {result.error}"
    assert result.order_id is not None
    assert "PARTIAL" in result.order_id
    assert result.filled_price == 108.0
    assert pos.quantity == 5.0, f"Quantity should be halved, got {pos.quantity}"
    assert pos.partial_exit_done is True
    print("  [PASS] ExecutionAgent.partial_close_position paper IBKR OK")


def test_exec_agent_partial_close_paper_oanda():
    """ExecutionAgent.partial_close_position works for OANDA paper mode."""
    db_url = _make_db()
    session = get_session(db_url)
    try:
        trade = Trade(
            symbol="EURUSD", broker="oanda", direction="short",
            entry_price=1.0800, quantity=10000.0, confidence=70.0,
            position_tier="MEDIUM", entry_time=datetime.utcnow(), status="open",
        )
        session.add(trade)
        session.commit()
        trade_id = trade.id
    finally:
        session.close()

    state = PortfolioStateManager(database_url=db_url)
    pos = Position(
        symbol="EURUSD", broker="oanda", direction="short",
        quantity=10000.0, entry_price=1.0800, stop_price=1.0900,
        take_profit_price=1.0600, confidence=70.0, position_tier="MEDIUM",
        entry_time=datetime.utcnow(), db_trade_id=trade_id,
    )
    state._positions["EURUSD"] = pos

    agent = ExecutionAgent(state_manager=state, database_url=db_url)
    result = agent.partial_close_position(position=pos, close_fraction=0.5, fill_price=1.0650)

    assert result.success
    assert "PARTIAL" in result.order_id
    assert result.filled_price == 1.0650
    assert abs(pos.quantity - 5000.0) < 0.001
    print("  [PASS] ExecutionAgent.partial_close_position paper OANDA OK")


# ─────────────────────────────────────────────────────────────────────────────
# Runner
# ─────────────────────────────────────────────────────────────────────────────

ALL_TESTS = [
    # state: partial_close_position
    test_partial_close_reduces_quantity,
    test_partial_close_pnl_long,
    test_partial_close_pnl_short,
    test_partial_close_marks_partial_exit_done,
    test_partial_close_updates_db_quantity,
    # state: snapshot / restore
    test_snapshot_includes_exit_state,
    test_restore_from_db_restores_exit_state,
    # orchestrator: Phase 1 hard stop
    test_phase1_stop_loss_fires,
    test_phase1_stop_fires_short,
    # orchestrator: TP streak
    test_tp_streak_increments_no_close,
    test_tp_streak_resets_when_price_falls_back,
    # orchestrator: Phase 2 partial + trailing stop
    test_phase2_partial_close_fires_at_streak_2,
    test_phase2_partial_close_only_once,
    test_phase2_trailing_stop_fires,
    test_phase2_permanent_once_partial_done,
    test_phase2_trailing_stop_fires_short,
    # execution agent
    test_exec_agent_partial_close_paper_ibkr,
    test_exec_agent_partial_close_paper_oanda,
]


if __name__ == "__main__":
    passed = 0
    failed = 0
    errors = []
    for fn in ALL_TESTS:
        try:
            fn()
            passed += 1
        except Exception as exc:
            failed += 1
            errors.append((fn.__name__, exc))
            print(f"  [FAIL] {fn.__name__}: {exc}")

    print(f"\n{'='*60}")
    print(f"Results: {passed}/{len(ALL_TESTS)} passed", end="")
    if failed:
        print(f"  |  {failed} FAILED")
    else:
        print("  — ALL PASSED")
    print("="*60)
    sys.exit(1 if failed else 0)
