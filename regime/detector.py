"""
regime/detector.py
Market regime detection using ADX, EMA crossovers, ATR, and Bollinger Band width.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import pandas as pd
import pandas_ta as ta


class Regime(Enum):
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"


@dataclass
class RegimeContext:
    regime: Regime
    adx: float
    ema20_above_ema50: bool
    atr: float
    atr_ratio: float          # current ATR / 30-day avg ATR
    bb_width: float
    bb_width_pct_rank: float  # percentile rank vs 6-month history (0–1)
    description: str

    def to_dict(self) -> dict:
        return {
            "regime": self.regime.value,
            "adx": round(self.adx, 2),
            "ema20_above_ema50": self.ema20_above_ema50,
            "atr": round(self.atr, 5),
            "atr_ratio": round(self.atr_ratio, 2),
            "bb_width": round(self.bb_width, 5),
            "bb_width_pct_rank": round(self.bb_width_pct_rank, 2),
            "description": self.description,
        }

    # ── Regime multipliers for confidence scoring ──────────────────────────────
    def signal_multiplier(self) -> dict[str, float]:
        """
        Per-category multiplier applied during confidence scoring.
        1.0 = no change, >1.0 = boost, <1.0 = dampen.
        """
        if self.regime == Regime.TRENDING_UP:
            return {
                "cat1": 1.2, "cat2": 1.2, "cat3": 1.0,
                "cat4": 0.8, "cat5": 1.1, "cat6": 1.0,
                "cat7": 1.3, "cat8": 1.0,
            }
        elif self.regime == Regime.TRENDING_DOWN:
            return {
                "cat1": 1.2, "cat2": 1.2, "cat3": 1.0,
                "cat4": 0.8, "cat5": 1.1, "cat6": 1.0,
                "cat7": 1.3, "cat8": 1.0,
            }
        elif self.regime == Regime.RANGING:
            return {
                "cat1": 0.7, "cat2": 0.6, "cat3": 1.2,
                "cat4": 1.3, "cat5": 0.9, "cat6": 1.4,
                "cat7": 0.7, "cat8": 1.0,
            }
        elif self.regime == Regime.HIGH_VOLATILITY:
            # Reduce all signal weights — unpredictable environment
            return {k: 0.7 for k in ("cat1","cat2","cat3","cat4","cat5","cat6","cat7","cat8")}
        else:  # LOW_VOLATILITY
            # Muted moves; dampen directional signals, boost range plays
            return {
                "cat1": 0.9, "cat2": 0.8, "cat3": 1.1,
                "cat4": 1.2, "cat5": 0.8, "cat6": 1.2,
                "cat7": 0.9, "cat8": 1.0,
            }


class RegimeDetector:
    """Detect market regime from 1-hour OHLCV data."""

    def detect(self, df_1h: pd.DataFrame) -> RegimeContext:
        """
        Analyse df_1h (must have ≥ 200 rows for reliable indicators).
        Returns RegimeContext with regime enum + supporting metrics.
        """
        if len(df_1h) < 50:
            raise ValueError(
                f"Need ≥ 50 1h candles for regime detection, got {len(df_1h)}."
            )

        df = df_1h.copy()

        # ── ADX(14) ────────────────────────────────────────────────────────────
        adx_df = ta.adx(df["high"], df["low"], df["close"], length=14)
        adx = float(adx_df["ADX_14"].iloc[-1]) if adx_df is not None else 20.0

        # ── EMA crossover ──────────────────────────────────────────────────────
        ema20 = ta.ema(df["close"], length=20)
        ema50 = ta.ema(df["close"], length=50)
        ema20_val = float(ema20.iloc[-1]) if ema20 is not None else float(df["close"].iloc[-1])
        ema50_val = float(ema50.iloc[-1]) if ema50 is not None else float(df["close"].iloc[-1])
        ema20_above = ema20_val > ema50_val

        # ── ATR(14) vs 30-day average ──────────────────────────────────────────
        atr_series = ta.atr(df["high"], df["low"], df["close"], length=14)
        current_atr = float(atr_series.iloc[-1]) if atr_series is not None else 0.001

        # 30-day average ATR (30 * 24 = 720 1h bars; use available if fewer)
        lookback = min(720, len(atr_series) - 1)
        avg_atr_30d = float(atr_series.iloc[-lookback:].mean()) if lookback > 0 else current_atr
        atr_ratio = current_atr / avg_atr_30d if avg_atr_30d > 0 else 1.0

        # ── Bollinger Band width vs 6-month low ────────────────────────────────
        bb = ta.bbands(df["close"], length=20, std=2)
        if bb is not None and "BBB_20_2.0" in bb.columns:
            bb_width_series = bb["BBB_20_2.0"]
        else:
            # Fallback: compute manually
            bb_width_series = (df["close"].rolling(20).std() * 2) / df["close"].rolling(20).mean()

        current_bw = float(bb_width_series.iloc[-1])

        # 6-month percentile rank (6 * 30 * 24 = 4320 bars; use available)
        history_bars = min(4320, len(bb_width_series))
        history = bb_width_series.iloc[-history_bars:].dropna()
        bb_pct_rank = float((history < current_bw).mean()) if len(history) > 0 else 0.5

        # ── Regime classification ──────────────────────────────────────────────
        regime, description = self._classify(
            adx=adx,
            ema20_above=ema20_above,
            atr_ratio=atr_ratio,
            bb_pct_rank=bb_pct_rank,
        )

        return RegimeContext(
            regime=regime,
            adx=adx,
            ema20_above_ema50=ema20_above,
            atr=current_atr,
            atr_ratio=atr_ratio,
            bb_width=current_bw,
            bb_width_pct_rank=bb_pct_rank,
            description=description,
        )

    @staticmethod
    def _classify(
        adx: float,
        ema20_above: bool,
        atr_ratio: float,
        bb_pct_rank: float,
    ) -> tuple[Regime, str]:
        # High volatility: ATR spike > 1.8x average OR BB width > 90th percentile
        if atr_ratio > 1.8 or bb_pct_rank > 0.90:
            return (
                Regime.HIGH_VOLATILITY,
                f"Elevated volatility (ATR ratio={atr_ratio:.2f}, BB rank={bb_pct_rank:.2f})",
            )

        # Low volatility: BB width < 10th percentile AND ADX < 20
        if bb_pct_rank < 0.10 and adx < 20:
            return (
                Regime.LOW_VOLATILITY,
                f"Compressed volatility (BB rank={bb_pct_rank:.2f}, ADX={adx:.1f})",
            )

        # Strong trend: ADX > 25
        if adx > 25:
            if ema20_above:
                return (
                    Regime.TRENDING_UP,
                    f"Trending up (ADX={adx:.1f}, EMA20 > EMA50)",
                )
            else:
                return (
                    Regime.TRENDING_DOWN,
                    f"Trending down (ADX={adx:.1f}, EMA20 < EMA50)",
                )

        # Default: ranging market
        return (
            Regime.RANGING,
            f"Ranging (ADX={adx:.1f}, no clear trend)",
        )
