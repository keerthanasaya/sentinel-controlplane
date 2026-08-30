"""Fire the scripted scenarios at a running Sentinel server and print a summary.

Usage:
    python -m demo.run_demo                 # one pass through all scenarios
    python -m demo.run_demo --server URL    # target a non-default server
    python -m demo.run_demo --surge 3       # replay everything 3x to simulate a spike
    python -m demo.run_demo --loop          # keep generating traffic (Ctrl-C to stop)

Start the server first:  uvicorn sentinel.app:app --reload
"""
from __future__ import annotations

import argparse
import time

import sys

import httpx

from .scenarios import SCENARIOS

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ACTION_GLYPH = {"pass": "PASS", "annotate": "ANNOTATE", "repair": "REPAIR", "block": "BLOCK"}


def warmup(client: httpx.Client, server: str) -> None:
    """Send a few clean requests per use case so the cost lane has a baseline."""
    clean = {
        "support-assistant": [
            ("What does standard shipping cost?", "Standard shipping is free on orders over 50 EUR."),
            ("How fast is express delivery?", "Express shipping costs 12 EUR and delivers within 1 to 2 business days."),
            ("Which countries do you ship to?", "Acme ships to the EU, the UK, Norway and Switzerland."),
            ("How do I end my AcmeCloud plan?", "You can cancel any time from the billing page with no fee."),
        ],
        "internal-kb": [
            ("How many annual leave days?", "Full-time employees accrue 25 days of annual leave per year."),
            ("How many sick days?", "Employees receive up to 10 paid sick days per year."),
            ("What is the remote work limit?", "Employees may work remotely up to 3 days per week with manager agreement."),
            ("What is the meal allowance?", "Meals while travelling are reimbursed up to 40 EUR per day."),
        ],
        "decision-support": [
            ("What is the advisory fee?", "The standard advisory fee is 0.9% of assets under management per year."),
            ("What is the Conservative portfolio?", "Conservative is 60% bonds / 40% equities, expected return about 4% per year."),
            ("When is sign-off needed?", "Any transfer or trade above 250,000 EUR requires a second adviser's sign-off."),
            ("Is there a performance fee?", "There is no performance fee on standard mandates."),
        ],
    }
    for uc, pairs in clean.items():
        for _ in range(4):
            for p, r in pairs:
                client.post(f"{server}/v1/review", json={"use_case": uc, "prompt": p, "response": r})


def run_once(client: httpx.Client, server: str, surge: int = 1) -> None:
    rows = []
    for rep in range(surge):
        for sc in SCENARIOS:
            payload = {
                "use_case": sc["use_case"],
                "prompt": sc["prompt"],
                "response": sc["response"],
                "history": sc.get("history", []),
                "is_agent": sc.get("is_agent", False),
                "retries": sc.get("retries", 0),
                "completion_tokens": sc.get("completion_tokens", 0),
            }
            t0 = time.perf_counter()
            resp = client.post(f"{server}/v1/review", json=payload)
            dt = (time.perf_counter() - t0) * 1000
            resp.raise_for_status()
            d = resp.json()
            rows.append((sc["name"], sc["use_case"], d["tier"], d["action"],
                         len(d["flags"]), d["telemetry"].get("sentinel_overhead_ms", 0),
                         sc["expect"]))

    print(f"\n{'scenario':38} {'use case':17} {'tier':7} {'action':12} flags  +lat(ms)  expected")
    print("-" * 110)
    for name, uc, tier, action, nf, lat, exp in rows:
        print(f"{name[:37]:38} {uc:17} {tier:7} {ACTION_GLYPH.get(action, action):12} "
              f"{nf:^5} {lat:8.0f}  {exp}")

    m = client.get(f"{server}/api/metrics").json()
    print("\nAggregate:")
    print(f"  decisions           {m['total_decisions']}")
    print(f"  by action           {m['by_action']}")
    print(f"  added latency p50/p95  {m['added_latency_ms']['p50']} / {m['added_latency_ms']['p95']} ms")
    print(f"  est. cost avoided   ${m['est_cost_avoided_usd']}")
    print(f"  open review items   {m['review_queue'].get('open', 0)}")
    print(f"  audit chain         {'intact' if m['chain']['ok'] else 'BROKEN'} "
          f"({m['chain']['count']} records)")
    print("\nOpen the dashboard to inspect any decision:  " + server + "/\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--server", default="http://127.0.0.1:8000")
    ap.add_argument("--surge", type=int, default=1, help="replay all scenarios N times")
    ap.add_argument("--loop", action="store_true", help="keep generating traffic")
    ap.add_argument("--no-warmup", action="store_true")
    args = ap.parse_args()

    with httpx.Client(timeout=60) as client:
        client.get(f"{args.server}/health").raise_for_status()
        if not args.no_warmup:
            print("warming up cost baselines…")
            warmup(client, args.server)
        run_once(client, args.server, surge=args.surge)
        while args.loop:
            time.sleep(4)
            run_once(client, args.server, surge=args.surge)


if __name__ == "__main__":
    main()
