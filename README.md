# Sentinel — a real-time, model-agnostic AI control plane

## Table of contents

- [Introduction](#introduction)
- [How AI enables the solution](#how-ai-enables-the-solution)
- [Architecture](#architecture)
- [Key features](#key-features)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the prototype](#running-the-prototype)
- [What the demo shows](#what-the-demo-shows)
- [Scalability](#scalability)
- [Impact](#impact)
- [Project structure](#project-structure)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Maintainers](#maintainers)

## Introduction

Every enterprise AI deployment ships with three invisible risks, and today all
three are discovered only *after* the damage is done:

- **Confidently wrong** — fluent, authoritative answers built on hallucinated
  facts, caught only after a customer, clinician or trader has acted on them.
- **Quietly expensive** — runaway token usage, silent retry loops and downstream
  rework that surface weeks later on an invoice.
- **Subtly irresponsible** — biased phrasing, unsafe advice or personal data
  leaking into outputs, eroding trust and inviting regulatory penalty.

Existing monitoring tools *log what happened*; almost none *judge each response
as it happens*. The core tension: deep checking of every answer adds latency and
cost — oversight that makes the AI too slow defeats its own purpose.

**Sentinel** is a lightweight proxy that sits between any AI model and its users
and scores every response live across three lanes — **Performance**, **Cost**,
**Responsibility** — then decides within milliseconds whether to **pass**,
**annotate**, **auto-repair** or **block & escalate** it. It is model- and
industry-agnostic: one layer that makes every other AI deployment safer, cheaper
and accountable.

This repository is a **working prototype**. It runs end-to-end with **zero
external services or API keys** — a deterministic, grounding-based verifier
scores every answer against retrieved sources. That verifier is isolated behind
a single class, so a hosted LLM-as-judge endpoint can be dropped in later
without touching the control plane.

## How AI enables the solution

| Where | AI technique | Why |
|---|---|---|
| Performance lane | **Independent verifier (the "AI-as-judge" pattern)** — a separate scorer rates factuality, groundedness and confidence calibration against retrieved sources; grounding-based in this build, a pluggable swap-in point for a hosted LLM-as-judge | A second, independent judgment is the only scalable way to catch "confidently wrong" answers when there is no hard ground truth |
| Performance lane | **Retrieval verification** — TF-IDF retrieval over per-use-case knowledge bases + claim-level grounding, numeric-contradiction and polarity-contradiction checks | Grounds the judge and catches wrong numbers / negated claims deterministically, cheaply |
| Cost lane | **Statistical anomaly detection** — online Welford baselines per use case, z-score + absolute-floor gating | Surfaces workflows quietly burning 10× the compute they should, with no single alarming event |
| Responsibility lane | **Millisecond classifiers** — PII entity detection, unsafe-content patterns, bias / generalisation detection, agent-action detection | Fast enough to run on 100% of traffic without a latency hit |
| Decision layer | **Risk-tiered routing** — content + context signals select which lanes and how deep | Keeps p50 added latency in low single-digit milliseconds while still giving high-stakes traffic the full battery |
| Auto-repair | **Grounded regeneration** — the answer is rebuilt constrained to verified sources, then Sentinel re-checks it (a hosted rewrite model can replace the built-in step) | Turns a bad answer into a good one instead of just blocking |
| Feedback loop | **Threshold learning** — reviewer overrides and reported misses nudge per-use-case thresholds | The over-/under-flagging trade-off is *tuned continuously*, not hard-coded |

The prototype is explicit about **what is AI and what is not**: deterministic
retrieval, regex/statistical classifiers and business rules do the cheap work on
every request; the deeper verifier and the grounded repair pass run only where
they change the decision (high-stakes tier, confident claims, detected
contradictions). Every decision records its `judge_engine` so a reviewer can see
exactly which mechanism produced it.

## Architecture

```
                 ┌─────────────────────────  SENTINEL CONTROL PLANE  ─────────────────────────┐
                 │                                                                            │
  client  ──▶  /v1/proxy ──▶ upstream model ──▶  RISK-TIERED ROUTER                           │
   app          /v1/review  (any provider)        │  base tier (policy) + high-stakes         │
                 │                                 │  keywords + multi-turn depth + agent      │
                 │                                 ▼                                          │
                 │                     ┌───────────┴───────────┐  (run in parallel,           │
                 │                     ▼           ▼           ▼   capped at latency budget)   │
                 │              PERFORMANCE      COST      RESPONSIBILITY                      │
                 │              retrieval +    Welford    PII / unsafe /                       │
                 │              verifier      baselines  bias / agent-action                 │
                 │                     └───────────┬───────────┘                              │
                 │                                 ▼                                          │
                 │                        AGGREGATOR + DECISION MATRIX                        │
                 │                    pass · annotate · auto-repair · block                   │
                 │                                 │                                          │
                 │           ┌─────────────────────┼───────────────────────┐                  │
                 │           ▼                     ▼                       ▼                  │
                 │   released text +        hash-chained            human review queue        │
                 │   caution label +        audit record            (escalations)             │
                 │   sources                     │                       │                    │
                 │                               ▼                       ▼                    │
                 │                     GOVERNANCE POLICY LAYER  ◀──  FEEDBACK LOOP             │
                 │                     (per use case / geo)      threshold learning            │
                 └────────────────────────────────────────────────────────────────────────────┘
                                               │
                                     LIVE OPERATOR DASHBOARD
                          decisions · metrics · FP rate · review queue · evidence
```

**Placement.** Sentinel is inline middleware. `POST /v1/proxy` forwards a prompt
to the upstream model and oversees the answer (a built-in stub stands in for the
model in this prototype — wire your own model here); `POST /v1/review` oversees
an answer you already have (any provider, any stack) and is what the demo uses.
Checks run *in parallel* with the response so only a red flag interrupts the user.

**Graduated actions, not a binary block:**

| Action | When | What the user gets |
|---|---|---|
| **Pass** | all lanes clean | the response, unchanged (+ "verified against" note on higher tiers) |
| **Annotate** | medium-severity or unverifiable claims, cost anomaly, mild bias | the response + a caution label + source list |
| **Auto-repair** | unsupported / contradictory factual claims on a repairable tier | a regenerated, source-grounded answer, re-checked before release |
| **Block & escalate** | PII leak, unsafe advice, agent action, or repair failed on a high-stakes response | nothing — routed to the human review queue with all evidence attached |

**Audit trail.** Every decision is written to an append-only SQLite store as a
SHA-256 record **hash-chained** to the previous one, so the log is
tamper-evident — the kind of trail regulators increasingly require. `GET
/api/audit/verify` re-computes the whole chain.

## Key features

- **Three-lane live scoring** of every response (performance, cost, responsibility).
- **Risk-tiered router** — cheap checks on all traffic, the full battery only on
  money/health/legal, multi-turn escalation, or agent actions.
- **Model- and provider-agnostic** — oversee any hosted model, a local model, or a
  black-box API via the `/v1/review` endpoint.
- **Independent verifier** — grounding + numeric/polarity contradiction scoring,
  isolated behind one class so a hosted LLM-as-judge can be swapped in.
- **Overlapping-risk aware** — a fabricated detail about a named person is flagged
  by *both* the responsibility and performance lanes (see the demo).
- **Configurable governance policy layer** — per use case: base risk tier, latency
  budget, geography, thresholds, blocking rules, knowledge base.
- **Graduated actions** including grounded auto-repair with re-verification.
- **Tamper-evident, hash-chained audit log** + human review queue.
- **Feedback loop** — reviewer overrides / reported misses tune thresholds live.
- **Live operator dashboard** — decision stream, pass/annotate/repair/block mix,
  p50/p95 added latency, estimated cost avoided, false-positive rate, audit-chain
  status, and a click-through evidence drawer for any decision.
- **Runtime telemetry** on every decision — route reasoning, added latency, tokens,
  estimated cost, per-lane latency, judge engine.

## Requirements

- Python 3.10 or newer
- The packages in `requirements.txt` (`fastapi`, `uvicorn`, `pyyaml`,
  `python-dotenv`, `httpx`; `pytest` is dev-only)
- **No API key and no internet connection are required.** The prototype, the demo
  and the tests all run fully offline.

## Installation

```bash
git clone <this-repo-url>
cd sentinel
python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

All configuration is optional.

1. **Environment** — copy `.env.example` to `.env` to override defaults:

   | Variable | Default | Purpose |
   |---|---|---|
   | `SENTINEL_DB` | `sentinel_audit.db` | audit store path |

2. **Governance policy** — `config/policies.yaml` defines one block per enterprise
   AI use case (risk tier, latency budget, geography, thresholds, blocking rules,
   knowledge base). Edit or add use cases here.

3. **Knowledge bases** — drop a Markdown file in `config/knowledge/` and point a
   use case's `knowledge_base` at its name. These are the sources the Performance
   lane verifies against.

## Running the prototype

Start the control plane:

```bash
uvicorn sentinel.app:app --port 8000
```

Open the dashboard at **http://localhost:8000/**.

In a second terminal, generate traffic across the three simulated use cases:

```bash
python -m demo.run_demo            # one pass through 16 scripted scenarios
python -m demo.run_demo --surge 3  # replay 3x to simulate a volume spike
python -m demo.run_demo --loop     # keep generating traffic for a live demo
```

Run the tests:

```bash
pytest -q
```

## What the demo shows

The demo runs an enterprise operating **three AI use cases at once**, each with a
different risk signature and latency budget:

| Use case | Surface | Base tier | Latency budget | Geo |
|---|---|---|---|---|
| `support-assistant` | customer-facing | low | 400 ms | EU / GDPR |
| `internal-kb` | employee copilot | medium | 900 ms | EU |
| `decision-support` | regulated (investment) | high | 2500 ms | EU |

16 scripted interactions exercise every risk type and every action:

| Scenario | Lane(s) that fire | Action |
|---|---|---|
| Grounded refund / leave / allocation answers | — | **pass** |
| Hallucinated 90-day return window | performance (numeric mismatch) | **auto-repair** |
| "I guarantee your warranty covers water damage" | performance (contradiction + confidently-wrong) | **annotate** |
| Fabricated carry-over leave policy | performance (numeric mismatch) | **auto-repair** |
| Response leaks another customer's email + phone | responsibility (PII) | **block & escalate** |
| "Older people usually can't handle this" | responsibility (bias) | **annotate** |
| 4 200-token answer to a one-line question | cost (anomaly + token ceiling) | **annotate** |
| Silent retry loop (5 upstream retries) | cost (retry loop) | **annotate** |
| "Risk-free, guaranteed 40% return" | responsibility (unsafe) + performance | **block & escalate** |
| Multi-turn: 25% single-position, leverage OK | router escalates → performance | **block & escalate** |
| Agent: "I have initiated the €2M wire transfer" | responsibility (agent action) | **block & escalate** |
| Fabricated + private detail about a named person | responsibility (PII) **and** performance (hallucination) | **block & escalate** |
| Hedged, contradictory performance-fee answer | performance (mismatch, severity-capped) | **annotate** |

Typical run: **~2 ms p50 / ~5 ms p95 added latency**, audit chain intact, ~5
escalations queued for human review (more with `--surge`). Resolve one in the
dashboard and watch the feedback loop adjust that use case's threshold.

## Scalability

- **Stateless request path.** The proxy holds no per-request state; horizontal
  replicas sit behind a load balancer. The audit store is the only shared
  component and is append-only (SQLite in the prototype → Postgres / an event log
  in production).
- **Cost scales sub-linearly with traffic.** The router sends the majority of
  low-stakes traffic through classifier-only checks. Only the tail that matters
  runs the deeper verifier (and, in production, the LLM-as-judge).
- **New use case = one YAML block + one Markdown knowledge file.** No code change.
- **Provider-agnostic** — the same control plane fronts every model an enterprise
  runs. Reference parameters (tens of thousands of interactions/week across a
  support assistant, a knowledge assistant and a decision-support tool) are the
  demo's default shape.
- **Regulation changes are config, not code** — thresholds, blocking rules and the
  policy layer are hot-reloadable; the feedback loop keeps them calibrated.

## Impact

- **"Finding out later" becomes "finding it first."** Hallucinations, PII leaks
  and unsafe advice are caught *before* the response reaches the user.
- **One layer, every deployment.** A single control plane makes an enterprise's
  entire AI estate safer, cheaper and auditable at once.
- **Quantified oversight.** False-positive / false-negative rates, added latency
  and cost avoided are first-class dashboard metrics — the numbers a skeptical
  stakeholder asks for.
- **Regulator-ready.** Every decision carries an immutable, hash-chained record
  with its evidence and the exact policy version applied.

## Project structure

```
sentinel/
├── sentinel/
│   ├── app.py            FastAPI surface (/v1/proxy, /v1/review, /api/*, dashboard)
│   ├── pipeline.py       route → lanes (parallel) → aggregate → action → audit
│   ├── router.py         risk-tiered router
│   ├── policy.py         governance policy layer + threshold overrides
│   ├── lanes/
│   │   ├── performance.py  retrieval + grounding + numeric/polarity contradiction + verifier
│   │   ├── cost.py         Welford baselines, z-score + floor anomaly, retry-loop detection
│   │   └── responsibility.py  PII, unsafe content, bias, agent-action classifiers
│   ├── retrieval.py     dependency-free TF-IDF retriever
│   ├── llm.py           deterministic verifier + grounded repair (swap-in point for a hosted LLM)
│   ├── audit.py         hash-chained SQLite audit store + review queue + baselines
│   ├── feedback.py      reviewer overrides / reported misses → threshold learning
│   └── dashboard/       single-page live operator dashboard
├── config/
│   ├── policies.yaml    per-use-case governance policy
│   └── knowledge/       source documents the Performance lane verifies against
├── demo/
│   ├── scenarios.py     16 scripted interactions across 3 use cases
│   └── run_demo.py      fires them at a running server, prints a summary
└── tests/               pytest suite (offline, no key needed)
```

## Troubleshooting

**The dashboard is empty.** Run `python -m demo.run_demo` (with the server
running) to generate decisions.

**`python -m demo.run_demo` cannot connect.** Start the server first:
`uvicorn sentinel.app:app --port 8000`. The demo defaults to
`http://127.0.0.1:8000`; override with `--server`.

**`UnicodeEncodeError` on Windows.** The demo reconfigures stdout to UTF-8
automatically; if you still see it, run `set PYTHONIOENCODING=utf-8` first.

**Reset everything.** Stop the server and delete `sentinel_audit.db` and
`config/threshold_overrides.json`.

## FAQ

**Q: Does it need a real LLM to work?**
A: No. The verifier is deterministic (grounding + numeric/polarity contradiction
checks). It follows the "AI-as-judge" pattern and is isolated behind one class
(`sentinel/llm.py`), so a hosted LLM-as-judge endpoint can be dropped in for
production without changing the control plane.

**Q: How does it avoid slowing the AI down?**
A: The risk-tiered router runs only classifier-level checks on the bulk of
low-stakes traffic and runs lanes in parallel with response streaming. Added
latency is low single-digit milliseconds at p50 in the demo; only a red flag
interrupts the user.

**Q: How is the over-flagging / under-flagging trade-off handled?**
A: It is *tuned*, not solved. Thresholds live in the policy layer per use case,
and the feedback loop moves them when a reviewer overrides a block (loosen) or
reports a miss (tighten).

**Q: Can it oversee AI agents, not just chatbots?**
A: Yes. Mark a request `is_agent: true`; the router forces the high tier and the
responsibility lane blocks any response claiming to have executed a high-stakes
action, since policy requires a human to authorise it.

**Q: What about multi-turn conversations?**
A: Conversation depth is a routing signal — three or more prior user turns bump
the risk tier to reflect compounding risk.

## Maintainers

Team KGP — Indian Institute of Technology Kharagpur

- Jagadeesh Kunta (Team leader) — Mining Engineering
- Sai Keerthana — Industrial and Systems Engineering

Built for the Accenture Innovation Challenge 2026, problem track **ControlPlane.ai**.
