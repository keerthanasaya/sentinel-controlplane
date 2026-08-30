"""Governance policy layer — per-use-case configuration with hot-reloadable
threshold overrides fed by the feedback loop."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from typing import Any

import yaml

from .settings import CONFIG_DIR, THRESHOLD_OVERRIDES


@dataclass
class Policy:
    use_case: str
    label: str
    surface: str
    base_tier: str
    latency_budget_ms: int
    geo: str
    weekly_volume: int
    knowledge_base: str
    high_stakes_keywords: list[str]
    thresholds: dict[str, float]
    block_on: list[str]
    on_timeout: str
    allow_repair: bool
    version: str

    def threshold(self, name: str, default: float = 0.0) -> float:
        return float(self.thresholds.get(name, default))


class PolicyStore:
    def __init__(self, path: Any = None):
        self.path = path or (CONFIG_DIR / "policies.yaml")
        self._raw: dict[str, Any] = {}
        self.policies: dict[str, Policy] = {}
        self.reload()

    def reload(self) -> None:
        self._raw = yaml.safe_load(open(self.path, "r", encoding="utf-8"))
        overrides = {}
        if THRESHOLD_OVERRIDES.exists():
            try:
                overrides = json.loads(THRESHOLD_OVERRIDES.read_text(encoding="utf-8"))
            except Exception:
                overrides = {}

        version = str(self._raw.get("version", "0"))
        defaults = self._raw.get("defaults", {})
        d_thresholds = defaults.get("thresholds", {})
        d_actions = defaults.get("actions", {})

        self.policies = {}
        for uc, cfg in self._raw.get("use_cases", {}).items():
            thresholds = {**d_thresholds, **cfg.get("thresholds", {})}
            thresholds.update(overrides.get(uc, {}))
            actions = {**d_actions, **cfg.get("actions", {})}
            self.policies[uc] = Policy(
                use_case=uc,
                label=cfg.get("label", uc),
                surface=cfg.get("surface", "internal"),
                base_tier=cfg.get("base_tier", "medium"),
                latency_budget_ms=int(cfg.get("latency_budget_ms", defaults.get("latency_budget_ms", 800))),
                geo=cfg.get("geo", "unspecified"),
                weekly_volume=int(cfg.get("weekly_volume", 10000)),
                knowledge_base=cfg.get("knowledge_base", ""),
                high_stakes_keywords=[k.lower() for k in cfg.get("high_stakes_keywords", [])],
                thresholds=thresholds,
                block_on=list(actions.get("block_on", [])),
                on_timeout=actions.get("on_timeout", "annotate"),
                allow_repair=bool(actions.get("allow_repair", True)),
                version=version,
            )

    def get(self, use_case: str) -> Policy:
        if use_case not in self.policies:
            raise KeyError(f"unknown use case '{use_case}' (known: {list(self.policies)})")
        return self.policies[use_case]

    def apply_threshold_override(self, use_case: str, name: str, value: float) -> None:
        data: dict[str, Any] = {}
        if THRESHOLD_OVERRIDES.exists():
            try:
                data = json.loads(THRESHOLD_OVERRIDES.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data.setdefault(use_case, {})[name] = round(float(value), 4)
        THRESHOLD_OVERRIDES.write_text(json.dumps(data, indent=2), encoding="utf-8")
        self.reload()

    def as_dict(self) -> dict[str, Any]:
        return {
            uc: {
                "label": p.label, "surface": p.surface, "base_tier": p.base_tier,
                "latency_budget_ms": p.latency_budget_ms, "geo": p.geo,
                "weekly_volume": p.weekly_volume, "knowledge_base": p.knowledge_base,
                "thresholds": p.thresholds, "block_on": p.block_on,
                "high_stakes_keywords": p.high_stakes_keywords, "version": p.version,
            }
            for uc, p in self.policies.items()
        }
