# Sentinel — Business Proposal

## 1. Problem framing

Enterprises have moved generative AI into production across many use cases at
once — customer-facing chatbots, internal copilots, decision-support tools inside
regulated workflows. Each carries a different risk signature depending on the
model, the data it draws on, and how its output is used downstream.

Three failure modes are structural, not incidental, and all three are discovered
**after** the damage is done:

1. **Confidently wrong.** Models produce fluent, authoritative answers on
   hallucinated facts. There is often no real-time ground truth to check against —
   the same knowledge gaps that cause the hallucination make verification hard.
2. **Quietly expensive.** Runaway token usage, silent retry loops and downstream
   rework inflate compute bills with no single alarming event.
3. **Subtly irresponsible.** Biased phrasing, unsafe advice or PII leakage erode
   trust and invite regulatory penalty long before anyone notices a pattern.

These risks **overlap** (a fabricated detail about a person is both a
hallucination and a privacy breach), **compound** in multi-turn and agentic
workflows, and are governed by **regulation that varies by geography and keeps
changing**. Enterprises consume foundation models via API, so a checker can only
work at the input/output layer.

The central tension: **deep checking of every answer adds latency and cost.**
Oversight that makes the AI too slow defeats its own purpose. Existing tools log
what happened; almost none judge each response as it happens.

**The need:** a model-agnostic control plane that observes every AI response live
across performance, cost and responsibility, and decides within milliseconds
whether to pass, flag, repair or escalate it.

## 2. Solution design

**Sentinel** is inline middleware between any AI model and its users. Every
response is scored live across three lanes and then subjected to a graduated
action.

### Risk-tiered routing (the latency answer)

A router assigns each interaction a tier from **policy** (use-case base tier) plus
**signals**: high-stakes keywords (money / health / legal), conversation depth,
and whether the response drives an agent action.

- **Low tier** — classifier-only checks (PII, unsafe, bias).
- **Medium tier** — adds retrieval grounding and contradiction checks.
- **High tier** — adds the deeper independent verifier and the full battery.

Lanes run in parallel with response streaming; only a red flag interrupts the
user. In the prototype this holds added latency to low single-digit milliseconds
at p50.

### The three lanes

| Lane | Mechanism | Catches |
|---|---|---|
| **Performance** | retrieval verification + claim-level grounding + numeric / polarity contradiction checks + an **independent verifier** scoring factuality, groundedness and calibration (grounding-based in the prototype; a hosted LLM-as-judge in production) | "confidently wrong", unsupported numbers, negated claims |
| **Cost** | online Welford baselines per use case; z-score + absolute-floor anomaly gating; retry-loop detection | workflows quietly burning 10× the compute they should |
| **Responsibility** | millisecond classifiers: PII entity detection (leak = PII in output not in input), unsafe-content patterns, bias/generalisation detection, agent-action detection | privacy leaks, unsafe advice, biased phrasing, unauthorised agent actions |

### Graduated actions

`Pass → Annotate (caution label + sources) → Auto-repair (grounded regeneration,
then re-check) → Block & escalate (human review queue, evidence attached)`.

### Governance & audit

A configurable **policy layer** (per use case, geography, risk appetite) drives
every threshold and blocking rule and is hot-reloadable. Every decision is
written to an **append-only, hash-chained audit log** with its evidence and the
exact policy version applied.

### Feedback loop

Reviewer overrides (false positive → loosen) and reported misses (false negative
→ tighten) nudge per-use-case thresholds, so the over-/under-flagging trade-off
is continuously calibrated rather than hard-coded.

### Metrics

False-positive / false-negative rates, added latency (p50/p95), and estimated
cost avoided are first-class, dashboard-level metrics.

## 3. Target users

| User | Need | What Sentinel gives them |
|---|---|---|
| **AI platform / MLOps team** | ship AI features without owning bespoke guardrails per app | one control plane in front of every model; new use case = one config block |
| **Risk, compliance & legal** | demonstrable oversight of AI outputs; audit trail per jurisdiction | policy layer + immutable hash-chained decision log |
| **Product owners** | fewer incidents, no latency regression | graduated actions; auto-repair instead of blunt blocking |
| **Human reviewers / ops** | triage only what matters, with context | review queue with full evidence; feedback loop that reduces their load over time |
| **Finance / FinOps** | control AI compute spend | cost-anomaly lane, projected-overspend estimates |

**Buyer:** Head of AI Platform / Chief AI Officer, co-sponsored by CISO / Chief
Risk Officer. **Initial footprint:** the 2–5 highest-risk GenAI use cases in a
regulated enterprise (financial services, healthcare, insurance, public sector).

## 4. Business case and impact

### Value drivers

- **Incident avoidance.** One prevented "confidently wrong" answer in a regulated
  workflow (mis-stated policy, unsuitable advice, PII disclosure) can cost far
  more than a year of oversight — in remediation, regulatory penalty and churn.
