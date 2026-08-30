"""The control plane itself: route -> run lanes in parallel -> aggregate ->
choose a graduated action -> (optionally) repair -> annotate -> record.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from . import lanes
from .audit import AuditStore
from .llm import llm
from .models import (
    Action,
    Decision,
    Flag,
    InteractionRequest,
    LaneResult,
    ModelResponse,
    RiskTier,
    Severity,
)
from .policy import Policy, PolicyStore
from .router import route

_PRICE_PER_1K = 0.009


class Sentinel:
    def __init__(self, store: AuditStore, policies: PolicyStore):
        self.store = store
        self.policies = policies

    async def evaluate(self, req: InteractionRequest, resp: ModelResponse) -> Decision:
        t_start = time.perf_counter()
        if not req.request_id:
            req.request_id = uuid.uuid4().hex[:12]
        policy = self.policies.get(req.use_case)
        rd = route(req, resp, policy)

        # ---- run lanes concurrently, each capped at the latency budget
        budget = policy.latency_budget_ms / 1000
        context_text = ""
        tasks: dict[str, asyncio.Task] = {}
        if rd.run_responsibility:
            tasks["responsibility"] = asyncio.create_task(
                asyncio.to_thread(lanes.run_responsibility, req, resp, policy, ""))
        if rd.run_cost:
            tasks["cost"] = asyncio.create_task(
                asyncio.to_thread(lanes.run_cost, req, resp, policy, self.store.baselines()))
        if rd.run_performance:
            tasks["performance"] = asyncio.create_task(
                asyncio.to_thread(lanes.run_performance, req, resp, policy, rd.deep_verify))

        # generous hard cap (a runaway check must never hang the response path)
        hard_cap = max(2.0, budget * 6)
        results: list[LaneResult] = []
        for name, task in tasks.items():
            try:
                res = await asyncio.wait_for(task, timeout=hard_cap)
                if name == "performance":
                    lane_res, context_text = res
                    results.append(lane_res)
                else:
                    results.append(res)
            except asyncio.TimeoutError:
                lr = LaneResult(lane=name, timed_out=True)
                lr.flags.append(Flag(name, "lane_timeout", Severity.LOW,
                                     f"{name} lane exceeded latency budget; fail-safe applied"))
                results.append(lr)

        # soft budget: note lanes that ran slower than the use case's latency budget
        for r in results:
            if r.latency_ms > policy.latency_budget_ms and not r.timed_out:
                r.evidence["over_budget"] = True

        flags = []
        seen: set = set()
        for r in results:
            for f in r.flags:
                key = (f.code, f.message)
                if key not in seen:
                    seen.add(key)
                    flags.append(f)
        action, escalate = self._decide(flags, rd.tier, policy)

        # ---- graduated actions
        final_text = resp.text
        annotations: list[str] = []
        repaired = False
        repair_note = None

        if action == Action.REPAIR:
            issues = [f.message for f in flags if f.severity.rank >= 2]
            fixed = llm.repair(req.prompt, resp.text, context_text, issues)
            recheck, _ = lanes.run_performance(
                InteractionRequest(use_case=req.use_case, prompt=req.prompt, history=req.history),
                ModelResponse(text=fixed.text), policy, rd.deep_verify)
            recheck.lane = "performance (post-repair)"
            repaired = True
            still_bad = any(f.severity.rank >= 3 for f in recheck.flags)
            if still_bad and rd.tier == RiskTier.HIGH:
                action, escalate = Action.BLOCK, True
                repair_note = "auto-repair did not fully resolve the issue on a high-stakes response"
            elif still_bad:
                final_text = fixed.text
                action = Action.ANNOTATE
                repair_note = "auto-repair applied; residual uncertainty remains — released with caution"
            else:
                final_text = fixed.text
                action = Action.REPAIR
                repair_note = f"regenerated against verified sources ({fixed.model})"
            results.append(recheck)

        src_labels = self._source_labels(results)

        if action == Action.BLOCK:
            final_text = ""
            annotations.append("Response withheld and escalated to human review.")
        elif action == Action.REPAIR:
            annotations.append(f"↻ Regenerated: {repair_note}")
            if src_labels:
                annotations.append("Sources: " + ", ".join(src_labels))
        elif action == Action.ANNOTATE:
            reasons = sorted({self._friendly(f) for f in flags if f.severity.rank >= 1})
            annotations.append("⚠ Caution: " + "; ".join(reasons) if reasons
                               else "⚠ Caution: automated checks flagged this response.")
            if repair_note:
                annotations.append(f"↻ {repair_note}")
            if src_labels:
                annotations.append("Sources: " + ", ".join(src_labels))
        elif action == Action.PASS and src_labels and rd.tier != RiskTier.LOW:
            annotations.append("Verified against: " + ", ".join(src_labels))

        overhead_ms = (time.perf_counter() - t_start) * 1000
        cost_avoided = self._cost_avoided(action, resp)

        telemetry = {
            "route": rd.to_dict(),
            "sentinel_overhead_ms": round(overhead_ms, 1),
            "upstream_latency_ms": round(resp.latency_ms, 1),
            "upstream_model": resp.model,
            "tokens": {"prompt": resp.prompt_tokens, "completion": resp.completion_tokens},
            "est_request_cost_usd": round(resp.total_tokens / 1000 * _PRICE_PER_1K, 5),
            "cost_avoided_usd": round(cost_avoided, 5),
            "lane_latency_ms": {r.lane: round(r.latency_ms, 1) for r in results},
            "judge_engine": next((r.evidence.get("judge", {}).get("engine")
                                  for r in results if r.lane == "performance"), None),
        }

        decision = Decision(
            request_id=req.request_id, use_case=req.use_case, tier=rd.tier, action=action,
            original_text=resp.text, final_text=final_text, annotations=annotations,
            flags=flags, lane_results=results, escalated=escalate, repaired=repaired,
            telemetry=telemetry, policy_version=policy.version,
        )
        decision = self.store.record(decision)

        # ---- update learned baselines AFTER scoring this request
        self.store.baseline_update(req.use_case, "total_tokens", float(resp.total_tokens))
        self.store.baseline_update(req.use_case, "latency_ms", float(resp.latency_ms))
        self.store.baseline_update(req.use_case, "cost_usd",
                                   resp.total_tokens / 1000 * _PRICE_PER_1K)
        return decision

    # ------------------------------------------------------------ decision matrix
    @staticmethod
    def _decide(flags: list[Flag], tier: RiskTier, policy: Policy) -> tuple[Action, bool]:
        codes = {f.code for f in flags}
        max_rank = max((f.severity.rank for f in flags), default=0)

        if codes & set(policy.block_on) or any(
            f.severity == Severity.CRITICAL for f in flags
        ):
            return Action.BLOCK, True

        if max_rank >= 3:  # HIGH
            repairable = bool(codes & {"weak_grounding", "confidently_wrong", "hallucination",
                                       "judge_low_score", "numeric_mismatch", "source_contradiction"})
            if policy.allow_repair and repairable and tier != RiskTier.LOW:
                return Action.REPAIR, False
            if tier == RiskTier.HIGH:
                return Action.BLOCK, True
            return Action.ANNOTATE, False

        if max_rank == 2:  # MEDIUM
            return Action.ANNOTATE, False
        if max_rank == 1:  # LOW
            return Action.ANNOTATE, False
        return Action.PASS, False

    @staticmethod
    def _friendly(f: Flag) -> str:
        return {
            "weak_grounding": "some details could not be verified against sources",
            "confidently_wrong": "stated more confidently than the evidence supports",
            "hallucination": "contains unverified claims",
            "numeric_mismatch": "figures don't match the source documents",
            "source_contradiction": "appears to contradict the source documents",
            "judge_low_score": "verifier flagged possible inaccuracy",
            "cost_anomaly": "unusually high compute for this request",
            "retry_loop": "repeated/looping request pattern",
            "token_ceiling": "response longer than policy allows",
            "bias_phrasing": "phrasing may contain an unfair generalisation",
            "pii_leak": "personal data exposure",
            "unsafe_high": "unsafe advice",
            "unsafe_medium": "potentially unsafe advice",
            "no_ground_truth": "no source available to verify",
            "lane_timeout": "a check timed out",
        }.get(f.code, f.message)

    @staticmethod
    def _source_labels(results: list[LaneResult]) -> list[str]:
        for r in results:
            if r.lane == "performance":
                srcs = r.evidence.get("sources", [])
                return sorted({f"{s['doc']} · {s['heading']}" for s in srcs})
        return []

    @staticmethod
    def _cost_avoided(action: Action, resp: ModelResponse) -> float:
        # Illustrative: a blocked/ repaired bad answer avoids downstream rework we
        # model as ~6x the request's own token cost.
        base = resp.total_tokens / 1000 * _PRICE_PER_1K
        if action == Action.BLOCK:
            return base * 6
        if action == Action.REPAIR:
            return base * 3
        return 0.0
