"""signals package — 8 independent signal categories."""
from dataclasses import dataclass, field


@dataclass
class SignalResult:
    vote: int          # +1 bullish | -1 bearish | 0 neutral
    reason: str
    params: dict = field(default_factory=dict)
