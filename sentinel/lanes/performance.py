"""Performance lane — factual verification.

1. Retrieve source chunks from the use case's knowledge base.
2. Ground each factual claim in the answer against those chunks (TF-IDF overlap).
3. Run the grounding-based verifier (deeper scoring on higher-risk traffic).
4. Compare the answer's *stated* confidence with its *actual* support:
   high stated confidence + low support == "confidently wrong".
"""
from __future__ import annotations

import re
import time

from ..llm import llm
from ..models import Flag, InteractionRequest, LaneResult, ModelResponse, Severity
from ..retrieval import Chunk, get_retriever, tokenize

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CONFIDENT = re.compile(
    r"\b(definitely|certainly|absolutely|without (a )?doubt|100%|for sure|"
    r"no question|rest assured|i (can )?assure you)\b", re.I)
_HEDGE = re.compile(
    r"\b(might|may|possibly|perhaps|i think|i believe|likely|probably|it seems|"
    r"not (entirely )?sure|as far as i know|generally)\b", re.I)
_FACTUAL_HINT = re.compile(
    r"\d|%|\bEUR\b|\$|\b(days?|weeks?|months?|years?|hours?|covers?|covered|eligible|"
    r"entitled|guarantee|refund|warranty|limit|fee|leave)\b", re.I)
# number, then the unit within a few words (handles "90-day", "15 unused leave days",
# "5 to 7 business days"); the number must not sit inside a longer digit group.
_NUM_UNIT = re.compile(
    r"(?<![\d,])(\d+(?:\.\d+)?)(?:[\s-]+[a-z]+){0,3}?[\s-]?"
    r"(%|percent|eur|usd|days?|weeks?|months?|years?|hours?)", re.I)
_NEG = re.compile(r"\b(not|no|never|cannot|can't|don't|doesn't|isn't|aren't|excluded|ineligible|"
                  r"not eligible|does not|do not)\b", re.I)
_ASSERT = re.compile(r"\b(yes|covered?|covers|eligible|included?|allowed|permitted|will|can|guarantee)\b", re.I)


def _is_factual(sentence: str) -> bool:
    return bool(_FACTUAL_HINT.search(sentence)) and len(tokenize(sentence)) >= 3


def _norm_unit(u: str) -> str:
    u = u.lower().rstrip("s")
    return {"percent": "%", "usd": "eur"}.get(u, u)


def _numeric_mismatch(answer: str, ctx_chunks) -> list[str]:
    """Answer states a number+unit that never appears in the sources, while the
    same unit *does* appear in the sources with different values."""
    ctx = " ".join(c.text for c in ctx_chunks).lower()
    ctx_pairs = {(v, _norm_unit(u)) for v, u in _NUM_UNIT.findall(ctx)}
    ctx_units = {u for _, u in ctx_pairs}
    bad = []
    for v, u in _NUM_UNIT.findall(answer.lower()):
        nu = _norm_unit(u)
        if (v, nu) not in ctx_pairs and nu in ctx_units:
            others = sorted({cv for cv, cu in ctx_pairs if cu == nu})
            bad.append(f"'{v} {u}' not in sources (sources say {', '.join(others)} {u})")
    return bad


def _contradiction(answer: str, ctx_chunks, retriever) -> str | None:
    """Flag only the risky direction: the answer asserts something positively
    while the most similar *source sentence* negates it."""
    src_sents = []
    for c in ctx_chunks:
        for s in _SENT_SPLIT.split(c.text):
            if len(tokenize(s)) >= 3:
                src_sents.append((c, s))
    for sent in _SENT_SPLIT.split(answer):
        if len(tokenize(sent)) < 4 or _NEG.search(sent) or not _ASSERT.search(sent):
            continue
        best, best_sim = None, 0.0
        for c, s in src_sents:
            sim = retriever.max_similarity(sent, [Chunk(c.doc, c.idx, s, c.heading)])
            if sim > best_sim:
                best, best_sim = (c, s), sim
        if best and best_sim > 0.34 and _NEG.search(best[1]):
            return (f"answer asserts what source {best[0].doc}#{best[0].idx} negates: "
                    f"\"{best[1].strip()[:90]}\"")
    return None


