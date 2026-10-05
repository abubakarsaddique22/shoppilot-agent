"""The support graph (Steps K and L): triage, gather_facts, decide, rules, approval_gate, execute, verify, reply, escalate.

triage is the first model call of the graph. It only sorts the customer's message: which kind of request is it, and
which order does it mention? It calls no tools. Everything after it (facts, decision, money) is decided elsewhere.
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, Field

from shoppilot.agents.state import Intent, TicketState
from shoppilot.approvals.service import get_approval, request_approval
from shoppilot.core.config import settings
from shoppilot.core.errors import AppError
from shoppilot.core.llm import get_llm
from shoppilot.core.logging import get_logger
from shoppilot.tools.context import audit, get_ctx
from shoppilot.tools.email import escalate_to_human, send_customer_email
from shoppilot.tools.orders import get_order, load_own_order, search_policy, track_shipment
from shoppilot.tools.refunds import Reason, issue_refund, propose_refund

log = get_logger(__name__)

# src/shoppilot/agents/support.py -> repo root is three levels up
PROMPTS_DIR = Path(__file__).resolve().parents[3] / "configs" / "prompts"


class Triage(BaseModel):
    """What the model must return. A fixed schema, so it cannot answer in free text."""

    intent: Intent
    order_ref: str | None = None


def load_prompt(name: str) -> dict[str, Any]:
    """Read configs/prompts/<name>.yaml (keys: version, system)."""
    return yaml.safe_load((PROMPTS_DIR / f"{name}.yaml").read_text(encoding="utf-8"))


def last_customer_text(state: TicketState) -> str:
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", None) == "human":
            return str(message.content)
    return ""


def triage(state: TicketState) -> dict[str, Any]:
    text = last_customer_text(state)
    if not text:
        return {"intent": "other", "order_ref": None}

    safe_text = text.replace("</customer_message>", "")  # the customer cannot close the data tag early
    try:
        raw = get_llm().with_structured_output(Triage).invoke(
            [
                SystemMessage(load_prompt("triage")["system"]),
                HumanMessage(f"<customer_message>\n{safe_text}\n</customer_message>"),
            ]
        )
        result = Triage.model_validate(raw)  # the answer may come back as a dict or as the model itself
    except Exception:  # malformed answer, rate limit, outage: the safe exit is a human
        log.warning("triage failed: sending the ticket to a human", exc_info=True)
        return {"intent": "other", "order_ref": None}

    # the model may only repeat a number that is really in the message
    ref = (result.order_ref or "").strip()
    number = ref.lstrip("#").strip()
    return {"intent": result.intent, "order_ref": ref if number and number in text else None}


# What gather_facts asks the policy knowledge base, per intent. Fixed text: the customer's words never go into the query.
POLICY_QUERIES: dict[str, str] = {
    "order_status": "delivery time and late deliveries",
    "refund": "refund window, late deliveries, damaged items and approval limits",
    "exchange": "exchange and replacement rules",
}
MAX_POLICY_HITS = 3
MAX_POLICY_CHARS = 600


def _fact(fact_id: str, source: str, data: dict[str, Any]) -> dict[str, Any]:
    """One piece of evidence. `decide` may only cite ids that exist in state["facts"]."""
    return {"id": fact_id, "source": source, "data": data}


def _out_of_budget(result: dict[str, Any]) -> bool:
    return result.get("error") == "BUDGET_EXCEEDED"


def gather_facts(state: TicketState) -> dict[str, Any]:
    """Read the facts the decision is based on. Plain code, no model (Table 12).

    Calls get_order, then track_shipment (if the order has a tracking number), then search_policy, one after another.
    They are not parallel on purpose: the tools read RunContext through a ContextVar and count calls in it, and
    a new thread would not see the ContextVar. Three quick reads do not need threads.

    Returns order (compact summary), facts (each with an id) and errors. It never guesses a missing fact:
    - no order number               -> errors ["NO_ORDER_REF"]
    - order missing or not theirs   -> errors ["ORDER_NOT_FOUND"] (the same answer in both cases, nothing leaks)
    - read budget used up           -> the facts so far plus errors ["BUDGET_EXCEEDED"]
    - courier does not answer       -> a tracking fact with status "unknown" (not an error)
    - no policy similar enough      -> the other facts plus errors ["NO_POLICY_FOUND"]
    """
    ref = state.get("order_ref")
    if not ref:
        return {"facts": [], "errors": ["NO_ORDER_REF"]}

    found = get_order.invoke({"order_ref": ref})
    if not found.get("ok"):
        return {"facts": [], "errors": [str(found.get("error", "TOOL_ERROR"))]}
    order: dict[str, Any] = found["order"]
    facts = [_fact(f"order:{order['id']}", "get_order", order)]
    errors: list[str] = []

    tracking_no = order.get("tracking_no")
    if tracking_no:
        shipment = track_shipment.invoke({"tracking_no": tracking_no})
        if _out_of_budget(shipment):
            return {"order": order, "facts": facts, "errors": ["BUDGET_EXCEEDED"]}
        data = {k: v for k, v in shipment.items() if k != "ok"} if shipment.get("ok") else {"status": "unknown", "events": []}
        facts.append(_fact(f"tracking:{tracking_no}", "track_shipment", data))

    intent = state.get("intent")
    query = POLICY_QUERIES.get(intent) if intent else None
    if query:
        policy = search_policy.invoke({"query": query})
        if _out_of_budget(policy):
            return {"order": order, "facts": facts, "errors": ["BUDGET_EXCEEDED"]}
        if policy.get("ok"):
            for hit in policy["hits"][:MAX_POLICY_HITS]:
                text = str(hit["text"])[:MAX_POLICY_CHARS]
                facts.append(_fact(f"policy:{hit['section']}", "search_policy", {"section": hit["section"], "text": text}))
        else:
            errors.append(str(policy.get("error", "NO_POLICY_FOUND")))

    return {"order": order, "facts": facts, "errors": errors}


MAX_DECIDE_ATTEMPTS = 2  # the first answer, plus one retry after we tell the model what was wrong

Action = Literal["refund", "reply", "escalate"]


class Decision(BaseModel):
    """What the model PROPOSES. A fixed schema, so it cannot answer in free text. It never decides money:
    the policy engine (the rules node) does, and the engine wins."""

    action: Action
    amount_pkr: int | None = Field(default=None, gt=0, le=100_000)
    reason: Reason | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(default="", max_length=300)


def _proposal(action: Action, summary: str) -> dict[str, Any]:
    """A proposal made by code, without a model call."""
    return Decision(action=action, summary=summary[:300]).model_dump()


def check_decision(decision: Decision, state: TicketState) -> str | None:
    """Code checks what the model proposed. Returns a problem text for the model, or None when it is fine.
    The text never repeats what the model wrote, so a hostile string cannot travel from one answer into the next."""
    known = {fact["id"] for fact in state.get("facts", [])}
    if any(evidence_id not in known for evidence_id in decision.evidence_ids):
        return "evidence_ids may only contain ids that are listed in <facts>"
    if decision.action == "refund":
        if state.get("intent") != "refund":
            return "a refund can only be proposed when the intent is refund"
        if decision.amount_pkr is None or decision.reason is None:
            return "a refund needs amount_pkr and reason"
        if not decision.evidence_ids:
            return "a refund needs at least one evidence id"
    return None


def _decide_input(state: TicketState) -> str:
    def clean(text: str) -> str:
        return text.replace("</customer_message>", "").replace("</facts>", "")  # data cannot close its own tag

    facts = clean(json.dumps(state.get("facts", []), ensure_ascii=False))
    return (
        f"Intent: {state.get('intent', 'other')}\n"
        f"<customer_message>\n{clean(last_customer_text(state))}\n</customer_message>\n"
        f"<facts>\n{facts}\n</facts>"
    )


def decide(state: TicketState) -> dict[str, Any]:
    """The model proposes one action (Decision). Code checks it with check_decision.

    No model call when the facts decide it already:
    - no order number, or the order is missing or not theirs -> reply (ask for the right order number)
    - no order, or the read budget ran out                    -> escalate with the partial facts
    - no policy found and the ticket is not a status question -> escalate, never invent a rule
    - the retry budget is already used up                      -> escalate
    A wrong or malformed answer is sent back to the model once. After that the ticket goes to a human.
    """
    errors = state.get("errors", [])
    intent = state.get("intent")
    if {"NO_ORDER_REF", "ORDER_NOT_FOUND"} & set(errors):
        return {"proposal": _proposal("reply", "ask the customer for the correct order number")}
    if not state.get("order") or "BUDGET_EXCEEDED" in errors:
        return {"proposal": _proposal("escalate", "the facts could not be read completely: " + ", ".join(errors))}
    if "NO_POLICY_FOUND" in errors and intent != "order_status":
        return {"proposal": _proposal("escalate", "no store policy was found for this request")}

    budget: dict[str, Any] = {"retries_used": 0, **(state.get("budget") or {})}
    if budget["retries_used"] >= settings.max_retries:
        return {"proposal": _proposal("escalate", "retry limit reached"), "budget": budget}

    messages: list[BaseMessage] = [SystemMessage(load_prompt("decide")["system"]), HumanMessage(_decide_input(state))]
    for attempt in range(MAX_DECIDE_ATTEMPTS):
        try:
            raw = get_llm().with_structured_output(Decision).invoke(messages)
            decision = Decision.model_validate(raw)  # the answer may come back as a dict or as the model itself
            problem = check_decision(decision, state)
        except Exception:  # malformed answer, rate limit, outage
            log.warning("decide: the model answer was malformed or missing", exc_info=True)
            problem = "your answer did not match the required schema"
        if problem is None:
            return {"proposal": decision.model_dump(), "budget": budget}
        if attempt == MAX_DECIDE_ATTEMPTS - 1 or budget["retries_used"] >= settings.max_retries:
            break
        budget["retries_used"] += 1
        messages = [*messages, HumanMessage(f"Your answer was rejected: {problem}. Answer again.")]

    return {
        "proposal": _proposal("escalate", "the model could not give a valid decision"),
        "errors": [*errors, "DECISION_FAILED"],
        "budget": budget,
    }


# What `rules` stores when no money is involved (a reply or an escalation) or when the engine could not be asked.
NO_RULING: dict[str, Any] = {
    "tier": "none",
    "allowed_amount": 0,
    "reasons": [],
    "policy_refs": [],
    "proposed_amount": None,
    "overridden": False,
}


def rules(state: TicketState) -> dict[str, Any]:
    """The policy engine decides, not the model (Table 12). Plain code, no model call.

    Only a refund proposal needs the engine. It is asked through the propose_refund tool, which loads the order of
    THIS customer itself, so the hidden facts (flagged customer, refund history, open refund) never go into the state.
    The ruling is final: tier (auto, manager, owner, deny), the allowed amount, the reasons and the policy sections.
    If the engine says less than the model proposed, or says no, the engine wins and the difference is written to
    the log and to audit_log (event policy_override) for the evaluation analysis.

    If the engine cannot be asked (budget, backend error), no money moves: the proposal becomes an escalation.
    """
    proposal = state.get("proposal") or {}
    if proposal.get("action") != "refund":
        return {"ruling": dict(NO_RULING)}

    args: dict[str, Any] = {
        "order_id": state.get("order", {}).get("id", ""),
        "amount_pkr": proposal["amount_pkr"],
        "reason": proposal["reason"],
    }
    evidence = _refund_evidence(state, proposal["reason"])
    if evidence:
        args["evidence"] = evidence

    result = propose_refund.invoke(args)
    if not result.get("ok"):
        code = str(result.get("error", "TOOL_ERROR"))
        log.warning("rules: the policy engine could not be asked: %s", code)
        return {
            "ruling": {**NO_RULING, "error": code},
            "proposal": _proposal("escalate", f"the policy engine could not be consulted: {code}"),
            "errors": [*state.get("errors", []), code],
        }

    overridden = result["tier"] == "deny" or result["allowed_amount"] != proposal["amount_pkr"]
    ruling = {
        "tier": result["tier"],
        "allowed_amount": result["allowed_amount"],
        "reasons": result["reasons"],
        "policy_refs": result["policy_refs"],
        "proposed_amount": proposal["amount_pkr"],
        "overridden": overridden,
    }
    if overridden:
        log.info("rules: the engine overrode the model proposal (tier %s)", result["tier"])
        audit(
            "policy_override",
            proposed_amount=proposal["amount_pkr"],
            allowed_amount=result["allowed_amount"],
            tier=result["tier"],
            reasons=result["reasons"],
        )
    return {"ruling": ruling}


def _refund_evidence(state: TicketState, reason: str) -> list[str]:
    """Evidence strings for the engine. Only a damage claim needs them: the customer's own words count as the
    description. rules and execute use this same helper, so the engine sees the same facts both times."""
    if reason != "damaged":
        return []
    description = last_customer_text(state).strip()[:300]
    return [description] if description else []


def approval_gate(state: TicketState) -> dict[str, Any]:
    """Pause at a manager or owner tier refund and wait for a human (Step M). Plain code, no model call.

    auto tier        -> nothing to wait for
    manager or owner -> create the approval row, call interrupt(): the graph is saved by the checkpointer and the
                        API call returns. Hours or days later the decision endpoint records the decision in the
                        approvals table and resumes the graph with Command(resume=...).

    On resume this node starts again from its first line, so everything before interrupt() must be safe to repeat:
    request_approval is keyed (ticket, order, tier, amount) and returns the same row, and the audit row is written
    only when the row was really created.

    After interrupt() the answer comes from the approvals TABLE, never from the resume payload.
    """
    ruling = state.get("ruling") or {}
    tier = ruling.get("tier")
    if tier == "auto":
        return {"approval": {"status": "auto"}}
    if tier not in ("manager", "owner"):
        return {"approval": {"status": "none"}}  # deny or no ruling: there is nothing to approve

    ctx = get_ctx()
    proposal = state.get("proposal") or {}
    ticket_id = state.get("ticket_id") or ctx.ticket_id
    order_id = state.get("order", {}).get("id", "")
    amount = int(ruling["allowed_amount"])
    payload = {
        "type": "refund_approval",
        "ticket_id": ticket_id,
        "order_id": order_id,
        "tier": tier,
        "amount_pkr": amount,
        "reasons": ruling.get("reasons", []),
        "policy_refs": ruling.get("policy_refs", []),
        "summary": proposal.get("summary", ""),
        "evidence_ids": proposal.get("evidence_ids", []),
    }
    approval_id, created = request_approval(
        ctx.session_factory,
        ticket_id=ticket_id,
        key=f"{ticket_id}:{order_id}:{tier}:{amount}",
        tier=tier,
        payload=payload,
        ttl_hours=settings.approval_ttl_hours,
        now=ctx.now(),
    )
    if created:
        audit("approval_requested", approval_id=approval_id, tier=tier, amount_pkr=amount)

    interrupt({**payload, "approval_id": approval_id})  # the graph stops here until a human decides

    row = get_approval(ctx.session_factory, approval_id) or {}
    return {
        "approval": {
            "status": row.get("status", "pending"),  # approved | rejected | expired | pending
            "by": row.get("decided_by"),
            "note": row.get("note"),
            "amount_pkr": row.get("amount_pkr"),  # what the human approved, maybe lowered
            "approval_id": approval_id,
        }
    }


def execute(state: TicketState) -> dict[str, Any]:
    """Move the money, but only for an engine-approved refund that is auto or approved by a human. No model call.

    - the idempotency key is ticket:order:refund, so a replay after a crash or a resume never refunds twice
    - the amount is the engine's allowed amount, or the lower amount the human approved
    - issue_refund runs the policy engine and the approval check again as the last line of defence
    - any error stops here: it goes to state["errors"] for verify and escalate, and nothing is retried blindly
    """
    proposal = state.get("proposal") or {}
    ruling = state.get("ruling") or {}
    approval = state.get("approval") or {}
    if (
        proposal.get("action") != "refund"
        or ruling.get("tier") not in ("auto", "manager", "owner")
        or approval.get("status") not in ("auto", "approved")
    ):
        return {"errors": ["NOT_APPROVED"]}

    ticket_id = state.get("ticket_id") or get_ctx().ticket_id
    order_id = state.get("order", {}).get("id", "")
    key = f"{ticket_id}:{order_id}:refund"
    if key in state.get("actions_taken", []):
        return {}  # already done in this run

    allowed = int(ruling["allowed_amount"])
    amount = min(int(approval.get("amount_pkr") or allowed), allowed)
    if amount <= 0:
        return {"errors": ["INVALID_AMOUNT"]}

    args: dict[str, Any] = {
        "order_id": order_id,
        "amount_pkr": amount,
        "reason": proposal["reason"],
        "idempotency_key": key,
    }
    evidence = _refund_evidence(state, proposal["reason"])
    if evidence:
        args["evidence"] = evidence
    if approval.get("approval_id") is not None:
        args["approval_id"] = approval["approval_id"]

    result = issue_refund.invoke(args)
    if not result.get("ok"):
        code = str(result.get("error", "TOOL_ERROR"))
        log.warning("execute: issue_refund failed: %s", code)
        return {"errors": [code]}
    return {
        "actions_taken": [*state.get("actions_taken", []), key],
        "result": {k: v for k, v in result.items() if k != "ok"},
    }


# ------------------------------------------------------------------------------------------------ verify
def verify(state: TicketState) -> dict[str, Any]:
    """Check that the refund really shows on the order (Step L). Plain code, no model call.

    execute said the refund was issued. verify does not trust that: it reads the order again from the shop and checks
    that refunded_total grew by at least the refunded amount, and that the refund status is "issued".

    - execute failed (errors in state)         -> verified False, the same errors, nothing else is tried
    - nothing was executed and no error        -> verified False, errors ["NOT_EXECUTED"]
    - the order cannot be read again           -> verified False, plus the error code of the shop
    - the refund does not show on the order    -> verified False, plus ["REFUND_NOT_CONFIRMED"] and an audit row

    There is no retry here on purpose. A second issue_refund cannot fix a missing refund (the idempotency key returns
    the same result), and a failed attempt would use up the second write of the ticket, which reply needs for the email
    (MAX_WRITES is 2). Any doubt about money goes to a human: after_verify sends it to escalate.
    """
    errors = list(state.get("errors", []))
    result = state.get("result") or {}
    if errors or not result.get("amount_pkr"):
        return {"verified": False, "errors": errors or ["NOT_EXECUTED"]}

    summary = state.get("order") or {}
    try:
        order = load_own_order(str(summary.get("id", "")))  # a shop read that checks the order is this customer's
    except AppError as err:
        log.warning("verify: the order could not be read again: %s", err.code)
        return {"verified": False, "errors": [*errors, err.code]}

    expected_total = int(summary.get("refunded_total") or 0) + int(result["amount_pkr"])
    if result.get("status") != "issued" or order.refunded_total < expected_total:
        log.warning("verify: the refund does not show on the order")
        audit(
            "refund_not_confirmed",
            order_id=order.id,
            expected_total=expected_total,
            shop_total=order.refunded_total,
            status=result.get("status"),
        )
        return {"verified": False, "errors": [*errors, "REFUND_NOT_CONFIRMED"]}
    return {"verified": True}


# ---------------------------------------------------------------------------------------------- escalate
def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


def _escalation_summary(state: TicketState) -> str:
    """One paragraph for the staff member, so nobody starts from zero. Built by code from the state.

    The customer's own words are NOT copied in (staff read them in the ticket). The only model text is the one-line
    note of the proposal, shortened and labelled.
    """
    order = state.get("order") or {}
    proposal = state.get("proposal") or {}
    ruling = state.get("ruling") or {}
    approval = state.get("approval") or {}
    errors = state.get("errors", [])
    sources = sorted({str(f.get("source")) for f in state.get("facts", [])})

    parts = [f"The request was classified as {state.get('intent', 'unknown')}."]
    if order:
        parts.append(
            f"Order {order.get('id')}: status {order.get('status')}, paid PKR {order.get('amount_paid')}, "
            f"already refunded PKR {order.get('refunded_total')}."
        )
    elif state.get("order_ref"):
        parts.append("The customer named an order, but it could not be loaded for this customer.")
    else:
        parts.append("No order number was found in the message.")
    parts.append("Checked: " + (", ".join(sources) or "nothing yet") + ".")
    if proposal:
        parts.append(f"The model proposed {proposal.get('action')} (model note: {_one_line(proposal.get('summary', ''), 200)}).")
    if ruling.get("tier") not in (None, "none"):
        parts.append(f"Policy engine: tier {ruling['tier']}, allowed PKR {ruling.get('allowed_amount')}.")
    if approval.get("status") not in (None, "none", "auto"):
        parts.append(f"Approval status: {approval['status']}.")
    if errors:
        parts.append("Errors: " + ", ".join(str(e) for e in errors) + ".")
    parts.append("Please read the customer message in the ticket and decide.")
    return " ".join(parts)[:1500]


def escalate(state: TicketState) -> dict[str, Any]:
    """The safe exit (Step L). Plain code, no model call. Always allowed: any role, no call budget.

    Marks the ticket as escalated and writes a one-paragraph summary to audit_log (event ticket_escalated).
    reply runs after it and only tells the customer that a person will look at the request.
    """
    result = escalate_to_human.invoke({"summary": _escalation_summary(state)})
    update: dict[str, Any] = {"escalated": True}
    if not result.get("ok"):
        code = str(result.get("error", "TOOL_ERROR"))
        log.warning("escalate: the ticket could not be marked: %s", code)
        update["errors"] = [*state.get("errors", []), code]
    return update


# ------------------------------------------------------------------------------------------------- reply
MAX_DETAILS_CHARS = 500
ESCALATED_DETAILS = "Thank you for your message. A member of our team will look at it and reply to you soon."
GENERAL_FALLBACK = "Thank you for your message. Our team will review it and get back to you."
# The model may write text for the customer, so code checks it: no address or link, and no sign that money moved.
UNSAFE_TEXT = re.compile(r"@|https?:|www\.", re.IGNORECASE)
MONEY_DONE = re.compile(
    r"\b(refunded|credited|reimbursed|issued|processed|approved)\b"
    r"|\brefund\s+(has|have|was)\b"
    r"|\brefund\s+(ho\s+gaya|ho\s+chuka|kar\s+di)",
    re.IGNORECASE,
)


class ReplyDetails(BaseModel):
    """What the model returns for reply: only the free-text part of the email. Everything else is a template."""

    details: str = Field(min_length=1, max_length=MAX_DETAILS_CHARS)


@dataclass
class ReplyPlan:
    """What reply will send, decided by code from the state. Only `details` can be model text."""

    template: str
    fields: dict[str, str] = field(default_factory=dict)
    details: str | None = None  # fixed text written by code
    outcome: str = ""  # what the model may say, when it writes the details
    fallback: str = ""  # used when the model fails or breaks a rule
    ask_model: bool = False


def _policy_ref(state: TicketState) -> str:
    """The policy section to quote: the engine's refs first, then the policy facts. Never an address or a link."""
    refs = (state.get("ruling") or {}).get("policy_refs") or [
        f["data"]["section"] for f in state.get("facts", []) if f.get("source") == "search_policy"
    ]
    text = _one_line(", ".join(str(r) for r in refs[:2]), 200)
    return text if text and not UNSAFE_TEXT.search(text) else "our returns policy"


