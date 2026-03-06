"""
dashboard.py
Streamlit multi-page monitoring dashboard for the trade bot.

Run: streamlit run dashboard.py
Or:  python main.py --mode dashboard
"""
from __future__ import annotations

import time
from datetime import datetime, UTC

import pandas as pd
import streamlit as st

from config.settings import settings
from database.models import get_session, Trade, SignalLog, PortfolioSnapshot, StrategyRegistry, OptimizationCycle
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


def fetch_recent_signals(limit: int = 20) -> list[SignalLog]:
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

    daily_pnl = snap.daily_pnl if snap and snap.daily_pnl is not None else 0.0
    open_pos_count = len(open_positions)

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("Total Capital", _fmt_usd(total_capital))
    with m2:
        st.metric("Daily P&L", _fmt_usd(daily_pnl), delta=f"{daily_pnl:+.2f}")
    with m3:
        st.metric("Open Positions", open_pos_count)
    with m4:
        try:
            from portfolio.pdt_tracker import PDTTracker
            pdt = PDTTracker()
            pdt_used = pdt.count_day_trades_rolling()
            pdt_limit = pdt.PDT_LIMIT
        except Exception:
            pdt_used, pdt_limit = 0, 3
        st.metric("PDT Used", f"{pdt_used}/{pdt_limit}", delta=None)

    st.divider()

    # Equity curve from snapshots
    snapshots = fetch_all_snapshots()
    if snapshots:
        st.subheader("Equity Curve")
        eq_df = pd.DataFrame([
            {"timestamp": s.timestamp, "equity": (s.total_equity or total_capital)}
            for s in snapshots
        ]).set_index("timestamp")
        st.line_chart(eq_df["equity"])
    else:
        st.info("No equity history yet — equity curve will appear after the first snapshot.")

    st.divider()
    st.subheader("Open Positions")
    if open_positions:
        open_data = [
            {
                "symbol":        t.symbol,
                "direction":     t.direction,
                "position_tier": t.position_tier,
                "quantity":      t.quantity,
                "entry_price":   t.entry_price,
                "entry_time":    t.entry_time,
            }
            for t in open_positions
        ]
        st.dataframe(pd.DataFrame(open_data), width="stretch")
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

    # Capital allocation bar
    st.subheader("Capital Allocation")
    open_positions = fetch_open_positions()
    deployed = sum(
        (t.quantity or 0) * (t.entry_price or 0) for t in open_positions
    )
    free = max(0.0, settings.bot.total_capital - deployed)
    alloc_df = pd.DataFrame({
        "Segment": ["Deployed", "Free (deployable)", "Cash reserve"],
        "USD": [deployed, max(0, free - settings.bot.cash_reserve), settings.bot.cash_reserve],
    }).set_index("Segment")
    st.bar_chart(alloc_df)

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
    st.caption("Most recent signal evaluation per instrument (last 20 signals)")

    recent_signals = fetch_recent_signals(20)

    if recent_signals:
        signal_data = [
            {
                "timestamp":      s.timestamp,
                "symbol":         s.symbol,
                "direction":      s.direction,
                "dominant_score": s.dominant_score,
                "position_tier":  s.position_tier,
                "regime":         s.regime,
            }
            for s in recent_signals
        ]
        df_signals = pd.DataFrame(signal_data)

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

        styled = df_signals.style.map(_color_score, subset=["dominant_score"])
        st.dataframe(styled, width="stretch")

        # Score distribution chart
        st.subheader("Score Distribution")
        score_series = df_signals["dominant_score"].dropna()
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

    closed_trades = fetch_closed_trades(50)

    if closed_trades:
        closed_data = [
            {
                "symbol":      t.symbol,
                "direction":   t.direction,
                "entry_price": t.entry_price,
                "exit_price":  t.exit_price,
                "pnl":         t.pnl_usd,
                "exit_reason": t.notes,
                "exit_time":   t.exit_time,
            }
            for t in closed_trades
        ]
        df_closed = pd.DataFrame(closed_data)

        # Filter controls
        col_f1, col_f2 = st.columns(2)
        with col_f1:
            symbols = ["All"] + sorted(df_closed["symbol"].unique().tolist())
            sym_filter = st.selectbox("Filter by symbol", symbols)
        with col_f2:
            dir_filter = st.selectbox("Filter by direction", ["All", "long", "short"])

        if sym_filter != "All":
            df_closed = df_closed[df_closed["symbol"] == sym_filter]
        if dir_filter != "All":
            df_closed = df_closed[df_closed["direction"] == dir_filter]

        def _color_pnl(val):
            try:
                v = float(val)
                if v > 0:
                    return "color: #155724; font-weight: bold"
                elif v < 0:
                    return "color: #721c24; font-weight: bold"
            except (TypeError, ValueError):
                pass
            return ""

        styled_closed = df_closed.style.map(_color_pnl, subset=["pnl"])
        st.dataframe(styled_closed, width="stretch")

        # Summary metrics
        st.divider()
        total_pnl = df_closed["pnl"].sum()
        wins = (df_closed["pnl"] > 0).sum()
        win_rate = wins / len(df_closed) * 100 if len(df_closed) > 0 else 0
        sm1, sm2, sm3, sm4 = st.columns(4)
        sm1.metric("Total Trades", len(df_closed))
        sm2.metric("Win Rate", f"{win_rate:.1f}%")
        sm3.metric("Total P&L", _fmt_usd(total_pnl))
        sm4.metric("Avg P&L/trade", _fmt_usd(total_pnl / len(df_closed)) if df_closed is not None and len(df_closed) > 0 else "$0.00")

        # Cumulative equity curve from trades
        st.subheader("Cumulative P&L from Closed Trades")
        pnl_series = df_closed.sort_values("exit_time")["pnl"].fillna(0)
        if not pnl_series.empty:
            cumulative = pnl_series.cumsum().reset_index(drop=True)
            st.line_chart(cumulative)

        # Drawdown
        st.subheader("Drawdown from Closed Trades")
        cumsum = pnl_series.cumsum()
        roll_max = cumsum.cummax()
        drawdown = cumsum - roll_max
        st.area_chart(drawdown.reset_index(drop=True))

    else:
        st.info("No closed trades yet.")


# ══════════════════════════════════════════════════════════════════════════════
# Page 5 — Cost Analysis
# ══════════════════════════════════════════════════════════════════════════════

elif page.startswith("5"):
    st.title("Cost Analysis")
    st.caption("Gross P&L vs net P&L after estimated fees and slippage.")

    closed_trades = fetch_closed_trades(200)

    if closed_trades:
        STOCK_FEE  = 0.001   # 0.1% round-trip
        FOREX_FEE  = 0.0003  # 0.03% round-trip
        SLIPPAGE   = 0.0005  # 0.05% per trade (conservative)

        rows = []
        for t in closed_trades:
            if t.entry_price is None or t.exit_price is None or t.quantity is None:
                continue
            gross = t.pnl_usd or 0.0
            position_usd = t.entry_price * t.quantity
            is_forex = getattr(t, "broker", "ibkr") == "oanda"
            fee_rate = FOREX_FEE if is_forex else STOCK_FEE
            cost = position_usd * (fee_rate + SLIPPAGE)
            net = gross - cost
            rows.append({
                "symbol":       t.symbol,
                "gross_pnl":    gross,
                "estimated_cost": cost,
                "net_pnl":      net,
                "exit_time":    t.exit_time,
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


# ── Auto-refresh ──────────────────────────────────────────────────────────────

if auto_refresh:
    time.sleep(30)
    st.rerun()
