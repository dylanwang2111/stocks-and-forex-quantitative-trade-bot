"""
optimization/proposal_parser.py
Parses and validates the JSON response returned by Gemini for parameter
change proposals. Invalid proposals are kept in the output list but flagged
with valid=False so callers can log or report them.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from optimization.prompts import LOCKED_PARAMS, TUNABLE_PARAMS

logger = logging.getLogger(__name__)


@dataclass
class Proposal:
    """A single parameter change proposal from Gemini."""

    param_name: str
    current_value: Any
    proposed_value: Any
    rationale: str
    valid: bool = True
    rejection_reason: str = ""


class ProposalParser:
    """
    Parse and validate the JSON response from Gemini Flash 2.0.

    Usage::

        parser = ProposalParser()
        proposals = parser.parse(gemini_response_text)
        good = parser.valid_proposals(proposals)
    """

    MAX_PROPOSALS = 3

    # Fields that every raw proposal dict must contain.
    _REQUIRED_FIELDS: tuple[str, ...] = ("param_name", "proposed_value", "rationale")

    # ------------------------------------------------------------------ #
    # Public API                                                           #
    # ------------------------------------------------------------------ #

    def parse(self, gemini_response: str) -> list[Proposal]:
        """
        Parse Gemini's JSON response into a list of validated Proposals.

        Steps:
        1. Strip markdown fencing if present (```json ... ```).
        2. json.loads() the cleaned response.
        3. Extract the "proposals" list from the parsed object.
        4. Validate each proposal via _validate_proposal().
        5. Cap the list at MAX_PROPOSALS (first 3 kept).
        6. Return the list — may contain Proposals with valid=False.

        On JSON parse error: logs an ERROR and returns [].

        Args:
            gemini_response: Raw text returned by the Gemini API.

        Returns:
            List of Proposal objects (possibly empty).
        """
        cleaned = self._strip_markdown_fencing(gemini_response)

        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            logger.error(
                "ProposalParser: failed to parse Gemini response as JSON. "
                "Error: %s. Raw response: %.200s",
                exc,
                gemini_response,
            )
            return []

        if not isinstance(parsed, dict):
            logger.error(
                "ProposalParser: expected a JSON object at the top level, "
                "got %s. Raw response: %.200s",
                type(parsed).__name__,
                gemini_response,
            )
            return []

        raw_proposals = parsed.get("proposals", [])
        if not isinstance(raw_proposals, list):
            logger.error(
                "ProposalParser: 'proposals' key is not a list (got %s).",
                type(raw_proposals).__name__,
            )
            return []

        # Cap before validating to avoid wasted work.
        capped = raw_proposals[: self.MAX_PROPOSALS]
        if len(raw_proposals) > self.MAX_PROPOSALS:
            logger.warning(
                "ProposalParser: Gemini returned %d proposals; "
                "only the first %d will be processed.",
                len(raw_proposals),
                self.MAX_PROPOSALS,
            )

        return [self._validate_proposal(raw) for raw in capped]

    def valid_proposals(self, proposals: list[Proposal]) -> list[Proposal]:
        """Return only proposals that passed all validation checks."""
        return [p for p in proposals if p.valid]

    # ------------------------------------------------------------------ #
    # Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _strip_markdown_fencing(text: str) -> str:
        """
        Remove ```json ... ``` (or plain ``` ... ```) wrapping if present.

        Gemini sometimes wraps its JSON output in markdown code fences even
        when instructed not to. This method strips them defensively.
        """
        stripped = text.strip()

        # Handle ```json or ``` opening fence.
        if stripped.startswith("```"):
            # Remove the opening fence line.
            first_newline = stripped.find("\n")
            if first_newline != -1:
                stripped = stripped[first_newline + 1 :]
            else:
                # Degenerate: the entire string is just the opening fence.
                stripped = ""

        # Remove trailing closing fence.
        if stripped.endswith("```"):
            stripped = stripped[: stripped.rfind("```")]

        return stripped.strip()

    def _validate_proposal(self, raw: dict) -> Proposal:
        """
        Validate one raw proposal dict.

        Returns a Proposal with valid=False and a rejection_reason if any
        check fails. Type coercion is attempted before failing on type
        mismatches so that minor format differences from Gemini are tolerated.

        Validation order:
        1. Check all required fields are present.
        2. Reject locked parameters.
        3. Reject unknown (not in TUNABLE_PARAMS) parameters.
        4. Coerce proposed_value to the expected type.
        5. Check proposed_value is within [min, max].
        """
        if not isinstance(raw, dict):
            return Proposal(
                param_name="<unknown>",
                current_value=None,
                proposed_value=None,
                rationale="",
                valid=False,
                rejection_reason="proposal entry is not a JSON object",
            )

        # --- 1. Required fields ---------------------------------------- #
        missing = [f for f in self._REQUIRED_FIELDS if f not in raw]
        if missing:
            reason = f"missing required fields: {missing}"
            logger.warning("ProposalParser: rejecting proposal — %s. Raw: %s", reason, raw)
            return Proposal(
                param_name=raw.get("param_name", "<unknown>"),
                current_value=raw.get("current_value"),
                proposed_value=raw.get("proposed_value"),
                rationale=raw.get("rationale", ""),
                valid=False,
                rejection_reason=reason,
            )

        param_name: str = str(raw["param_name"])
        proposed_value: Any = raw["proposed_value"]
        rationale: str = str(raw["rationale"])
        current_value: Any = raw.get("current_value")

        # --- 2. Locked parameters -------------------------------------- #
        if param_name in LOCKED_PARAMS:
            reason = "locked parameter"
            logger.warning(
                "ProposalParser: rejecting proposal for '%s' — %s.", param_name, reason
            )
            return Proposal(
                param_name=param_name,
                current_value=current_value,
                proposed_value=proposed_value,
                rationale=rationale,
                valid=False,
                rejection_reason=reason,
            )

        # --- 3. Unknown parameters ------------------------------------- #
        if param_name not in TUNABLE_PARAMS:
            reason = "unknown parameter"
            logger.warning(
                "ProposalParser: rejecting proposal for '%s' — %s.", param_name, reason
            )
            return Proposal(
                param_name=param_name,
                current_value=current_value,
                proposed_value=proposed_value,
                rationale=rationale,
                valid=False,
                rejection_reason=reason,
            )

        param_spec = TUNABLE_PARAMS[param_name]
        expected_type: str = param_spec["type"]  # "float" or "int"

        # --- 4. Type coercion ------------------------------------------ #
        coerced_value, coerce_error = self._coerce_type(proposed_value, expected_type)
        if coerce_error:
            reason = f"type error: {coerce_error}"
            logger.warning(
                "ProposalParser: rejecting proposal for '%s' — %s.", param_name, reason
            )
            return Proposal(
                param_name=param_name,
                current_value=current_value,
                proposed_value=proposed_value,
                rationale=rationale,
                valid=False,
                rejection_reason=reason,
            )

        # --- 5. Range check -------------------------------------------- #
        lo: float = param_spec["min"]
        hi: float = param_spec["max"]
        if not (lo <= coerced_value <= hi):
            reason = f"out of range: {coerced_value} not in [{lo}, {hi}]"
            logger.warning(
                "ProposalParser: rejecting proposal for '%s' — %s.", param_name, reason
            )
            return Proposal(
                param_name=param_name,
                current_value=current_value,
                proposed_value=coerced_value,
                rationale=rationale,
                valid=False,
                rejection_reason=reason,
            )

        return Proposal(
            param_name=param_name,
            current_value=current_value,
            proposed_value=coerced_value,
            rationale=rationale,
            valid=True,
            rejection_reason="",
        )

    @staticmethod
    def _coerce_type(value: Any, expected_type: str) -> tuple[Any, str]:
        """
        Attempt to coerce *value* to the expected type ("float" or "int").

        Returns (coerced_value, "") on success.
        Returns (original_value, error_message) on failure.
        """
        try:
            if expected_type == "float":
                return float(value), ""
            if expected_type == "int":
                # Reject values that are clearly floats with a fractional part
                # (e.g. holding_days=2.7 makes no sense as an integer param).
                as_float = float(value)
                as_int = int(as_float)
                if as_float != as_int:
                    return value, (
                        f"cannot losslessly convert {value!r} to int "
                        f"(fractional part would be discarded)"
                    )
                return as_int, ""
            # Unrecognised type spec — pass through unchanged.
            return value, ""
        except (TypeError, ValueError) as exc:
            return value, str(exc)


# --------------------------------------------------------------------------- #
# Test function                                                                #
# --------------------------------------------------------------------------- #


def test_proposal_parser() -> None:  # noqa: C901  (acceptable complexity for a test)
    """
    Inline smoke-tests for ProposalParser.  Run with:

        python -m optimization.proposal_parser

    All tests must pass (assertions raise AssertionError on failure).
    """
    import sys

    parser = ProposalParser()
    failures: list[str] = []

    def check(label: str, condition: bool) -> None:
        status = "PASS" if condition else "FAIL"
        print(f"  [{status}] {label}")
        if not condition:
            failures.append(label)

    print("\n--- ProposalParser Tests ---\n")

    # ------------------------------------------------------------------ #
    # Test 1: Two valid proposals                                         #
    # ------------------------------------------------------------------ #
    print("Test 1: Two valid proposals")
    raw_json = json.dumps(
        {
            "proposals": [
                {
                    "param_name": "confidence_threshold",
                    "current_value": 55.0,
                    "proposed_value": 62.0,
                    "rationale": "Win rate is only 51%, filter marginal signals.",
                },
                {
                    "param_name": "holding_days",
                    "current_value": 3,
                    "proposed_value": 5,
                    "rationale": "Average winning trade duration is 4.8 days.",
                },
            ]
        }
    )
    result = parser.parse(raw_json)
    valid = parser.valid_proposals(result)
    check("returns 2 proposals total", len(result) == 2)
    check("both are valid", len(valid) == 2)

    # ------------------------------------------------------------------ #
    # Test 2: Locked parameter                                            #
    # ------------------------------------------------------------------ #
    print("\nTest 2: Locked parameter (total_capital)")
    raw_json = json.dumps(
        {
            "proposals": [
                {
                    "param_name": "total_capital",
                    "current_value": 500,
                    "proposed_value": 750,
                    "rationale": "More capital means bigger positions.",
                }
            ]
        }
    )
    result = parser.parse(raw_json)
    check("one proposal returned", len(result) == 1)
    check("proposal is invalid", not result[0].valid)
    check(
        "rejection mentions 'locked'",
        "locked" in result[0].rejection_reason.lower(),
    )

    # ------------------------------------------------------------------ #
    # Test 3: Out-of-range value                                          #
    # ------------------------------------------------------------------ #
    print("\nTest 3: Out-of-range value (confidence_threshold=90)")
    raw_json = json.dumps(
        {
            "proposals": [
                {
                    "param_name": "confidence_threshold",
                    "current_value": 55.0,
                    "proposed_value": 90.0,
                    "rationale": "Very aggressive filter.",
                }
            ]
        }
    )
    result = parser.parse(raw_json)
    check("one proposal returned", len(result) == 1)
    check("proposal is invalid", not result[0].valid)
    check(
        "rejection mentions 'out of range'",
        "out of range" in result[0].rejection_reason.lower(),
    )

    # ------------------------------------------------------------------ #
    # Test 4: Unknown parameter                                           #
    # ------------------------------------------------------------------ #
    print("\nTest 4: Unknown parameter")
    raw_json = json.dumps(
        {
            "proposals": [
                {
                    "param_name": "magic_parameter",
                    "current_value": 1,
                    "proposed_value": 2,
                    "rationale": "No idea what this does.",
                }
            ]
        }
    )
    result = parser.parse(raw_json)
    check("one proposal returned", len(result) == 1)
    check("proposal is invalid", not result[0].valid)
    check(
        "rejection mentions 'unknown'",
        "unknown" in result[0].rejection_reason.lower(),
    )

    # ------------------------------------------------------------------ #
    # Test 5: More than 3 proposals → capped at 3                        #
    # ------------------------------------------------------------------ #
    print("\nTest 5: More than 3 proposals → capped at 3")
    raw_json = json.dumps(
        {
            "proposals": [
                {
                    "param_name": "confidence_threshold",
                    "current_value": 55.0,
                    "proposed_value": 60.0,
                    "rationale": "r1",
                },
                {
                    "param_name": "holding_days",
                    "current_value": 3,
                    "proposed_value": 4,
                    "rationale": "r2",
                },
                {
                    "param_name": "min_lead_gap",
                    "current_value": 10.0,
                    "proposed_value": 12.0,
                    "rationale": "r3",
                },
                {
                    "param_name": "confidence_threshold",
                    "current_value": 55.0,
                    "proposed_value": 65.0,
                    "rationale": "r4 — fourth, should be dropped",
                },
            ]
        }
    )
    result = parser.parse(raw_json)
    check("capped at 3 proposals", len(result) == 3)

    # ------------------------------------------------------------------ #
    # Test 6: Markdown-fenced JSON                                        #
    # ------------------------------------------------------------------ #
    print("\nTest 6: Markdown-fenced JSON (```json ... ```)")
    fenced = (
        "```json\n"
        + json.dumps(
            {
                "proposals": [
                    {
                        "param_name": "min_lead_gap",
                        "current_value": 10.0,
                        "proposed_value": 14.0,
                        "rationale": "Spread is frequently exceeding 10 pips.",
                    }
                ]
            }
        )
        + "\n```"
    )
    result = parser.parse(fenced)
    valid = parser.valid_proposals(result)
    check("parsed correctly despite fencing", len(result) == 1)
    check("proposal is valid", len(valid) == 1)
    check("correct param_name", result[0].param_name == "min_lead_gap")

    # ------------------------------------------------------------------ #
    # Test 7: Malformed JSON                                              #
    # ------------------------------------------------------------------ #
    print("\nTest 7: Malformed JSON")
    result = parser.parse("This is not JSON at all { broken }")
    check("returns empty list on malformed JSON", result == [])

    # ------------------------------------------------------------------ #
    # Summary                                                             #
    # ------------------------------------------------------------------ #
    print(f"\n{'=' * 40}")
    if failures:
        print(f"FAILED: {len(failures)} test(s) — {failures}")
        sys.exit(1)
    else:
        print(f"All tests passed.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    test_proposal_parser()