def _status_fallback(state: TicketState) -> str:
    text = f"Your order is currently: {(state.get('order') or {}).get('status', 'unknown')}."
    tracking = next((f for f in state.get("facts", []) if f.get("source") == "track_shipment"), None)
    if tracking:
        text += f" Courier status: {(tracking.get('data') or {}).get('status', 'unknown')}."
    return text


def _reply_plan(state: TicketState) -> ReplyPlan:
    """Pick the template from what REALLY happened. The model never picks it."""
    errors = set(state.get("errors", []))
    order_id = str((state.get("order") or {}).get("id", ""))
    ruling = state.get("ruling") or {}
    approval = state.get("approval") or {}
    result = state.get("result") or {}

    if state.get("escalated"):
        return ReplyPlan("general_reply", details=ESCALATED_DETAILS)
    if {"NO_ORDER_REF", "ORDER_NOT_FOUND"} & errors:
        return ReplyPlan("need_verification")  # the same text for a missing and for a foreign order: nothing leaks
    if state.get("verified") and result:
        return ReplyPlan(
            "refund_confirmed",
            {"order_id": order_id, "amount_pkr": str(result.get("amount_pkr", ""))},
            details=f"Refund reference: {result.get('refund_id')}.",
        )
    if approval.get("status") == "rejected":
        return ReplyPlan(
            "refund_denied",
            {"order_id": order_id, "policy_ref": _policy_ref(state)},
            outcome="A manager reviewed the refund request and did not approve it. No money was refunded.",
            fallback="A manager reviewed your request and could not approve it.",
            ask_model=True,
        )
    if ruling.get("tier") == "deny":
        reasons = "; ".join(str(r) for r in ruling.get("reasons", [])) or "the request is outside the store policy"
        return ReplyPlan(
            "refund_denied",
            {"order_id": order_id, "policy_ref": _policy_ref(state)},
            outcome=f"The refund is not possible under the store policy ({reasons}). No money was refunded.",
            fallback=f"Reason: {reasons}."[:MAX_DETAILS_CHARS],
            ask_model=True,
        )
    outcome = "Answer the customer's question using only the facts. This email moves no money and promises no refund."
    if state.get("intent") == "order_status" and order_id:
        return ReplyPlan(
            "status_update", {"order_id": order_id}, outcome=outcome, fallback=_status_fallback(state), ask_model=True
        )
    return ReplyPlan("general_reply", outcome=outcome, fallback=GENERAL_FALLBACK, ask_model=True)


