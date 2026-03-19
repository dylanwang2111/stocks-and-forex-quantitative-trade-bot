"""
dashboard.py
Streamlit multi-page monitoring dashboard for the trade bot.

Run: streamlit run dashboard.py
Or:  python main.py --mode dashboard
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, UTC

import pandas as pd
import streamlit as st

from config.settings import settings
from database.models import get_session, Trade, SignalLog, PortfolioSnapshot, StrategyRegistry, OptimizationCycle, EventLog
from portfolio.watchlist import UNIVERSE

st.set_page_config(page_title="Trade Bot", page_icon="📈", layout="wide")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _fmt_usd(value: float) -> str:
    return f"${value:,.2f}"


def _trading_mode_badge() -> str:
    return settings.bot.trading_mode.upper()


# ── Data fetchers (all wrapped in try/except) ──────────────────────────────────

def fetch_latest_snapshot() -> PortfolioSnapshot | None:
    try:
        session = get_session(settings.bot.database_url)
        snap = (
            session.query(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.timestamp.desc())
            .first()
        )
        session.close()
        return snap
    except Exception:
        return None


def fetch_open_positions() -> list[Trade]:
    try:
        session = get_session(settings.bot.database_url)
        rows = session.query(Trade).filter(Trade.status == "open").all()
        session.close()
        return rows
    except Exception:
        return []


def fetch_stop_tp_map() -> dict[str, dict]:
    """Return {symbol: {stop_price, take_profit_price}} from latest snapshot,
    with fallback to Trade table columns for symbols not in the snapshot."""
    result: dict[str, dict] = {}
    try:
        session = get_session(settings.bot.database_url)
        # Primary: latest snapshot positions_detail
        snap = (
            session.query(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.timestamp.desc())
            .first()
        )
        if snap and snap.positions_detail:
            result = {d["symbol"]: d for d in snap.positions_detail if "symbol" in d}
        # Fallback: open trades with stop/tp persisted in Trade table
        open_trades = session.query(Trade).filter(Trade.status == "open").all()
        for t in open_trades:
            if t.symbol not in result and (t.stop_price or t.take_profit_price):
                result[t.symbol] = {
                    "stop_price": t.stop_price,
                    "take_profit_price": t.take_profit_price,
                }
        session.close()
    except Exception:
        pass
    return result


def fetch_recent_signals(limit: int = 200) -> list[SignalLog]:
    try:
        session = get_session(settings.bot.database_url)
        rows = (
            session.query(SignalLog)
            .order_by(SignalLog.timestamp.desc())
            .limit(limit)
            .all()
        )
        session.close()
        return rows
    except Exception:
        return []


def fetch_closed_trades(limit: int = 50) -> list[Trade]:
    try:
        session = get_session(settings.bot.database_url)
        rows = (
            session.query(Trade)
            .filter(Trade.status == "closed")
            .order_by(Trade.exit_time.desc())
            .limit(limit)
            .all()
        )
        session.close()
        return rows
    except Exception:
        return []


def fetch_all_snapshots() -> list[PortfolioSnapshot]:
    try:
        session = get_session(settings.bot.database_url)
        rows = (
            session.query(PortfolioSnapshot)
            .order_by(PortfolioSnapshot.timestamp.asc())
            .all()
        )
        session.close()
        return rows
    except Exception:
        return []


def fetch_strategy_registry() -> list[StrategyRegistry]:
    try:
        session = get_session(settings.bot.database_url)
        rows = (
            session.query(StrategyRegistry)
            .order_by(StrategyRegistry.deployed_at.desc())
            .all()
        )
        session.close()
        return rows
    except Exception:
        return []


def fetch_all_trades() -> list[Trade]:
    """Return every trade row (open + closed), newest first."""
    try:
        session = get_session(settings.bot.database_url)
        rows = (
            session.query(Trade)
            .order_by(Trade.entry_time.desc())
            .all()
        )
        session.close()
        return rows
    except Exception:
        return []


def fetch_partial_close_events() -> list:
    """Return EventLog rows of type 'partial_close', newest first."""
    try:
        session = get_session(settings.bot.database_url)
        rows = (
            session.query(EventLog)
            .filter(EventLog.event_type == "partial_close")
            .order_by(EventLog.timestamp.desc())
            .all()
        )
        session.close()
        return rows
    except Exception:
        return []


def fetch_optimization_cycles(limit: int = 20) -> list[OptimizationCycle]:
    try:
        session = get_session(settings.bot.database_url)
        rows = (
            session.query(OptimizationCycle)
            .order_by(OptimizationCycle.started_at.desc())
            .limit(limit)
            .all()
        )
        session.close()
        return rows
    except Exception:
        return []


# ── Sidebar ────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("Trade Bot")
    mode = _trading_mode_badge()
    if mode == "LIVE":
        st.error("LIVE TRADING")
    else:
        st.info("PAPER MODE")

    page = st.radio(
        "Navigate",
        [
            "1 — Live Status",
            "2 — Portfolio View",
            "3 — Signal Board",
            "4 — Trade History",
            "5 — Cost Analysis",
            "6 — Optimizer",
            "7 — Strategy Registry",
            "8 — Transactions",
        ],
    )

    st.markdown("---")
    auto_refresh = st.checkbox("Auto-refresh (30s)", value=False)
    st.caption(f"Last render: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} UTC")


# ══════════════════════════════════════════════════════════════════════════════
# Page 1 — Live Status
# ══════════════════════════════════════════════════════════════════════════════

if page.startswith("1"):
    st.title("Live Status")

    snap = fetch_latest_snapshot()
    open_positions = fetch_open_positions()

    total_capital   = settings.bot.total_capital
    cash_reserve    = settings.bot.cash_reserve
    deployable      = settings.bot.deployable_capital
    cash_reserve_pct = settings.bot.cash_reserve_pct * 100

    col_cap, col_mode, col_status = st.columns(3)

    with col_cap:
        st.subheader("Capital")
        st.write(f"**Total:** {_fmt_usd(total_capital)}")
        st.write(f"**Cash reserve:** {_fmt_usd(cash_reserve)} ({cash_reserve_pct:.0f}%)")
        st.write(f"**Deployable:** {_fmt_usd(deployable)}")

    with col_mode:
        st.subheader("Mode")
        if mode == "LIVE":
            st.error("LIVE")
        else:
            st.info("PAPER")

    with col_status:
        st.subheader("Status")
        if snap is not None:
            last_ts = (
                snap.timestamp.strftime("%Y-%m-%d %H:%M:%S")
                if snap.timestamp else "unknown"
            )
            st.success("Running")
            st.caption(f"Last snapshot: {last_ts} UTC")
        else:
            st.warning("No snapshot data yet")

    st.divider()

    # Compute P&L live from DB
    import json as _json
    try:
        _today_naive = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        _sess = get_session(settings.bot.database_url)
        _all_closed = _sess.query(Trade).filter(Trade.status == "closed").all()
        _all_partials = _sess.query(EventLog).filter(EventLog.event_type == "partial_close").all()
        # All-time realized
        total_realized = sum(t.pnl_usd for t in _all_closed if t.pnl_usd is not None)
        for _ev in _all_partials:
            _m = _ev.event_metadata or {}
            if isinstance(_m, str):
                _m = _json.loads(_m)
            total_realized += _m.get("pnl_usd", 0.0)
        total_realized = round(total_realized, 4)
        # Today's realized
        daily_pnl = sum(t.pnl_usd for t in _all_closed
                        if t.pnl_usd is not None and t.exit_time and t.exit_time >= _today_naive)
        for _ev in _all_partials:
            if _ev.timestamp and _ev.timestamp >= _today_naive:
                _m = _ev.event_metadata or {}
                if isinstance(_m, str):
                    _m = _json.loads(_m)
                daily_pnl += _m.get("pnl_usd", 0.0)
        daily_pnl = round(daily_pnl, 4)
        _sess.close()
    except Exception:
        total_realized = snap.daily_pnl if snap and snap.daily_pnl is not None else 0.0
        daily_pnl = total_realized
    open_pos_count = len(open_positions)

    # Unrealized P&L from current prices
    unrealized_pnl = 0.0
    try:
        from data.fetcher import fetch_candles
        for t in open_positions:
            df = fetch_candles(t.symbol, "1h")
            if df is not None and not df.empty:
                current_price = float(df["close"].iloc[-1])
                open_qty = t.remaining_quantity if t.remaining_quantity is not None else t.quantity
                if t.direction == "long":
                    unrealized_pnl += (current_price - t.entry_price) * open_qty
                else:
                    unrealized_pnl += (t.entry_price - current_price) * open_qty
    except Exception:
        pass

    total_pnl = total_realized + unrealized_pnl

    current_equity = total_capital + total_realized

    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        equity_delta = total_realized
        st.metric("Equity", _fmt_usd(current_equity), delta=f"{equity_delta:+.2f}" if equity_delta else None)
    with m2:
        st.metric("Realized P&L", _fmt_usd(total_realized), delta=f"today {daily_pnl:+.2f}")
    with m3:
        st.metric("Unrealized P&L", _fmt_usd(unrealized_pnl), delta=f"{unrealized_pnl:+.2f}")
    with m4:
        st.metric("Total P&L", _fmt_usd(total_pnl), delta=f"{total_pnl:+.2f}")
    with m5:
        try:
            from portfolio.pdt_tracker import PDTTracker
            pdt = PDTTracker()
            pdt_used = pdt.count_day_trades_rolling()
            pdt_limit = pdt.PDT_LIMIT
        except Exception:
            pdt_used, pdt_limit = 0, 3
        st.metric("PDT Used", f"{pdt_used}/{pdt_limit}", delta=None)

    st.divider()

    # Equity curve — reconstructed from actual trade events (not snapshots, which were flat)
    st.subheader("Equity Curve")
    try:
        _eq_sess = get_session(settings.bot.database_url)
        _eq_closed   = _eq_sess.query(Trade).filter(Trade.status == "closed", Trade.exit_time != None).all()
        _eq_partials = _eq_sess.query(EventLog).filter(EventLog.event_type == "partial_close").all()
        _eq_sess.close()

        _eq_events = []
        for t in _eq_closed:
            _eq_events.append({"time": t.exit_time, "pnl": t.pnl_usd or 0.0})
        for ev in _eq_partials:
            _m = ev.event_metadata or {}
            if isinstance(_m, str):
                import json as _jj; _m = _jj.loads(_m)
            _eq_events.append({"time": ev.timestamp, "pnl": _m.get("pnl_usd", 0.0)})

        if _eq_events:
            _eq_df = pd.DataFrame(_eq_events).sort_values("time")
            _eq_df["equity"] = total_capital + _eq_df["pnl"].cumsum()
            # Prepend starting point
            _start = pd.DataFrame([{"time": _eq_df["time"].iloc[0] - pd.Timedelta(minutes=1),
                                     "equity": total_capital}])
            _eq_df = pd.concat([_start, _eq_df[["time", "equity"]]]).set_index("time")
            st.line_chart(_eq_df["equity"])
        else:
            st.info("No closed trades yet — equity curve will appear after the first trade closes.")
    except Exception:
        st.info("Equity curve unavailable.")

    st.divider()
    st.subheader("Open Positions")
    if open_positions:
        stop_tp = fetch_stop_tp_map()
        swing_days = settings.bot.swing_holding_days
        open_data = []
        for t in open_positions:
            current_price = None
            unreal = None
            try:
                from data.fetcher import fetch_candles
                df = fetch_candles(t.symbol, "1h")
                if df is not None and not df.empty:
                    current_price = round(float(df["close"].iloc[-1]), 4)
                    open_qty = t.remaining_quantity if t.remaining_quantity is not None else t.quantity
                    if t.direction == "long":
                        unreal = round((current_price - t.entry_price) * open_qty, 2)
                    else:
                        unreal = round((t.entry_price - current_price) * open_qty, 2)
            except Exception:
                pass

            detail          = stop_tp.get(t.symbol, {})
            stop            = detail.get("stop_price")
            tp              = detail.get("take_profit_price")
            partial_done    = detail.get("partial_exit_done", False)

            # Phase — derived from live price (more accurate than stale snapshot streak)
            at_tp_now = False
            if current_price and tp:
                if t.direction == "long" and current_price >= tp:
                    at_tp_now = True
                elif t.direction == "short" and current_price <= tp:
                    at_tp_now = True

            in_phase2 = partial_done or at_tp_now
            if partial_done:
                phase_label = "2 — trailing"
            elif at_tp_now:
                phase_label = "2 — past TP"
            else:
                phase_label = "1"

            # Days held / days remaining — count weekdays only (Mon–Fri)
            if t.entry_time:
                entry_date = t.entry_time.date()
                now_date   = datetime.now(UTC).replace(tzinfo=None).date()
                days_held  = sum(
                    1 for i in range((now_date - entry_date).days)
                    if (entry_date + timedelta(days=i + 1)).weekday() < 5
                )
            else:
                days_held = None
            days_left = (swing_days - days_held) if days_held is not None else None

            # Distance from CURRENT price to stop/TP (positive = price needs to move that far)
            dist_stop = dist_tp = None
            if stop and current_price:
                if t.direction == "long":
                    dist_stop = round((current_price - stop) / current_price * 100, 2)
                else:
                    dist_stop = round((stop - current_price) / current_price * 100, 2)
            if tp and current_price:
                if t.direction == "long":
                    dist_tp = round((tp - current_price) / current_price * 100, 2)
                else:
                    dist_tp = round((current_price - tp) / current_price * 100, 2)

            # TP progress % — NOT capped at 100; >100 means we're in Phase 2 territory
            tp_progress = None
            if current_price and tp and t.entry_price:
                total_range = abs(tp - t.entry_price)
                if total_range > 0:
                    moved = (
                        (current_price - t.entry_price)
                        if t.direction == "long"
                        else (t.entry_price - current_price)
                    )
                    tp_progress = round(moved / total_range * 100, 1)

            open_data.append({
                "symbol":       t.symbol,
                "dir":          t.direction,
                "phase":        phase_label,
                "partial":      "✓" if partial_done else "",
                "entry":        t.entry_price,
                "current":      current_price,
                "stop":         round(stop, 4) if stop else None,
                "target":       round(tp, 4) if tp else None,
                "to_stop%":     dist_stop,
                "to_target%":   dist_tp,
                "TP_prog%":     tp_progress,
                "unreal_pnl$":  unreal,
                "qty":          t.remaining_quantity if t.remaining_quantity is not None else t.quantity,
                "entry_qty":    t.quantity,
                "days_held":    days_held,
                "days_left":    days_left,
                "confidence":   round(t.confidence, 1),
            })

        df_open = pd.DataFrame(open_data)

        def _color_open(row):
            styles = [""] * len(row)
            cols = list(row.index)

            def idx(name):
                return cols.index(name) if name in cols else None

            # unreal_pnl$: green/red
            i = idx("unreal_pnl$")
            if i is not None and row["unreal_pnl$"] is not None:
                try:
                    v = float(row["unreal_pnl$"])
                    styles[i] = (
                        "color: #155724; font-weight: bold" if v > 0
                        else "color: #721c24; font-weight: bold" if v < 0
                        else ""
                    )
                except (TypeError, ValueError):
                    pass

            # TP_prog%: green gradient; orange/red if negative
            i = idx("TP_prog%")
            if i is not None and row["TP_prog%"] is not None:
                try:
                    p = float(row["TP_prog%"])
                    if p >= 100:
                        styles[i] = "background-color: #0a3622; color: #75b798; font-weight: bold"
                    elif p >= 75:
                        styles[i] = "background-color: #155724; color: white"
                    elif p >= 40:
                        styles[i] = "background-color: #d4edda; color: #155724"
                    elif p < 0:
                        styles[i] = "color: #721c24"
                except (TypeError, ValueError):
                    pass

            # days_left: red if ≤1
            i = idx("days_left")
            if i is not None and row["days_left"] is not None:
                try:
                    if int(row["days_left"]) <= 1:
                        styles[i] = "color: #721c24; font-weight: bold"
                except (TypeError, ValueError):
                    pass

            # to_stop%: red if < 0.5% (very close to stop)
            i = idx("to_stop%")
            if i is not None and row["to_stop%"] is not None:
                try:
                    if float(row["to_stop%"]) < 0.5:
                        styles[i] = "color: #721c24; font-weight: bold"
                except (TypeError, ValueError):
                    pass

            return styles

        styled_open = df_open.style.apply(_color_open, axis=1)
        st.dataframe(styled_open, width="stretch")

        st.caption(
            "**phase**: 1 = hard stop active | 2 — past TP = price past target, trailing activating | "
            "2 — trailing = trailing stop live  |  **partial ✓** = 50% closed at TP confirmation  |  "
            "**to_stop%** / **to_target%** = distance from current price (positive = not yet hit)  |  "
            "**TP_prog%** > 100 = price past original target"
        )
    else:
        st.info("No open positions")


# ══════════════════════════════════════════════════════════════════════════════
# Page 2 — Portfolio View
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("2"):
    st.title("Portfolio View")

    # Universe cards
    st.subheader("Active Universe")
    cols = st.columns(min(len(UNIVERSE), 6))
    for idx, inst in enumerate(UNIVERSE):
        with cols[idx % len(cols)]:
            with st.container(border=True):
                st.markdown(f"**{inst.symbol}**")
                st.caption(f"{inst.broker} | {inst.asset_type}")

    st.divider()

    # Capital allocation — read fresh from env to avoid stale module cache
    import os as _os
    _ibkr_cap  = float(_os.getenv("IBKR_CAPITAL",  "0") or 0) or settings.bot.total_capital
    _oanda_cap = float(_os.getenv("OANDA_CAPITAL", "0") or 0) or settings.bot.total_capital
    _reserve_pct = float(_os.getenv("CASH_RESERVE_PCT", "0.30"))
    _total_cap = float(_os.getenv("TOTAL_CAPITAL", str(settings.bot.total_capital)))

    st.subheader("Capital Allocation")
    open_positions = fetch_open_positions()

    _ibkr_pos  = [p for p in open_positions if (p.broker or "ibkr") == "ibkr"]
    _oanda_pos = [p for p in open_positions if (p.broker or "ibkr") == "oanda"]
    _ibkr_dep  = sum((p.quantity or 0) * (p.entry_price or 0) for p in _ibkr_pos)
    _oanda_dep = sum((p.quantity or 0) * (p.entry_price or 0) for p in _oanda_pos)
    _ibkr_res  = _ibkr_cap  * _reserve_pct
    _oanda_res = _oanda_cap * _reserve_pct
    _ibkr_avail  = max(0.0, _ibkr_cap  - _ibkr_res  - _ibkr_dep)
    _oanda_avail = max(0.0, _oanda_cap - _oanda_res - _oanda_dep)

    alloc_df = pd.DataFrame([
        {
            "Broker":      "IBKR (stocks)",
            "Pool $":      f"${_ibkr_cap:,.0f}",
            "Reserve $":   f"${_ibkr_res:,.0f}",
            "Deployed $":  f"${_ibkr_dep:,.2f}",
            "Available $": f"${_ibkr_avail:,.2f}",
            "Utilization": f"{_ibkr_dep / _ibkr_cap * 100:.0f}%" if _ibkr_cap else "—",
            "Positions":   len(_ibkr_pos),
        },
        {
            "Broker":      "OANDA (forex/crypto)",
            "Pool $":      f"${_oanda_cap:,.0f}",
            "Reserve $":   f"${_oanda_res:,.0f}",
            "Deployed $":  f"${_oanda_dep:,.2f}",
            "Available $": f"${_oanda_avail:,.2f}",
            "Utilization": f"{_oanda_dep / _oanda_cap * 100:.0f}%" if _oanda_cap else "—",
            "Positions":   len(_oanda_pos),
        },
        {
            "Broker":      "TOTAL",
            "Pool $":      f"${_total_cap:,.0f}",
            "Reserve $":   f"${(_ibkr_res + _oanda_res):,.0f}",
            "Deployed $":  f"${(_ibkr_dep + _oanda_dep):,.2f}",
            "Available $": f"${(_ibkr_avail + _oanda_avail):,.2f}",
            "Utilization": f"{(_ibkr_dep + _oanda_dep) / _total_cap * 100:.0f}%" if _total_cap else "—",
            "Positions":   len(open_positions),
        },
    ])
    st.dataframe(alloc_df, use_container_width=True, hide_index=True)

    # Utilization progress bars
    for _lbl, _dep, _cap in [("IBKR", _ibkr_dep, _ibkr_cap), ("OANDA", _oanda_dep, _oanda_cap)]:
        _pct = min(1.0, _dep / _cap) if _cap else 0
        st.caption(f"{_lbl}  ${_dep:,.2f} / ${_cap:,.0f}  ({_pct*100:.0f}% deployed)")
        st.progress(_pct)

    st.divider()

    # Correlation matrix
    st.subheader("Correlation Blacklist (from watchlist)")
    try:
        from portfolio.watchlist import CORRELATION_BLACKLIST
        if CORRELATION_BLACKLIST:
            rows = [{"Pair": f"{a} ↔ {b}"} for a, b in CORRELATION_BLACKLIST]
            st.dataframe(pd.DataFrame(rows), width="stretch")
        else:
            st.info("No correlation pairs defined.")
    except Exception as exc:
        st.warning(f"Could not load correlation data: {exc}")

    st.divider()

    # Session timing
    st.subheader("Session Timing (UTC)")
    timing = {
        "Stock (IBKR)": "13:30 – 20:00",
        "Forex (OANDA)": "00:00 – 23:59 (Sun–Fri)",
        "EUR/USD peak":  "07:00 – 16:00",
        "GBP/USD peak":  "07:00 – 16:00",
    }
    for market, window in timing.items():
        st.write(f"- **{market}**: {window}")


# ══════════════════════════════════════════════════════════════════════════════
# Page 3 — Signal Board
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("3"):
    st.title("Signal Board")

    recent_signals = fetch_recent_signals(200)

    if recent_signals:
        # ── Filters ────────────────────────────────────────────────────────
        sf1, sf2, sf3 = st.columns(3)
        all_syms_sig = ["All"] + sorted({s.symbol for s in recent_signals if s.symbol})
        with sf1:
            sym_filter = st.selectbox("Symbol", all_syms_sig, key="sig_sym")
        with sf2:
            tier_filter = st.selectbox("Min tier", ["All", "SMALL", "MEDIUM", "LARGE", "FULL"], key="sig_tier")
        with sf3:
            tradeable_only = st.checkbox("Tradeable only (≥SMALL)", key="sig_tradeable")

        signal_data = [
            {
                "time":    s.timestamp,
                "symbol":  s.symbol,
                "dir":     s.direction,
                "score":   round(s.dominant_score, 1) if s.dominant_score else None,
                "tier":    s.position_tier,
                "regime":  s.regime,
                "c1":      s.cat1_trend,
                "c2":      s.cat2_strength,
                "c3":      s.cat3_momentum,
                "c4":      s.cat4_volatility,
                "c5":      s.cat5_volume,
                "c6":      s.cat6_structure,
                "c7":      s.cat7_mtf,
                "c8":      s.cat8_macro,
            }
            for s in recent_signals
        ]
        df_signals = pd.DataFrame(signal_data)

        _tradeable_tiers = {"SMALL", "MEDIUM", "LARGE", "FULL"}
        _tier_order = {"SMALL": 1, "MEDIUM": 2, "LARGE": 3, "FULL": 4}
        if sym_filter != "All":
            df_signals = df_signals[df_signals["symbol"] == sym_filter]
        if tradeable_only:
            df_signals = df_signals[df_signals["tier"].isin(_tradeable_tiers)]
        if tier_filter != "All":
            min_rank = _tier_order.get(tier_filter, 0)
            df_signals = df_signals[df_signals["tier"].map(lambda t: _tier_order.get(t, 0)) >= min_rank]

        def _color_score(val):
            try:
                v = float(val)
                if v >= 75:
                    return "background-color: #155724; color: white"
                if v >= 65:
                    return "background-color: #d4edda; color: #155724"
                if v >= 55:
                    return "background-color: #fff3cd; color: #856404"
            except (TypeError, ValueError):
                pass
            return ""

        st.caption(f"Showing {len(df_signals)} of {len(recent_signals)} signals (last 200 scans)")
        styled = df_signals.style.map(_color_score, subset=["score"])
        st.dataframe(styled, use_container_width=True)

        # Score distribution chart
        st.subheader("Score Distribution")
        score_series = df_signals["score"].dropna()
        if not score_series.empty:
            hist_df = score_series.value_counts().sort_index()
            st.bar_chart(hist_df)
    else:
        st.info("No signal data yet — signals appear after the first scan cycle.")


# ══════════════════════════════════════════════════════════════════════════════
# Page 4 — Trade History
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("4"):
    st.title("Trade History")

    closed_trades   = fetch_closed_trades(50)
    partial_events  = fetch_partial_close_events()

    import json as _json

    def _color_pnl(val):
        try:
            v = float(val)
            if v > 0:   return "color: #155724; font-weight: bold"
            elif v < 0: return "color: #721c24; font-weight: bold"
        except (TypeError, ValueError):
            pass
        return ""

    # ── Build partial close rows ───────────────────────────────────────────
    all_trades_list = fetch_all_trades()
    all_trades_by_id = {tr.id: tr for tr in all_trades_list}
    partial_rows = []
    for ev in partial_events:
        meta = ev.event_metadata or {}
        if isinstance(meta, str):
            meta = _json.loads(meta)
        sym   = ev.symbol
        # Use db_trade_id from metadata; fallback to most recent trade for this symbol before event
        _meta_tid = meta.get("db_trade_id")
        t = all_trades_by_id.get(_meta_tid) if _meta_tid else None
        if t is None:
            t = next(
                (tr for tr in sorted(all_trades_list, key=lambda x: x.id, reverse=True)
                 if tr.symbol == sym and tr.entry_time <= ev.timestamp),
                None,
            )
        pnl   = meta.get("pnl_usd")
        price = meta.get("exit_price")
        frac  = meta.get("fraction", 0.5)
        cqty  = meta.get("close_qty")
        if cqty is None and pnl is not None and price is not None and t:
            spread = abs(t.entry_price - price)
            cqty = round(abs(pnl) / spread, 6) if spread > 0 else None
        partial_rows.append({
            "time":      ev.timestamp,
            "symbol":    sym,
            "direction": t.direction if t else "",
            "type":      f"PARTIAL ({int(frac*100)}%)",
            "entry_price": t.entry_price if t else None,
            "close_price": price,
            "qty":       cqty,
            "pnl":       pnl,
            "reason":    f"phase-2 partial",
        })

    # ── Shared symbol / direction filter ──────────────────────────────────
    all_syms = sorted(set(
        [t.symbol for t in closed_trades] + [r["symbol"] for r in partial_rows]
    ))
    col_f1, col_f2 = st.columns(2)
    with col_f1:
        sym_filter = st.selectbox("Filter by symbol", ["All"] + all_syms, key="th_sym")
    with col_f2:
        dir_filter = st.selectbox("Filter by direction", ["All", "long", "short"], key="th_dir")

    # ── Section 1: Full closes ─────────────────────────────────────────────
    st.subheader("Full Closes")
    if closed_trades:
        closed_data = [
            {
                "time":        t.exit_time,
                "symbol":      t.symbol,
                "direction":   t.direction,
                "entry_price": t.entry_price,
                "exit_price":  t.exit_price,
                "qty":         t.quantity,
                "pnl":         t.pnl_usd,
                "reason":      t.notes or "",
            }
            for t in closed_trades
        ]
        df_closed = pd.DataFrame(closed_data)
        if sym_filter != "All":
            df_closed = df_closed[df_closed["symbol"] == sym_filter]
        if dir_filter != "All":
            df_closed = df_closed[df_closed["direction"] == dir_filter]
        st.dataframe(df_closed.style.map(_color_pnl, subset=["pnl"]), use_container_width=True)
    else:
        st.info("No full closes yet.")
        df_closed = pd.DataFrame()

    # ── Section 2: Partial closes ──────────────────────────────────────────
    st.subheader("Partial Closes")
    if partial_rows:
        df_partial = pd.DataFrame(partial_rows)
        if sym_filter != "All":
            df_partial = df_partial[df_partial["symbol"] == sym_filter]
        if dir_filter != "All":
            df_partial = df_partial[df_partial["direction"] == dir_filter]
        st.dataframe(df_partial.style.map(_color_pnl, subset=["pnl"]), use_container_width=True)
    else:
        st.info("No partial closes yet.")
        df_partial = pd.DataFrame()

    # ── Summary metrics (full + partial combined) ──────────────────────────
    st.divider()
    full_pnl    = df_closed["pnl"].sum()    if not df_closed.empty  else 0.0
    partial_pnl = df_partial["pnl"].sum()   if not df_partial.empty else 0.0
    total_pnl   = full_pnl + partial_pnl
    n_full      = len(df_closed)
    wins        = int((df_closed["pnl"] > 0).sum()) if not df_closed.empty else 0
    win_rate    = wins / n_full * 100 if n_full > 0 else 0

    sm1, sm2, sm3, sm4, sm5 = st.columns(5)
    sm1.metric("Full Closes",    n_full)
    sm2.metric("Partial Closes", len(df_partial) if not df_partial.empty else 0)
    sm3.metric("Win Rate",       f"{win_rate:.1f}%")
    sm4.metric("Realized (full)", _fmt_usd(full_pnl))
    sm5.metric("Realized (all)",  _fmt_usd(total_pnl))

    # ── Cumulative P&L chart (full + partial combined, time-sorted) ────────
    st.subheader("Cumulative Realized P&L")
    events = []
    if not df_closed.empty:
        for _, r in df_closed.iterrows():
            events.append({"time": r["time"], "pnl": r["pnl"] or 0})
    if not df_partial.empty:
        for _, r in df_partial.iterrows():
            events.append({"time": r["time"], "pnl": r["pnl"] or 0})
    if events:
        df_ev = pd.DataFrame(events).sort_values("time")
        df_ev["cumulative_pnl"] = df_ev["pnl"].cumsum()
        st.line_chart(df_ev.set_index("time")["cumulative_pnl"])


# ══════════════════════════════════════════════════════════════════════════════
# Page 5 — Cost Analysis
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("5"):
    st.title("Cost Analysis")
    st.caption("Gross P&L vs net P&L after estimated fees and slippage. Includes partial closes.")

    closed_trades = fetch_closed_trades(200)

    # Also fetch partial close events
    try:
        _s5 = get_session(settings.bot.database_url)
        _partial_events = (
            _s5.query(EventLog)
            .filter(EventLog.event_type == "partial_close")
            .order_by(EventLog.timestamp.desc())
            .limit(200)
            .all()
        )
        _s5.close()
    except Exception:
        _partial_events = []

    if closed_trades or _partial_events:
        STOCK_FEE  = 0.001   # 0.1% round-trip
        FOREX_FEE  = 0.0003  # 0.03% round-trip
        SLIPPAGE   = 0.0005  # 0.05% per trade (conservative)

        rows = []
        for t in closed_trades:
            if t.entry_price is None or t.exit_price is None or t.quantity is None:
                continue
            gross = t.pnl_usd or 0.0
            close_qty = t.remaining_quantity if t.remaining_quantity is not None else t.quantity
            position_usd = t.entry_price * close_qty
            is_forex = getattr(t, "broker", "ibkr") == "oanda"
            fee_rate = FOREX_FEE if is_forex else STOCK_FEE
            cost = position_usd * (fee_rate + SLIPPAGE)
            net = gross - cost
            rows.append({
                "symbol":         t.symbol,
                "type":           "full close",
                "gross_pnl":      gross,
                "estimated_cost": cost,
                "net_pnl":        net,
                "exit_time":      t.exit_time,
            })

        # Build a symbol→trade lookup for partial close fallback calculations
        _trade_by_sym = {t.symbol: t for t in closed_trades}

        for evt in _partial_events:
            meta = evt.event_metadata or {}
            if isinstance(meta, str):
                import json as _j
                meta = _j.loads(meta)
            gross      = meta.get("pnl_usd", 0.0)
            exit_price = meta.get("exit_price", 0.0)
            qty        = meta.get("close_qty") or 0.0
            if not exit_price:
                continue
            # Fallback: back-calculate qty from pnl and entry-exit spread (old records)
            if not qty and gross and evt.symbol in _trade_by_sym:
                _entry = _trade_by_sym[evt.symbol].entry_price
                spread = abs(_entry - exit_price)
                if spread > 0:
                    qty = abs(gross) / spread
            if not qty:
                continue
            position_usd = exit_price * qty
            # Determine broker from open trade if available
            sym = evt.symbol or ""
            _t = next((t for t in closed_trades if t.symbol == sym), None)
            is_forex = (_t.broker == "oanda") if _t else (sym in ("EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF"))
            fee_rate = FOREX_FEE if is_forex else STOCK_FEE
            cost = position_usd * (fee_rate + SLIPPAGE)
            net = gross - cost
            rows.append({
                "symbol":         sym,
                "type":           "partial close",
                "gross_pnl":      gross,
                "estimated_cost": cost,
                "net_pnl":        net,
                "exit_time":      evt.timestamp,
            })

        if rows:
            df_cost = pd.DataFrame(rows)
            total_gross = df_cost["gross_pnl"].sum()
            total_cost  = df_cost["estimated_cost"].sum()
            total_net   = df_cost["net_pnl"].sum()

            c1, c2, c3 = st.columns(3)
            c1.metric("Gross P&L",    _fmt_usd(total_gross))
            c2.metric("Total Costs",  _fmt_usd(total_cost), delta=f"-{total_cost:.2f}")
            c3.metric("Net P&L",      _fmt_usd(total_net))

            st.divider()
            st.subheader("Gross vs Net P&L per Trade")
            chart_df = df_cost[["gross_pnl", "net_pnl"]].reset_index(drop=True)
            st.line_chart(chart_df)

            st.subheader("Running Cost Total")
            cost_cumsum = df_cost.sort_values("exit_time")["estimated_cost"].cumsum().reset_index(drop=True)
            st.area_chart(cost_cumsum)

            st.divider()
            st.subheader("Per-Trade Detail")
            st.dataframe(df_cost.sort_values("exit_time", ascending=False), width="stretch")
        else:
            st.info("No closed trades with price data available.")
    else:
        st.info("No closed trades yet.")


# ══════════════════════════════════════════════════════════════════════════════
# Page 6 — Optimizer
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("6"):
    st.title("Optimizer")

    opt_cycles = fetch_optimization_cycles(20)

    if opt_cycles:
        st.subheader("Recent Optimization Cycles")
        cycle_data = [
            {
                "started_at":    c.started_at,
                "param_changed": str(c.params_after or "")[:60],
                "accepted":      "✅ Yes" if c.accepted else "❌ No",
                "notes":         (c.notes or "")[:80],
                "IS sharpe":     round(c.in_sample_sharpe or 0, 2),
                "OOS sharpe":    round(c.oos_sharpe or 0, 2),
                "p_value":       round(c.p_value or 1.0, 4),
            }
            for c in opt_cycles
        ]
        st.dataframe(pd.DataFrame(cycle_data), width="stretch")

        # Summary
        accepted = sum(1 for c in opt_cycles if c.accepted)
        rejected = len(opt_cycles) - accepted
        a1, a2 = st.columns(2)
        a1.metric("Total Accepted", accepted)
        a2.metric("Total Rejected", rejected)
    else:
        st.info("No optimization cycles run yet. Need ≥50 trades to trigger the optimizer.")

    st.divider()
    st.subheader("Run Optimization Now")
    st.caption("Requires ≥50 closed trades. Runs the full 7-step pipeline.")
    if st.button("Run Optimizer (no approval gate)", type="secondary"):
        with st.spinner("Running optimization pipeline…"):
            try:
                from optimization.pipeline import OptimizationPipeline
                pipeline = OptimizationPipeline(database_url=settings.bot.database_url)
                pipeline.run(require_human_approval=False)
                st.success("Optimization complete. Refresh to see new results.")
            except Exception as exc:
                st.error(f"Optimizer error: {exc}")


# ══════════════════════════════════════════════════════════════════════════════
# Page 7 — Strategy Registry
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("7"):
    st.title("Strategy Registry")
    st.caption("All deployed parameter versions. Most recent first.")

    registry = fetch_strategy_registry()

    if registry:
        reg_data = [
            {
                "name":       r.name,
                "version":    r.version,
                "created_at": r.created_at,
                "is_active":  "✅ Active" if r.is_active else "—",
                "description": r.description or "",
                "params":     str(r.params)[:120] + ("…" if r.params and len(str(r.params)) > 120 else ""),
            }
            for r in registry
        ]
        st.dataframe(pd.DataFrame(reg_data), width="stretch")

        # Highlight active version
        active = [r for r in registry if r.is_active]
        if active:
            st.subheader("Active Parameters")
            st.json(active[0].params or {})
        else:
            st.info("No active strategy version found.")
    else:
        st.info("No strategy versions yet. Run the optimizer to create the first entry.")


# ══════════════════════════════════════════════════════════════════════════════
# Page 8 — Transactions
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("8"):
    st.title("Transactions")
    st.caption("Full ledger of every entry, partial close, and exit — chronological.")

    all_trades    = fetch_all_trades()
    partial_events = fetch_partial_close_events()

    if not all_trades:
        st.info("No transactions yet.")
    else:
        # ── Build unified ledger ───────────────────────────────────────────
        rows = []

        # Map trade_id → trade for partial close join
        trade_by_id = {t.id: t for t in all_trades}

        # 1. ENTRY row for every trade
        # original_qty: sum of all partial closes for this trade + current remaining qty
        partial_closed_by_trade: dict[int, float] = {}
        for ev in partial_events:
            meta = ev.event_metadata or {}
            sym  = ev.symbol
            _mid = meta.get("db_trade_id")
            matched_t = trade_by_id.get(_mid) if _mid else None
            if matched_t is None:
                matched_t = next(
                    (t for t in sorted(all_trades, key=lambda x: x.id, reverse=True)
                     if t.symbol == sym and t.entry_time <= ev.timestamp),
                    None,
                )
            if not matched_t:
                continue
            cqty = meta.get("close_qty")
            # Fallback for old records: back-calculate from pnl / price spread
            if cqty is None:
                pnl_m  = meta.get("pnl_usd")
                price_m = meta.get("exit_price")
                if pnl_m is not None and price_m is not None:
                    spread = abs(matched_t.entry_price - price_m)
                    if spread > 0:
                        cqty = round(abs(pnl_m) / spread, 6)
            if cqty is not None:
                partial_closed_by_trade[matched_t.id] = (
                    partial_closed_by_trade.get(matched_t.id, 0.0) + cqty
                )

        stop_tp_map = fetch_stop_tp_map()

        for t in all_trades:
            stp = stop_tp_map.get(t.symbol, {})
            # Fallback to Trade row columns for closed trades not in snapshot/stop_tp_map
            stop_val   = stp.get("stop_price")        or t.stop_price
            target_val = stp.get("take_profit_price") or t.take_profit_price
            rows.append({
                "time":      t.entry_time,
                "type":      "OPEN",
                "symbol":    t.symbol,
                "broker":    t.broker,
                "direction": t.direction,
                "price":     t.entry_price,
                "qty":       round(t.quantity, 6),
                "stop":      round(stop_val, 5) if stop_val else None,
                "target":    round(target_val, 5) if target_val else None,
                "pnl_usd":   None,
                "reason":    "entry",
                "tier":      t.position_tier,
                "confidence": round(t.confidence, 1) if t.confidence else None,
                "trade_id":  t.id,
            })

        # 2. PARTIAL CLOSE row from EventLog
        for ev in partial_events:
            meta = ev.event_metadata or {}
            sym    = ev.symbol
            price  = meta.get("exit_price")
            pnl    = meta.get("pnl_usd")
            frac   = meta.get("fraction", 0.5)
            qty_closed = meta.get("close_qty")
            # Use db_trade_id from metadata for exact match; fall back to symbol match
            meta_trade_id = meta.get("db_trade_id")
            matched = trade_by_id.get(meta_trade_id) if meta_trade_id else None
            if matched is None:
                # Fallback: find the trade open at the time of this partial close
                matched = next(
                    (t for t in sorted(all_trades, key=lambda x: x.id, reverse=True)
                     if t.symbol == sym and t.entry_time <= ev.timestamp),
                    None,
                )
            # Direction from metadata (most reliable) or matched trade
            direction = meta.get("direction") or (matched.direction if matched else "")
            # Fallback for old records without close_qty: back-calculate from pnl and spread
            if qty_closed is None and pnl is not None and matched and price:
                spread = abs(matched.entry_price - price)
                if spread > 0:
                    qty_closed = round(abs(pnl) / spread, 6)

            rows.append({
                "time":      ev.timestamp,
                "type":      "PARTIAL CLOSE",
                "symbol":    sym,
                "broker":    matched.broker if matched else "",
                "direction": direction,
                "price":     price,
                "qty":       qty_closed,
                "stop":      None,
                "target":    None,
                "pnl_usd":   pnl,
                "reason":    f"partial ({int(frac*100)}%)",
                "tier":      matched.position_tier if matched else "",
                "confidence": None,
                "trade_id":  meta_trade_id or (matched.id if matched else None),
            })

        # 3. CLOSE row for every closed trade
        for t in all_trades:
            if t.status != "closed" or t.exit_time is None:
                continue
            rows.append({
                "time":      t.exit_time,
                "type":      "CLOSE",
                "symbol":    t.symbol,
                "broker":    t.broker,
                "direction": t.direction,
                "price":     t.exit_price,
                "qty":       t.remaining_quantity if t.remaining_quantity is not None else t.quantity,
                "stop":      None,
                "target":    None,
                "pnl_usd":   t.pnl_usd,
                "reason":    t.notes or "",
                "tier":      t.position_tier,
                "confidence": None,
                "trade_id":  t.id,
            })

        df_tx = pd.DataFrame(rows).sort_values("time", ascending=False).reset_index(drop=True)

        # ── Filters ────────────────────────────────────────────────────────
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            syms = ["All"] + sorted(df_tx["symbol"].dropna().unique().tolist())
            sym_f = st.selectbox("Symbol", syms, key="tx_sym")
        with fc2:
            types = ["All"] + sorted(df_tx["type"].unique().tolist())
            type_f = st.selectbox("Type", types, key="tx_type")
        with fc3:
            dirs = ["All", "long", "short"]
            dir_f = st.selectbox("Direction", dirs, key="tx_dir")

        if sym_f  != "All": df_tx = df_tx[df_tx["symbol"]    == sym_f]
        if type_f != "All": df_tx = df_tx[df_tx["type"]      == type_f]
        if dir_f  != "All": df_tx = df_tx[df_tx["direction"] == dir_f]

        # ── Summary metrics ────────────────────────────────────────────────
        n_open    = (df_tx["type"] == "OPEN").sum()
        n_partial = (df_tx["type"] == "PARTIAL CLOSE").sum()
        n_close   = (df_tx["type"] == "CLOSE").sum()
        realized  = df_tx.loc[df_tx["type"].isin(["CLOSE", "PARTIAL CLOSE"]), "pnl_usd"].sum()

        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Entries",       n_open)
        sm2.metric("Partial Closes", n_partial)
        sm3.metric("Full Closes",   n_close)
        sm4.metric("Realized P&L",  _fmt_usd(realized or 0.0),
                   delta=f"{realized:+.2f}" if realized else None)

        st.divider()

        # ── Color styling ──────────────────────────────────────────────────
        def _color_tx(row):
            styles = [""] * len(row)
            cols = list(row.index)

            def idx(name):
                return cols.index(name) if name in cols else None

            # type column
            i = idx("type")
            if i is not None:
                if row["type"] == "OPEN":
                    styles[i] = "color: #0d6efd; font-weight: bold"
                elif row["type"] == "PARTIAL CLOSE":
                    styles[i] = "color: #fd7e14; font-weight: bold"
                elif row["type"] == "CLOSE":
                    styles[i] = "color: #6c757d; font-weight: bold"

            # pnl_usd
            i = idx("pnl_usd")
            if i is not None and row["pnl_usd"] is not None:
                try:
                    v = float(row["pnl_usd"])
                    styles[i] = (
                        "color: #155724; font-weight: bold" if v > 0
                        else "color: #721c24; font-weight: bold" if v < 0
                        else ""
                    )
                except (TypeError, ValueError):
                    pass

            return styles

        styled_tx = df_tx.style.apply(_color_tx, axis=1)
        st.dataframe(styled_tx, width="stretch")

        # ── Cumulative realized P&L chart ─────────────────────────────────
        closes_df = (
            df_tx[df_tx["type"].isin(["CLOSE", "PARTIAL CLOSE"]) & df_tx["pnl_usd"].notna()]
            .sort_values("time")
        )
        if not closes_df.empty:
            st.subheader("Cumulative Realized P&L")
            cumulative = closes_df["pnl_usd"].cumsum().reset_index(drop=True)
            st.line_chart(cumulative)


# ── Auto-refresh ──────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(30)
    st.rerun()
