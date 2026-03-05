"""
agents/signal_engine.py
Orchestrates all 8 signal categories.
Each category is called independently — exceptions never crash the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from signals import SignalResult
import signals.cat1_trend_direction as cat1
import signals.cat2_trend_strength  as cat2
import signals.cat3_momentum         as cat3
import signals.cat4_volatility_band  as cat4
import signals.cat5_volume           as cat5
import signals.cat6_price_structure  as cat6
import signals.cat7_multi_timeframe  as cat7
import signals.cat8_macro_news       as cat8


@dataclass
class SignalBundle:
    symbol: str
    cat1: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    cat2: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    cat3: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    cat4: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    cat5: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    cat6: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    cat7: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    cat8: SignalResult = field(default_factory=lambda: SignalResult(0, "not run"))
    errors: dict[str, str] = field(default_factory=dict)

    def votes(self) -> dict[str, int]:
        return {
            "cat1": self.cat1.vote,
            "cat2": self.cat2.vote,
            "cat3": self.cat3.vote,
            "cat4": self.cat4.vote,
            "cat5": self.cat5.vote,
            "cat6": self.cat6.vote,
            "cat7": self.cat7.vote,
            "cat8": self.cat8.vote,
        }

    def reasons(self) -> dict[str, str]:
        return {
            "cat1": self.cat1.reason,
            "cat2": self.cat2.reason,
            "cat3": self.cat3.reason,
            "cat4": self.cat4.reason,
            "cat5": self.cat5.reason,
            "cat6": self.cat6.reason,
            "cat7": self.cat7.reason,
            "cat8": self.cat8.reason,
        }


class SignalEngine:
    """
    Evaluates all 8 signal categories for a given symbol and returns
    a SignalBundle. Exceptions in individual categories are caught and
    recorded — the engine always returns a result.
    """

    def evaluate(
        self,
        symbol: str,
        dfs: dict[str, pd.DataFrame],
    ) -> SignalBundle:
        """
        Args:
            symbol: bot-internal symbol (e.g. "NVDA", "EURUSD")
            dfs:    dict with keys "5m", "15m", "1h" → pd.DataFrame

        Returns:
            SignalBundle with all 8 category results
        """
        bundle = SignalBundle(symbol=symbol)

        df_5m  = dfs.get("5m",  pd.DataFrame())
        df_15m = dfs.get("15m", pd.DataFrame())
        df_1h  = dfs.get("1h",  pd.DataFrame())

        # ── Cat 1: Trend Direction (15m) ───────────────────────────────────────
        bundle.cat1 = self._safe_eval("cat1", cat1.evaluate, df_15m)

        # ── Cat 2: Trend Strength (15m) ────────────────────────────────────────
        bundle.cat2 = self._safe_eval("cat2", cat2.evaluate, df_15m)

        # ── Cat 3: Momentum (1h) ───────────────────────────────────────────────
        bundle.cat3 = self._safe_eval("cat3", cat3.evaluate, df_1h)

        # ── Cat 4: Volatility Band (1h) ────────────────────────────────────────
        bundle.cat4 = self._safe_eval("cat4", cat4.evaluate, df_1h)

        # ── Cat 5: Volume (5m or 1h) ───────────────────────────────────────────
        df_vol = df_5m if not df_5m.empty else df_1h
        bundle.cat5 = self._safe_eval("cat5", cat5.evaluate, df_vol)

        # ── Cat 6: Price Structure (1h) ────────────────────────────────────────
        bundle.cat6 = self._safe_eval("cat6", cat6.evaluate, df_1h)

        # ── Cat 7: Multi-Timeframe (5m + 15m + 1h) — double weight ────────────
        bundle.cat7 = self._safe_eval(
            "cat7", cat7.evaluate, df_5m, df_15m, df_1h
        )

        # ── Cat 8: Macro / News (Gemini, cached 60 min) ────────────────────────
        bundle.cat8 = self._safe_eval("cat8", cat8.evaluate, symbol)

        # Propagate errors
        bundle.errors = {k: v for k, v in bundle.errors.items() if v}
        return bundle

    @staticmethod
    def _safe_eval(
        cat_name: str,
        fn: Any,
        *args: Any,
    ) -> SignalResult:
        try:
            return fn(*args)
        except Exception as exc:
            return SignalResult(
                vote=0,
                reason=f"[{cat_name} ERROR] {type(exc).__name__}: {exc}",
                params={"error": str(exc)},
            )