def _reply_input(state: TicketState, outcome: str) -> str:
    def clean(text: str) -> str:
        return text.replace("</customer_message>", "").replace("</facts>", "").replace("</outcome>", "")

    facts = clean(json.dumps(state.get("facts", []), ensure_ascii=False))
    return (
        f"<outcome>\n{clean(outcome)}\n</outcome>\n"
        f"<customer_message>\n{clean(last_customer_text(state))}\n</customer_message>\n"
        f"<facts>\n{facts}\n</facts>"
    )


def _details_ok(text: str) -> bool:
    return bool(text) and len(text) <= MAX_DETAILS_CHARS and not UNSAFE_TEXT.search(text) and not MONEY_DONE.search(text)


def _write_details(state: TicketState, plan: ReplyPlan) -> str:
    """The model writes the details. If it fails or breaks a rule, the fixed fallback text is used instead."""
    try:
        raw = (
            get_llm(temperature=0.3)
            .with_structured_output(ReplyDetails)
            .invoke(
                [
                    SystemMessage(load_prompt("reply")["system"]),
                    HumanMessage(_reply_input(state, plan.outcome)),
                ]
            )
        )
        text = ReplyDetails.model_validate(raw).details.strip()
    except Exception:  # malformed answer, rate limit, outage, missing model setting
        log.warning("reply: the model answer was malformed or missing, using the fixed text", exc_info=True)
        return plan.fallback
    if not _details_ok(text):
        log.warning("reply: the model text broke a rule, using the fixed text")
        return plan.fallback
    return text


