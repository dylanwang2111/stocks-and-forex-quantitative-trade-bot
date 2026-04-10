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
        symbol="TSLA",
        broker="ibkr",
        asset_type="stock",
        yf_symbol="TSLA",
        active_hours_utc="13:30–20:00",
        slippage_pct=0.001,
        min_position_usd=10.0,
        correlated_with=["QQQ"],
        notes="Tesla. High-beta momentum stock.",
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
        symbol="XOM",
        broker="ibkr",
        asset_type="stock",
        yf_symbol="XOM",
        active_hours_utc="13:30–20:00",
        slippage_pct=0.0006,
        min_position_usd=10.0,
        notes="ExxonMobil. Integrated major."),
    Instrument(
        symbol="BTCUSD",
        broker="oanda",
        asset_type="crypto",
        yf_symbol="BTC-USD",
        active_hours_utc="00:00–23:59",   # 24h weekdays (OANDA closes weekends)
        slippage_pct=0.001,
        min_position_usd=10.0,
        correlated_with=[],
        notes="Bitcoin / USD — long only via OANDA",
    ),
    Instrument(
        symbol="ETHUSD",
        broker="oanda",
        asset_type="crypto",
        yf_symbol="ETH-USD",
        active_hours_utc="00:00–23:59",
        slippage_pct=0.001,
        min_position_usd=10.0,
        correlated_with=["BTCUSD"],
        notes="Ethereum / USD — long only via OANDA",
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
    Instrument(symbol="AMD",  broker="ibkr", asset_type="stock", yf_symbol="AMD",
               active_hours_utc="13:30–20:00", slippage_pct=0.001, min_position_usd=10.0,
               correlated_with=["NVDA", "QQQ", "XLK"], notes="AMD. High-beta semiconductor. Strong trend + momentum setups."),
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
    Instrument(symbol="LLY",  broker="ibkr", asset_type="stock", yf_symbol="LLY",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=[], notes="Eli Lilly. High-momentum pharma. GLP-1/weight-loss drug tailwind."),
    # ── Energy (oil / petroleum / integrated) ───────────────────────────────────
    Instrument(symbol="XOM",  broker="ibkr", asset_type="stock", yf_symbol="XOM",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["CVX", "OXY", "COP"], notes="ExxonMobil. Integrated major."),
    Instrument(symbol="CVX",  broker="ibkr", asset_type="stock", yf_symbol="CVX",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["XOM", "OXY", "COP", "XLE"], notes="Chevron. Integrated major."),
    Instrument(symbol="OXY",  broker="ibkr", asset_type="stock", yf_symbol="OXY",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=["XOM", "CVX", "COP", "XLE"], notes="Occidental Petroleum. High oil beta."),
    Instrument(symbol="COP",  broker="ibkr", asset_type="stock", yf_symbol="COP",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["XOM", "CVX", "OXY", "XLE"], notes="ConocoPhillips. E&P focused."),
    Instrument(symbol="SLB",  broker="ibkr", asset_type="stock", yf_symbol="SLB",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=["HAL", "XLE"], notes="SLB (Schlumberger). Oilfield services leader."),
    Instrument(symbol="HAL",  broker="ibkr", asset_type="stock", yf_symbol="HAL",
               active_hours_utc="13:30–20:00", slippage_pct=0.0009, min_position_usd=10.0,
               correlated_with=["SLB", "XLE"], notes="Halliburton. Oilfield services."),
    Instrument(symbol="MPC",  broker="ibkr", asset_type="stock", yf_symbol="MPC",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=["VLO", "XLE"], notes="Marathon Petroleum. Downstream refining."),
    Instrument(symbol="VLO",  broker="ibkr", asset_type="stock", yf_symbol="VLO",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=["MPC", "XLE"], notes="Valero Energy. Largest US refiner."),
    Instrument(symbol="XLE",  broker="ibkr", asset_type="stock", yf_symbol="XLE",
               active_hours_utc="13:30–20:00", slippage_pct=0.0005, min_position_usd=10.0,
               correlated_with=["CVX", "OXY", "COP", "SLB", "HAL", "MPC", "VLO"],
               notes="SPDR Energy Select Sector ETF. Diversified oil/gas exposure."),
    # ── Gold & Precious Metals ──────────────────────────────────────────────────
    Instrument(symbol="GOLD",  broker="ibkr", asset_type="stock", yf_symbol="GOLD",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=["NEM", "GDX", "GDXJ"], notes="Barrick Gold. World's largest gold miner."),
    Instrument(symbol="NEM",   broker="ibkr", asset_type="stock", yf_symbol="NEM",
               active_hours_utc="13:30–20:00", slippage_pct=0.0008, min_position_usd=10.0,
               correlated_with=["GOLD", "GDX", "GDXJ"], notes="Newmont. Leading gold producer."),
    Instrument(symbol="GDX",   broker="ibkr", asset_type="stock", yf_symbol="GDX",
               active_hours_utc="13:30–20:00", slippage_pct=0.0006, min_position_usd=10.0,
               correlated_with=["GDXJ", "GOLD", "NEM", "GLD"], notes="VanEck Gold Miners ETF. Senior miners."),
    Instrument(symbol="GDXJ",  broker="ibkr", asset_type="stock", yf_symbol="GDXJ",
               active_hours_utc="13:30–20:00", slippage_pct=0.0007, min_position_usd=10.0,
               correlated_with=["GDX", "GOLD", "NEM", "GLD"], notes="VanEck Junior Gold Miners ETF. Higher beta to gold."),
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
    # ── Crypto ──────────────────────────────────────────────────────────────────
    Instrument(symbol="BTCUSD", broker="oanda", asset_type="crypto", yf_symbol="BTC-USD",
               active_hours_utc="00:00–23:59", slippage_pct=0.001, min_position_usd=10.0,
               correlated_with=["ETHUSD"], notes="Bitcoin / USD via OANDA. 24h weekdays."),
    Instrument(symbol="ETHUSD", broker="oanda", asset_type="crypto", yf_symbol="ETH-USD",
               active_hours_utc="00:00–23:59", slippage_pct=0.001, min_position_usd=10.0,
               correlated_with=["BTCUSD"], notes="Ethereum / USD via OANDA. 24h weekdays."),
]

