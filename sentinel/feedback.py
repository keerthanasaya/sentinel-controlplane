"""Feedback loop.

When a reviewer resolves a queue item, or a user reports a missed issue, that
signal nudges the relevant policy threshold and is stored as a labelled example.
Over-flagging (overridden reviews) loosens the threshold; confirmed misses
(false negatives) tighten it. Everything is logged to the audit store.
"""
from __future__ import annotations

from typing import Any

from .audit import AuditStore
from .policy import PolicyStore

# which threshold a given flag code is tied to
_CODE_TO_THRESHOLD = {
    "weak_grounding": "grounding_min",
    "confidently_wrong": "grounding_min",
    "hallucination": "grounding_min",
    "judge_low_score": "judge_factuality_min",
    "cost_anomaly": "cost_zscore",
    "retry_loop": "cost_zscore",
}
_STEP = {
    "grounding_min": 0.03,
    "judge_factuality_min": 0.03,
    "cost_zscore": 0.5,
}


class FeedbackEngine:
    def __init__(self, store: AuditStore, policies: PolicyStore):
        self.store = store
        self.policies = policies

    def resolve_review(self, review_id: int, status: str, reviewer: str,
                       note: str = "") -> dict[str, Any]:
        if status not in {"upheld", "overridden"}:
            raise ValueError("status must be 'upheld' or 'overridden'")
        row = self.store.resolve_review(review_id, status, reviewer, note)
        if not row:
            raise KeyError(f"review {review_id} not found")

        decision = self.store.get_decision(row["decision_id"])
        adjustments: list[dict[str, Any]] = []
        if decision:
            codes = {f["code"] for f in decision["flags"]}
            self.store.add_feedback(
                row["decision_id"],
                label="false_positive" if status == "overridden" else "true_positive",
                note=note)
            for code in codes:
                thr = _CODE_TO_THRESHOLD.get(code)
                if not thr:
                    continue
                policy = self.policies.get(decision["use_case"])
                cur = policy.threshold(thr, 0.0)
                step = _STEP.get(thr, 0.02)
                # overridden == we were too aggressive -> relax; upheld -> hold/tighten a touch
                new = cur - step if status == "overridden" else cur + step * 0.4
                if thr in ("grounding_min", "judge_factuality_min"):
                    new = max(0.2, min(0.95, new))
                else:
                    new = max(1.5, min(6.0, new))
                if abs(new - cur) > 1e-6:
                    self.policies.apply_threshold_override(decision["use_case"], thr, new)
                    adjustments.append({"use_case": decision["use_case"], "threshold": thr,
                                        "from": round(cur, 3), "to": round(new, 3)})
        return {"review": row, "threshold_adjustments": adjustments}

    def report_false_negative(self, decision_row_id: int, note: str = "") -> dict[str, Any]:
        decision = self.store.get_decision(decision_row_id)
        if not decision:
            raise KeyError(f"decision {decision_row_id} not found")
        self.store.add_feedback(decision_row_id, "false_negative", note)
        adjustments = []
        policy = self.policies.get(decision["use_case"])
        for thr in ("grounding_min", "judge_factuality_min"):
            cur = policy.threshold(thr, 0.0)
            new = min(0.95, cur + _STEP[thr])
            self.policies.apply_threshold_override(decision["use_case"], thr, new)
            adjustments.append({"use_case": decision["use_case"], "threshold": thr,
                                "from": round(cur, 3), "to": round(new, 3)})
        return {"decision_id": decision_row_id, "threshold_adjustments": adjustments}