def reply(state: TicketState) -> dict[str, Any]:
    """Write the customer email from a template plus details, and queue it (Step L). The last node of every path.

    Code picks the template from what REALLY happened (_reply_plan), so the email can never promise an action that was
    not executed:
    - escalated                      -> general_reply, fixed text: a person will look at it (no model call)
    - no or foreign order            -> need_verification (no model call)
    - refund executed and verified   -> refund_confirmed, amount and reference from the result (no model call)
    - manager rejected, engine deny  -> refund_denied with the policy section; the model only words the reason
    - information only               -> status_update or general_reply; the model words the answer from the facts

    The model text must pass _details_ok (no address, no link, no sign that money moved) or the fixed fallback is used.
    The idempotency key is ticket:reply:template, so a replay sends the same email once. If the email cannot be queued
    (role, email limit), the error goes to state and the run still ends: escalate is never entered a second time.
    """
    plan = _reply_plan(state)
    details = _write_details(state, plan) if plan.ask_model else plan.details
    fields = dict(plan.fields)
    if details is not None:
        fields["details"] = details

    ticket_id = state.get("ticket_id") or get_ctx().ticket_id
    sent = send_customer_email.invoke(
        {"template": plan.template, "fields": fields, "idempotency_key": f"{ticket_id}:reply:{plan.template}"}
    )
    if not sent.get("ok"):
        code = str(sent.get("error", "TOOL_ERROR"))
        log.warning("reply: the email was not queued: %s", code)
        return {
            "outgoing": {"template": plan.template, "sent": False, "error": code},
            "errors": [*state.get("errors", []), code],
        }
    return {"outgoing": {"template": plan.template, "sent": True}}


