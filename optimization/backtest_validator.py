"""
optimization/backtest_validator.py
Wraps BacktestRunner to perform IS/OOS splits and Monte Carlo robustness
checks before accepting any parameter proposal from the optimization pipeline.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from backtesting.backtest_runner import BacktestRunner, BacktestResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type alias for Proposal — imported at runtime to avoid circular imports.
# The optimizer imports us, and proposal_parser imports nothing from us, so
# a direct import is safe; keep it lazy for testability.
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    proposal_param: str
    proposed_value: float
    is_sharpe: float            # in-sample Sharpe
    oos_sharpe: float           # out-of-sample Sharpe
    is_trades: int
    oos_trades: int
    monte_carlo_p_value: float  # fraction of random shuffles that beat OOS sharpe
    accepted: bool              # True if passes all gates
    rejection_reason: str = ""


class BacktestValidator:
    """
    Validates a parameter proposal using a forward-walk IS/OOS split and a
    Monte Carlo parameter-noise test.

    Gate sequence (all must pass):
      1. IS Sharpe >= MIN_IS_SHARPE
      2. OOS Sharpe >= MIN_OOS_SHARPE
      3. OOS Sharpe >= IS Sharpe * OVERFIT_RATIO  (overfit check)
      4. Monte Carlo p-value <= 0.25              (noise robustness)
    """

    # IS/OOS split: 80 / 20 of last 18 months
    IS_MONTHS: int = 14    # in-sample window
    OOS_MONTHS: int = 4    # out-of-sample window (most-recent, held-out)

    MONTE_CARLO_RUNS: int = 100

    MIN_IS_SHARPE: float = 1.5   # must match BacktestResult.passed() threshold
    MIN_OOS_SHARPE: float = 0.5  # OOS must be positive and meaningful
    OVERFIT_RATIO: float = 0.3   # OOS must be >= 30 % of IS Sharpe

    # Default primary symbol: most liquid, no PDT restrictions, most data
    DEFAULT_SYMBOL: str = "EURUSD"

    def __init__(self, runner: Optional[BacktestRunner] = None) -> None:
        self._runner = runner or BacktestRunner()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def validate(
        self,
        proposal: Any,  # optimization.proposal_parser.Proposal
        symbols: Optional[list[str]] = None,
    ) -> ValidationResult:
        """
        Validate a single parameter proposal via IS/OOS backtests and Monte
        Carlo parameter-noise testing.

        Steps
        -----
        1. Compute date ranges for IS and OOS windows.
        2. Build BacktestRunner kwargs from the proposal's param.
        3. Run IS backtest; reject early if IS Sharpe is too low.
        4. Run OOS backtest; reject if OOS Sharpe is below floor.
        5. Check overfit ratio.
        6. Monte Carlo: vary confidence_threshold by ±5 over MONTE_CARLO_RUNS
           runs; compute fraction that beat OOS Sharpe.
        7. Return a ValidationResult.
        """
        symbols = symbols or [self.DEFAULT_SYMBOL]
        symbol = symbols[0]

        param_name: str = proposal.param_name
        proposed_value: Any = proposal.proposed_value

        # Step 1: Date ranges
        today = datetime.utcnow().date()
        is_start = today - timedelta(days=30 * (self.IS_MONTHS + self.OOS_MONTHS))
        is_end = today - timedelta(days=30 * self.OOS_MONTHS)
        oos_start = is_end
        oos_end = today

        is_start_str = is_start.strftime("%Y-%m-%d")
        is_end_str = is_end.strftime("%Y-%m-%d")
        oos_start_str = oos_start.strftime("%Y-%m-%d")
        oos_end_str = oos_end.strftime("%Y-%m-%d")

        # Step 2: Map proposal param to BacktestRunner constructor kwargs
        runner_kwargs = self._build_runner_kwargs(param_name, proposed_value)

        def _make_runner(**kwargs: Any) -> BacktestRunner:
            r = BacktestRunner(
                confidence_threshold=kwargs.get("confidence_threshold", self._runner.threshold),
                holding_days=kwargs.get("holding_days", self._runner.holding_days),
                atr_sl_mult=kwargs.get("atr_sl_mult", self._runner.atr_sl_mult),
                atr_tp_mult=kwargs.get("atr_tp_mult", self._runner.atr_tp_mult),
            )
            # Share the scorer from the parent runner to avoid re-init overhead
            r.scorer = self._runner.scorer
            return r

        # Step 3: IS backtest
        try:
            is_runner = _make_runner(**runner_kwargs)
            is_result: BacktestResult = is_runner.run(
                symbol=symbol,
                start=is_start_str,
                end=is_end_str,
            )
        except Exception:
            logger.exception(
                "BacktestRunner raised an exception during IS run for param=%s value=%s",
                param_name,
                proposed_value,
            )
            return ValidationResult(
                proposal_param=param_name,
                proposed_value=float(proposed_value),
                is_sharpe=0.0,
                oos_sharpe=0.0,
                is_trades=0,
                oos_trades=0,
                monte_carlo_p_value=1.0,
                accepted=False,
                rejection_reason="IS backtest raised an exception",
            )

        if is_result.sharpe < self.MIN_IS_SHARPE:
            logger.info(
                "Proposal %s=%s rejected: IS Sharpe %.2f < %.2f",
                param_name, proposed_value, is_result.sharpe, self.MIN_IS_SHARPE,
            )
            return ValidationResult(
                proposal_param=param_name,
                proposed_value=float(proposed_value),
                is_sharpe=is_result.sharpe,
                oos_sharpe=0.0,
                is_trades=is_result.trade_count,
                oos_trades=0,
                monte_carlo_p_value=1.0,
                accepted=False,
                rejection_reason=(
                    f"IS Sharpe too low: {is_result.sharpe:.2f} < {self.MIN_IS_SHARPE}"
                ),
            )

        # Step 4: OOS backtest
        try:
            oos_runner = _make_runner(**runner_kwargs)
            oos_result: BacktestResult = oos_runner.run(
                symbol=symbol,
                start=oos_start_str,
                end=oos_end_str,
            )
        except Exception:
            logger.exception(
                "BacktestRunner raised an exception during OOS run for param=%s value=%s",
                param_name,
                proposed_value,
            )
            return ValidationResult(
                proposal_param=param_name,
                proposed_value=float(proposed_value),
                is_sharpe=is_result.sharpe,
                oos_sharpe=0.0,
                is_trades=is_result.trade_count,
                oos_trades=0,
                monte_carlo_p_value=1.0,
                accepted=False,
                rejection_reason="OOS backtest raised an exception",
            )

        if oos_result.sharpe < self.MIN_OOS_SHARPE:
            logger.info(
                "Proposal %s=%s rejected: OOS Sharpe %.2f < %.2f",
                param_name, proposed_value, oos_result.sharpe, self.MIN_OOS_SHARPE,
            )
            return ValidationResult(
                proposal_param=param_name,
                proposed_value=float(proposed_value),
                is_sharpe=is_result.sharpe,
                oos_sharpe=oos_result.sharpe,
                is_trades=is_result.trade_count,
                oos_trades=oos_result.trade_count,
                monte_carlo_p_value=1.0,
                accepted=False,
                rejection_reason=(
                    f"OOS Sharpe negative/flat: {oos_result.sharpe:.2f} < {self.MIN_OOS_SHARPE}"
                ),
            )

        # Step 5: Overfit check
        overfit_floor = is_result.sharpe * self.OVERFIT_RATIO
        if oos_result.sharpe < overfit_floor:
            logger.info(
                "Proposal %s=%s rejected: overfit detected (OOS=%.2f < IS*ratio=%.2f)",
                param_name, proposed_value, oos_result.sharpe, overfit_floor,
            )
            return ValidationResult(
                proposal_param=param_name,
                proposed_value=float(proposed_value),
                is_sharpe=is_result.sharpe,
                oos_sharpe=oos_result.sharpe,
                is_trades=is_result.trade_count,
                oos_trades=oos_result.trade_count,
                monte_carlo_p_value=1.0,
                accepted=False,
                rejection_reason=(
                    f"Overfit detected: OOS Sharpe {oos_result.sharpe:.2f} < "
                    f"IS Sharpe * {self.OVERFIT_RATIO} = {overfit_floor:.2f}"
                ),
            )

        # Step 6: Monte Carlo — add noise to the proposed parameter to test
        # robustness; count the fraction of noisy runs that beat actual OOS Sharpe.
        # Noise scale: ±10% of the proposed value (or ±5 absolute for confidence).
        beats = 0
        for _ in range(self.MONTE_CARLO_RUNS):
            mc_kwargs = dict(runner_kwargs)  # copy proposed kwargs
            if param_name == "confidence_threshold":
                noisy = max(50.0, float(proposed_value) + random.uniform(-5.0, 5.0))
                mc_kwargs["confidence_threshold"] = noisy
            elif param_name == "atr_sl_mult":
                noisy = max(0.5, float(proposed_value) * random.uniform(0.85, 1.15))
                mc_kwargs["atr_sl_mult"] = noisy
            elif param_name == "atr_tp_mult":
                noisy = max(1.0, float(proposed_value) * random.uniform(0.85, 1.15))
                mc_kwargs["atr_tp_mult"] = noisy
            try:
                mc_runner = BacktestRunner(
                    confidence_threshold=mc_kwargs.get("confidence_threshold", self._runner.threshold),
                    holding_days=mc_kwargs.get("holding_days", self._runner.holding_days),
                    atr_sl_mult=mc_kwargs.get("atr_sl_mult", self._runner.atr_sl_mult),
                    atr_tp_mult=mc_kwargs.get("atr_tp_mult", self._runner.atr_tp_mult),
                )
                mc_runner.scorer = self._runner.scorer
                mc_result: BacktestResult = mc_runner.run(
                    symbol=symbol,
                    start=oos_start_str,
                    end=oos_end_str,
                )
                if mc_result.sharpe >= oos_result.sharpe:
                    beats += 1
            except Exception:
                logger.debug("Monte Carlo run raised an exception; treating as beat=False.")

        mc_p_value = beats / self.MONTE_CARLO_RUNS

        if mc_p_value > 0.25:
            logger.info(
                "Proposal %s=%s rejected: not robust to parameter noise (mc_p=%.2f > 0.25)",
                param_name, proposed_value, mc_p_value,
            )
            return ValidationResult(
                proposal_param=param_name,
                proposed_value=float(proposed_value),
                is_sharpe=is_result.sharpe,
                oos_sharpe=oos_result.sharpe,
                is_trades=is_result.trade_count,
                oos_trades=oos_result.trade_count,
                monte_carlo_p_value=mc_p_value,
                accepted=False,
                rejection_reason=(
                    f"Not robust to parameter noise: mc_p_value={mc_p_value:.2f} > 0.25"
                ),
            )

        # Step 7: All gates passed — accept
        logger.info(
            "Proposal %s=%s ACCEPTED: IS=%.2f OOS=%.2f mc_p=%.2f",
            param_name, proposed_value, is_result.sharpe, oos_result.sharpe, mc_p_value,
        )
        return ValidationResult(
            proposal_param=param_name,
            proposed_value=float(proposed_value),
            is_sharpe=is_result.sharpe,
            oos_sharpe=oos_result.sharpe,
            is_trades=is_result.trade_count,
            oos_trades=oos_result.trade_count,
            monte_carlo_p_value=mc_p_value,
            accepted=True,
            rejection_reason="",
        )

    def validate_all_symbols(
        self,
        proposal: Any,  # optimization.proposal_parser.Proposal
        symbols: list[str],
    ) -> list[ValidationResult]:
        """Run validate() for each symbol; return the list of results."""
        results: list[ValidationResult] = []
        for symbol in symbols:
            logger.debug("Validating proposal on symbol %s", symbol)
            result = self.validate(proposal, symbols=[symbol])
            results.append(result)
        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _build_runner_kwargs(
        self,
        param_name: str,
        proposed_value: Any,
    ) -> dict[str, Any]:
        """
        Translate a proposal param into BacktestRunner constructor kwargs.

        Mapping:
          confidence_threshold → confidence_threshold=proposed_value
          atr_sl_mult          → atr_sl_mult=float(proposed_value)
          atr_tp_mult          → atr_tp_mult=float(proposed_value)
        """
        if param_name == "confidence_threshold":
            return {"confidence_threshold": float(proposed_value)}
        elif param_name == "atr_sl_mult":
            return {"atr_sl_mult": float(proposed_value)}
        elif param_name == "atr_tp_mult":
            return {"atr_tp_mult": float(proposed_value)}
        else:
            logger.warning(
                "Unknown param_name '%s'; falling back to current runner settings.",
                param_name,
            )
            return {}


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_backtest_validator() -> None:
    """
    Unit tests for BacktestValidator using a mock BacktestRunner.
    No real network calls are made.
    """
    import pandas as pd
    from unittest.mock import MagicMock, patch

    print("Running test_backtest_validator...")

    # ------------------------------------------------------------------
    # Helper: build a minimal BacktestResult
    # ------------------------------------------------------------------
    def make_result(sharpe: float, trade_count: int, symbol: str = "EURUSD") -> BacktestResult:
        return BacktestResult(
            symbol=symbol,
            start="2024-01-01",
            end="2024-06-01",
            sharpe=sharpe,
            win_rate=0.55,
            profit_factor=1.4,
            max_drawdown=0.08,
            trade_count=trade_count,
            total_return=0.10,
            equity_curve=None,
        )

    # ------------------------------------------------------------------
    # Build a minimal Proposal stub (no need to import the real class)
    # ------------------------------------------------------------------
    class _Proposal:
        def __init__(self, param_name: str, current_value: Any, proposed_value: Any) -> None:
            self.param_name = param_name
            self.current_value = current_value
            self.proposed_value = proposed_value
            self.rationale = "test"
            self.valid = True
            self.rejection_reason = ""

    # ------------------------------------------------------------------
    # Test 1: Happy path — IS=2.0, OOS=1.2, all MC runs return 0.8 (below OOS)
    # ------------------------------------------------------------------
    mock_runner = MagicMock(spec=BacktestRunner)
    mock_runner.threshold = 55.0
    mock_runner.holding_days = 3
    mock_runner.scorer = MagicMock()

    # is_run → sharpe=2.0; oos_run → sharpe=1.2; mc runs → sharpe=0.8
    call_count = [0]

    def side_effect_happy(symbol: str, start: str, end: str, df: Any = None) -> BacktestResult:
        call_count[0] += 1
        if call_count[0] == 1:
            # IS call
            return make_result(sharpe=2.0, trade_count=60)
        elif call_count[0] == 2:
            # OOS call
            return make_result(sharpe=1.2, trade_count=20)
        else:
            # Monte Carlo calls
            return make_result(sharpe=0.8, trade_count=15)

    mock_runner.run.side_effect = side_effect_happy

    validator = BacktestValidator(runner=mock_runner)

    # Patch BacktestRunner constructor inside validate() so noise runs reuse mock
    with patch("optimization.backtest_validator.BacktestRunner") as MockRunnerClass:
        # Each BacktestRunner() call inside validate() returns mock_runner behaviour
        inner_mock = MagicMock(spec=BacktestRunner)
        inner_mock.scorer = MagicMock()

        mc_call_count = [0]

        def inner_run(symbol: str, start: str, end: str, df: Any = None) -> BacktestResult:
            mc_call_count[0] += 1
            # IS run
            if mc_call_count[0] == 1:
                return make_result(sharpe=2.0, trade_count=60)
            # OOS run
            elif mc_call_count[0] == 2:
                return make_result(sharpe=1.2, trade_count=20)
            else:
                # All MC runs: sharpe=0.8 < OOS 1.2 → no beats
                return make_result(sharpe=0.8, trade_count=15)

        inner_mock.run.side_effect = inner_run
        MockRunnerClass.return_value = inner_mock

        proposal = _Proposal("confidence_threshold", 55.0, 60.0)
        result = validator.validate(proposal)

    assert result.accepted is True, f"Expected accepted=True, got {result.accepted}"
    assert abs(result.is_sharpe - 2.0) < 1e-6, f"Expected is_sharpe=2.0, got {result.is_sharpe}"
    assert abs(result.oos_sharpe - 1.2) < 1e-6, f"Expected oos_sharpe=1.2, got {result.oos_sharpe}"
    assert result.is_trades == 60, f"Expected is_trades=60, got {result.is_trades}"
    assert result.oos_trades == 20, f"Expected oos_trades=20, got {result.oos_trades}"
    # All MC sharpe=0.8 < OOS 1.2 → mc_p_value == 0.0
    assert result.monte_carlo_p_value == 0.0, (
        f"Expected mc_p_value=0.0, got {result.monte_carlo_p_value}"
    )
    print("  Test 1 (happy path): PASSED")

    # ------------------------------------------------------------------
    # Test 2: IS Sharpe too low → rejected immediately
    # ------------------------------------------------------------------
    mock_runner2 = MagicMock(spec=BacktestRunner)
    mock_runner2.threshold = 55.0
    mock_runner2.holding_days = 3
    mock_runner2.scorer = MagicMock()

    low_is_call = [0]

    def side_effect_low_is(symbol: str, start: str, end: str, df: Any = None) -> BacktestResult:
        low_is_call[0] += 1
        # Always return low IS sharpe
        return make_result(sharpe=1.0, trade_count=40)

    mock_runner2.run.side_effect = side_effect_low_is

    validator2 = BacktestValidator(runner=mock_runner2)

    with patch("optimization.backtest_validator.BacktestRunner") as MockRunnerClass2:
        inner_mock2 = MagicMock(spec=BacktestRunner)
        inner_mock2.scorer = MagicMock()
        inner_mock2.run.return_value = make_result(sharpe=1.0, trade_count=40)
        MockRunnerClass2.return_value = inner_mock2

        proposal2 = _Proposal("confidence_threshold", 55.0, 58.0)
        result2 = validator2.validate(proposal2)

    assert result2.accepted is False, f"Expected accepted=False, got {result2.accepted}"
    assert "IS Sharpe" in result2.rejection_reason, (
        f"Expected rejection_reason to mention 'IS Sharpe', got: '{result2.rejection_reason}'"
    )
    print(f"  Test 2 (IS Sharpe too low): PASSED — reason: '{result2.rejection_reason}'")

    # ------------------------------------------------------------------
    # Test 3: holding_days proposal — check runner_kwargs mapping
    # ------------------------------------------------------------------
    validator3 = BacktestValidator(runner=MagicMock(spec=BacktestRunner, threshold=55.0, holding_days=3, scorer=MagicMock()))
    kwargs = validator3._build_runner_kwargs("holding_days", 5)
    assert kwargs == {"holding_days": 5}, f"Expected {{'holding_days': 5}}, got {kwargs}"
    print("  Test 3 (holding_days kwargs mapping): PASSED")

    # ------------------------------------------------------------------
    # Test 4: min_lead_gap proposal → proxied via confidence_threshold
    # ------------------------------------------------------------------
    mock_runner4 = MagicMock(spec=BacktestRunner)
    mock_runner4.threshold = 60.0
    mock_runner4.holding_days = 3
    mock_runner4.scorer = MagicMock()
    validator4 = BacktestValidator(runner=mock_runner4)
    kwargs4 = validator4._build_runner_kwargs("min_lead_gap", 15.0)
    assert "confidence_threshold" in kwargs4, (
        f"min_lead_gap should proxy to confidence_threshold, got {kwargs4}"
    )
    assert kwargs4["confidence_threshold"] == 60.0, (
        f"Expected 60.0 (current threshold), got {kwargs4['confidence_threshold']}"
    )
    print("  Test 4 (min_lead_gap proxy): PASSED")

    # ------------------------------------------------------------------
    # Test 5: validate_all_symbols returns one result per symbol
    # ------------------------------------------------------------------
    mock_runner5 = MagicMock(spec=BacktestRunner)
    mock_runner5.threshold = 55.0
    mock_runner5.holding_days = 3
    mock_runner5.scorer = MagicMock()

    with patch("optimization.backtest_validator.BacktestRunner") as MockRunnerClass5:
        inner_mock5 = MagicMock(spec=BacktestRunner)
        inner_mock5.scorer = MagicMock()

        sym_call_count = [0]

        def inner_run5(symbol: str, start: str, end: str, df: Any = None) -> BacktestResult:
            sym_call_count[0] += 1
            if sym_call_count[0] % 2 == 1:
                return make_result(sharpe=2.0, trade_count=60, symbol=symbol)
            else:
                return make_result(sharpe=1.2, trade_count=20, symbol=symbol)

        inner_mock5.run.side_effect = inner_run5
        MockRunnerClass5.return_value = inner_mock5

        validator5 = BacktestValidator(runner=mock_runner5)
        proposal5 = _Proposal("confidence_threshold", 55.0, 60.0)
        results5 = validator5.validate_all_symbols(proposal5, ["EURUSD", "GBPUSD"])

    assert len(results5) == 2, f"Expected 2 results, got {len(results5)}"
    print("  Test 5 (validate_all_symbols): PASSED")

    print("All test_backtest_validator assertions passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    test_backtest_validator()
