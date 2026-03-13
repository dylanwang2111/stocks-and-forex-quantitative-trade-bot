"""
agents/confidence_scorer.py
Maps signal votes + regime context → confidence score → position tier.

Scoring system:
  - 8 categories, each votes +1 / -1 / 0
  - Cat7 (MTF) is double weight: can vote ±2
  - Max possible raw score = 9 (7 cats × 1 + cat7 × 2)
  - Regime multipliers applied per-category BEFORE summing
  - Score mapped to 0–100 range
  - Min 10-point lead gap between bull/bear required for directional trade
  - SMALL tier starts at 55 to match backtest entry threshold
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from agents.signal_engine import SignalBundle
from regime.detector import RegimeContext


class PositionTier(Enum):
    NO_TRADE = "NO_TRADE"    # score < 45 — no signal
    WATCH    = "WATCH"       # 45–54  — monitor only, no entry
    SMALL    = "SMALL"       # 55–69  — 25% of max position
    MEDIUM   = "MEDIUM"      # 70–79  — 50% of max position
    LARGE    = "LARGE"       # 80–89  — 75% of max position
    FULL     = "FULL"        # ≥ 90   — 100% of max position

    def size_fraction(self) -> float:
        """Fraction of maximum position size to deploy."""
        return {
            PositionTier.NO_TRADE: 0.00,
            PositionTier.WATCH:    0.00,
            PositionTier.SMALL:    0.25,
            PositionTier.MEDIUM:   0.50,
            PositionTier.LARGE:    0.75,
            PositionTier.FULL:     1.00,
        }[self]


@dataclass
class ConfidenceResult:
    bull_score:       float           # 0–100
    bear_score:       float           # 0–100
    direction:        str             # "long" | "short" | "neutral"
    dominant_score:   float           # max(bull, bear)
    position_tier:    PositionTier
    breakdown:        dict            # per-category weighted contributions
    macro_multiplier: float = 1.0    # HIGH→0.5, MEDIUM→0.75, LOW→1.0

    def tradeable(self) -> bool:
        return self.position_tier not in (PositionTier.NO_TRADE, PositionTier.WATCH)

    def to_dict(self) -> dict:
        return {
            "bull_score":       round(self.bull_score, 1),
            "bear_score":       round(self.bear_score, 1),
            "direction":        self.direction,
            "dominant_score":   round(self.dominant_score, 1),
            "position_tier":    self.position_tier.value,
            "size_fraction":    self.position_tier.size_fraction(),
            "tradeable":        self.tradeable(),
            "macro_multiplier": self.macro_multiplier,
            "breakdown":        self.breakdown,
        }


class ConfidenceScorer:
    """
    Converts a SignalBundle + RegimeContext into a ConfidenceResult.
    """

    # Maximum raw score (used for normalisation to 0–100)
    # Cats 1-6, 8 → max ±1 each (7 cats)
    # Cat7 → max ±2 (double weight)
    # Total max = 7 + 2 = 9
    MAX_RAW = 9.0

    # Minimum lead gap between bull and bear (in 0–100 points)
    MIN_LEAD_GAP = 10.0

    # Confidence thresholds — calibrated to match backtest entry at 55
    THRESHOLDS = [
        (90, PositionTier.FULL),
        (80, PositionTier.LARGE),
        (70, PositionTier.MEDIUM),
        (55, PositionTier.SMALL),
        (45, PositionTier.WATCH),
    ]

    def score(
        self,
        bundle: SignalBundle,
        regime: RegimeContext,
    ) -> ConfidenceResult:
        """
        Args:
            bundle: SignalBundle from SignalEngine
            regime: RegimeContext from RegimeDetector

        Returns:
            ConfidenceResult with all scoring details
        """
        votes = bundle.votes()
        multipliers = regime.signal_multiplier()

        # ── Weighted bull / bear accumulation ─────────────────────────────────
        bull_raw = 0.0
        bear_raw = 0.0
        breakdown: dict[str, dict] = {}

        for cat in ("cat1", "cat2", "cat3", "cat4", "cat5", "cat6", "cat7", "cat8"):
            vote = votes.get(cat, 0)
            mult = multipliers.get(cat, 1.0)
            weighted = vote * mult

            # Cat7 already encodes ±2 in its vote, so no extra weighting needed
            if weighted > 0:
                bull_raw += weighted
            elif weighted < 0:
                bear_raw += abs(weighted)

            breakdown[cat] = {
                "vote":     vote,
                "mult":     round(mult, 2),
                "weighted": round(weighted, 3),
                "reason":   getattr(bundle, cat).reason,
            }

        # ── Normalise to 0–100 ─────────────────────────────────────────────────
        # Max bull or bear raw can exceed MAX_RAW with multipliers; cap at MAX_RAW
        effective_max = self.MAX_RAW
        bull_score = min(100.0, (bull_raw / effective_max) * 100)
        bear_score = min(100.0, (bear_raw / effective_max) * 100)

        # ── Determine direction ────────────────────────────────────────────────
        if bull_score > bear_score:
            dominant_score = bull_score
            direction = "long"
        elif bear_score > bull_score:
            dominant_score = bear_score
            direction = "short"
        else:
            dominant_score = bull_score
            direction = "neutral"

        # ── Minimum lead gap check ─────────────────────────────────────────────
        lead_gap = abs(bull_score - bear_score)
        if lead_gap < self.MIN_LEAD_GAP:
            direction = "neutral"
            dominant_score = max(bull_score, bear_score)

        # ── Map to position tier ───────────────────────────────────────────────
        if direction == "neutral":
            tier = PositionTier.NO_TRADE
        else:
            tier = self._score_to_tier(dominant_score)

        # ── Macro risk multiplier from Cat8 params ─────────────────────────────
        _MACRO_MULT = {"HIGH": 0.5, "MEDIUM": 0.75, "LOW": 1.0}
        risk_level = bundle.cat8.params.get("risk_level", "LOW").upper()
        macro_multiplier = _MACRO_MULT.get(risk_level, 1.0)

        return ConfidenceResult(
            bull_score=bull_score,
            bear_score=bear_score,
            direction=direction,
            dominant_score=dominant_score,
            position_tier=tier,
            breakdown=breakdown,
            macro_multiplier=macro_multiplier,
        )

    def _score_to_tier(self, score: float) -> PositionTier:
        for threshold, tier in self.THRESHOLDS:
            if score >= threshold:
                return tier
        return PositionTier.NO_TRADE

    def simple_signal(
        self,
        votes: dict[str, int],
        threshold: float = 55.0,
    ) -> tuple[int, float]:
        """
        Lightweight scoring for backtesting (no regime context).
        Returns (direction_int, score) where direction_int = +1/-1/0.
        """
        bull_raw = sum(v for v in votes.values() if v > 0)
        bear_raw = sum(abs(v) for v in votes.values() if v < 0)

        bull_score = min(100.0, (bull_raw / self.MAX_RAW) * 100)
        bear_score = min(100.0, (bear_raw / self.MAX_RAW) * 100)
        lead_gap   = abs(bull_score - bear_score)

        if lead_gap < self.MIN_LEAD_GAP:
            return 0, max(bull_score, bear_score)

        if bull_score >= threshold and bull_score > bear_score:
            return +1, bull_score
        if bear_score >= threshold and bear_score > bull_score:
            return -1, bear_score
        return 0, max(bull_score, bear_score)