# ------------------------------------------------------------------------------------------------- edges
HANDLED_INTENTS = {"order_status", "refund", "exchange"}


def after_triage(state: TicketState) -> Literal["gather_facts", "escalate"]:
    """Only the three intents this agent handles read facts. Anything else (other, product_question) goes to a human."""
    return "gather_facts" if state.get("intent") in HANDLED_INTENTS else "escalate"


def after_rules(state: TicketState) -> Literal["approval_gate", "reply", "escalate"]:
    """Route by what the model proposed AND what the engine ruled. A missing proposal is never a reason to act."""
    action = (state.get("proposal") or {}).get("action")
    if action == "reply":
        return "reply"
    if action != "refund":
        return "escalate"  # escalate, or nothing usable
    tier = (state.get("ruling") or {}).get("tier")
    if tier == "deny":
        return "reply"  # a polite denial, no human needed
    if tier in ("auto", "manager", "owner"):
        return "approval_gate"  # auto passes straight through the gate
    return "escalate"


def after_approval(state: TicketState) -> Literal["execute", "reply", "escalate"]:
    """Only an auto tier or a recorded human approval reaches execute. A rejection is answered; anything else
    (pending after a resume, expired, no row) goes to a person."""
    status = (state.get("approval") or {}).get("status")
    if status in ("auto", "approved"):
        return "execute"
    if status == "rejected":
        return "reply"
    return "escalate"


