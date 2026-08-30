"""Cost lane — tracks tokens, latency and retry loops per request against learned
per-use-case baselines and surfaces workflows quietly burning far more compute
than they should. No single request looks alarming; the anomaly does.
"""
from __future__ import annotations

import hashlib
import time
from collections import defaultdict, deque

from ..models import Flag, InteractionRequest, LaneResult, ModelResponse, Severity

# Rough blended price (USD per 1k tokens) just for an illustrative cost figure.
_PRICE_PER_1K = 0.009

# in-memory recent-prompt window for retry-loop detection (per process)
_recent: dict[str, deque] = defaultdict(lambda: deque(maxlen=40))


def _prompt_hash(use_case: str, prompt: str) -> str:
    return hashlib.sha1(f"{use_case}|{prompt.strip().lower()}".encode()).hexdigest()[:12]


def run(req: InteractionRequest, resp: ModelResponse, policy, baselines) -> LaneResult:
    t0 = time.perf_counter()
    out = LaneResult(lane="cost")

    tokens = resp.total_tokens
    est_cost = tokens / 1000 * _PRICE_PER_1K
    metrics = {"total_tokens": tokens, "latency_ms": resp.latency_ms, "cost_usd": est_cost}
    # an anomaly must also be materially large in absolute terms — a statistical
    # z-score alone fires on trivial deltas when the baseline has low variance
    floors = {
        "total_tokens": max(900, policy.threshold("max_tokens_per_response", 1500) * 1.3),
        "latency_ms": max(3000.0, policy.latency_budget_ms * 3),
        "cost_usd": 0.012,
    }

    z_scores = {}
    worst_z = 0.0
    for metric, value in metrics.items():
        n, mean, std = baselines.stats(policy.use_case, metric)
        if n >= 8 and std > 1e-9 and value > mean * 1.5 and value >= floors[metric]:
            z = (value - mean) / std
        else:
            z = 0.0
        z_scores[metric] = round(z, 2)
        worst_z = max(worst_z, z)

    zthr = policy.threshold("cost_zscore", 3.5)
    if worst_z >= zthr:
        drivers = [m for m, z in z_scores.items() if z >= zthr]
        # crude projection: this excess, if systemic across the use case's weekly volume
        _, mean_cost, _ = baselines.stats(policy.use_case, "cost_usd")
        excess = max(0.0, est_cost - mean_cost)
        projected_month = excess * policy.weekly_volume * 4.33
        out.flags.append(Flag(
            lane="cost", code="cost_anomaly", severity=_sev(worst_z, zthr),
            message=f"Compute anomaly on {', '.join(drivers)} (z={worst_z:.1f} vs baseline)",
            evidence={
                "z_scores": z_scores,
                "tokens": tokens,
                "projected_monthly_overspend_usd": round(projected_month, 2),
            },
        ))
        out.score = min(1.0, worst_z / (zthr * 2))

    # hard ceiling from policy
    ceiling = policy.threshold("max_tokens_per_response", 1e9)
    if resp.completion_tokens > ceiling:
        out.flags.append(Flag(
            lane="cost", code="token_ceiling", severity=Severity.MEDIUM,
            message=f"Completion {resp.completion_tokens} tokens exceeds policy ceiling {int(ceiling)}",
            evidence={"completion_tokens": resp.completion_tokens, "ceiling": int(ceiling)},
        ))
        out.score = max(out.score, 0.6)

    # retry-loop detection
    ph = _prompt_hash(policy.use_case, req.prompt)
    win = _recent[policy.use_case]
    repeats = sum(1 for h in win if h == ph)
    win.append(ph)
    if resp.retries >= 3 or repeats >= 4:
        out.flags.append(Flag(
            lane="cost", code="retry_loop", severity=Severity.HIGH,
            message=f"Silent retry loop (upstream retries={resp.retries}, identical prompt x{repeats + 1})",
            evidence={"retries": resp.retries, "identical_prompt_repeats": repeats + 1},
        ))
        out.score = max(out.score, 0.75)

    out.evidence = {"metrics": metrics, "z_scores": z_scores, "est_cost_usd": round(est_cost, 5)}
    out.latency_ms = (time.perf_counter() - t0) * 1000
    return out


def _sev(z: float, thr: float) -> Severity:
    if z >= thr * 2:
        return Severity.HIGH
    if z >= thr * 1.4:
        return Severity.MEDIUM
    return Severity.LOW
