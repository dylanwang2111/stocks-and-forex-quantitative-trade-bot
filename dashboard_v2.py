"""
dashboard_v2.py
FastAPI-based trading dashboard — serves static SPA + JSON API endpoints.

Run:  uvicorn dashboard_v2:app --host 0.0.0.0 --port 8050 --reload
Or:   python main.py --mode dashboard_v2
"""
from __future__ import annotations

import json
import logging
import math
import os
import secrets
import socket
import time

logger = logging.getLogger(__name__)
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from config.settings import settings
from database.models import (
    EventLog,
    OptimizationCycle,
    PortfolioSnapshot,
    SignalLog,
    StrategyRegistry,
    Trade,
)

ROOT = Path(__file__).resolve().parent

# ── Auth ───────────────────────────────────────────────────────────────────────
_DASH_USER = os.getenv("DASHBOARD_USERNAME", "")
_DASH_PASS = os.getenv("DASHBOARD_PASSWORD", "")
_AUTH_ENABLED = bool(_DASH_USER and _DASH_PASS)
if not _AUTH_ENABLED:
    logger.warning("DASHBOARD_USERNAME/PASSWORD not set — dashboard is unauthenticated")

_http_basic = HTTPBasic(auto_error=False)


def _require_auth(credentials: Optional[HTTPBasicCredentials] = Depends(_http_basic)):
    if not _AUTH_ENABLED:
        return
    if credentials is None:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), _DASH_USER.encode("utf-8")
    ) and secrets.compare_digest(
        credentials.password.encode("utf-8"), _DASH_PASS.encode("utf-8")
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )


# ── DB engine ──────────────────────────────────────────────────────────────────
_db_url = settings.bot.database_url
if _db_url.startswith("sqlite:///") and not _db_url.startswith("sqlite:////"):
    _db_file = ROOT / _db_url[len("sqlite:///"):]
    _db_url = f"sqlite:///{_db_file}"

engine = create_engine(
    _db_url,
    connect_args={"check_same_thread": False, "timeout": 10},
)
with engine.connect() as _c:
    _c.execute(text("PRAGMA journal_mode=WAL"))


@contextmanager
def get_db():
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


# ── App ────────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Trade Bot Dashboard v2",
    docs_url=None,
    redoc_url=None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[],          # block all cross-origin requests
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Authorization", "Content-Type"],
)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


# ── Broker account sync ────────────────────────────────────────────────────────

_broker_cache: dict = {"data": None, "ts": 0.0}
_BROKER_CACHE_TTL = 60.0  # seconds


def _fetch_oanda_live() -> dict | None:
    """Fetch live account summary from OANDA REST API."""
    try:
        import oandapyV20
        import oandapyV20.endpoints.accounts as oanda_accounts
        api_key    = settings.oanda.api_key
        account_id = settings.oanda.account_id
        env        = getattr(settings.oanda, "environment", "practice")
        if not api_key or not account_id:
            return None
        client = oandapyV20.API(access_token=api_key, environment=env)
        r = oanda_accounts.AccountSummary(account_id)
        client.request(r)
        acct = r.response.get("account", {})
        return {
            "balance":        round(float(acct.get("balance",       0)), 2),
            "nav":            round(float(acct.get("NAV",           0)), 2),
            "unrealized_pnl": round(float(acct.get("unrealizedPL",  0)), 2),
            "realized_pnl":   round(float(acct.get("pl",            0)), 2),
            "open_trades":    int(acct.get("openTradeCount",  0)),
            "open_positions": int(acct.get("openPositionCount", 0)),
            "currency":       acct.get("currency", "USD"),
            "status":         "live",
        }
    except Exception as exc:
        logger.debug("OANDA account sync failed: %s", exc)
        return {"status": "offline", "error": str(exc)}


def _fetch_ibkr_live() -> dict | None:
    """Fetch live account values from IBKR via ib_insync."""
    try:
        from ib_insync import IB, util
        util.logToConsole(logging.CRITICAL)  # suppress ib_insync noise
        host      = settings.ibkr.host
        port      = settings.ibkr.port
        # Use a dedicated dashboard client ID — never conflicts with the bot
        dash_cid  = int(os.getenv("IBKR_DASH_CLIENT_ID", "99"))
        ib = IB()
        ib.connect(host, port, clientId=dash_cid, timeout=8, readonly=True)
        vals = {v.tag: float(v.value) for v in ib.accountValues()
                if v.currency in ("USD", "BASE") and v.value not in ("", "N/A")}
        ib.disconnect()
        return {
            "balance":        round(vals.get("TotalCashValue",     0), 2),
            "nav":            round(vals.get("NetLiquidation",     0), 2),
            "unrealized_pnl": round(vals.get("UnrealizedPnL",      0), 2),
            "realized_pnl":   round(vals.get("RealizedPnL",        0), 2),
            "gross_pnl":      round(vals.get("GrossPositionValue", 0), 2),
            "status":         "live",
        }
    except Exception as exc:
        logger.warning("IBKR account sync failed: %s", exc)
        return {"status": "offline", "error": str(exc)}


def _broker_sync(force: bool = False) -> dict:
    """Return cached broker account data; refresh if stale or forced."""
    now = time.time()
    if not force and _broker_cache["data"] and (now - _broker_cache["ts"]) < _BROKER_CACHE_TTL:
        return _broker_cache["data"]
    data = {
        "oanda":        _fetch_oanda_live(),
        "ibkr":         _fetch_ibkr_live(),
        "synced_at":    datetime.utcnow().isoformat() + "Z",
        "ttl":          _BROKER_CACHE_TTL,
        "trading_mode": settings.bot.trading_mode,
    }
    _broker_cache["data"] = data
    _broker_cache["ts"]   = now
    return data


# ── Helpers ────────────────────────────────────────────────────────────────────

