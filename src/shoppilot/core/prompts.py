"""Load a prompt from configs/prompts/<name>.yaml (keys: version, system). Used by every agent."""
from pathlib import Path
from typing import Any

import yaml

# src/shoppilot/core/prompts.py -> repo root is three levels up
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "configs" / "prompts"


def load_prompt(name: str) -> dict[str, Any]:
    return yaml.safe_load((PROMPTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def prompt_versions(*names: str) -> dict[str, Any]:
    """{"decide": 2, ...}: goes into the LangSmith metadata of a run, so a trace can be filtered by prompt version."""
    return {name: load_prompt(name).get("version") for name in names}