def after_verify(state: TicketState) -> Literal["reply", "escalate"]:
    return "reply" if state.get("verified") else "escalate"


def build_support_graph(checkpointer: Any = None) -> Any:
    """Wire the nodes into the support graph of Figure 4 and compile it.

    START -> triage -> (gather_facts | escalate)
    gather_facts -> decide -> rules -> (approval_gate | reply | escalate)
    approval_gate -> (execute | reply | escalate)        execute -> verify -> (reply | escalate)
    escalate -> reply -> END

    Pass a checkpointer (InMemorySaver in tests, the Postgres saver in the API) for interrupt() and resume to work.
    Use thread_id = ticket id in the config.
    """
    g = StateGraph(TicketState)
    for name, fn in [
        ("triage", triage),
        ("gather_facts", gather_facts),
        ("decide", decide),
        ("rules", rules),
        ("approval_gate", approval_gate),
        ("execute", execute),
        ("verify", verify),
        ("reply", reply),
        ("escalate", escalate),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "triage")
    g.add_conditional_edges("triage", after_triage, ["gather_facts", "escalate"])
    g.add_edge("gather_facts", "decide")
    g.add_edge("decide", "rules")
    g.add_conditional_edges("rules", after_rules, ["approval_gate", "reply", "escalate"])
    g.add_conditional_edges("approval_gate", after_approval, ["execute", "reply", "escalate"])
    g.add_edge("execute", "verify")
    g.add_conditional_edges("verify", after_verify, ["reply", "escalate"])
    g.add_edge("escalate", "reply")
    g.add_edge("reply", END)
    return g.compile(checkpointer=checkpointer)
