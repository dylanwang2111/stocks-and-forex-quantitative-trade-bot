"""
agents/risk_agent.py
Converts a ConfidenceResult + current price into concrete position-sizing
parameters (quantity, stop-loss, take-profit, risk dollars).

Algorithm (Half-Kelly / confidence-tier scaling):
  1. size_fraction  = position_tier.size_fraction()
  2. max_position   = total_capital * 0.667  → $333
  3. position_usd   = max_position * size_fraction
  4. Clamp to available_cash (if state_manager supplied)
  5. quantity       = position_usd / current_price
                     stocks  → round 4 dp (fractional shares)
                     forex   → round to nearest int (OANDA units)
  6. Recompute position_usd = quantity * current_price
  7. Stop / TP from direction + entry price
  8. risk_dollars   = |entry - stop| * quantity
  9. Cap risk at broker_capital × risk_per_trade; scale quantity down if breached
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from agents.confidence_scorer import ConfidenceResult, PositionTier
from portfolio.state import PortfolioStateManager
from portfolio.watchlist import get_instrument
from config.settings import settings

logger = logging.getLogger(__name__)

# ATR-based stop/TP multipliers (matching backtest parameters)
_ATR_SL_MULT: dict[str, float] = {"stock": 2.0, "forex": 1.5, "crypto": 2.0}
_ATR_TP_MULT: dict[str, float] = {"stock": 4.0, "forex": 4.0, "crypto": 4.0}
_TARGET_ATR_PCT = 0.02   # target 2% ATR exposure per position (for vol scaling)


# ---------------------------------------------------------------------------
# Output dataclass
# ---------------------------------------------------------------------------

@dataclass
class RiskParams:
    position_size_usd: float    # USD value of the position
    quantity: float             # units to trade (shares or forex units)
    entry_price: float          # current market price (used for order)
    stop_price: float           # stop-loss price
    take_profit_price: float    # take-profit price
    risk_dollars: float         # actual dollars at risk (stop distance * qty)
    position_tier: str          # PositionTier.value
    size_fraction: float        # e.g. 0.25, 0.50, 0.75, 1.00


# ---------------------------------------------------------------------------
# RiskAgent
# ---------------------------------------------------------------------------

class RiskAgent:
    STOP_PCT    = 0.015   # 1.5% stop distance from entry
    TP_PCT      = 0.030   # 3.0% take-profit distance (2:1 RR)

    def __init__(self, state_manager: PortfolioStateManager | None = None) -> None:
        self._state = state_manager

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def compute(
        self,
        confidence_result: ConfidenceResult,
        current_price: float,
        symbol: str,
        atr: float | None = None,
    ) -> RiskParams | None:
        """
        Compute position sizing.

        Parameters
        ----------
        atr:
            ATR(14) value from 1h data. When provided, stops and TP are placed
            at ATR multiples and position size is scaled by volatility.

        Returns None if:
        - tier is NO_TRADE or WATCH
        - available_cash < instrument min_position_usd
        - rounded quantity == 0
        - any unexpected error (logged as WARNING)
        """
        try:
            return self._compute(confidence_result, current_price, symbol, atr=atr)
        except Exception as exc:
            logger.warning(
                "RiskAgent.compute: unexpected error for %s — %s", symbol, exc, exc_info=True
            )
            return None

    # ------------------------------------------------------------------
    # Internal implementation
    # ------------------------------------------------------------------

    def _compute(
        self,
        confidence_result: ConfidenceResult,
        current_price: float,
        symbol: str,
        atr: float | None = None,
    ) -> RiskParams | None:

        tier = confidence_result.position_tier

        # ── Gate: must be tradeable ────────────────────────────────────────
        if tier in (PositionTier.NO_TRADE, PositionTier.WATCH):
            logger.debug(
                "RiskAgent: %s tier=%s — not tradeable, skipping", symbol, tier.value
            )
            return None

        # ── Instrument metadata ────────────────────────────────────────────
        try:
            instrument = get_instrument(symbol)
        except KeyError:
            logger.warning("RiskAgent: unknown symbol '%s'", symbol)
            return None

        # ── Step 1: size_fraction from tier ───────────────────────────────
        size_fraction = tier.size_fraction()

        # ── Step 2: max position = 2/3 of this broker's capital ──────────
        broker_cap = settings.bot.broker_capital(instrument.broker)
        max_position_usd = broker_cap * 0.667

        # ── Step 3: target position in USD ────────────────────────────────
        position_size_usd = max_position_usd * size_fraction

        # ── Step 3b: apply macro risk multiplier ──────────────────────────
        macro_mult = getattr(confidence_result, "macro_multiplier", 1.0)
        macro_mult = max(0.0, min(macro_mult, 1.0))  # clamp to [0, 1]
        position_size_usd *= macro_mult
        if macro_mult < 1.0:
            logger.info(
                "RiskAgent: %s macro reduction %.0f%% (multiplier=%.2f)",
                symbol, macro_mult * 100, macro_mult,
            )

        # Step 3c: volatility-adjusted sizing — scale down high-vol instruments
        if atr is not None and atr > 0 and current_price > 0:
            atr_pct = atr / current_price
            vol_scale = min(1.0, _TARGET_ATR_PCT / atr_pct)
            vol_scale = max(0.35, vol_scale)  # floor at 35% of slot
            position_size_usd *= vol_scale
            if vol_scale < 1.0:
                logger.info(
                    "RiskAgent: %s vol-scaled %.0f%% (atr_pct=%.2f%%)",
                    symbol, vol_scale * 100, atr_pct * 100,
                )

        # ── Step 4: clamp to available cash in this broker's pool ────────
        if self._state is not None:
            available = self._state.available_cash(broker=instrument.broker)
            if available < instrument.min_position_usd:
                logger.debug(
                    "RiskAgent: %s insufficient cash (available=%.2f < min=%.2f)",
                    symbol, available, instrument.min_position_usd,
                )
                return None
            position_size_usd = min(position_size_usd, available)

        # ── Step 5: raw quantity ───────────────────────────────────────────
        if current_price <= 0:
            logger.warning("RiskAgent: %s current_price=%.6f is non-positive", symbol, current_price)
            return None

        raw_qty = position_size_usd / current_price
        quantity = self._round_quantity(raw_qty, instrument.asset_type)

        # ── Guard: quantity rounded to zero ───────────────────────────────
        if quantity == 0:
            logger.debug(
                "RiskAgent: %s quantity rounded to 0 (raw=%.6f), skipping", symbol, raw_qty
            )
            return None

        # ── Step 6: recompute position_size_usd after rounding ────────────
        position_size_usd = quantity * current_price

        # Step 7: stop and take-profit prices (ATR-based when available, fixed % fallback)
        direction = confidence_result.direction
        entry_price = current_price
        asset_type = instrument.asset_type

        if atr is not None and atr > 0:
            sl_dist = atr * _ATR_SL_MULT.get(asset_type, 2.0)
            tp_dist = atr * _ATR_TP_MULT.get(asset_type, 4.0)
        else:
            sl_dist = entry_price * self.STOP_PCT
            tp_dist = entry_price * self.TP_PCT

        if direction == "long":
            stop_price        = entry_price - sl_dist
            take_profit_price = entry_price + tp_dist
        elif direction == "short":
            stop_price        = entry_price + sl_dist
            take_profit_price = entry_price - tp_dist
        else:
            logger.warning(
                "RiskAgent: %s direction='%s' is not long/short after tier check",
                symbol, direction,
            )
            return None

        # ── Step 8: risk in dollars ────────────────────────────────────────
        stop_distance = abs(entry_price - stop_price)
        risk_dollars  = stop_distance * quantity

        # ── Step 9: cap risk at MAX_RISK_USD (per-broker pool) ────────────
        max_risk_usd = broker_cap * settings.bot.risk_per_trade
        if risk_dollars > max_risk_usd:
            capped_qty = max_risk_usd / stop_distance
            # Floor (not round) so risk_dollars never exceeds MAX_RISK_USD after rounding
            quantity   = self._floor_quantity(capped_qty, instrument.asset_type)

            if quantity == 0:
                logger.debug(
                    "RiskAgent: %s quantity=0 after risk-cap (stop_distance=%.6f)", symbol, stop_distance
                )
                return None

            # Recompute derived values after cap
            position_size_usd = quantity * current_price
            risk_dollars      = stop_distance * quantity

        # ── Emit DEBUG log ─────────────────────────────────────────────────
        logger.debug(
            "RiskAgent: %s tier=%s size_usd=%.2f qty=%s stop=%.4f tp=%.4f",
            symbol,
            tier.value,
            position_size_usd,
            quantity,
            stop_price,
            take_profit_price,
        )

        return RiskParams(
            position_size_usd = round(position_size_usd, 4),
            quantity          = quantity,
            entry_price       = round(entry_price, 6),
            stop_price        = round(stop_price, 6),
            take_profit_price = round(take_profit_price, 6),
            risk_dollars      = round(risk_dollars, 6),
            position_tier     = tier.value,
            size_fraction     = size_fraction,
        )

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------

    @staticmethod
    def _round_quantity(qty: float, asset_type: str) -> float:
        """
        Round quantity to the precision required by the asset type.
        - stocks : 4 decimal places (fractional shares)
        - forex  : nearest integer (OANDA units)
        """
        if asset_type == "forex":
            return float(round(qty))
        if asset_type == "crypto":
            return round(qty, 4)
        # stocks
        return round(qty, 4)

    @staticmethod
    def _floor_quantity(qty: float, asset_type: str) -> float:
        """
        Floor quantity (used after risk-cap) so risk_dollars never exceeds MAX_RISK_USD.
        - stocks : floor to 4 decimal places
        - forex  : floor to nearest integer
        """
        if asset_type == "forex":
            return float(math.floor(qty))
        # stocks: floor at 4 dp
        factor = 10_000.0
        return math.floor(qty * factor) / factor


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def test_risk_agent() -> None:
    """
    Verifies:
    1. SMALL tier EURUSD → qty ≈ 77 units
    2. FULL  tier SPY    → qty ≈ 0.6660 shares
    3. risk_dollars <= MAX_RISK_USD after capping
    4. NO_TRADE → returns None
    5. long: stop below entry, tp above entry
    6. short: stop above entry, tp below entry
    """
    from dataclasses import dataclass as _dc

    # ── Minimal ConfidenceResult factory ──────────────────────────────────
    def make_cr(tier: PositionTier, direction: str = "long") -> ConfidenceResult:
        score_map = {
            PositionTier.NO_TRADE: 0.0,
            PositionTier.WATCH:   60.0,
            PositionTier.SMALL:   70.0,
            PositionTier.MEDIUM:  80.0,
            PositionTier.LARGE:   90.0,
            PositionTier.FULL:   100.0,
        }
        dominant = score_map[tier]
        bull = dominant if direction == "long"  else 0.0
        bear = dominant if direction == "short" else 0.0
        return ConfidenceResult(
            bull_score=bull,
            bear_score=bear,
            direction=direction,
            dominant_score=dominant,
            position_tier=tier,
            breakdown={},
        )

    agent = RiskAgent(state_manager=None)

    # ── Test 1: SMALL tier — EURUSD at 1.08 ──────────────────────────────
    # EURUSD is forex → broker="oanda" → uses broker_capital("oanda")
    oanda_cap = settings.bot.broker_capital("oanda")
    max_pos_oanda = oanda_cap * 0.667
    cr_small = make_cr(PositionTier.SMALL, "long")
    rp = agent.compute(cr_small, current_price=1.08, symbol="EURUSD")
    assert rp is not None, "SMALL tier should produce RiskParams"
    expected_qty  = round(max_pos_oanda * 0.25 / 1.08)
    expected_size = expected_qty * 1.08
    assert rp.quantity == float(expected_qty), f"Expected qty={expected_qty}, got {rp.quantity}"
    assert rp.position_tier == "SMALL"
    assert rp.size_fraction == 0.25
    assert abs(rp.position_size_usd - expected_size) < 0.01, (
        f"position_size_usd mismatch: {rp.position_size_usd}"
    )
    print(f"Test 1 PASS — EURUSD SMALL: qty={rp.quantity}, size_usd={rp.position_size_usd:.2f}")

    # ── Test 2: FULL tier — SPY at $500 ──────────────────────────────────
    # SPY is stock → broker="ibkr" → uses broker_capital("ibkr")
    ibkr_cap = settings.bot.broker_capital("ibkr")
    max_pos_ibkr = ibkr_cap * 0.667
    cr_full = make_cr(PositionTier.FULL, "long")
    rp2 = agent.compute(cr_full, current_price=500.0, symbol="SPY")
    assert rp2 is not None, "FULL tier should produce RiskParams"
    expected_raw_qty  = max_pos_ibkr / 500.0
    stop_distance_spy = 500.0 * 0.015
    max_risk_usd      = ibkr_cap * settings.bot.risk_per_trade
    if expected_raw_qty * stop_distance_spy > max_risk_usd:
        capped = max_risk_usd / stop_distance_spy
        expected_qty2 = math.floor(capped * 10000) / 10000
    else:
        expected_qty2 = round(expected_raw_qty, 4)
    assert rp2.quantity == expected_qty2, f"Expected qty={expected_qty2} (post-cap), got {rp2.quantity}"
    assert rp2.position_tier == "FULL"
    assert rp2.size_fraction == 1.00
    print(f"Test 2 PASS — SPY FULL: qty={rp2.quantity}, size_usd={rp2.position_size_usd:.2f} (risk-capped)")

    # ── Test 3: risk_dollars <= broker_capital * risk_per_trade ──────────
    # For EURUSD SMALL: stop_distance = 1.08 * 0.015 = 0.0162
    # risk_dollars should be well under max_risk_usd, no cap needed
    _max_risk = oanda_cap * settings.bot.risk_per_trade
    assert rp.risk_dollars <= _max_risk, (
        f"risk_dollars={rp.risk_dollars} exceeds max_risk_usd={_max_risk}"
    )
    # Force a scenario where capping IS triggered: FULL SPY (capped against ibkr pool)
    _max_risk_ibkr = ibkr_cap * settings.bot.risk_per_trade
    assert rp2.risk_dollars <= _max_risk_ibkr, (
        f"risk_dollars={rp2.risk_dollars} exceeds max_risk_usd after cap"
    )
    print(f"Test 3 PASS — risk_dollars capped: EURUSD={rp.risk_dollars:.4f}, SPY={rp2.risk_dollars:.4f}")

    # ── Test 4: NO_TRADE returns None ────────────────────────────────────
    cr_notrade = make_cr(PositionTier.NO_TRADE, "neutral")
    rp_none = agent.compute(cr_notrade, current_price=1.08, symbol="EURUSD")
    assert rp_none is None, f"NO_TRADE should return None, got {rp_none}"
    print("Test 4 PASS — NO_TRADE returns None")

    # ── Test 5: long → stop below entry, tp above entry ──────────────────
    cr_long = make_cr(PositionTier.MEDIUM, "long")
    rp_long = agent.compute(cr_long, current_price=1.0800, symbol="EURUSD")
    assert rp_long is not None
    assert rp_long.stop_price < rp_long.entry_price, (
        f"long stop {rp_long.stop_price} should be below entry {rp_long.entry_price}"
    )
    assert rp_long.take_profit_price > rp_long.entry_price, (
        f"long tp {rp_long.take_profit_price} should be above entry {rp_long.entry_price}"
    )
    print(
        f"Test 5 PASS — long: entry={rp_long.entry_price:.4f} "
        f"stop={rp_long.stop_price:.4f} tp={rp_long.take_profit_price:.4f}"
    )

    # ── Test 6: short → stop above entry, tp below entry ─────────────────
    cr_short = make_cr(PositionTier.MEDIUM, "short")
    rp_short = agent.compute(cr_short, current_price=1.0800, symbol="EURUSD")
    assert rp_short is not None
    assert rp_short.stop_price > rp_short.entry_price, (
        f"short stop {rp_short.stop_price} should be above entry {rp_short.entry_price}"
    )
    assert rp_short.take_profit_price < rp_short.entry_price, (
        f"short tp {rp_short.take_profit_price} should be below entry {rp_short.entry_price}"
    )
    print(
        f"Test 6 PASS — short: entry={rp_short.entry_price:.4f} "
        f"stop={rp_short.stop_price:.4f} tp={rp_short.take_profit_price:.4f}"
    )

    print("\ntest_risk_agent: ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_risk_agent()
