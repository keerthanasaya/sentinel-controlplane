"""Risk-tiered router.

Decides how much oversight a single interaction gets. Cheap checks always run;
the deeper verifier + retrieval verification only runs for high-stakes traffic
(money, health, legal), multi-turn escalation, or agent actions. This is what
keeps p50 added latency low while still covering the responses that matter.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import InteractionRequest, ModelResponse, RiskTier, bump_tier
from .policy import Policy


@dataclass
class RouteDecision:
    tier: RiskTier
    reasons: list[str]
    run_cost: bool
    run_responsibility: bool
    run_performance: bool
    deep_verify: bool

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "reasons": self.reasons,
            "lanes": {
                "cost": self.run_cost,
                "responsibility": self.run_responsibility,
                "performance": self.run_performance,
            },
            "deep_verify": self.deep_verify,
        }


def route(req: InteractionRequest, resp: ModelResponse, policy: Policy) -> RouteDecision:
    tier = RiskTier(policy.base_tier)
    reasons = [f"base tier for '{policy.use_case}' = {tier.value}"]

    haystack = f"{req.prompt}\n{resp.text}".lower()
    hits = sorted({k for k in policy.high_stakes_keywords if k in haystack})
    if hits:
        tier = bump_tier(tier)
        reasons.append(f"high-stakes keywords {hits} -> +1 tier")

    turns = len([m for m in req.history if m.get("role") == "user"])
    if turns >= 3:
        tier = bump_tier(tier)
        reasons.append(f"multi-turn conversation ({turns} prior user turns) -> +1 tier (compounding risk)")

    if req.is_agent:
        tier = RiskTier.HIGH
        reasons.append("response drives a downstream agent action -> forced HIGH tier")

    run_performance = tier != RiskTier.LOW
    deep_verify = tier == RiskTier.HIGH
    if not run_performance:
        reasons.append("LOW tier: skipping retrieval/verification, fast classifiers only")

    return RouteDecision(
        tier=tier,
        reasons=reasons,
        run_cost=True,
        run_responsibility=True,
        run_performance=run_performance,
        deep_verify=deep_verify,
    )
