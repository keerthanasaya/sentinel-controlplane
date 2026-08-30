"""FastAPI surface for Sentinel.

  POST /v1/proxy   — proxy a prompt to the upstream model, then oversee the answer
  POST /v1/review  — oversee an answer you already have (used to inject known-bad
                     answers in the demo)
  GET  /api/*      — dashboard data (decisions, metrics, review queue, policies)
  GET  /           — the live operator dashboard
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .audit import AuditStore
from .feedback import FeedbackEngine
from .llm import llm
from .models import InteractionRequest, ModelResponse
from .pipeline import Sentinel
from .policy import PolicyStore
from .settings import DASHBOARD_DIR, settings

state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = AuditStore(settings.db_path)
    policies = PolicyStore()
    state["store"] = store
    state["policies"] = policies
    state["sentinel"] = Sentinel(store, policies)
    state["feedback"] = FeedbackEngine(store, policies)
    yield


app = FastAPI(title="Sentinel — real-time AI control plane", version="0.1.0", lifespan=lifespan)


class ProxyIn(BaseModel):
    use_case: str
    prompt: str
    history: list[dict] = Field(default_factory=list)
    is_agent: bool = False
    system: str = "You are a helpful enterprise assistant. Be concise."
    request_id: str = ""


class ReviewIn(BaseModel):
    use_case: str
    prompt: str
    response: str
    history: list[dict] = Field(default_factory=list)
    is_agent: bool = False
    request_id: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    upstream_latency_ms: float = 0.0
    retries: int = 0
    model: str = "external"


class ResolveIn(BaseModel):
    status: str
    reviewer: str = "reviewer"
    note: str = ""


class FalseNegIn(BaseModel):
    decision_id: int
    note: str = ""


def _sentinel() -> Sentinel:
    return state["sentinel"]


@app.get("/health")
def health():
    return {"status": "ok", "verifier": "deterministic"}


@app.post("/v1/proxy")
async def proxy(body: ProxyIn):
    try:
        policy = state["policies"].get(body.use_case)
    except KeyError as e:
        raise HTTPException(400, str(e))
    max_tokens = int(policy.threshold("max_tokens_per_response", 800))
    mr = llm.generate(body.system, body.prompt, body.history, max_tokens=max_tokens)
    req = InteractionRequest(use_case=body.use_case, prompt=body.prompt, history=body.history,
                             is_agent=body.is_agent, request_id=body.request_id)
    decision = await _sentinel().evaluate(req, mr)
    return _decision_response(decision)


@app.post("/v1/review")
async def review(body: ReviewIn):
    if body.use_case not in state["policies"].policies:
        raise HTTPException(400, f"unknown use case '{body.use_case}'")
    pt = body.prompt_tokens or max(1, len(body.prompt) // 4)
    ct = body.completion_tokens or max(1, len(body.response) // 4)
    mr = ModelResponse(text=body.response, model=body.model, prompt_tokens=pt,
                       completion_tokens=ct, latency_ms=body.upstream_latency_ms,
                       retries=body.retries)
    req = InteractionRequest(use_case=body.use_case, prompt=body.prompt, history=body.history,
                             is_agent=body.is_agent, request_id=body.request_id)
    decision = await _sentinel().evaluate(req, mr)
    return _decision_response(decision)


def _decision_response(decision) -> dict:
    d = decision.to_dict()
    return {
        "request_id": d["request_id"],
        "action": d["action"],
        "tier": d["tier"],
        "released_text": d["final_text"] if d["action"] != "block" else None,
        "annotations": d["annotations"],
        "escalated": d["escalated"],
        "repaired": d["repaired"],
        "review_id": decision.review_id,
        "decision_id": d["telemetry"].get("decision_row_id"),
        "flags": d["flags"],
        "telemetry": d["telemetry"],
        "audit_hash": d["hash"],
    }


@app.get("/api/decisions")
def decisions(limit: int = 60, use_case: str | None = None):
    return state["store"].recent_decisions(limit=limit, use_case=use_case)


@app.get("/api/decisions/{row_id}")
def decision_detail(row_id: int):
    d = state["store"].get_decision(row_id)
    if not d:
        raise HTTPException(404, "not found")
    return d


@app.get("/api/metrics")
def metrics():
    return state["store"].metrics()


@app.get("/api/review-queue")
def review_queue(status: str | None = "open"):
    return state["store"].review_queue(status=status)


@app.post("/api/review-queue/{review_id}/resolve")
def resolve(review_id: int, body: ResolveIn):
    try:
        return state["feedback"].resolve_review(review_id, body.status, body.reviewer, body.note)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, str(e))


@app.post("/api/feedback/false-negative")
def false_negative(body: FalseNegIn):
    try:
        return state["feedback"].report_false_negative(body.decision_id, body.note)
    except KeyError as e:
        raise HTTPException(404, str(e))


@app.get("/api/policies")
def policies():
    return {"version": next(iter(state["policies"].policies.values())).version
            if state["policies"].policies else "0",
            "use_cases": state["policies"].as_dict()}


@app.get("/api/audit/verify")
def audit_verify():
    return state["store"].verify_chain()


if DASHBOARD_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(DASHBOARD_DIR)), name="static")

    @app.get("/")
    def dashboard():
        return FileResponse(str(DASHBOARD_DIR / "index.html"))