- **Compute savings.** The cost lane surfaces runaway prompts and retry loops that
  typically account for a meaningful share of an enterprise LLM bill.
- **Reviewer efficiency.** Risk-tiered routing + the feedback loop mean humans see
  a shrinking, higher-precision queue.
- **Faster approval to ship.** A ready-made audit trail shortens the risk sign-off
  that currently gates enterprise AI rollouts.

### Illustrative model (enterprise with ~40k AI interactions/week across 3 use cases)

| Item | Assumption | Annualised |
|---|---|---|
| Interactions overseen | 40k/week | ~2.1M/year |
| Share needing the LLM-as-judge, production (high tier + triggered) | ~12% | ~250k judge calls |
| Judge cost (small model, cached) | ~$0.0006 / call | ~$150/year direct model cost |
| Compute reclaimed (anomaly + retry suppression) | 5–10% of LLM spend | material, use-case dependent |
| Incidents prevented | even 1–2 serious/year | dominant term |

The oversight compute cost is negligible next to the downside it removes — that
asymmetry is the core of the business case.

### Pricing

Platform subscription by volume tier + overseen-use-case count; premium for
regulated-industry policy packs and on-prem / VPC deployment. Land with 2–3 use
cases, expand across the AI estate.

### Why now / moat

- Regulators are moving from "log it" to "govern it" — audit trails are becoming
  mandatory.
- Model-agnostic positioning: value grows as an enterprise adds models and agents.
- The feedback loop compounds — the policy library and labelled-example set per
  customer become switching costs.

## 5. Phased roadmap

| Phase | Timeline | Scope | Exit criteria |
|---|---|---|---|
| **0 — Prototype** *(this repo)* | done | 3 lanes, router, graduated actions, hash-chained audit, dashboard, feedback loop, deterministic grounding-based verifier, 16-scenario demo | core mechanism demonstrated on simulated multi-use-case traffic |
| **1 — Design partner pilot** | 0–3 months | 1 regulated enterprise, 2–3 use cases; swap TF-IDF → managed vector store; Postgres/event-log audit; SSO; real FP/FN measurement | agreed FP/FN targets met on real traffic; latency budget respected |
| **2 — Production hardening** | 3–6 months | HA stateless deployment, per-region policy packs, streaming/token-level interception, connectors (major LLM providers and local models), reviewer console | first use case in production; SOC 2 path started |
| **3 — Estate rollout** | 6–12 months | self-serve onboarding of new use cases, agent-action oversight (tool-call gating), policy-as-code with CI, drift monitoring on the judge itself | 10+ use cases; measurable compute savings; audit accepted by customer's regulator liaison |
| **4 — Platform** | 12+ months | marketplace of policy packs by industry, benchmark/leaderboard of model trustworthiness, managed multi-tenant offering | multi-customer; policy library is a defensible asset |

## 6. Key risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| **No reliable ground truth** to verify claims | verifier can be wrong | combine retrieval + numeric/polarity checks + AI-as-judge; abstain and annotate ("insufficient evidence") rather than assert; never auto-repair on a high tier without re-check |
| **Over-flagging → alert fatigue**, users bypass warnings | oversight ignored | risk-tiered routing limits flags to what matters; graduated actions (annotate, not block); feedback loop loosens thresholds on overrides; FP rate is a tracked KPI |
| **Under-flagging → liability** | real harm reaches users | escalation-biased defaults on high tier; reported misses tighten thresholds; regular replay of a labelled regression set |
| **Added latency** regresses UX | product rejects the layer | parallel lanes, classifier-only fast path, per-tier latency budgets with fail-safe on timeout; p50/p95 on the dashboard |
| **The judge model itself drifts / is jailbroken** | silent quality loss | monitor judge score distributions over time; periodic human-graded calibration set; judge model is swappable via config |
| **Regulation diverges by geography** and evolves | hard-coded rules age | policy-as-code per region; thresholds and blocking rules are config, not code; audit records the policy version applied |
| **API-only model access** limits inspection | can't see model internals | Sentinel is explicitly an input/output-layer control plane; no dependency on logprobs or weights |
| **Prompt-injection via retrieved content** | poisoned "sources" | treat retrieved context as data, not instruction; the judge is given a fixed rubric; knowledge bases are governed inputs |
| **Buyer sees it as "just another gateway"** | slow sales | lead with the audit trail + FP/FN metrics + incident case studies, not features |

## 7. Success metrics

- **Trustworthiness:** false-positive rate < target (tuned per use case),
  false-negative rate on the regression set trending to zero.
- **Latency:** p95 added latency within each use case's budget.
- **Efficiency:** review-queue volume per 1k interactions falling quarter on
  quarter; % compute reclaimed.
- **Adoption:** number of use cases onboarded; % of enterprise AI traffic behind
  Sentinel.
- **Assurance:** audit chain verifiable at any time; time-to-risk-sign-off for a
  new AI feature.


