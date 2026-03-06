"""
portfolio/watchlist.py
Instrument universe with per-instrument metadata.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field

_UNIVERSE_LOCK = threading.Lock()


@dataclass(frozen=True)
class Instrument:
    symbol: str
    broker: str                   # "ibkr" | "oanda"
    asset_type: str               # "stock" | "forex"
    yf_symbol: str                # symbol as yfinance expects it
    active_hours_utc: str         # human-readable trading window
    slippage_pct: float           # realistic round-trip slippage fraction
    min_position_usd: float       # minimum meaningful position size
    correlated_with: list[str] = field(default_factory=list)
    notes: str = ""


# ── Universe ───────────────────────────────────────────────────────────────────
UNIVERSE: list[Instrument] = [
    Instrument(
        symbol="SPY",
        broker="ibkr",
        asset_type="stock",
        yf_symbol="SPY",
        active_hours_utc="13:30–20:00",
        slippage_pct=0.0005,   # tight spread ETF
        min_position_usd=10.0,
        correlated_with=["QQQ"],
        notes="S&P 500 ETF. Never hold with QQQ simultaneously (0.95 corr).",
    ),
    Instrument(
        symbol="QQQ",
        broker="ibkr",
        asset_type="stock",
        yf_symbol="QQQ",
        active_hours_utc="13:30–20:00",
        slippage_pct=0.0005,
        min_position_usd=10.0,
        correlated_with=["SPY", "NVDA"],
        notes="Nasdaq-100 ETF. Never hold with SPY or NVDA simultaneously.",
    ),
    Instrument(
        symbol="NVDA",
        broker="ibkr",
        asset_type="stock",
        yf_symbol="NVDA",
        active_hours_utc="13:30–20:00",
        slippage_pct=0.001,
        min_position_usd=10.0,
        correlated_with=["QQQ"],
        notes="High volatility / momentum stock. Fractional shares on IBKR.",
    ),
    Instrument(
        symbol="AAPL",
        broker="ibkr",
        asset_type="stock",
        yf_symbol="AAPL",
        active_hours_utc="13:30–20:00",
        slippage_pct=0.0007,
        min_position_usd=10.0,
        correlated_with=[],
        notes="Stable large-cap. Fractional shares on IBKR.",
    ),
    Instrument(
        symbol="EURUSD",
        broker="oanda",
        asset_type="forex",
        yf_symbol="EURUSD=X",
        active_hours_utc="00:00–22:00",   # 24/5, avoid thin Asia session
        slippage_pct=0.0002,              # ~0.2 pip on 1.1000 price
        min_position_usd=10.0,
        correlated_with=["GBPUSD"],
        notes="Primary intraday instrument. No PDT restriction. 24/5 trading.",
    ),
    Instrument(
        symbol="GBPUSD",
        broker="oanda",
        asset_type="forex",
        yf_symbol="GBPUSD=X",
        active_hours_utc="07:00–20:00",   # London + overlap session
        slippage_pct=0.0003,
        min_position_usd=10.0,
        correlated_with=["EURUSD"],
        notes="Secondary forex. Strong trends only. Never hold with EURUSD.",
    ),
]

# ── Candidate pool for portfolio selection ─────────────────────────────────────
# PortfolioAgent scores these weekly and picks the best performers for UNIVERSE.
CANDIDATE_POOL: list[Instrument] = [
    # ── ETFs ────────────────────────────────────────────────────────────────────
    Instrument(symbol="SPY",  broker="ibkr", asset_type="stock", yf_symbol="SPY",
               active_hours_utc="13:30–20:00", slippage_pct=0.0005, min_position_usd=10.0,
               correlated_with=["QQQ", "IWM"], notes="S&P 500 ETF"),
    Instrument(symbol="QQQ",  broker="ibkr", asset_type="stock", yf_symbol="QQQ",
               active_hours_utc="13:30–20:00", slippage_pct=0.0005, min_position_usd=10.0,
               correlated_with=["SPY", "NVDA", "MSFT", "AAPL"], notes="Nasdaq-100 ETF"),
    Instrument(symbol="IWM",  broker="ibkr", asset_type="stock", yf_symbol="IWM",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["SPY"], notes="Russell 2000 small-cap ETF"),
    Instrument(symbol="GLD",  broker="ibkr", asset_type="stock", yf_symbol="GLD",
               active_hours_utc="13:30–20:00", slippage_pct=0.0005, min_position_usd=10.0,
               correlated_with=[], notes="Gold ETF. Moves independently of equities."),
    Instrument(symbol="XLK",  broker="ibkr", asset_type="stock", yf_symbol="XLK",
               active_hours_utc="13:30–20:00", slippage_pct=0.0005, min_position_usd=10.0,
               correlated_with=["QQQ", "AAPL", "MSFT"], notes="Technology sector ETF"),
    Instrument(symbol="XLF",  broker="ibkr", asset_type="stock", yf_symbol="XLF",
               active_hours_utc="13:30–20:00", slippage_pct=0.0005, min_position_usd=10.0,
               correlated_with=["JPM", "GS", "BAC"], notes="Financials sector ETF"),
    # ── Technology ──────────────────────────────────────────────────────────────
    Instrument(symbol="AAPL", broker="ibkr", asset_type="stock", yf_symbol="AAPL",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["QQQ", "XLK"], notes="Apple. Mega-cap tech."),
    Instrument(symbol="MSFT", broker="ibkr", asset_type="stock", yf_symbol="MSFT",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["QQQ", "XLK"], notes="Microsoft. Stable mega-cap."),
    Instrument(symbol="NVDA", broker="ibkr", asset_type="stock", yf_symbol="NVDA",
               active_hours_utc="13:30–20:00", slippage_pct=0.001, min_position_usd=10.0,
               correlated_with=["QQQ", "XLK"], notes="Nvidia. High volatility / momentum."),
    Instrument(symbol="GOOGL", broker="ibkr", asset_type="stock", yf_symbol="GOOGL",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["QQQ", "XLK"], notes="Alphabet. Mega-cap tech."),
    Instrument(symbol="AMZN", broker="ibkr", asset_type="stock", yf_symbol="AMZN",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["QQQ"], notes="Amazon. E-commerce + cloud."),
    Instrument(symbol="META", broker="ibkr", asset_type="stock", yf_symbol="META",
               active_hours_utc="13:30–20:00", slippage_pct=0.001, min_position_usd=10.0,
               correlated_with=["QQQ"], notes="Meta. Social media + AI."),
    Instrument(symbol="TSLA", broker="ibkr", asset_type="stock", yf_symbol="TSLA",
               active_hours_utc="13:30–20:00", slippage_pct=0.001, min_position_usd=10.0,
               correlated_with=[], notes="Tesla. High volatility momentum stock."),
    # ── Financials ──────────────────────────────────────────────────────────────
    Instrument(symbol="JPM",  broker="ibkr", asset_type="stock", yf_symbol="JPM",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["XLF", "GS", "BAC"], notes="JPMorgan Chase."),
    Instrument(symbol="GS",   broker="ibkr", asset_type="stock", yf_symbol="GS",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=["XLF", "JPM"], notes="Goldman Sachs."),
    Instrument(symbol="BAC",  broker="ibkr", asset_type="stock", yf_symbol="BAC",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["XLF", "JPM"], notes="Bank of America."),
    # ── Healthcare ──────────────────────────────────────────────────────────────
    Instrument(symbol="JNJ",  broker="ibkr", asset_type="stock", yf_symbol="JNJ",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["UNH"], notes="Johnson & Johnson. Defensive healthcare."),
    Instrument(symbol="UNH",  broker="ibkr", asset_type="stock", yf_symbol="UNH",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["JNJ"], notes="UnitedHealth. Health insurance leader."),
    # ── Energy ──────────────────────────────────────────────────────────────────
    Instrument(symbol="XOM",  broker="ibkr", asset_type="stock", yf_symbol="XOM",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["CVX"], notes="ExxonMobil."),
    Instrument(symbol="CVX",  broker="ibkr", asset_type="stock", yf_symbol="CVX",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["XOM"], notes="Chevron."),
    # ── Consumer ────────────────────────────────────────────────────────────────
    Instrument(symbol="WMT",  broker="ibkr", asset_type="stock", yf_symbol="WMT",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["COST"], notes="Walmart. Defensive consumer staple."),
    Instrument(symbol="COST", broker="ibkr", asset_type="stock", yf_symbol="COST",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["WMT"], notes="Costco. Strong retail growth."),
    # ── Industrials ─────────────────────────────────────────────────────────────
    Instrument(symbol="CAT",  broker="ibkr", asset_type="stock", yf_symbol="CAT",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=[], notes="Caterpillar. Infrastructure / commodities proxy."),
    # ── Forex ───────────────────────────────────────────────────────────────────
    Instrument(symbol="EURUSD", broker="oanda", asset_type="forex", yf_symbol="EURUSD=X",
               active_hours_utc="00:00–22:00", slippage_pct=0.0002, min_position_usd=10.0,
               correlated_with=["GBPUSD"], notes="EUR/USD. Primary forex. No PDT."),
    Instrument(symbol="GBPUSD", broker="oanda", asset_type="forex", yf_symbol="GBPUSD=X",
               active_hours_utc="07:00–20:00", slippage_pct=0.0003, min_position_usd=10.0,
               correlated_with=["EURUSD"], notes="GBP/USD. London session."),
    Instrument(symbol="USDJPY", broker="oanda", asset_type="forex", yf_symbol="JPY=X",
               active_hours_utc="00:00–22:00", slippage_pct=0.0002, min_position_usd=10.0,
               correlated_with=[], notes="USD/JPY. Safe haven proxy. Note: yfinance returns JPY per USD."),
    Instrument(symbol="AUDUSD", broker="oanda", asset_type="forex", yf_symbol="AUDUSD=X",
               active_hours_utc="22:00–20:00", slippage_pct=0.0003, min_position_usd=10.0,
               correlated_with=[], notes="AUD/USD. Commodity-linked currency."),
    Instrument(symbol="USDCAD", broker="oanda", asset_type="forex", yf_symbol="CAD=X",
               active_hours_utc="13:00–21:00", slippage_pct=0.0003, min_position_usd=10.0,
               correlated_with=[], notes="USD/CAD. Oil-linked currency."),
    Instrument(symbol="USDCHF", broker="oanda", asset_type="forex", yf_symbol="CHF=X",
               active_hours_utc="07:00–20:00", slippage_pct=0.0003, min_position_usd=10.0,
               correlated_with=[], notes="USD/CHF. Safe haven. European session."),
]

# ── Quick-access helpers ───────────────────────────────────────────────────────

UNIVERSE_BY_SYMBOL: dict[str, Instrument] = {i.symbol: i for i in UNIVERSE}

# Pairs that must not be held simultaneously (undirected)
CORRELATION_BLACKLIST: list[frozenset[str]] = [
    frozenset({"SPY", "QQQ"}),
    frozenset({"QQQ", "NVDA"}),
    frozenset({"EURUSD", "GBPUSD"}),
]


def set_active_universe(instruments: list["Instrument"]) -> None:
    """
    Replace the active trading universe with the given instruments.
    Updates UNIVERSE, UNIVERSE_BY_SYMBOL, and CORRELATION_BLACKLIST in-place
    so all existing module references remain valid.
    Called by PortfolioAgent.select() after scoring candidates.
    Thread-safe: acquires _UNIVERSE_LOCK.
    """
    global CORRELATION_BLACKLIST
    with _UNIVERSE_LOCK:
        UNIVERSE.clear()
        UNIVERSE.extend(instruments)

        UNIVERSE_BY_SYMBOL.clear()
        UNIVERSE_BY_SYMBOL.update({i.symbol: i for i in instruments})

        # Rebuild correlation blacklist from correlated_with fields
        seen: set[frozenset] = set()
        new_blacklist: list[frozenset] = []
        for inst in instruments:
            for corr in inst.correlated_with:
                pair = frozenset({inst.symbol, corr})
                if pair not in seen:
                    seen.add(pair)
                    new_blacklist.append(pair)
        CORRELATION_BLACKLIST = new_blacklist


def get_universe_snapshot() -> list["Instrument"]:
    """Return a thread-safe copy of the current active UNIVERSE."""
    with _UNIVERSE_LOCK:
        return list(UNIVERSE)


def get_instrument(symbol: str) -> Instrument:
    """Return instrument metadata or raise KeyError."""
    return UNIVERSE_BY_SYMBOL[symbol]


def are_correlated(sym_a: str, sym_b: str) -> bool:
    """Return True if two symbols are in the correlation blacklist."""
    pair = frozenset({sym_a, sym_b})
    return pair in CORRELATION_BLACKLIST


def active_symbols() -> list[str]:
    return [i.symbol for i in UNIVERSE]
