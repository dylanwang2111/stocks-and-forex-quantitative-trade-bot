"""
optimization/prompts.py
Versioned Gemini prompt templates for the optimization pipeline.

Gemini Flash 2.0 is asked to review a performance report and propose parameter
changes from within the set of TUNABLE_PARAMS only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PROMPT_VERSION = "1.0"

# Tunable parameters with their valid ranges and defaults.
# Only these three may ever appear in a Gemini proposal.
TUNABLE_PARAMS: dict[str, dict[str, Any]] = {
    "confidence_threshold": {
        "min": 55.0,
        "max": 85.0,
        "default": 55.0,
        "type": "float",
    },
    "holding_days": {
        "min": 1,
        "max": 14,
        "default": 3,
        "type": "int",
    },
    "min_lead_gap": {
        "min": 5.0,
        "max": 25.0,
        "default": 10.0,
        "type": "float",
    },
}

# Parameters that must never appear in any proposal.
# Gemini is told about these explicitly so it understands what is off-limits.
LOCKED_PARAMS: list[str] = [
    "total_capital",
    "cash_reserve",
    "max_positions",
    "risk_per_trade",
    "rsi_period",
    "ema_length",
    "atr_multiplier",
    "stop_pct",
    "tp_pct",
]


def build_optimization_prompt(performance_report_dict: dict) -> str:
    """
    Build the prompt to send to Gemini for optimization proposals.

    The prompt:
    1. Describes the task clearly and concisely.
    2. Embeds the performance report as compact JSON.
    3. Lists every tunable parameter with its current value and valid range.
    4. Explicitly lists every locked parameter and forbids touching them.
    5. Specifies the required JSON response format with a concrete example.
    6. Limits the model to a maximum of 3 proposals.
    7. Requires conservative changes (≤ 20 % from the current value).
    8. Instructs the model to return ONLY valid JSON — no markdown, no prose.

    Args:
        performance_report_dict: A dictionary containing trading performance
            metrics (win_rate, sharpe_ratio, total_trades, max_drawdown, etc.)
            for the most recent evaluation window.

    Returns:
        A fully-rendered prompt string ready to send to Gemini Flash 2.0.
    """
    current_params = get_current_params()
    performance_json = json.dumps(performance_report_dict, indent=2)

    # Build a structured block listing every tunable parameter.
    tunable_lines: list[str] = []
    for name, spec in TUNABLE_PARAMS.items():
        current = current_params.get(name, spec["default"])
        tunable_lines.append(
            f'  - {name}: current={current}, type={spec["type"]}, '
            f'range=[{spec["min"]}, {spec["max"]}]'
        )
    tunable_block = "\n".join(tunable_lines)

    locked_block = ", ".join(LOCKED_PARAMS)

    # Build a concrete example of the expected JSON response format.
    example_response = json.dumps(
        {
            "proposals": [
                {
                    "param_name": "confidence_threshold",
                    "current_value": 55.0,
                    "proposed_value": 60.0,
                    "rationale": (
                        "Win rate is only 52% — raising the threshold will "
                        "filter out marginal signals and improve signal quality."
                    ),
                }
            ]
        },
        indent=4,
    )

    prompt = f"""\
You are a quantitative trading system optimizer. Your job is to review a \
performance report from a live algorithmic trading bot and propose conservative \
parameter adjustments that are supported directly by the data.

## Performance Report

{performance_json}

## Tunable Parameters (ONLY these may be proposed)

{tunable_block}

## Locked Parameters (NEVER propose changes to these)

{locked_block}

These locked parameters control capital allocation, risk limits, and low-level \
signal construction. They are outside the scope of this optimization cycle and \
must not appear in any proposal.

## Task

Analyze the performance report above. Based solely on the data provided, \
propose up to 3 parameter changes that are likely to improve trading \
performance. Follow these rules strictly:

1. Only propose changes to parameters listed under "Tunable Parameters".
2. Never propose changes to any parameter listed under "Locked Parameters".
3. Every proposed value must fall within the stated [min, max] range.
4. Keep changes conservative: proposed_value must not deviate more than 20% \
from current_value.
5. Every proposal must include a clear, data-driven rationale that references \
specific metrics from the performance report.
6. If the data does not support a change to a parameter, do not propose one. \
Fewer than 3 proposals is acceptable.
7. Do not repeat the same parameter in more than one proposal.

## Response Format

Return ONLY a JSON object — no markdown fencing, no explanatory text before \
or after, no code blocks. The JSON must match this schema exactly:

{example_response}

If you have no proposals to make, return: {{"proposals": []}}

Return your response now.
"""

    return prompt


def get_current_params() -> dict[str, Any]:
    """
    Return the current values of all tunable parameters.

    confidence_threshold is read from settings. holding_days and min_lead_gap
    are hardcoded Phase 3 defaults because they are not yet stored in settings.
    """
    from config.settings import settings

    return {
        "confidence_threshold": settings.bot.min_confidence,
        "holding_days": 3,      # hardcoded default for Phase 3
        "min_lead_gap": 10.0,   # hardcoded default for Phase 3
    }
