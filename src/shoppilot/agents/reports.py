"""Reports agent (Step P): the numbers of one day, a short summary and up to 3 recommended actions.

    fetch_numbers -> write_summary -> finish          (fetch_numbers failing goes straight to finish)

- Level L0/L1: it only reads numbers and writes text. It sends nothing and moves no money.
- The CODE fetches the numbers (sales_summary tool). The model only writes words about them: a short summary and up to
  3 actions. The text is checked by code (length, no email address, no link). If the model fails or its text is
  rejected, the summary and actions are built by plain code from the same numbers, so a model outage never stops
  the daily report.
- The numbers are data, not instructions: they sit inside <facts> tags and the prompt says so.
- Tool allow-list: sales_summary (tools/context.py AGENT_TOOLS), enforced by the tool layer.
- Saving the HTML report and queueing the owner email is done by jobs/daily_report.py, which calls this graph.
"""
import json
import re
from typing import Any, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, Field

from shoppilot.agents.state import ReportState
from shoppilot.core.llm import get_llm
from shoppilot.core.logging import get_logger
from shoppilot.core.prompts import load_prompt
from shoppilot.tools.reports import sales_summary

log = get_logger(__name__)

MAX_ACTIONS = 3
MAX_LOW_STOCK_SHOWN = 10  # the model does not need a long list
UNSAFE_TEXT = re.compile(r"@|https?:|www\.|<\s*[a-z/!]", re.IGNORECASE)  # email address, link, HTML tag


class ReportText(BaseModel):
    """What the model must return. A fixed schema, so it cannot answer in free text."""

    summary: str = Field(min_length=10, max_length=600)
    actions: list[str] = Field(min_length=1, max_length=MAX_ACTIONS)


# ------------------------------------------------------------------------------------------------ helpers (not graph nodes)
def plain_summary(numbers: dict[str, Any]) -> str:
    """The fallback summary, built by code from the numbers only."""
    text = (
        f"{numbers['orders_count']} orders were placed for PKR {numbers['sales_pkr']:,}. "
        f"{numbers['refunds_count']} refunds were made for PKR {numbers['refunds_pkr']:,}."
    )
    if numbers["late_orders"]:
        text += f" {numbers['late_orders']} orders are late."
    if numbers["low_stock"]:
        text += f" {len(numbers['low_stock'])} products are at or below their reorder point."
    return text


def plain_actions(numbers: dict[str, Any]) -> list[str]:
    """The fallback actions, built by code from the numbers only."""
    actions = [
        f"Reorder {item['sku']} ({item['on_hand']} on hand, reorder point {item['reorder_point']})"
        for item in numbers["low_stock"]
    ]
    if numbers["late_orders"]:
        actions.append(f"Ask the courier about the {numbers['late_orders']} late orders")
    return actions[:MAX_ACTIONS] or ["No action needed today"]


def check_text(text: ReportText) -> bool:
    """True when the model text is safe to show: no email address, link or HTML tag, and no empty action."""
    parts = [text.summary, *text.actions]
    return all(p.strip() for p in parts) and not any(UNSAFE_TEXT.search(p) for p in parts)


def _ask_model(numbers: dict[str, Any]) -> ReportText | None:
    facts = {**numbers, "low_stock": numbers["low_stock"][:MAX_LOW_STOCK_SHOWN]}
    try:
        raw = (
            get_llm(temperature=0.2)
            .with_structured_output(ReportText)
            .invoke(
                [
                    SystemMessage(load_prompt("reports")["system"]),
                    HumanMessage(f"<facts>\n{json.dumps(facts)}\n</facts>"),
                ]
            )
        )
        text = ReportText.model_validate(raw)
    except Exception:
        log.warning("reports: no usable model answer, using the plain summary", exc_info=True)
        return None
    if not check_text(text):
        log.warning("reports: the model text was rejected, using the plain summary")
        return None
    return text


# ----------------------------------------------------------------------------------------------------------- nodes
def fetch_numbers(state: ReportState) -> dict[str, Any]:
    result = sales_summary.invoke({})
    if not result.get("ok"):
        return {"errors": [str(result.get("error", "TOOL_ERROR"))]}
    return {"numbers": {k: v for k, v in result.items() if k != "ok"}}


def write_summary(state: ReportState) -> dict[str, Any]:
    numbers = state["numbers"]
    answer = _ask_model(numbers)
    if answer is None:
        return {"summary": plain_summary(numbers), "actions": plain_actions(numbers)}
    return {
        "summary": " ".join(answer.summary.split()),
        "actions": [" ".join(a.split()) for a in answer.actions[:MAX_ACTIONS]],
    }


def finish(state: ReportState) -> dict[str, Any]:
    """The text for the staff member, built by code."""
    errors = state.get("errors", [])
    if errors:
        return {"outcome": f"No report was made: {', '.join(errors)}."}
    numbers = state["numbers"]
    actions = "; ".join(state.get("actions", []))
    return {
        "outcome": (
            f"Report for {numbers['day']}: {numbers['orders_count']} orders, PKR {numbers['sales_pkr']:,} sales, "
            f"{numbers['refunds_count']} refunds, {numbers['late_orders']} late orders. "
            f"{state.get('summary', '')} Actions: {actions}."
        )
    }


# ----------------------------------------------------------------------------------------------------------- graph
def after_fetch(state: ReportState) -> Literal["write_summary", "finish"]:
    return "finish" if state.get("errors") else "write_summary"


def build_reports_graph(checkpointer: Any = None) -> Any:
    g = StateGraph(ReportState)
    for name, fn in [("fetch_numbers", fetch_numbers), ("write_summary", write_summary), ("finish", finish)]:
        g.add_node(name, fn)
    g.add_edge(START, "fetch_numbers")
    g.add_conditional_edges("fetch_numbers", after_fetch)
    g.add_edge("write_summary", "finish")
    g.add_edge("finish", END)
    return g.compile(checkpointer=checkpointer)
