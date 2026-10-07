"""Step U: run the evaluation (blueprint section 14).

    uv run python scripts/build_cases_v1.py                   # once: write the cases file
    uv run python scripts/run_eval.py                         # all cases as a LangSmith experiment
    uv run python scripts/run_eval.py --subset smoke --local --gate        # 10 cases, no upload, exit 1 on a failed gate (CI)
    uv run python scripts/run_eval.py --repeats 3 --report --label graph-v2     # 3 runs per case, add a block to docs/eval/report.md
    uv run python scripts/run_eval.py --save-baseline         # store this run as the baseline of the CI gate
    uv run python scripts/run_eval.py --judge                 # also score the replies 1 to 5 with a model
    uv run python scripts/run_eval.py --router                # router accuracy on 20 mixed inputs (Step O)

The model comes from .env (SHOP_LLM_PROVIDER, SHOP_LLM_MODEL), like everywhere else. Change ONE thing between two runs
(a model, a prompt, a limit) and give each run its own --label, so the experiment table in docs/eval/report.md can say
which change helped.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import UTC, datetime
from typing import Any

from dotenv import load_dotenv
from langsmith import Client, evaluate

from shoppilot.agents.support import GRAPH_VERSION
from shoppilot.core.config import settings
from shoppilot.core.prompts import prompt_versions
from shoppilot.evaluation.dataset import (
    BASELINE_FILE,
    DATASET,
    REPORT_FILE,
    RESULTS_DIR,
    ROUTER_FILE,
    load_cases,
    read_jsonl,
    sync_dataset,
)
from shoppilot.evaluation.evaluators import TARGETS, aggregate, build_evaluators, check_gate
from shoppilot.evaluation.run import policy_search, run_case


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Step U: evaluate ShopPilot")
    p.add_argument("--subset", choices=["full", "smoke"], default="full", help="smoke = the 10 cases of CI")
    p.add_argument("--category", help="only this category, for example refund_approval")
    p.add_argument("--repeats", type=int, default=1, help="run every case this many times (the model is not deterministic)")
    p.add_argument("--local", action="store_true", help="do not use LangSmith datasets and experiments")
    p.add_argument("--recreate-dataset", action="store_true", help="delete the LangSmith dataset and upload the cases again")
    p.add_argument("--real-kb", action="store_true", help="use the Postgres policy knowledge base instead of fixed policy text")
    p.add_argument("--judge", action="store_true", help="score the replies 1 to 5 with a model (costs tokens)")
    p.add_argument("--label", help="name of this run, for example graph-v2-groq (default: provider, model and subset)")
    p.add_argument("--gate", action="store_true", help="exit 1 when a hard gate fails or task success dropped more than 3 points")
    p.add_argument("--save-baseline", action="store_true", help="store this run as the baseline of the gate")
    p.add_argument("--report", action="store_true", help="append a block with this run to docs/eval/report.md")
    p.add_argument("--router", action="store_true", help="only the router accuracy (needs the model)")
    p.add_argument("--price-in", type=float, default=0.0, help="price of 1 million input tokens (your currency)")
    p.add_argument("--price-out", type=float, default=0.0, help="price of 1 million output tokens (your currency)")
    return p.parse_args()


# ---------------------------------------------------------------------------------------------------- running
def rows_local(cases: list[dict[str, Any]], evaluators: list[Any], repeats: int) -> list[dict[str, Any]]:
    rows = []
    for case in cases:
        for _ in range(repeats):
            outputs = run_case(case)
            scores: dict[str, float | None] = {}
            for fn in evaluators:
                result = fn(outputs=outputs, reference_outputs=case["expect"])
                scores[result["key"]] = result.get("score")
            rows.append({"case": case, "outputs": outputs, "scores": scores, "expect": case["expect"]})
            print(f"  {case['id']:<16}{'ok' if scores.get('outcome_correct') == 1.0 else 'FAIL'}", flush=True)
    return rows


def rows_langsmith(cases: list[dict[str, Any]], evaluators: list[Any], args: argparse.Namespace, label: str) -> list[dict[str, Any]]:
    client = Client()
    sync_dataset(client, load_cases(), recreate=args.recreate_dataset)
    wanted = {c["id"] for c in cases}
    examples = [e for e in client.list_examples(dataset_name=DATASET) if (e.inputs or {}).get("id") in wanted]
    if len(examples) != len(wanted):
        raise SystemExit("The LangSmith dataset does not match the cases file: run again with --recreate-dataset")

    def target(inputs: dict) -> dict:
        return run_case(inputs)

    results = evaluate(
        target,
        data=examples,
        evaluators=evaluators,
        experiment_prefix=label,
        metadata={"provider": settings.llm_provider, "model": settings.llm_model, "subset": args.subset,
                  "kb": "real" if args.real_kb else "fixed", "cases": len(cases), "graph_version": GRAPH_VERSION,
                  "prompt_versions": prompt_versions("triage", "decide", "reply")},
        num_repetitions=args.repeats,
        max_concurrency=1,  # one case at a time: the llm_down fault patches a global
    )  # fmt: skip
    rows = []
    for r in results:
        scores = {e.key: e.score for e in r["evaluation_results"]["results"]}
        rows.append({"case": r["example"].inputs, "outputs": r["run"].outputs or {}, "scores": scores, "expect": r["example"].outputs or {}})
    return rows


# --------------------------------------------------------------------------------------------------- printing
def passed(row: dict[str, Any]) -> bool:
    return row["scores"].get("outcome_correct") == 1.0


def mark(score: float | None) -> str:
    return "-" if score is None else ("ok" if score == 1.0 else "NO")


def print_table(rows: list[dict[str, Any]]) -> None:
    print(f"\n{'case':<16}{'category':<19}{'result':<8}{'safe':<6}{'tools':<7}{'refund':<8}{'tier':<9}template")
    for r in rows:
        s, o = r["scores"], r["outputs"]
        safe = s.get("no_wrong_refund") == 1.0 and s.get("no_approval_bypass") == 1.0
        print(
            f"{r['case'].get('id', '?'):<16}{r['case'].get('category', '?'):<19}{'PASS' if passed(r) else 'FAIL':<8}"
            f"{'ok' if safe else 'NO':<6}{mark(s.get('trajectory_ok')):<7}{o.get('refund_pkr', '-')!s:<8}"
            f"{o.get('tier') or '-':<9}{o.get('template') or o.get('outcome', '')[:30] or '-'}"
        )
    for r in (r for r in rows if not passed(r)):
        o, e = r["outputs"], r["expect"]
        print(f"\n--- FAIL: {r['case'].get('id')} ({r['case'].get('category')}) ---")
        print(f"Ticket   : {o.get('ticket')}")
        print(f"Expected : { {k: v for k, v in e.items() if k not in ('forbidden_strings',)} }")
        print(f"Got      : refund={o.get('refund_pkr')} tier={o.get('tier')} escalated={o.get('escalated')} "
              f"template={o.get('template')} approvals={[a.get('status') for a in o.get('approvals', [])]}")
        print(f"Tools    : {o.get('tools')}   Errors: {o.get('errors')}   Crash: {o.get('error')}")
        emails = o.get("emails") or []
        print(f"Last mail: {str(emails[-1])[:300] if emails else (o.get('outcome') or '(none)')}")


def fmt(value: Any, digits: int = 3) -> str:
    return "n/a" if value is None else (f"{value:.{digits}f}" if isinstance(value, float) else str(value))


def print_metrics(m: dict[str, Any]) -> None:
    print(f"\nCases run: {m['cases']}")
    for key, (op, limit) in TARGETS.items():
        value = m.get(key)
        ok = value is not None and (value >= limit if op == ">=" else value < limit if key == "latency_p95_s" else value <= limit)
        print(f"  {key:<22}{fmt(value):<9}target {op} {limit:<6}{'ok' if ok else ('-' if value is None else 'MISS')}")
    print(f"  {'wrong_refund_cases':<22}{m['wrong_refund_cases']:<9}target = 0     {'ok' if m['wrong_refund_cases'] == 0 else 'GATE'}")
    print(f"  {'approval_bypass_cases':<22}{m['approval_bypass_cases']:<9}target = 0     {'ok' if m['approval_bypass_cases'] == 0 else 'GATE'}")
    print(f"  {'tokens in/out per case':<22}{m['tokens_in_avg']}/{m['tokens_out_avg']}   cost per ticket {m['cost_per_ticket']:.4f}")
    print("  success by category: " + ", ".join(f"{k} {v:.2f}" for k, v in m["by_category"].items()))


# ------------------------------------------------------------------------------------------------------ saving
def load_baseline(subset: str) -> dict[str, Any] | None:
    if not BASELINE_FILE.exists():
        return None
    return json.loads(BASELINE_FILE.read_text(encoding="utf-8")).get(subset)


def save_baseline(subset: str, label: str, metrics: dict[str, Any]) -> None:
    data = json.loads(BASELINE_FILE.read_text(encoding="utf-8")) if BASELINE_FILE.exists() else {}
    data[subset] = {"label": label, "saved_at": datetime.now(UTC).isoformat(timespec="seconds"), "task_success": metrics["task_success"],
                    "cases": metrics["cases"]}  # fmt: skip
    BASELINE_FILE.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"Baseline for '{subset}' saved to {BASELINE_FILE}")


def save_results(label: str, metrics: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = RESULTS_DIR / f"{stamp}-{label}.json"
    cases = [{"id": r["case"].get("id"), "passed": passed(r), "scores": r["scores"],
              "refund_pkr": r["outputs"].get("refund_pkr"), "tier": r["outputs"].get("tier"),
              "template": r["outputs"].get("template"), "tools": r["outputs"].get("tools"),
              "seconds": r["outputs"].get("seconds")} for r in rows]  # fmt: skip
    path.write_text(json.dumps({"label": label, "metrics": metrics, "cases": cases}, indent=2, default=str), encoding="utf-8")
    print(f"Results saved to {path}")


def append_report(label: str, args: argparse.Namespace, m: dict[str, Any]) -> None:
    """One block per run. Copy the numbers you want into the experiment table (Table 28) at the top of the file."""
    lines = [
        f"\n### {label}\n",
        f"{datetime.now(UTC):%Y-%m-%d %H:%M} UTC | model `{settings.llm_provider}/{settings.llm_model}` | {m['cases']} runs | "
        f"subset {args.subset} | repeats {args.repeats} | policy search {'real' if args.real_kb else 'fixed'}\n",
        "| Metric | Value | Target |", "|---|---|---|",
    ]
    for key, (op, limit) in TARGETS.items():
        lines.append(f"| {key} | {fmt(m.get(key))} | {op} {limit} |")
    lines += [
        f"| wrong_refund_cases | {m['wrong_refund_cases']} | 0 |",
        f"| approval_bypass_cases | {m['approval_bypass_cases']} | 0 |",
        f"| tokens in / out per case | {m['tokens_in_avg']} / {m['tokens_out_avg']} | tracked |",
        f"| cost per ticket | {m['cost_per_ticket']:.4f} | tracked |",
        "\nSuccess by category: " + ", ".join(f"{k} {v:.2f}" for k, v in m["by_category"].items()) + "\n",
    ]
    REPORT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with REPORT_FILE.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"Block added to {REPORT_FILE}")


# ----------------------------------------------------------------------------------------------------- router
def run_router(args: argparse.Namespace) -> int:
    from shoppilot.agents.supervisor import route_item

    items = read_jsonl(ROUTER_FILE)
    if not items:
        raise SystemExit(f"{ROUTER_FILE} is empty or missing: run scripts/build_cases_v1.py first")
    wrong = []
    for item in items:
        got = route_item(item["source"], item["text"])
        if got["route"] != item["expect"]["route"]:
            wrong.append((item, got))
        print(f"  {item['id']}  expected {item['expect']['route']:<10}got {got['route']:<10}by {got.get('by')}")
    accuracy = (len(items) - len(wrong)) / len(items)
    print(f"\nRouter accuracy: {accuracy:.2f} ({len(items) - len(wrong)} of {len(items)}), target at least 0.95")
    for item, got in wrong:
        print(f"  wrong: {item['text']!r} -> {got['route']} ({got.get('reason')})")
    return 1 if args.gate and accuracy < 0.95 else 0


# ------------------------------------------------------------------------------------------------------- main
def main() -> None:
    load_dotenv()  # LANGSMITH_* are read from the environment
    args = parse_args()
    if args.router:
        sys.exit(run_router(args))

    cases = load_cases(subset=args.subset, category=args.category)
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", args.label or f"{settings.llm_provider}-{settings.llm_model}-{args.subset}")
    evaluators = build_evaluators(judge=args.judge)
    print(f"Running {len(cases)} cases x {args.repeats} as '{label}' ({'local' if args.local else 'LangSmith experiment'})")

    with policy_search("real" if args.real_kb else "fixed"):
        rows = rows_local(cases, evaluators, args.repeats) if args.local else rows_langsmith(cases, evaluators, args, label)

    metrics = aggregate(rows, price_in=args.price_in, price_out=args.price_out)
    print_table(rows)
    print_metrics(metrics)
    save_results(label, metrics, rows)
    if args.report:
        append_report(label, args, metrics)
    if args.save_baseline:
        save_baseline(args.subset, label, metrics)

    if args.gate:
        baseline = load_baseline(args.subset)
        if baseline is None:
            print(f"\n(no baseline for '{args.subset}' yet: only the hard gates are checked. Use --save-baseline.)")
        problems = check_gate(metrics, baseline)
        if problems:
            print("\nGATE FAILED:\n  - " + "\n  - ".join(problems))
            sys.exit(1)
        print("\nGATE PASSED")


if __name__ == "__main__":
    main()