# ── Quick-access helpers ───────────────────────────────────────────────────────

UNIVERSE_BY_SYMBOL: dict[str, Instrument] = {i.symbol: i for i in UNIVERSE}

# Full pool lookup — used as fallback when an open position's symbol is no longer
# in the active UNIVERSE (e.g. dropped by weekly PortfolioAgent.select()).
CANDIDATE_POOL_BY_SYMBOL: dict[str, Instrument] = {i.symbol: i for i in CANDIDATE_POOL}

# Pairs that must not be held simultaneously (undirected)
CORRELATION_BLACKLIST: list[frozenset[str]] = [
    frozenset({"SPY", "QQQ"}),
    frozenset({"QQQ", "NVDA"}),
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
    """Return instrument metadata.

    Checks active UNIVERSE first; falls back to CANDIDATE_POOL so that open
    positions for symbols dropped from the weekly universe can still be managed
    (partial closes, exits, etc.).  Raises KeyError only if unknown to both.
    """
    try:
        return UNIVERSE_BY_SYMBOL[symbol]
    except KeyError:
        return CANDIDATE_POOL_BY_SYMBOL[symbol]


def is_crypto_symbol(symbol: str) -> bool:
    """Return True if *symbol* is a crypto instrument (trades on weekends)."""
    try:
        return get_instrument(symbol).asset_type == "crypto"
    except KeyError:
        return False


def are_correlated(sym_a: str, sym_b: str) -> bool:
    """Return True if two symbols are in the correlation blacklist."""
    pair = frozenset({sym_a, sym_b})
    return pair in CORRELATION_BLACKLIST


def active_symbols() -> list[str]:
    return [i.symbol for i in UNIVERSE]
