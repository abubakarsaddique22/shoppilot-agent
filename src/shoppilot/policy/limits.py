"""Refund limits. Values come from configs/settings.dev.yaml (section "limits"), so the owner can change them without a code release."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

# src/shoppilot/policy/limits.py -> repo root is three levels up
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "configs" / "settings.dev.yaml"


class Limits(BaseModel):
    model_config = ConfigDict(frozen=True)

    auto_refund_limit_pkr: int = 3000
    manager_limit_pkr: int = 15000
    auto_refunds_per_day_pkr: int = 30000
    refund_window_days: int = 14
    late_threshold_days: int = 5
    repeat_refund_count: int = 2  # this many refunds in the last 90 days sends the case to the owner


def load_limits(path: Path | str | None = None) -> Limits:
    """Read the "limits" section of the YAML file. Missing keys keep their defaults, unknown keys are ignored."""
    data = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8")) or {}
    return Limits(**(data.get("limits") or {}))
