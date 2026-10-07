"""Step U: the evaluation cases and the LangSmith dataset.

A case is one line of data/eval/cases_v1.jsonl (built by scripts/build_cases_v1.py):

    id, category   the case name and its row in blueprint Table 26
    agent          support | inventory | listing
    smoke          true for the 10 cases that run in CI
    scenario, index   which seeded order is "mine" (the index counts inside the scenario, like in dataset v0)
    ticket_text    {order}, {other} and {other_email} are filled in at run time
    approvals      the scripted human decisions, in order: {"status": "approved" | "rejected", "amount_pkr": optional}
    fault          courier_down | llm_down | null
    sku            inventory cases only
    expect         the expected outcome (checked by evaluators.py)

Only the inputs go to LangSmith as inputs, and `expect` goes as the reference outputs.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]  # .../src/shoppilot/evaluation/dataset.py -> repository root
DATA_DIR = ROOT / "data" / "eval"
CASES_FILE = DATA_DIR / "cases_v1.jsonl"
ROUTER_FILE = DATA_DIR / "router_v1.jsonl"
REDTEAM_FILE = DATA_DIR / "redteam.jsonl"
RESULTS_DIR = DATA_DIR / "results"
BASELINE_FILE = DATA_DIR / "baseline.json"
REPORT_FILE = ROOT / "docs" / "eval" / "report.md"

DATASET = "shoppilot-support-v1"
EVAL_NOW = datetime(2026, 10, 1, 12, 0, 0)  # the fixed clock of the seed, the shop and the run context (same as the tests)

INPUT_KEYS = ("id", "category", "agent", "scenario", "index", "ticket_text", "approvals", "fault", "sku")


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def load_cases(
    path: Path = CASES_FILE, *, subset: str = "full", category: str | None = None
) -> list[dict[str, Any]]:
    """The cases of the file. subset="smoke" keeps only the cases marked for CI."""
    cases = read_jsonl(path)
    if not cases:
        raise SystemExit(f"{path} is empty or missing. Build it first: uv run python scripts/build_cases_v1.py")
    if subset == "smoke":
        cases = [c for c in cases if c.get("smoke")]
    if category:
        cases = [c for c in cases if c["category"] == category]
    return cases


def to_example(case: dict[str, Any]) -> dict[str, Any]:
    """One case as a LangSmith example: inputs for the target, `expect` as the reference outputs."""
    inputs = {key: case.get(key) for key in INPUT_KEYS}
    return {
        "inputs": inputs,
        "outputs": case["expect"],
        "metadata": {"category": case["category"], "smoke": bool(case.get("smoke"))},
    }


def sync_dataset(client: Any, cases: list[dict[str, Any]], *, recreate: bool = False, name: str = DATASET) -> None:
    """Create the dataset in LangSmith from the cases file. An existing dataset is kept unless recreate is True."""
    if client.has_dataset(dataset_name=name):
        if not recreate:
            existing = len(list(client.list_examples(dataset_name=name)))
            note = "" if existing == len(cases) else f" (it has {existing} examples, the file has {len(cases)}: use --recreate-dataset)"
            print(f"Dataset '{name}' already exists{note}.")
            return
        client.delete_dataset(dataset_name=name)
        print(f"Deleted the old '{name}'.")
    dataset = client.create_dataset(name, description=f"Step U: {len(cases)} support, inventory, listing and safety cases")
    client.create_examples(dataset_id=dataset.id, examples=[to_example(c) for c in cases])
    print(f"Created '{name}' with {len(cases)} examples.")