def _parse_meta(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return raw

# ── Fee constants & cost helpers ───────────────────────────────────────────────
STOCK_FEE  = 0.0005   # ~0.05% round-trip: IBKR ~$0.005/share + exchange/reg fees
FOREX_FEE  = 0.00008  # ~0.008% round-trip: OANDA spread (EURUSD ~0.6 pip, USDJPY ~0.8 pip)
CRYPTO_FEE = 0.0005   # ~0.05% round-trip: exchange maker/taker spread
SLIPPAGE   = 0.0002   # ~0.02%: conservative market impact / price improvement
_FOREX_SYMS  = {"EURUSD","GBPUSD","USDJPY","AUDUSD","USDCAD","USDCHF","NZDUSD"}
_CRYPTO_SYMS = {"BTCUSD","ETHUSD"}

def _pos_usd(symbol: str, price: float, qty: float) -> float:
    """Return position value in USD, correcting for USD-base forex pairs."""
    if len(symbol) == 6 and symbol.upper().startswith("USD"):
        return qty
    return price * qty

def _fee_rate(symbol: str, broker: str | None) -> float:
    if symbol in _CRYPTO_SYMS:
        return CRYPTO_FEE
    if symbol in _FOREX_SYMS or (broker or "ibkr") == "oanda":
        return FOREX_FEE
    return STOCK_FEE


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_equity_curve(total_capital: float, db: Session) -> list[dict]:
    closed = db.query(Trade).filter(
        Trade.status == "closed", Trade.exit_time.isnot(None)
    ).all()
    partials = db.query(EventLog).filter(
        EventLog.event_type == "partial_close"
    ).all()

    events = []
    for t in closed:
        events.append({"time": t.exit_time, "pnl": t.pnl_usd or 0.0})
    for ev in partials:
        m = _parse_meta(ev.event_metadata)
        events.append({"time": ev.timestamp, "pnl": m.get("pnl_usd", 0.0)})

    if not events:
        return []

    events.sort(key=lambda e: e["time"])
    # Prepend baseline point
    start_pt = {
        "time": events[0]["time"] - timedelta(minutes=1),
        "pnl": 0.0,
    }
    events = [start_pt] + events

    cumsum = 0.0
    curve = []
    for e in events:
        cumsum += e["pnl"]
        curve.append({"t": _iso(e["time"]), "v": round(total_capital + cumsum, 2)})

    # Downsample to 300 points max
    if len(curve) > 300:
        step = len(curve) // 300
        curve = [curve[i] for i in range(0, len(curve), step)]

    # Append a live "now" point based on realized P&L only
    curve.append({"t": _iso(datetime.utcnow()), "v": round(total_capital + cumsum, 2)})

    return curve


def _get_stop_tp_map(db: Session) -> dict[str, dict]:
    result: dict[str, dict] = {}
    snap = (
        db.query(PortfolioSnapshot)
        .order_by(PortfolioSnapshot.timestamp.desc())
        .first()
    )
    if snap and snap.positions_detail:
        detail = snap.positions_detail
        if isinstance(detail, str):
            detail = json.loads(detail)
        result = {d["symbol"]: d for d in detail if "symbol" in d}

    open_trades = db.query(Trade).filter(Trade.status == "open").all()
    for t in open_trades:
        if t.symbol not in result and (t.stop_price or t.take_profit_price):
            result[t.symbol] = {
                "stop_price": t.stop_price,
                "take_profit_price": t.take_profit_price,
            }
    return result


def _calc_pnl_totals(db: Session, total_capital: float) -> dict:
    today_naive = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    all_closed = db.query(Trade).filter(Trade.status == "closed").all()
    all_partials = db.query(EventLog).filter(EventLog.event_type == "partial_close").all()

    total_realized = 0.0
    daily_pnl = 0.0
    wins = 0
    n_closed = len(all_closed)
    realized_by_broker: dict[str, float] = {}

    for t in all_closed:
        pnl = t.pnl_usd or 0.0
        total_realized += pnl
        broker = (t.broker or "ibkr").lower()
        realized_by_broker[broker] = realized_by_broker.get(broker, 0.0) + pnl
        if t.exit_time and t.exit_time >= today_naive:
            daily_pnl += pnl
        if pnl > 0:
            wins += 1

    for ev in all_partials:
        m = _parse_meta(ev.event_metadata)
        pnl = m.get("pnl_usd", 0.0)
        total_realized += pnl
        broker = (m.get("broker") or "ibkr").lower()
        realized_by_broker[broker] = realized_by_broker.get(broker, 0.0) + pnl
        if ev.timestamp and ev.timestamp >= today_naive:
            daily_pnl += pnl

    win_rate = (wins / n_closed * 100) if n_closed > 0 else 0.0
    return {
        "total_realized": round(total_realized, 4),
        "daily_pnl": round(daily_pnl, 4),
        "win_rate": round(win_rate, 1),
        "n_closed": n_closed,
        "wins": wins,
        "realized_by_broker": realized_by_broker,
    }


# ── API: /api/overview ─────────────────────────────────────────────────────────

@app.get("/api/broker-sync")
def api_broker_sync(force: bool = False):
    """Live broker account data from OANDA + IBKR. Cached for 60 s."""
    try:
        return _broker_sync(force=force)
    except Exception as exc:
        logger.exception("broker-sync error")
        return JSONResponse({"error": "Internal server error"}, status_code=500)


@app.get("/api/overview")
def api_overview():
    try:
        total_capital = settings.bot.total_capital
        ibkr_cap  = float(os.getenv("IBKR_CAPITAL",  "0") or 0) or total_capital
        oanda_cap = float(os.getenv("OANDA_CAPITAL", "0") or 0) or total_capital
        reserve_pct = float(os.getenv("CASH_RESERVE_PCT", "0"))

        with get_db() as db:
            snap = (
                db.query(PortfolioSnapshot)
                .order_by(PortfolioSnapshot.timestamp.desc())
                .first()
            )
            open_trades    = db.query(Trade).filter(Trade.status == "open").all()
            closed_trades  = db.query(Trade).filter(Trade.status == "closed").all()
            partial_evts   = db.query(EventLog).filter(EventLog.event_type == "partial_close").all()
            pnl = _calc_pnl_totals(db, total_capital)

        costs_by_broker = _calc_costs_by_broker(closed_trades, partial_evts, open_trades)

        snap_age = None
        last_snap_time = None
        bot_running = False
        drawdown_pct = None
        if snap:
            last_snap_time = _iso(snap.timestamp)
            snap_age = (datetime.utcnow() - snap.timestamp).total_seconds()
            drawdown_pct = snap.drawdown_pct
        # Check if main.py process is actually running
        try:
            _pidfile = Path(__file__).parent / "logs" / "bot.pid"
            _pid = int(_pidfile.read_text().strip())
            import os as _os
            _os.kill(_pid, 0)
            bot_running = True
        except Exception:
            bot_running = snap_age is not None and snap_age < 5400

        # Unrealized P&L — fetch live prices (snapshot never stores current_price)
        from data.fetcher import fetch_candles as _fetch_candles
        total_unrealized = 0.0
        for t in open_trades:
            if not t.entry_price or not t.quantity:
                continue
            # Use remaining_quantity after partial closes; fall back to original quantity
            qty = t.remaining_quantity if t.remaining_quantity is not None else t.quantity
            cp: Optional[float] = None
            try:
                df = _fetch_candles(t.symbol, "1h")
                if df is not None and not df.empty:
                    cp = float(df["close"].iloc[-1])
            except Exception:
                pass
            if cp:
                # For USD-base forex (USDJPY, USDCHF, USDCAD), 1 OANDA unit = 1 USD base.
                # P&L in USD = (price_diff / current_price) × qty.
                _usd_base = len(t.symbol) == 6 and t.symbol.upper().startswith("USD")
                if t.direction == "long":
                    total_unrealized += ((cp - t.entry_price) / cp * qty if _usd_base else (cp - t.entry_price) * qty)
                else:
                    total_unrealized += ((t.entry_price - cp) / cp * qty if _usd_base else (t.entry_price - cp) * qty)
        total_unrealized = round(total_unrealized, 2)

        with get_db() as db2:
            curve = _build_equity_curve(total_capital, db2)

        # Capital allocation — use remaining_quantity (post-partial-close) for deployed
        def _eff_qty(t: Trade) -> float:
            return t.remaining_quantity if t.remaining_quantity is not None else (t.quantity or 0)

        ibkr_pos  = [t for t in open_trades if (t.broker or "ibkr") == "ibkr"]
        oanda_pos = [t for t in open_trades if (t.broker or "ibkr") == "oanda"]
        def _deployed_usd(t: Trade) -> float:
            # For USD-base forex pairs (USDJPY, USDCAD, USDCHF): 1 OANDA unit = 1 USD
            # → deployed = quantity, NOT quantity * price (which gives inflated notional)
            sym = (t.symbol or "").upper()
            if (t.broker or "ibkr") == "oanda" and sym.startswith("USD"):
                return _eff_qty(t)
            return _eff_qty(t) * (t.entry_price or 0)

        ibkr_dep  = sum(_deployed_usd(t) for t in ibkr_pos)
        oanda_dep = sum(_deployed_usd(t) for t in oanda_pos)
        ibkr_res  = ibkr_cap  * reserve_pct
        oanda_res = oanda_cap * reserve_pct
        total_dep = ibkr_dep + oanda_dep
        realized  = pnl["total_realized"]
        realized_by_broker = pnl.get("realized_by_broker", {})
        ibkr_realized  = realized_by_broker.get("ibkr",  0.0)
        oanda_realized = realized_by_broker.get("oanda", 0.0)
        ibkr_costs  = costs_by_broker.get("ibkr",  0.0)
        oanda_costs = costs_by_broker.get("oanda", 0.0)
        ibkr_pool  = ibkr_cap  + ibkr_realized  - ibkr_costs
        oanda_pool = oanda_cap + oanda_realized - oanda_costs

        # ── Overlay with live broker data when available ───────────────────────
        sync = _broker_sync()
        oanda_live = sync.get("oanda") or {}
        ibkr_live  = sync.get("ibkr")  or {}
        broker_sync_status = {
            "oanda": oanda_live.get("status", "offline"),
            "ibkr":  ibkr_live.get("status",  "offline"),
            "synced_at": sync.get("synced_at"),
        }

        # In live trading mode, broker NAV is authoritative (real money balance).
        # In paper mode, the practice account has an artificial default balance
        # (e.g. OANDA practice = $100,000), so we keep the configured capital.
        if settings.bot.trading_mode == "live":
            if oanda_live.get("status") == "live":
                oanda_pool    = oanda_live["nav"]
                oanda_cap     = oanda_live["balance"]
                oanda_realized = oanda_live["realized_pnl"]
            if ibkr_live.get("status") == "live":
                ibkr_pool    = ibkr_live["nav"]
                ibkr_cap     = ibkr_live["balance"]
                ibkr_realized = ibkr_live["realized_pnl"]

        # Recompute totals with broker-sourced values
        realized = ibkr_realized + oanda_realized
        ibkr_res  = ibkr_cap  * reserve_pct
        oanda_res = oanda_cap * reserve_pct

        # PDT
        try:
            from portfolio.pdt_tracker import PDTTracker
            pdt = PDTTracker()
            pdt_used = pdt.count_day_trades_rolling()
            pdt_limit = pdt.PDT_LIMIT
        except Exception:
            pdt_used, pdt_limit = 0, 3

        # Use broker unrealized P&L when both brokers are live (more accurate)
        oanda_unreal = oanda_live.get("unrealized_pnl") if oanda_live.get("status") == "live" else None
        ibkr_unreal  = ibkr_live.get("unrealized_pnl")  if ibkr_live.get("status")  == "live" else None
        if oanda_unreal is not None or ibkr_unreal is not None:
            total_unrealized = round((oanda_unreal or 0) + (ibkr_unreal or 0), 2)

        return {
            "trading_mode": settings.bot.trading_mode,
            "bot_running": bot_running,
            "total_capital": total_capital,
            # Mark-to-market equity: pool (capital + realized − costs) + open unrealized
            "current_equity": round(ibkr_pool + oanda_pool + total_unrealized, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": total_unrealized,
            "broker_sync": broker_sync_status,
            "daily_pnl": pnl["daily_pnl"],
            "win_rate": pnl["win_rate"],
            "open_positions": len(open_trades),
            "drawdown_pct": drawdown_pct,
            "pdt_used": pdt_used,
            "pdt_limit": pdt_limit,
            "last_snapshot_time": last_snap_time,
            "equity_curve": curve,
            "capital_allocation": {
                "ibkr": {
                    "pool": round(ibkr_pool, 2),
                    "reserve": round(ibkr_res, 2),
                    "deployed": round(ibkr_dep, 2),
                    "available": round(max(0.0, ibkr_pool - ibkr_res - ibkr_dep), 2),
                    "utilization_pct": round(ibkr_dep / ibkr_pool * 100, 1) if ibkr_pool else 0,
                    "positions": len(ibkr_pos),
                },
                "oanda": {
                    "pool": round(oanda_pool, 2),
                    "reserve": round(oanda_res, 2),
                    "deployed": round(oanda_dep, 2),
                    "available": round(max(0.0, oanda_pool - oanda_res - oanda_dep), 2),
                    "utilization_pct": round(oanda_dep / oanda_pool * 100, 1) if oanda_pool else 0,
                    "positions": len(oanda_pos),
                },
                "total": {
                    "pool": round(ibkr_pool + oanda_pool, 2),
                    "deployed": round(total_dep, 2),
                    "available": round(max(0.0, ibkr_pool + oanda_pool - total_dep), 2),
                    "utilization_pct": round(total_dep / (ibkr_pool + oanda_pool) * 100, 1) if (ibkr_pool + oanda_pool) else 0,
                    "positions": len(open_trades),
                },
            },
        }
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── API: /api/positions ────────────────────────────────────────────────────────

@app.get("/api/positions")
def api_positions():
    try:
        swing_days = settings.bot.swing_holding_days
        with get_db() as db:
            open_trades = db.query(Trade).filter(Trade.status == "open").all()
            stop_tp_map = _get_stop_tp_map(db)
            closed_syms = {
                t.symbol for t in db.query(Trade).filter(Trade.status.in_(["closed", "cancelled"])).all()
                if t.symbol not in {t2.symbol for t2 in open_trades}
            }

        rows = []
        now = datetime.utcnow()
        open_syms = {t.symbol for t in open_trades}

        def _build_position_row(
            symbol: str,
            direction: str,
            entry_price: float | None,
            quantity: float | None,
            broker: str | None,
            confidence: float | None,
            position_tier: str | None,
            entry_time,
            trade_id: int | None,
            detail: dict,
            stop_override=None,
            tp_override=None,
        ) -> dict:
            stop = stop_override or detail.get("stop_price")
            tp   = tp_override   or detail.get("take_profit_price")
            partial_done  = detail.get("partial_exit_done", False)
            current_price = detail.get("current_price")

            unrealized = None
            if current_price and entry_price and quantity:
                _usd_base = len(symbol) == 6 and symbol.upper().startswith("USD")
                if direction == "long":
                    diff = (current_price - entry_price) / current_price if _usd_base else (current_price - entry_price)
                else:
                    diff = (entry_price - current_price) / current_price if _usd_base else (entry_price - current_price)
                unrealized = round(diff * quantity, 2)

            tp_progress = None
            if current_price and tp and entry_price:
                total_range = abs(tp - entry_price)
                if total_range > 0:
                    moved = (
                        (current_price - entry_price)
                        if direction == "long"
                        else (entry_price - current_price)
                    )
                    tp_progress = round(moved / total_range * 100, 1)

            at_tp_now = False
            if current_price and tp:
                if direction == "long" and current_price >= tp:
                    at_tp_now = True
                elif direction == "short" and current_price <= tp:
                    at_tp_now = True

            tp_breach_streak = detail.get("tp_breach_streak", 0)
            if partial_done:
                phase = "2-trailing"
            elif tp_breach_streak >= 3 or at_tp_now:
                phase = "2-past-tp"
            else:
                phase = "1"

            dist_stop = dist_tp = None
            if stop and current_price:
                if direction == "long":
                    dist_stop = round((current_price - stop) / current_price * 100, 2)
                else:
                    dist_stop = round((stop - current_price) / current_price * 100, 2)
            if tp and current_price:
                if direction == "long":
                    dist_tp = round((tp - current_price) / current_price * 100, 2)
                else:
                    dist_tp = round((current_price - tp) / current_price * 100, 2)

            if entry_time and isinstance(entry_time, str):
                try:
                    entry_time = datetime.fromisoformat(entry_time.replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    entry_time = None

            if entry_time:
                from datetime import timedelta
                from portfolio.watchlist import is_crypto_symbol
                entry_date = entry_time.date()
                now_date   = now.date()
                if is_crypto_symbol(symbol):
                    days_held = (now_date - entry_date).days
                else:
                    days_held = sum(
                        1 for i in range((now_date - entry_date).days)
                        if (entry_date + timedelta(days=i + 1)).weekday() < 5
                    )
            else:
                days_held = None
            days_left = (swing_days - days_held) if days_held is not None else None

            return {
                "trade_id":          trade_id,
                "symbol":            symbol,
                "broker":            broker,
                "direction":         direction,
                "phase":             phase,
                "partial_done":      partial_done,
                "entry_price":       entry_price,
                "current_price":     current_price,
                "stop_price":        round(stop, 4) if stop else None,
                "take_profit_price": round(tp, 4) if tp else None,
                "dist_stop_pct":     dist_stop,
                "dist_tp_pct":       dist_tp,
                "tp_progress_pct":   tp_progress,
                "unrealized_pnl":    unrealized,
                "quantity":          quantity,
                "size_usd":          round(quantity, 2) if (broker or "ibkr") == "oanda" and (symbol or "").upper().startswith("USD") else round((quantity or 0) * (entry_price or 0), 2),
                "days_held":         days_held,
                "days_left":         days_left,
                "confidence":        round(confidence, 1) if confidence else None,
                "position_tier":     position_tier,
                "entry_time":        _iso(entry_time) if entry_time and not isinstance(entry_time, str) else entry_time,
            }

        for t in open_trades:
            detail = stop_tp_map.get(t.symbol, {})
            # Use remaining_quantity after partial closes; fall back to original quantity
            effective_qty = t.remaining_quantity if t.remaining_quantity is not None else t.quantity
            rows.append(_build_position_row(
                symbol=t.symbol,
                direction=t.direction,
                entry_price=t.entry_price,
                quantity=effective_qty,
                broker=t.broker,
                confidence=t.confidence,
                position_tier=t.position_tier,
                entry_time=t.entry_time,
                trade_id=t.id,
                detail=detail,
                stop_override=t.stop_price,
                tp_override=t.take_profit_price,
            ))

        # Fallback: include positions from the snapshot that have no open Trade record.
        # This handles cases where in-memory state (snapshot) is ahead of the DB
        # (e.g. a position was opened but the DB status update was delayed).
        for sym, detail in stop_tp_map.items():
            if sym in open_syms:
                continue  # already covered by Trade table
            if sym in closed_syms:
                continue  # closed in DB — don't show from stale snapshot
            if not detail.get("direction") or not detail.get("entry_price"):
                continue
            rows.append(_build_position_row(
                symbol=sym,
                direction=detail.get("direction", "long"),
                entry_price=detail.get("entry_price"),
                quantity=detail.get("quantity"),
                broker=detail.get("broker"),
                confidence=detail.get("confidence"),
                position_tier=detail.get("position_tier"),
                entry_time=detail.get("entry_time"),
                trade_id=detail.get("db_trade_id"),
                detail=detail,
            ))

        # Live price fetch — same routing as dashboard.py: fetch_candles per symbol
        # (OANDA primary for forex/crypto, IBKR primary for stocks, yfinance fallback)
        from data.fetcher import fetch_candles as _fetch_candles
        rows_needing_price = [r for r in rows if r["current_price"] is None]
        for r in rows_needing_price:
            cp: Optional[float] = None
            try:
                df = _fetch_candles(r["symbol"], "1h")
                if df is not None and not df.empty:
                    cp = float(df["close"].iloc[-1])
            except Exception as exc:
                logger.warning("dashboard: price fetch failed for %s: %s", r["symbol"], exc)
            if cp is None:
                continue
            r["current_price"] = cp
            ep        = r["entry_price"]
            qty       = r["quantity"]
            direction = r["direction"]
            stop      = r["stop_price"]
            tp        = r["take_profit_price"]

            if ep and qty:
                _usd_base = len(r["symbol"]) == 6 and r["symbol"].upper().startswith("USD")
                if _usd_base:
                    diff = (cp - ep) / cp if direction == "long" else (ep - cp) / cp
                else:
                    diff = (cp - ep) if direction == "long" else (ep - cp)
                r["unrealized_pnl"] = round(diff * qty, 2)
            if stop:
                dist = (cp - stop) / cp if direction == "long" else (stop - cp) / cp
                r["dist_stop_pct"] = round(dist * 100, 2)
            if tp:
                dist = (tp - cp) / cp if direction == "long" else (cp - tp) / cp
                r["dist_tp_pct"] = round(dist * 100, 2)
            if tp and ep:
                total = abs(tp - ep)
                if total > 0:
                    moved = (cp - ep) if direction == "long" else (ep - cp)
                    r["tp_progress_pct"] = round(moved / total * 100, 1)
            # Update phase now that we have a real price
            at_tp = ((direction == "long" and cp >= tp) or
                     (direction == "short" and cp <= tp)) if tp else False
            if r.get("partial_done"):
                r["phase"] = "2-trailing"
            elif r.get("tp_breach_streak", 0) >= 6 or at_tp:
                r["phase"] = "2-past-tp"
            else:
                r["phase"] = "1"

        return rows
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── API: /api/signals ──────────────────────────────────────────────────────────

@app.get("/api/signals")
def api_signals(
    symbol: str = Query(""),
    tier: str = Query(""),
    tradeable_only: bool = Query(False),
    days: int = Query(5, ge=1, le=90),
):
    TIER_ORDER = {"SMALL": 1, "MEDIUM": 2, "LARGE": 3, "FULL": 4}
    TRADEABLE  = {"SMALL", "MEDIUM", "LARGE", "FULL"}
    try:
        cutoff = datetime.utcnow() - timedelta(days=days)
        with get_db() as db:
            rows = (
                db.query(SignalLog)
                .filter(SignalLog.timestamp >= cutoff)
                .order_by(SignalLog.timestamp.desc())
                .all()
            )

        signals = []
        for s in rows:
            tier_val = s.position_tier or ""
            if symbol and s.symbol != symbol:
                continue
            if tradeable_only and tier_val not in TRADEABLE:
                continue
            if tier:
                min_rank = TIER_ORDER.get(tier, 0)
                if TIER_ORDER.get(tier_val, 0) < min_rank:
                    continue
            signals.append({
                "id":         s.id,
                "timestamp":  _iso(s.timestamp),
                "symbol":     s.symbol,
                "direction":  s.direction,
                "score":      round(s.dominant_score, 1) if s.dominant_score else None,
                "bull_score": round(s.bull_score, 1) if s.bull_score else None,
                "bear_score": round(s.bear_score, 1) if s.bear_score else None,
                "tier":       tier_val,
                "regime":     s.regime,
                "macro_risk": s.macro_risk_level,
                "c1": s.cat1_trend,
                "c2": s.cat2_strength,
                "c3": s.cat3_momentum,
                "c4": s.cat4_volatility,
                "c5": s.cat5_volume,
                "c6": s.cat6_structure,
                "c7": s.cat7_mtf,
                "c8": s.cat8_macro,
            })

        all_syms = sorted({s.symbol for s in rows if s.symbol})
        return {"signals": signals, "total": len(signals), "symbols": all_syms}
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── API: /api/trades ───────────────────────────────────────────────────────────

@app.get("/api/trades")
def api_trades(
    symbol: str = Query(""),
    direction: str = Query(""),
    year: int = Query(0),
    month: int = Query(0),
    day: int = Query(0),
    hour: int = Query(-1),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=10000),
):
    from portfolio.watchlist import get_instrument as _get_inst
    def _asset_type(sym: str) -> str:
        try:
            return _get_inst(sym).asset_type
        except Exception:
            s = sym.upper()
            if s in ("BTCUSD", "ETHUSD", "XRPUSD"):
                return "crypto"
            if len(s) == 6 and s.isalpha():
                return "forex"
            return "stock"

    try:
        with get_db() as db:
            all_trades    = db.query(Trade).order_by(Trade.entry_time.desc()).all()
            closed_trades = [t for t in all_trades if t.status == "closed"]
            partial_evts  = (
                db.query(EventLog)
                .filter(EventLog.event_type == "partial_close")
                .order_by(EventLog.timestamp.desc())
                .all()
            )

        all_trades_by_id = {t.id: t for t in all_trades}

        # Build stop/tp map from snapshot for open positions
        with get_db() as db2:
            snap_map = _get_stop_tp_map(db2)

        rows = []
        # Entry row for every trade (open and closed) — mirrors old dashboard behaviour
        for t in all_trades:
            snap_detail = snap_map.get(t.symbol, {}) if t.status == "open" else {}
            stop = snap_detail.get("stop_price") or t.stop_price
            tp   = snap_detail.get("take_profit_price") or t.take_profit_price
            rows.append({
                "time": t.entry_time, "type": "OPEN", "symbol": t.symbol,
                "broker": t.broker, "direction": t.direction,
                "asset_type": _asset_type(t.symbol),
                "price": t.entry_price, "quantity": t.quantity,
                "pnl_usd": None, "pnl_pct": None, "reason": "entry",
                "tier": t.position_tier, "confidence": t.confidence,
                "trade_id": t.id, "regime": t.regime,
                "stop_price": round(stop, 4) if stop else None,
                "take_profit_price": round(tp, 4) if tp else None,
                "entry_time": _iso(t.entry_time), "exit_time": None,
            })

        # Partial closes
        for ev in partial_evts:
            meta = _parse_meta(ev.event_metadata)
            sym  = ev.symbol
            tid  = meta.get("db_trade_id")
            t    = all_trades_by_id.get(tid) if tid else None
            if t is None:
                t = next(
                    (tr for tr in sorted(all_trades, key=lambda x: x.id, reverse=True)
                     if tr.symbol == sym and tr.entry_time <= ev.timestamp),
                    None,
                )
            direction_val = meta.get("direction") or (t.direction if t else "")
            frac  = meta.get("fraction", 0.5)
            cqty  = meta.get("close_qty")
            price = meta.get("exit_price")
            pnl   = meta.get("pnl_usd")
            if cqty is None and pnl is not None and price and t:
                spread = abs(t.entry_price - price)
                cqty = round(abs(pnl) / spread, 6) if spread > 0 else None
            rows.append({
                "time": ev.timestamp, "type": f"PARTIAL {int(frac*100)}%",
                "symbol": sym, "broker": t.broker if t else "",
                "direction": direction_val,
                "asset_type": _asset_type(sym),
                "price": price, "quantity": cqty,
                "pnl_usd": pnl, "pnl_pct": None, "reason": "phase-2 partial",
                "tier": t.position_tier if t else "", "confidence": t.confidence if t else None,
                "trade_id": tid or (t.id if t else None), "regime": t.regime if t else None,
                "stop_price": None, "take_profit_price": None,
                "entry_time": _iso(t.entry_time) if t else None, "exit_time": _iso(ev.timestamp),
            })

        # Full closes
        for t in closed_trades:
            rows.append({
                "time": t.exit_time, "type": "CLOSE", "symbol": t.symbol,
                "broker": t.broker, "direction": t.direction,
                "asset_type": _asset_type(t.symbol),
                "price": t.exit_price, "quantity": t.remaining_quantity if t.remaining_quantity is not None else t.quantity,
                "pnl_usd": t.pnl_usd, "pnl_pct": t.pnl_pct, "reason": t.notes or "",
                "tier": t.position_tier, "confidence": t.confidence,
                "trade_id": t.id, "regime": t.regime,
                "stop_price": None, "take_profit_price": None,
                "entry_time": _iso(t.entry_time), "exit_time": _iso(t.exit_time),
            })

        # Filter
        if symbol:
            rows = [r for r in rows if r["symbol"] == symbol]
        if direction:
            rows = [r for r in rows if r["direction"] == direction]
        if year or month or day or hour >= 0:
            def _match_date(r):
                t = r["time"]
                if t is None:
                    return False
                if year and t.year != year:
                    return False
                if month and t.month != month:
                    return False
                if day and t.day != day:
                    return False
                if hour >= 0 and t.hour != hour:
                    return False
                return True
            rows = [r for r in rows if _match_date(r)]

        rows.sort(key=lambda r: r["time"] or datetime.min, reverse=True)

        # Serialize times
        for r in rows:
            r["time"] = _iso(r["time"])

        # Summary
        closed_pnl   = sum(r["pnl_usd"] or 0 for r in rows if r["type"] == "CLOSE")
        partial_pnl  = sum(r["pnl_usd"] or 0 for r in rows if "PARTIAL" in r["type"])
        close_rows   = [r for r in rows if r["type"] == "CLOSE"]
        wins         = sum(1 for r in close_rows if (r["pnl_usd"] or 0) > 0)
        win_rate     = wins / len(close_rows) * 100 if close_rows else 0

        # Paginate
        total = len(rows)
        pages = math.ceil(total / page_size) if page_size else 1
        start = (page - 1) * page_size
        rows  = rows[start: start + page_size]

        return {
            "rows": rows,
            "summary": {
                "realized_pnl":     round(closed_pnl + partial_pnl, 4),
                "full_closes":      len(close_rows),
                "partial_closes":   sum(1 for r in rows if "PARTIAL" in r["type"]),
                "win_rate":         round(win_rate, 1),
            },
            "page": page, "page_size": page_size, "total": total, "pages": pages,
        }
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── API: /api/costs ────────────────────────────────────────────────────────────

def _calc_costs_by_broker(closed_trades, partial_evts, open_trades) -> dict:
    """Return total estimated costs (fees + slippage) keyed by broker name."""
    costs: dict[str, float] = {}

    for t in closed_trades:
        if t.entry_price is None or t.quantity is None:
            continue
        broker = (t.broker or "ibkr").lower()
        # Use remaining_quantity (post-partial-close) so we don't double-count
        # the portion already charged in the partial_evts loop below.
        exit_qty = t.remaining_quantity if t.remaining_quantity is not None else t.quantity
        pos_usd = _pos_usd(t.symbol or "", t.entry_price, exit_qty)
        costs[broker] = costs.get(broker, 0.0) + pos_usd * (_fee_rate(t.symbol or "", t.broker) + SLIPPAGE)

    trade_by_sym = {t.symbol: t for t in closed_trades}
    for ev in partial_evts:
        meta  = _parse_meta(ev.event_metadata)
        sym   = ev.symbol or ""
        price = meta.get("exit_price", 0.0)
        qty   = meta.get("close_qty") or 0.0
        gross = meta.get("pnl_usd", 0.0)
        if not price:
            continue
        if not qty and gross and sym in trade_by_sym:
            spread = abs(trade_by_sym[sym].entry_price - price)
            if spread > 0:
                _usd_base = len(sym) == 6 and sym.upper().startswith("USD")
                qty = abs(gross) * price / spread if _usd_base else abs(gross) / spread
        if not qty:
            continue
        ref = trade_by_sym.get(sym)
        broker = ((ref.broker if ref else None) or "ibkr").lower()
        pos_usd = _pos_usd(sym, price, qty)
        costs[broker] = costs.get(broker, 0.0) + pos_usd * (_fee_rate(sym, broker) + SLIPPAGE)

    for t in open_trades:
        if t.entry_price is None or t.quantity is None:
            continue
        broker = (t.broker or "ibkr").lower()
        pos_usd = _pos_usd(t.symbol or "", t.entry_price, t.quantity)
        costs[broker] = costs.get(broker, 0.0) + pos_usd * (_fee_rate(t.symbol or "", t.broker) + SLIPPAGE)

    return costs

@app.get("/api/costs")
def api_costs():
    try:
        with get_db() as db:
            closed = db.query(Trade).filter(Trade.status == "closed").order_by(Trade.exit_time).all()
            open_trades = db.query(Trade).filter(Trade.status == "open").order_by(Trade.entry_time).all()
            partials = (
                db.query(EventLog)
                .filter(EventLog.event_type == "partial_close")
                .order_by(EventLog.timestamp)
                .all()
            )

        trade_by_sym = {t.symbol: t for t in closed}
        rows = []

        for t in closed:
            if t.entry_price is None or t.exit_price is None or t.quantity is None:
                continue
            gross    = t.pnl_usd or 0.0
            exit_qty = t.remaining_quantity if t.remaining_quantity is not None else t.quantity
            pos_usd  = _pos_usd(t.symbol or "", t.entry_price, exit_qty)
            cost     = pos_usd * (_fee_rate(t.symbol or "", t.broker) + SLIPPAGE)
            rows.append({
                "symbol": t.symbol, "type": "full close", "exit_time": _iso(t.exit_time),
                "gross_pnl": round(gross, 4), "estimated_cost": round(cost, 4),
                "net_pnl": round(gross - cost, 4),
            })

        for ev in partials:
            meta  = _parse_meta(ev.event_metadata)
            gross = meta.get("pnl_usd", 0.0)
            price = meta.get("exit_price", 0.0)
            qty   = meta.get("close_qty") or 0.0
            sym   = ev.symbol or ""
            if not price:
                continue
            if not qty and gross and sym in trade_by_sym:
                spread = abs(trade_by_sym[sym].entry_price - price)
                if spread > 0:
                    _usd_base = len(sym) == 6 and sym.upper().startswith("USD")
                    if _usd_base:
                        # pnl_usd = spread_in_quote / exit_price * qty  →  qty = pnl * price / spread
                        qty = abs(gross) * price / spread
                    else:
                        qty = abs(gross) / spread
            if not qty:
                continue
            broker   = trade_by_sym[sym].broker if sym in trade_by_sym else None
            pos_usd  = _pos_usd(sym, price, qty)
            cost     = pos_usd * (_fee_rate(sym, broker) + SLIPPAGE)
            rows.append({
                "symbol": sym, "type": "partial close", "exit_time": _iso(ev.timestamp),
                "gross_pnl": round(gross, 4), "estimated_cost": round(cost, 4),
                "net_pnl": round(gross - cost, 4),
            })

        # Open position entry costs (already paid, exit not yet incurred)
        open_rows = []
        for t in open_trades:
            if t.entry_price is None or t.quantity is None:
                continue
            pos_usd = _pos_usd(t.symbol or "", t.entry_price, t.quantity)
            cost    = pos_usd * (_fee_rate(t.symbol or "", t.broker) + SLIPPAGE)
            open_rows.append({
                "symbol": t.symbol, "type": "open entry", "exit_time": _iso(t.entry_time),
                "gross_pnl": 0.0, "estimated_cost": round(cost, 4),
                "net_pnl": round(-cost, 4),
            })

        total_gross     = sum(r["gross_pnl"] for r in rows)
        total_cost      = sum(r["estimated_cost"] for r in rows)
        open_entry_cost = sum(r["estimated_cost"] for r in open_rows)
        return {
            "summary": {
                "total_gross_pnl":    round(total_gross, 4),
                "total_cost":         round(total_cost, 4),
                "total_net_pnl":      round(total_gross - total_cost, 4),
                "open_entry_cost":    round(open_entry_cost, 4),
            },
            "rows": rows + open_rows,
        }
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── API: /api/run-optimizer ────────────────────────────────────────────────

@app.post("/api/run-optimizer")
def api_run_optimizer():
    try:
        from config.settings import settings as bot_settings
        from optimization.pipeline import OptimizationPipeline
        pipeline = OptimizationPipeline(database_url=bot_settings.bot.database_url)
        pipeline.run(require_human_approval=False)
        return {"status": "ok", "message": "Optimization complete."}
    except Exception as exc:
        logger.exception("run-optimizer failed")
        return JSONResponse({"status": "error", "message": "Internal server error"}, status_code=500)


# ── API: /api/optimization ─────────────────────────────────────────────────

@app.get("/api/optimization")
def api_optimization(
    strategy: str = Query(""),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=10000),
):
    try:
        with get_db() as db:
            q = db.query(OptimizationCycle).order_by(OptimizationCycle.started_at.desc())
            if strategy:
                q = q.filter(OptimizationCycle.strategy_name == strategy)
            all_rows = q.all()

        total = len(all_rows)
        pages = math.ceil(total / page_size) if page_size else 1
        start = (page - 1) * page_size
        rows  = all_rows[start: start + page_size]

        strategies_in_result = sorted({r.strategy_name for r in all_rows if r.strategy_name})

        return {
            "rows": [
                {
                    "id":               r.id,
                    "strategy_name":    r.strategy_name,
                    "started_at":       _iso(r.started_at),
                    "completed_at":     _iso(r.completed_at),
                    "in_sample_start":  _iso(r.in_sample_start),
                    "in_sample_end":    _iso(r.in_sample_end),
                    "oos_start":        _iso(r.oos_start),
                    "oos_end":          _iso(r.oos_end),
                    "in_sample_sharpe": round(r.in_sample_sharpe, 3) if r.in_sample_sharpe is not None else None,
                    "oos_sharpe":       round(r.oos_sharpe, 3) if r.oos_sharpe is not None else None,
                    "in_sample_trades": r.in_sample_trades,
                    "oos_trades":       r.oos_trades,
                    "params_before":    r.params_before,
                    "params_after":     r.params_after,
                    "accepted":         r.accepted,
                    "p_value":          round(r.p_value, 4) if r.p_value is not None else None,
                    "notes":            r.notes,
                }
                for r in rows
            ],
            "total":      total,
            "page":       page,
            "page_size":  page_size,
            "pages":      pages,
            "strategies": strategies_in_result,
        }
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── API: /api/strategies ────────────────────────────────────────────────────

@app.get("/api/strategies")
def api_strategies():
    try:
        with get_db() as db:
            rows = (
                db.query(StrategyRegistry)
                .order_by(StrategyRegistry.created_at.desc())
                .all()
            )
        return [
            {
                "id":          r.id,
                "name":        r.name,
                "version":     r.version,
                "is_active":   r.is_active,
                "created_at":  _iso(r.created_at),
                "params":      r.params,
                "description": r.description,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)


# ── API: /api/status ───────────────────────────────────────────────────────────

def _ping_tcp(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _ping_oanda() -> bool:
    try:
        import urllib.request
        env = settings.oanda.environment
        base = "https://api-fxtrade.oanda.com" if env == "live" else "https://api-fxpractice.oanda.com"
        req = urllib.request.Request(f"{base}/v3/accounts", headers={"Authorization": f"Bearer {settings.oanda.api_key}"})
        urllib.request.urlopen(req, timeout=3)
        return True
    except Exception:
        return False


@app.get("/api/status")
def api_status():
    try:
        total_capital = settings.bot.total_capital

        with get_db() as db:
            snap = (
                db.query(PortfolioSnapshot)
                .order_by(PortfolioSnapshot.timestamp.desc())
                .first()
            )
            pnl = _calc_pnl_totals(db, total_capital)
            recent_events = (
                db.query(EventLog)
                .order_by(EventLog.timestamp.desc())
                .limit(50)
                .all()
            )
            strategies = []
            try:
                strategies = (
                    db.query(StrategyRegistry)
                    .order_by(StrategyRegistry.created_at.desc())
                    .limit(10)
                    .all()
                )
            except Exception:
                pass

        snap_age = None
        bot_running = False
        last_snap = None
        if snap:
            snap_age = (datetime.utcnow() - snap.timestamp).total_seconds()
            last_snap = _iso(snap.timestamp)
        try:
            _pidfile = Path(__file__).parent / "logs" / "bot.pid"
            _pid = int(_pidfile.read_text().strip())
            import os as _os
            _os.kill(_pid, 0)
            bot_running = True
        except Exception:
            bot_running = snap_age is not None and snap_age < 5400

        # Broker health
        ibkr_ok  = _ping_tcp(settings.ibkr.host, settings.ibkr.port)
        oanda_ok = _ping_oanda() if settings.oanda.enabled else False

        # Circuit breaker inference
        daily_loss_limit = total_capital * 0.03
        cb_tripped = pnl["daily_pnl"] < -daily_loss_limit
        cb_reason  = f"Daily loss limit hit (${pnl['daily_pnl']:.2f} < -${daily_loss_limit:.2f})" if cb_tripped else ""

        # Active universe
        try:
            from portfolio.watchlist import UNIVERSE
            universe = [
                {"symbol": i.symbol, "broker": i.broker, "asset_type": i.asset_type}
                for i in UNIVERSE
            ]
        except Exception:
            universe = []

        return {
            "trading_mode": settings.bot.trading_mode,
            "bot_running": bot_running,
            "last_snapshot": last_snap,
            "last_snapshot_age_seconds": round(snap_age, 0) if snap_age else None,
            "brokers": {
                "ibkr": {
                    "status": "healthy" if ibkr_ok else "down",
                    "enabled": settings.ibkr.enabled,
                },
                "oanda": {
                    "status": "healthy" if oanda_ok else ("down" if settings.oanda.enabled else "disabled"),
                    "environment": settings.oanda.environment,
                    "enabled": settings.oanda.enabled,
                },
            },
            "circuit_breaker": {
                "tripped": cb_tripped,
                "reason": cb_reason,
                "daily_pnl": pnl["daily_pnl"],
                "daily_loss_limit": round(daily_loss_limit, 2),
            },
            "scheduler": {
                "scan_interval": "every 15 minutes",
                "snapshot_interval": "every 60 minutes",
                "prescreen_schedule": "daily 05:00 UTC (Tue–Sun)",
                "portfolio_schedule": "weekly Monday 00:00 UTC",
            },
            "active_universe": universe,
            "recent_events": [
                {
                    "id": ev.id,
                    "timestamp": _iso(ev.timestamp),
                    "event_type": ev.event_type,
                    "symbol": ev.symbol,
                    "description": ev.description,
                }
                for ev in recent_events
            ],
            "strategies": [
                {
                    "name": s.name, "version": s.version,
                    "is_active": s.is_active,
                    "created_at": _iso(s.created_at),
                    "params": s.params,
                }
                for s in strategies
            ],
        }
    except Exception as exc:
        logger.exception("API error"); return JSONResponse({"error": "Internal server error"}, status_code=500)
