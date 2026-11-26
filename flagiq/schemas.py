"""
FlagIQ — Data contracts.
All data flowing through the experiment uses these schemas.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Literal, Optional


DecisionType = Literal["ANSWER", "CLARIFY", "DUPLICATE", "ESCALATE"]


@dataclass
class IssueReport:
    """A single GitHub issue normalised into FlagIQ format."""
    identity: str               # "{repo}#{number}" — used as Flagsmith identity
    repo: str                   # e.g. "python/cpython"
    number: int                 # GitHub issue number
    title: str
    body: str
    created_at: str = ""                                        # ISO-8601 creation timestamp
    state_reason: str = ""                                      # GitHub API state_reason (evaluation-only)
    labels: list[str] = field(default_factory=list)             # evaluation-only
    comment_count: int = 0                                      # evaluation-only
    has_linked_pr: bool = False                                 # evaluation-only
    real_resolution: DecisionType = "ANSWER"                    # ground-truth outcome


@dataclass
class DecisionResult:
    """Output of the decision engine for one issue."""
    identity: str
    strategy: str               # "answer_first" or "clarify_first"
    decision: DecisionType
    confidence: float           # 0.0 – 1.0 (creation-time evidence strength)
    real_resolution: DecisionType
    correct: bool               # decision == real_resolution
    false_escalation: bool      # decision == ESCALATE and real != ESCALATE
    false_answer: bool          # decision == ANSWER and real == ESCALATE
    evidence: list[str] = field(default_factory=list)
    net_value_usd: float = 0.0  # engineering dollars saved (+) or wasted (-)


@dataclass
class ExperimentSummary:
    """Aggregate stats after all issues are processed."""
    total: int
    variant_a_count: int
    variant_b_count: int
    variant_a_accuracy: float
    variant_b_accuracy: float
    variant_a_false_escalation_rate: float
    variant_b_false_escalation_rate: float
    win_probability: Optional[float] = None
    winner: Optional[str] = None
