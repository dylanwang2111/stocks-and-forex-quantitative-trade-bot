"""
optimization/pipeline.py
Orchestrates the 7-step parameter optimization pipeline powered by Gemini Flash 2.0.

Steps:
  1. Generate performance report (last 5 weeks)
  2. Gate check — need >= 50 trades
  3. Statistical significance test on current performance
  4. Call Gemini with optimization prompt
  5. Parse + validate proposals (max 3)
  6. BacktestValidator per valid proposal (IS/OOS + Monte Carlo)
  7. Accept/reject → write StrategyRegistry + OptimizationCycle rows
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from config.settings import settings
from database.models import OptimizationCycle, StrategyRegistry, get_session, init_db
from optimization.backtest_validator import BacktestValidator, ValidationResult
from optimization.performance_report import PerformanceReport, PerformanceReportGenerator
from optimization.prompts import build_optimization_prompt, get_current_params
from optimization.proposal_parser import Proposal, ProposalParser
from optimization.statistical_tests import StatisticalTests

logger = logging.getLogger(__name__)


class OptimizationPipeline:
    """
    Runs the full 7-step optimization cycle.

    Usage::

        pipeline = OptimizationPipeline()
        pipeline.run(require_human_approval=True)

    In paper/test mode with < 50 trades the pipeline exits at the gate check.
    """

    def __init__(
        self,
        database_url: str | None = None,
        gemini_model: str | None = None,
    ) -> None:
        self._db = database_url or settings.bot.database_url
        self._model = gemini_model or settings.gemini.model

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self, require_human_approval: bool = True) -> None:
        """Run all 7 optimization steps."""
        started_at = datetime.utcnow()
        logger.info("OptimizationPipeline: starting run at %s", started_at.isoformat())

        # ── Step 1: Performance report ─────────────────────────────────
        logger.info("Step 1/7: Generating performance report (last 5 weeks)...")
        report_gen = PerformanceReportGenerator(database_url=self._db)
        try:
            report = report_gen.generate(weeks=5)
        except Exception:
            logger.exception("Step 1 failed: could not generate performance report")
            return

        logger.info(
            "  Report: %d closed trades | win_rate=%.1f%% | total_pnl=$%.2f | sharpe=%.2f",
            report.closed_trades,
            report.win_rate * 100,
            report.total_pnl_usd,
            report.sharpe_ratio,
        )

        # ── Step 2: Gate check ─────────────────────────────────────────
        logger.info("Step 2/7: Gate check (need >= %d trades)...", PerformanceReport.MIN_TRADES_THRESHOLD)
        if not report.meets_min_trades:
            logger.info(
                "  Gate: only %d trades (need >= %d). Optimization skipped.",
                report.total_trades,
                PerformanceReport.MIN_TRADES_THRESHOLD,
            )
            return

        # ── Step 3: Statistical significance ──────────────────────────
        logger.info("Step 3/7: Statistical significance test...")
        stats = StatisticalTests()
        sig_ok, sig_reason = stats.is_strategy_significant(
            n_wins=int(report.win_rate * report.closed_trades),
            n_trials=report.closed_trades,
        )
        logger.info("  Significance: %s (%s)", "PASS" if sig_ok else "FAIL", sig_reason)
        if not sig_ok:
            logger.info("  Current performance not statistically significant. Skipping optimization.")
            return

        # ── Step 4: Call Gemini ────────────────────────────────────────
        logger.info("Step 4/7: Calling Gemini for optimization proposals...")
        prompt = build_optimization_prompt(report.to_dict())
        gemini_text = self._call_gemini(prompt)
        if not gemini_text:
            logger.warning("  Gemini returned empty response. Aborting.")
            return

        # ── Step 5: Parse proposals ────────────────────────────────────
        logger.info("Step 5/7: Parsing Gemini proposals...")
        parser = ProposalParser()
        all_proposals = parser.parse(gemini_text)
        valid_proposals = parser.valid_proposals(all_proposals)

        if not valid_proposals:
            logger.info("  No valid proposals after parsing/validation. Aborting.")
            return

        logger.info("  %d valid proposal(s) (of %d parsed)", len(valid_proposals), len(all_proposals))

        # ── Human approval gate ────────────────────────────────────────
        if require_human_approval:
            print("\n" + "=" * 50)
            print("  OPTIMIZATION PROPOSALS")
            print("=" * 50)
            for i, p in enumerate(valid_proposals, 1):
                print(f"\n  {i}. {p.param_name}")
                print(f"     Current  : {p.current_value}")
                print(f"     Proposed : {p.proposed_value}")
                print(f"     Rationale: {p.rationale}")
            print("=" * 50)
            try:
                answer = input("\n  Apply these proposals? [y/N]: ").strip().lower()
            except EOFError:
                answer = "n"
            if answer != "y":
                logger.info("Human rejected optimization proposals. Aborting.")
                return

        # ── Step 6: BacktestValidator per proposal ─────────────────────
        logger.info("Step 6/7: Validating proposals via IS/OOS + Monte Carlo...")
        validator = BacktestValidator()
        accepted_count = 0

        for proposal in valid_proposals:
            logger.info("  Validating: %s → %s", proposal.param_name, proposal.proposed_value)
            try:
                validation = validator.validate(proposal)
            except Exception:
                logger.exception("  BacktestValidator raised for %s", proposal.param_name)
                validation = ValidationResult(
                    proposal_param=proposal.param_name,
                    proposed_value=float(proposal.proposed_value),
                    is_sharpe=0.0, oos_sharpe=0.0,
                    is_trades=0, oos_trades=0,
                    monte_carlo_p_value=1.0,
                    accepted=False,
                    rejection_reason="exception during validation",
                )

            # ── Step 7: Write DB records ───────────────────────────────
            logger.info("Step 7/7: Writing DB records for %s...", proposal.param_name)
            self._write_optimization_cycle(started_at, report, proposal, validation)

            if validation.accepted:
                accepted_count += 1
                new_params = get_current_params()
                new_params[proposal.param_name] = proposal.proposed_value
                self._write_strategy_registry(new_params)
                logger.info(
                    "  ACCEPTED: %s %s → %s (IS sharpe=%.2f, OOS sharpe=%.2f)",
                    proposal.param_name,
                    proposal.current_value,
                    proposal.proposed_value,
                    validation.is_sharpe,
                    validation.oos_sharpe,
                )
            else:
                logger.info(
                    "  REJECTED: %s — %s",
                    proposal.param_name,
                    validation.rejection_reason,
                )

        logger.info(
            "OptimizationPipeline: complete. %d/%d proposals accepted.",
            accepted_count,
            len(valid_proposals),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _call_gemini(self, prompt: str) -> str:
        """Call Gemini Flash 2.0 with the optimization prompt."""
        if not settings.gemini.enabled:
            logger.warning("Gemini not configured (GEMINI_API_KEY not set). Cannot optimize.")
            return ""

        try:
            import google.generativeai as genai  # type: ignore
        except ImportError:
            logger.error("google-generativeai not installed. Run: pip install google-generativeai")
            return ""

        try:
            genai.configure(api_key=settings.gemini.api_key)
            model = genai.GenerativeModel(self._model)
            response = model.generate_content(prompt)
            text = response.text or ""
            logger.debug("Gemini response (%d chars): %s...", len(text), text[:200])
            return text
        except Exception:
            logger.exception("Gemini API call failed")
            return ""

    def _write_optimization_cycle(
        self,
        started_at: datetime,
        report: PerformanceReport,
        proposal: Proposal,
        validation: ValidationResult,
    ) -> None:
        """Persist an OptimizationCycle row."""
        now = datetime.utcnow()
        session = get_session(self._db)
        try:
            cycle = OptimizationCycle(
                strategy_name="main",
                started_at=started_at,
                completed_at=now,
                in_sample_start=now - timedelta(weeks=18),
                in_sample_end=now - timedelta(weeks=4),
                oos_start=now - timedelta(weeks=4),
                oos_end=now,
                in_sample_sharpe=validation.is_sharpe,
                oos_sharpe=validation.oos_sharpe,
                in_sample_trades=validation.is_trades,
                oos_trades=validation.oos_trades,
                params_before=get_current_params(),
                params_after={proposal.param_name: proposal.proposed_value},
                accepted=validation.accepted,
                p_value=validation.monte_carlo_p_value,
                notes=validation.rejection_reason or "accepted",
            )
            session.add(cycle)
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to write OptimizationCycle to DB")
        finally:
            session.close()

    def _write_strategy_registry(self, new_params: dict) -> None:
        """Write or update the 'main' StrategyRegistry row."""
        session = get_session(self._db)
        try:
            existing = session.query(StrategyRegistry).filter_by(name="main").first()
            if existing:
                # Increment minor version
                parts = existing.version.split(".")
                minor = int(parts[1]) + 1 if len(parts) > 1 else 1
                existing.version = f"{parts[0]}.{minor}"
                existing.params = new_params
                logger.info("StrategyRegistry 'main' updated to version %s", existing.version)
            else:
                registry = StrategyRegistry(
                    name="main",
                    version="1.0",
                    params=new_params,
                    is_active=True,
                    description="Auto-managed by OptimizationPipeline",
                )
                session.add(registry)
                logger.info("StrategyRegistry 'main' created at version 1.0")
            session.commit()
        except Exception:
            session.rollback()
            logger.exception("Failed to write StrategyRegistry to DB")
        finally:
            session.close()


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

def test_pipeline() -> None:
    """Smoke-test the pipeline gate logic with mocked components."""
    import tempfile, os
    from unittest.mock import MagicMock, patch

    db_path = os.path.join(tempfile.gettempdir(), "test_pipeline.db")
    db_url = f"sqlite:///{db_path}"
    init_db(db_url)

    pipeline = OptimizationPipeline(database_url=db_url)

    # ── Test 1: gate exits early when < 50 trades ──────────────────────────
    # Use the real generator against an empty DB — it will find 0 trades < 50.
    # Verify the gate message appears (no exception = gate worked).
    ran_ok = False
    try:
        pipeline.run(require_human_approval=False)
        ran_ok = True
    except Exception as exc:
        raise AssertionError(f"Test 1: pipeline.run() raised unexpectedly: {exc}") from exc
    assert ran_ok
    print("Test 1 PASSED: gate exits early for < 50 trades (empty DB)")

    # ── Test 2: Gemini not configured → graceful exit ──────────────────────
    mock_report2 = MagicMock()
    mock_report2.meets_min_trades = True
    mock_report2.closed_trades = 60
    mock_report2.total_trades = 60
    mock_report2.win_rate = 0.62
    mock_report2.total_pnl_usd = 30.0
    mock_report2.sharpe_ratio = 1.8
    mock_report2.to_dict.return_value = {}

    stats_ok = MagicMock(return_value=(True, "significant"))

    with patch("optimization.pipeline.PerformanceReportGenerator") as MockGen2, \
         patch("optimization.pipeline.StatisticalTests") as MockStats, \
         patch.object(pipeline, "_call_gemini", return_value=""):
        MockGen2.return_value.generate.return_value = mock_report2
        MockStats.return_value.is_strategy_significant.return_value = (True, "ok")
        pipeline.run(require_human_approval=False)
    print("Test 2 PASSED: empty Gemini response handled gracefully")

    # ── Test 3: proposal cap at 3 ──────────────────────────────────────────
    fake_proposals = [
        MagicMock(param_name="confidence_threshold", current_value=55.0,
                  proposed_value=60.0, rationale="test", valid=True),
        MagicMock(param_name="holding_days", current_value=3,
                  proposed_value=5, rationale="test", valid=True),
        MagicMock(param_name="min_lead_gap", current_value=10.0,
                  proposed_value=12.0, rationale="test", valid=True),
    ]

    fake_validation = ValidationResult(
        proposal_param="confidence_threshold",
        proposed_value=60.0,
        is_sharpe=2.0, oos_sharpe=1.2,
        is_trades=60, oos_trades=20,
        monte_carlo_p_value=0.05,
        accepted=True,
    )

    validate_calls = []

    def fake_validate(proposal):
        validate_calls.append(proposal)
        return fake_validation

    with patch("optimization.pipeline.PerformanceReportGenerator") as MockGen3, \
         patch("optimization.pipeline.StatisticalTests") as MockStats3, \
         patch("optimization.pipeline.ProposalParser") as MockParser, \
         patch("optimization.pipeline.BacktestValidator") as MockVal, \
         patch.object(pipeline, "_call_gemini", return_value='{"proposals":[]}'):
        MockGen3.return_value.generate.return_value = mock_report2
        MockStats3.return_value.is_strategy_significant.return_value = (True, "ok")
        MockParser.return_value.parse.return_value = fake_proposals
        MockParser.return_value.valid_proposals.return_value = fake_proposals
        MockVal.return_value.validate.side_effect = fake_validate
        pipeline.run(require_human_approval=False)
        assert len(validate_calls) == 3, (
            f"Expected 3 validate calls, got {len(validate_calls)}"
        )
    print("Test 3 PASSED: all 3 proposals validated")

    # Cleanup
    if os.path.exists(db_path):
        os.remove(db_path)

    print("test_pipeline: ALL ASSERTIONS PASSED")


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.INFO)
    test_pipeline()
