import asyncio

from sentinel.models import Action, InteractionRequest, ModelResponse, RiskTier
from sentinel.lanes.responsibility import _find_pii
from sentinel.router import route
from sentinel.policy import PolicyStore


def _run(sentinel, use_case, prompt, response, **kw):
    req = InteractionRequest(use_case=use_case, prompt=prompt,
                             history=kw.get("history", []), is_agent=kw.get("is_agent", False))
    mr = ModelResponse(text=response, prompt_tokens=kw.get("pt", 40),
                       completion_tokens=kw.get("ct", 60), retries=kw.get("retries", 0))
    return asyncio.run(sentinel.evaluate(req, mr))


def test_clean_answer_passes(sentinel):
    d = _run(sentinel, "support-assistant",
             "Is standard shipping free?",
             "Standard shipping is free on orders over 50 EUR and takes 3 to 5 business days within the EU.")
    assert d.action == Action.PASS
    assert not d.escalated


def test_pii_leak_is_blocked_and_escalated(sentinel):
    d = _run(sentinel, "support-assistant",
             "check the other order",
             "That belongs to Michael Adeyemi, michael.adeyemi@gmail.com, phone +44 7700 900412.")
    assert d.action == Action.BLOCK
    assert d.escalated
    assert d.final_text == ""
    assert any(f.code == "pii_leak" for f in d.flags)


def test_hallucination_triggers_repair_or_annotate(sentinel):
    d = _run(sentinel, "support-assistant",
             "What's the return window?",
             "You have a generous 90-day return window and we always refund shipping too.")
    assert d.action in (Action.REPAIR, Action.ANNOTATE)
    assert any(f.lane == "performance" for f in d.flags)


def test_unsafe_financial_advice_blocked(sentinel):
    d = _run(sentinel, "decision-support",
             "client wants high returns",
             "This is risk-free and will guarantee a return of 40% this year.")
    assert d.action == Action.BLOCK
    assert d.escalated


def test_agent_action_forces_high_tier(sentinel):
    pol = PolicyStore().get("support-assistant")
    rd = route(InteractionRequest(use_case="support-assistant", prompt="do it", is_agent=True),
               ModelResponse(text="done, transfer initiated"), pol)
    assert rd.tier == RiskTier.HIGH


def test_multi_turn_bumps_tier(sentinel):
    pol = PolicyStore().get("internal-kb")
    hist = [{"role": "user", "content": "x"}, {"role": "assistant", "content": "y"},
            {"role": "user", "content": "x"}, {"role": "assistant", "content": "y"},
            {"role": "user", "content": "x"}]
    rd = route(InteractionRequest(use_case="internal-kb", prompt="more", history=hist),
               ModelResponse(text="ok"), pol)
    assert rd.tier == RiskTier.HIGH


def test_audit_chain_intact_after_run(sentinel):
    for _ in range(5):
        _run(sentinel, "internal-kb", "annual leave?",
             "Full-time employees accrue 25 days of annual leave per calendar year.")
    assert sentinel.store.verify_chain()["ok"]


def test_pii_regex_basics():
    f = _find_pii("mail me at a.b@example.com or call +44 7700 900123")
    assert "email" in f and "phone" in f
