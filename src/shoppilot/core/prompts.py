"""Load a prompt from configs/prompts/<name>.yaml (keys: version, system). Used by every agent."""
from pathlib import Path
from typing import Any

import yaml

# src/shoppilot/core/prompts.py -> repo root is three levels up
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "configs" / "prompts"


def load_prompt(name: str) -> dict[str, Any]:
    return yaml.safe_load((PROMPTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))
