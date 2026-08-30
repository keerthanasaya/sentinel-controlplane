"""Append-only audit store (SQLite).

Every decision is written with a SHA-256 hash chained to the previous record, so
the log is tamper-evident — the kind of trail regulators increasingly ask for.
Also holds learned cost baselines (Welford) and the human review queue.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from typing import Any

from .models import Decision

_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT, use_case TEXT, tier TEXT, action TEXT,
    escalated INTEGER, repaired INTEGER,
    original_text TEXT, final_text TEXT,
    annotations TEXT, flags TEXT, lane_results TEXT, telemetry TEXT,
    policy_version TEXT, created_at REAL,
    prev_hash TEXT, hash TEXT
);
CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER, use_case TEXT, reason TEXT,
    status TEXT DEFAULT 'open', reviewer TEXT, resolution_note TEXT,
    created_at REAL, resolved_at REAL
);
CREATE TABLE IF NOT EXISTS baselines (
    use_case TEXT, metric TEXT, n INTEGER, mean REAL, m2 REAL,
    PRIMARY KEY (use_case, metric)
);
CREATE TABLE IF NOT EXISTS feedback_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id INTEGER, label TEXT, note TEXT, created_at REAL
);
"""


class AuditStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------- hash chain
    def _last_hash(self) -> str:
        row = self._conn.execute("SELECT hash FROM decisions ORDER BY id DESC LIMIT 1").fetchone()
        return row["hash"] if row else "GENESIS"

    @staticmethod
    def _digest(prev_hash: str, d: Decision) -> str:
        core = {
            "request_id": d.request_id, "use_case": d.use_case, "tier": d.tier.value,
            "action": d.action.value, "original_text": d.original_text,
            "final_text": d.final_text, "flags": [f.to_dict() for f in d.flags],
            "created_at": d.created_at,
        }
        blob = prev_hash + json.dumps(core, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def record(self, d: Decision) -> Decision:
        with self._lock:
            d.prev_hash = self._last_hash()
            d.hash = self._digest(d.prev_hash, d)
            cur = self._conn.execute(
                """INSERT INTO decisions
                   (request_id, use_case, tier, action, escalated, repaired, original_text,
                    final_text, annotations, flags, lane_results, telemetry, policy_version,
                    created_at, prev_hash, hash)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (d.request_id, d.use_case, d.tier.value, d.action.value,
                 int(d.escalated), int(d.repaired), d.original_text, d.final_text,
                 json.dumps(d.annotations), json.dumps([f.to_dict() for f in d.flags]),
                 json.dumps([r.to_dict() for r in d.lane_results]), json.dumps(d.telemetry),
                 d.policy_version, d.created_at, d.prev_hash, d.hash),
            )
            decision_row_id = cur.lastrowid
            if d.escalated:
                reason = "; ".join(f.message for f in d.flags
                                   if f.severity.rank >= 3) or "escalated for human review"
                rc = self._conn.execute(
                    """INSERT INTO review_queue (decision_id, use_case, reason, created_at)
                       VALUES (?,?,?,?)""",
                    (decision_row_id, d.use_case, reason, time.time()),
                )
                d.review_id = rc.lastrowid
            self._conn.commit()
        d.telemetry["decision_row_id"] = decision_row_id
        return d

    def verify_chain(self) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT * FROM decisions ORDER BY id ASC").fetchall()
        prev = "GENESIS"
        for r in rows:
            core = {
                "request_id": r["request_id"], "use_case": r["use_case"], "tier": r["tier"],
                "action": r["action"], "original_text": r["original_text"],
                "final_text": r["final_text"], "flags": json.loads(r["flags"]),
                "created_at": r["created_at"],
            }
            expect = hashlib.sha256(
                (prev + json.dumps(core, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
            if expect != r["hash"] or r["prev_hash"] != prev:
                return {"ok": False, "broken_at_id": r["id"], "count": len(rows)}
            prev = r["hash"]
        return {"ok": True, "count": len(rows)}

    # -------------------------------------------------------------- baselines
    def baseline_stats(self, use_case: str, metric: str) -> tuple[int, float, float]:
        row = self._conn.execute(
            "SELECT n, mean, m2 FROM baselines WHERE use_case=? AND metric=?",
            (use_case, metric)).fetchone()
        if not row or row["n"] < 2:
            return (row["n"] if row else 0, row["mean"] if row else 0.0, 0.0)
        std = math.sqrt(row["m2"] / (row["n"] - 1))
        return row["n"], row["mean"], std

    # convenience wrapper object for the cost lane
    def baselines(self) -> "BaselineView":
        return BaselineView(self)

    def baseline_update(self, use_case: str, metric: str, value: float) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT n, mean, m2 FROM baselines WHERE use_case=? AND metric=?",
                (use_case, metric)).fetchone()
            if not row:
                self._conn.execute(
                    "INSERT INTO baselines (use_case, metric, n, mean, m2) VALUES (?,?,1,?,0)",
                    (use_case, metric, value))
            else:
                n = row["n"] + 1
                delta = value - row["mean"]
                mean = row["mean"] + delta / n
                m2 = row["m2"] + delta * (value - mean)
                self._conn.execute(
                    "UPDATE baselines SET n=?, mean=?, m2=? WHERE use_case=? AND metric=?",
                    (n, mean, m2, use_case, metric))
            self._conn.commit()

    # ------------------------------------------------------------ read paths
    def recent_decisions(self, limit: int = 50, use_case: str | None = None) -> list[dict]:
        q = "SELECT * FROM decisions"
        args: list[Any] = []
        if use_case:
            q += " WHERE use_case=?"
            args.append(use_case)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return [self._row_to_decision(r) for r in self._conn.execute(q, args).fetchall()]

    def get_decision(self, row_id: int) -> dict | None:
        r = self._conn.execute("SELECT * FROM decisions WHERE id=?", (row_id,)).fetchone()
        return self._row_to_decision(r) if r else None

    @staticmethod
    def _row_to_decision(r: sqlite3.Row) -> dict:
        return {
            "id": r["id"], "request_id": r["request_id"], "use_case": r["use_case"],
            "tier": r["tier"], "action": r["action"],
            "escalated": bool(r["escalated"]), "repaired": bool(r["repaired"]),
            "original_text": r["original_text"], "final_text": r["final_text"],
            "annotations": json.loads(r["annotations"]), "flags": json.loads(r["flags"]),
            "lane_results": json.loads(r["lane_results"]), "telemetry": json.loads(r["telemetry"]),
            "policy_version": r["policy_version"], "created_at": r["created_at"],
            "prev_hash": r["prev_hash"], "hash": r["hash"],
        }

    def review_queue(self, status: str | None = "open") -> list[dict]:
        q = "SELECT * FROM review_queue"
        args: list[Any] = []
        if status:
            q += " WHERE status=?"
            args.append(status)
        q += " ORDER BY id DESC"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def resolve_review(self, review_id: int, status: str, reviewer: str, note: str) -> dict | None:
        with self._lock:
            self._conn.execute(
                "UPDATE review_queue SET status=?, reviewer=?, resolution_note=?, resolved_at=? WHERE id=?",
                (status, reviewer, note, time.time(), review_id))
            self._conn.commit()
        r = self._conn.execute("SELECT * FROM review_queue WHERE id=?", (review_id,)).fetchone()
        return dict(r) if r else None

    def add_feedback(self, decision_id: int, label: str, note: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO feedback_events (decision_id, label, note, created_at) VALUES (?,?,?,?)",
                (decision_id, label, note, time.time()))
            self._conn.commit()

    # ---------------------------------------------------------------- metrics
    def metrics(self) -> dict[str, Any]:
        rows = self._conn.execute("SELECT * FROM decisions").fetchall()
        total = len(rows)
        by_action: dict[str, int] = {}
        by_use_case: dict[str, dict[str, int]] = {}
        added_latencies: list[float] = []
        cost_saved = 0.0
        for r in rows:
            by_action[r["action"]] = by_action.get(r["action"], 0) + 1
            uc = by_use_case.setdefault(r["use_case"], {})
            uc[r["action"]] = uc.get(r["action"], 0) + 1
            tel = json.loads(r["telemetry"])
            if "sentinel_overhead_ms" in tel:
                added_latencies.append(tel["sentinel_overhead_ms"])
            cost_saved += tel.get("cost_avoided_usd", 0.0)

        rq = self._conn.execute("SELECT status, COUNT(*) c FROM review_queue GROUP BY status").fetchall()
        rq_counts = {r["status"]: r["c"] for r in rq}
        resolved = rq_counts.get("upheld", 0) + rq_counts.get("overridden", 0)
        fp_rate = (rq_counts.get("overridden", 0) / resolved) if resolved else None
        fn = self._conn.execute(
            "SELECT COUNT(*) c FROM feedback_events WHERE label='false_negative'").fetchone()["c"]

        added_latencies.sort()

        def pct(p: float) -> float:
            if not added_latencies:
                return 0.0
            return round(added_latencies[min(len(added_latencies) - 1, int(p * len(added_latencies)))], 1)

        return {
            "total_decisions": total,
            "by_action": by_action,
            "by_use_case": by_use_case,
            "added_latency_ms": {"p50": pct(0.5), "p95": pct(0.95)},
            "est_cost_avoided_usd": round(cost_saved, 4),
            "review_queue": rq_counts,
            "false_positive_rate": round(fp_rate, 3) if fp_rate is not None else None,
            "false_negatives_reported": fn,
            "chain": self.verify_chain(),
        }


class BaselineView:
    """Thin adapter passed to the cost lane."""

    def __init__(self, store: AuditStore):
        self._store = store

    def stats(self, use_case: str, metric: str) -> tuple[int, float, float]:
        return self._store.baseline_stats(use_case, metric)

    def update(self, use_case: str, metric: str, value: float) -> None:
        self._store.baseline_update(use_case, metric, value)
