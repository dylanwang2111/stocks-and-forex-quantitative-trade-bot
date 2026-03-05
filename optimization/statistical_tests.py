"""
optimization/statistical_tests.py
Statistical significance testing for the optimization pipeline.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class BinomialTestResult:
    n_trials: int
    n_wins: int
    win_rate: float
    p_value: float
    significant: bool   # p_value < alpha
    alpha: float


@dataclass
class BonferroniResult:
    n_tests: int
    individual_alpha: float
    corrected_alpha: float              # individual_alpha / n_tests
    individual_p_values: list[float]
    significant_after_correction: list[bool]


# ---------------------------------------------------------------------------
# Internal math helpers (used when scipy is unavailable)
# ---------------------------------------------------------------------------

def _comb(n: int, k: int) -> int:
    """Binomial coefficient C(n, k). Requires Python 3.8+."""
    return math.comb(n, k)


def _binom_pmf(k: int, n: int, p: float) -> float:
    """Binomial probability mass function P(X = k)."""
    return _comb(n, k) * (p ** k) * ((1 - p) ** (n - k))


def _binomial_p_value_manual(n_wins: int, n_trials: int, p: float) -> float:
    """
    One-tailed p-value: P(X >= n_wins | n=n_trials, p=p).
    Computed as sum of PMF from n_wins to n_trials.
    """
    return sum(_binom_pmf(k, n_trials, p) for k in range(n_wins, n_trials + 1))


# ---------------------------------------------------------------------------
# Main class
# ---------------------------------------------------------------------------

class StatisticalTests:
    """Provides binomial significance testing and Bonferroni correction."""

    DEFAULT_ALPHA: float = 0.05

    def binomial_test(
        self,
        n_wins: int,
        n_trials: int,
        expected_win_rate: float = 0.5,
        alpha: float = DEFAULT_ALPHA,
    ) -> BinomialTestResult:
        """
        One-tailed binomial test: is win_rate significantly > expected_win_rate?

        Uses scipy.stats.binomtest when available; falls back to a manual
        binomial CDF computation otherwise.

        Parameters
        ----------
        n_wins:             Number of successful outcomes.
        n_trials:           Total number of trials.
        expected_win_rate:  Null-hypothesis win rate (default 0.5).
        alpha:              Significance level (default 0.05).

        Returns
        -------
        BinomialTestResult with p_value and significant flag.
        """
        if n_trials <= 0:
            raise ValueError("n_trials must be a positive integer.")
        if not (0.0 <= expected_win_rate <= 1.0):
            raise ValueError("expected_win_rate must be in [0, 1].")
        if n_wins < 0 or n_wins > n_trials:
            raise ValueError("n_wins must be in [0, n_trials].")

        win_rate = n_wins / n_trials

        try:
            from scipy.stats import binomtest as _scipy_binomtest

            result = _scipy_binomtest(n_wins, n_trials, expected_win_rate, alternative="greater")
            p_value = float(result.pvalue)
            logger.debug("binomial_test: used scipy (p=%.6f)", p_value)
        except ImportError:
            p_value = _binomial_p_value_manual(n_wins, n_trials, expected_win_rate)
            logger.debug("binomial_test: used manual CDF (p=%.6f)", p_value)

        return BinomialTestResult(
            n_trials=n_trials,
            n_wins=n_wins,
            win_rate=win_rate,
            p_value=p_value,
            significant=p_value < alpha,
            alpha=alpha,
        )

    def bonferroni_correction(
        self,
        p_values: list[float],
        alpha: float = DEFAULT_ALPHA,
    ) -> BonferroniResult:
        """
        Apply Bonferroni correction for multiple comparisons.

        corrected_alpha = alpha / n_tests
        Each individual p_value is significant if p_value < corrected_alpha.

        Parameters
        ----------
        p_values: List of individual p-values from separate hypothesis tests.
        alpha:    Family-wise error rate to control (default 0.05).

        Returns
        -------
        BonferroniResult with per-test significance flags at corrected alpha.
        """
        if not p_values:
            raise ValueError("p_values list must not be empty.")

        n_tests = len(p_values)
        corrected_alpha = alpha / n_tests
        significant_after_correction = [p < corrected_alpha for p in p_values]

        logger.debug(
            "bonferroni_correction: n_tests=%d, corrected_alpha=%.6f",
            n_tests,
            corrected_alpha,
        )

        return BonferroniResult(
            n_tests=n_tests,
            individual_alpha=alpha,
            corrected_alpha=corrected_alpha,
            individual_p_values=list(p_values),
            significant_after_correction=significant_after_correction,
        )

    def is_strategy_significant(
        self,
        n_wins: int,
        n_trials: int,
        min_trades: int = 50,
    ) -> tuple[bool, str]:
        """
        Combined significance check for a trading strategy.

        Gate 1: n_trials >= min_trades (sample size gate)
        Gate 2: One-tailed binomial test at default alpha

        Parameters
        ----------
        n_wins:     Number of winning trades.
        n_trials:   Total number of closed trades.
        min_trades: Minimum required sample size before testing (default 50).

        Returns
        -------
        (significant: bool, reason: str)
        """
        if n_trials < min_trades:
            reason = (
                f"Insufficient sample: {n_trials} trades is below the minimum "
                f"of {min_trades} required before testing significance."
            )
            logger.info("is_strategy_significant: %s", reason)
            return False, reason

        test_result = self.binomial_test(
            n_wins=n_wins,
            n_trials=n_trials,
            expected_win_rate=0.5,
            alpha=self.DEFAULT_ALPHA,
        )

        if test_result.significant:
            reason = (
                f"Strategy is statistically significant: "
                f"win_rate={test_result.win_rate:.1%}, "
                f"p_value={test_result.p_value:.4f} < alpha={test_result.alpha}."
            )
            logger.info("is_strategy_significant: %s", reason)
            return True, reason
        else:
            reason = (
                f"Strategy is NOT statistically significant: "
                f"win_rate={test_result.win_rate:.1%}, "
                f"p_value={test_result.p_value:.4f} >= alpha={test_result.alpha}."
            )
            logger.info("is_strategy_significant: %s", reason)
            return False, reason


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

def test_statistical_tests() -> None:
    """
    Unit tests for StatisticalTests.

    Cases:
    1. 30/50 wins (60%) vs p=0.5 → significant (p < 0.05)
    2. 26/50 wins (52%) vs p=0.5 → NOT significant
    3. Bonferroni: 5 tests each with p=0.04, alpha=0.05 →
       corrected_alpha=0.01 → NOT significant
    4. Bonferroni: 3 tests each with p=0.001, alpha=0.05 →
       corrected_alpha=0.0167 → all significant
    5. is_strategy_significant: n_trials=30 (< 50 min) → (False, reason mentions "min trades")
    """
    print("Running test_statistical_tests...")
    st = StatisticalTests()

    # --- Case 1: 35/50 wins (70%) → significant (p << 0.05) ---
    # Note: 30/50 gives p≈0.10 (not significant). Need 35+ for p<0.05 with n=50.
    r1 = st.binomial_test(n_wins=35, n_trials=50)
    assert r1.significant, (
        f"Case 1 FAILED: expected significant for 35/50 wins, got p={r1.p_value:.6f}"
    )
    assert r1.p_value < 0.05, f"Case 1 FAILED: p={r1.p_value} not < 0.05"
    print(f"Case 1 PASSED: 35/50 wins, p={r1.p_value:.6f}, significant={r1.significant}")

    # --- Case 2: 26/50 wins → NOT significant ---
    r2 = st.binomial_test(n_wins=26, n_trials=50)
    assert not r2.significant, (
        f"Case 2 FAILED: expected NOT significant for 26/50 wins, got p={r2.p_value:.6f}"
    )
    print(f"Case 2 PASSED: 26/50 wins, p={r2.p_value:.6f}, significant={r2.significant}")

    # --- Case 3: Bonferroni, 5 tests p=0.04 → corrected alpha=0.01 → NOT significant ---
    p_values_3 = [0.04] * 5
    b3 = st.bonferroni_correction(p_values=p_values_3, alpha=0.05)
    assert abs(b3.corrected_alpha - 0.01) < 1e-9, (
        f"Case 3 FAILED: expected corrected_alpha=0.01, got {b3.corrected_alpha}"
    )
    assert not any(b3.significant_after_correction), (
        f"Case 3 FAILED: expected all non-significant, got {b3.significant_after_correction}"
    )
    print(
        f"Case 3 PASSED: 5 tests p=0.04, corrected_alpha={b3.corrected_alpha:.4f}, "
        f"significant={b3.significant_after_correction}"
    )

    # --- Case 4: Bonferroni, 3 tests p=0.001 → all significant ---
    p_values_4 = [0.001] * 3
    b4 = st.bonferroni_correction(p_values=p_values_4, alpha=0.05)
    corrected_alpha_4 = 0.05 / 3
    assert abs(b4.corrected_alpha - corrected_alpha_4) < 1e-9, (
        f"Case 4 FAILED: expected corrected_alpha={corrected_alpha_4:.6f}, got {b4.corrected_alpha}"
    )
    assert all(b4.significant_after_correction), (
        f"Case 4 FAILED: expected all significant, got {b4.significant_after_correction}"
    )
    print(
        f"Case 4 PASSED: 3 tests p=0.001, corrected_alpha={b4.corrected_alpha:.6f}, "
        f"significant={b4.significant_after_correction}"
    )

    # --- Case 5: is_strategy_significant with n_trials=30 < 50 min ---
    sig5, reason5 = st.is_strategy_significant(n_wins=20, n_trials=30, min_trades=50)
    assert sig5 is False, f"Case 5 FAILED: expected False, got {sig5}"
    assert "min" in reason5.lower(), (
        f"Case 5 FAILED: expected 'min' in reason, got: {reason5}"
    )
    print(f"Case 5 PASSED: n_trials=30 < 50, significant={sig5}, reason='{reason5}'")

    print("All test_statistical_tests assertions passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    test_statistical_tests()
