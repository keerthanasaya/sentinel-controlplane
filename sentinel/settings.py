"""Runtime configuration, loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # dotenv is optional
    pass

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
KNOWLEDGE_DIR = CONFIG_DIR / "knowledge"
DASHBOARD_DIR = ROOT / "sentinel" / "dashboard"
THRESHOLD_OVERRIDES = CONFIG_DIR / "threshold_overrides.json"


@dataclass
class Settings:
    db_path: str = os.getenv("SENTINEL_DB", str(ROOT / "sentinel_audit.db"))


settings = Settings()
