"""Core datatypes shared across the control plane."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskTier(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Action(str, Enum):
    PASS = "pass"           # response released unchanged
    ANNOTATE = "annotate"   # released with a caution label + sources
    REPAIR = "repair"       # regenerated with grounding, then released/annotated
    BLOCK = "block"         # withheld from the user, escalated to human review


class Severity(str, Enum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return ["info", "low", "medium", "high", "critical"].index(self.value)


_TIERS = [RiskTier.LOW, RiskTier.MEDIUM, RiskTier.HIGH]


def bump_tier(tier: RiskTier, steps: int = 1) -> RiskTier:
    return _TIERS[min(len(_TIERS) - 1, _TIERS.index(tier) + steps)]


@dataclass
class Flag:
    lane: str
    code: str
    severity: Severity
    message: str
    evidence: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "code": self.code,
            "severity": self.severity.value,
            "message": self.message,
            "evidence": self.evidence,
        }


@dataclass
class LaneResult:
    lane: str
    score: float = 0.0           # 0..1 risk score (1 = worst)
    flags: list[Flag] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    skipped: bool = False
    timed_out: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane": self.lane,
            "score": round(self.score, 3),
            "flags": [f.to_dict() for f in self.flags],
            "evidence": self.evidence,
            "latency_ms": round(self.latency_ms, 1),
            "skipped": self.skipped,
            "timed_out": self.timed_out,
        }


@dataclass
class InteractionRequest:
    use_case: str
    prompt: str
    history: list[dict[str, str]] = field(default_factory=list)
    is_agent: bool = False           # response drives a downstream action, not just text
    request_id: str = ""
    client_meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelResponse:
    text: str
    model: str = "unknown"
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    retries: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass
class Decision:
    request_id: str
    use_case: str
    tier: RiskTier
    action: Action
    original_text: str
    final_text: str
    annotations: list[str]
    flags: list[Flag]
    lane_results: list[LaneResult]
    escalated: bool
    repaired: bool
    telemetry: dict[str, Any]
    policy_version: str
    created_at: float = field(default_factory=time.time)
    prev_hash: str = ""
    hash: str = ""
    review_id: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "use_case": self.use_case,
            "tier": self.tier.value,
            "action": self.action.value,
            "original_text": self.original_text,
            "final_text": self.final_text,
            "annotations": self.annotations,
            "flags": [f.to_dict() for f in self.flags],
            "lane_results": [r.to_dict() for r in self.lane_results],
            "escalated": self.escalated,
            "repaired": self.repaired,
            "telemetry": self.telemetry,
            "policy_version": self.policy_version,
            "created_at": self.created_at,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
            "review_id": self.review_id,
        }