def run(req: InteractionRequest, resp: ModelResponse, policy, deep_verify: bool) -> tuple[LaneResult, str]:
    t0 = time.perf_counter()
    out = LaneResult(lane="performance")
    answer = (resp.text or "").strip()

    retriever = get_retriever(policy.knowledge_base) if policy.knowledge_base else None
    retrieved = retriever.search(f"{req.prompt} {answer}", k=4) if retriever else []
    ctx_chunks = [c for c, _ in retrieved if _ > 0.02]
    context_text = "\n".join(f"[{c.doc}#{c.idx} {c.heading}] {c.text}" for c in ctx_chunks)

    # ---- grounding of factual claims
    sentences = [s for s in _SENT_SPLIT.split(answer) if s.strip()]
    factual = [s for s in sentences if _is_factual(s)]
    unsupported: list[str] = []
    if retriever and factual:
        for s in factual:
            sim = retriever.max_similarity(s, ctx_chunks) if ctx_chunks else 0.0
            if sim < 0.12:
                unsupported.append(s.strip())
        grounding = 1.0 - len(unsupported) / len(factual)
    else:
        grounding = 1.0 if not factual else 0.5

    num_mismatch = _numeric_mismatch(answer, ctx_chunks) if ctx_chunks else []
    contradiction = _contradiction(answer, ctx_chunks, retriever) if (ctx_chunks and retriever) else None
    if num_mismatch:
        grounding = min(grounding, 0.35)
    if contradiction:
        grounding = min(grounding, 0.3)

    stated_confident = bool(_CONFIDENT.search(answer))
    hedged = bool(_HEDGE.search(answer))

    def _sev(s: Severity) -> Severity:
        # an answer that already hedged its own uncertainty is annotated, not blocked
        if hedged and not stated_confident and s.rank > Severity.MEDIUM.rank:
            return Severity.MEDIUM
        return s

    # ---- grounding-based verifier (full scoring vs a lighter grounding-only pass)
    judge = llm.judge(
        question=req.prompt, answer=answer, context=context_text,
        offline_grounding=grounding, confident_language=stated_confident,
    ) if (deep_verify or not retriever or stated_confident or num_mismatch or contradiction) else {
        "verdict": "ok", "factuality": round(0.4 + 0.6 * grounding, 3),
        "groundedness": round(grounding, 3), "calibration": 1.0 if not stated_confident else grounding,
        "unsupported_claims": unsupported, "engine": "grounding-only",
    }

    g_min = policy.threshold("grounding_min", 0.5)

    verdict = judge.get("verdict", "ok")
    if verdict == "fabricated":
        out.flags.append(Flag("performance", "hallucination", Severity.CRITICAL,
                              "Verifier judged the answer fabricated / unsupported by any source",
                              {"judge": judge, "unsupported": unsupported}))
        out.score = 1.0
    elif verdict in ("partially_unsupported", "insufficient_evidence") or grounding < g_min:
        sev = Severity.HIGH if grounding < g_min * 0.7 else Severity.MEDIUM
        detail = (f"{len(unsupported)} factual claim(s) unsupported by sources"
                  if unsupported else "claims weakly supported by sources")
        out.flags.append(Flag("performance", "weak_grounding", _sev(sev),
                              f"{detail} (grounding {grounding:.0%} < {g_min:.0%})",
                              {"unsupported": unsupported, "judge": judge}))
        out.score = max(out.score, 1.0 - grounding)

    if num_mismatch:
        out.flags.append(Flag("performance", "numeric_mismatch", _sev(Severity.HIGH),
                              "Answer contains figures that contradict the source documents",
                              {"details": num_mismatch}))
        out.score = max(out.score, 0.85)
    if contradiction:
        out.flags.append(Flag("performance", "source_contradiction", _sev(Severity.HIGH),
                              "Answer appears to contradict the closest matching source",
                              {"details": contradiction}))
        out.score = max(out.score, 0.8)

    # ---- confidently wrong
    if stated_confident and (grounding < g_min or verdict != "ok" or judge.get("calibration", 1.0) < 0.5):
        out.flags.append(Flag("performance", "confidently_wrong", Severity.HIGH,
                              "Answer is stated with high confidence but poorly supported",
                              {"calibration": judge.get("calibration"), "grounding": round(grounding, 3)}))
        out.score = max(out.score, 0.85)

    if not ctx_chunks and factual and verdict != "ok":
        out.flags.append(Flag("performance", "no_ground_truth", Severity.LOW,
                              "No source context available to verify factual claims",
                              {}))
        out.score = max(out.score, 0.4)

    out.evidence = {
        "grounding": round(grounding, 3),
        "factual_claims": len(factual),
        "unsupported_claims": unsupported,
        "stated_confident": stated_confident,
        "hedged": hedged,
        "judge": judge,
        "sources": [{"doc": c.doc, "idx": c.idx, "heading": c.heading} for c in ctx_chunks],
    }
    out.latency_ms = (time.perf_counter() - t0) * 1000
    return out, context_text
