"""The support graph (Steps K and L).

START -> triage -> (gather_facts | escalate)
gather_facts -> decide -> rules -> (approval_gate | reply | escalate)
approval_gate -> (execute | reply | escalate)
execute -> verify -> (reply | escalate)
escalate -> reply -> END

Only triage, decide and reply call the model. Everything else is plain code.
The model proposes, the policy engine decides (rules node), a human approves money (approval_gate).
"""
import json
from dataclasses import dataclass, field
from typing import Any, Literal

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
from shoppilot.core.prompts import load_prompt
from shoppilot.guardrails.sanitize import injection_flags, remove_delimiters, wrap_untrusted
from shoppilot.guardrails.validators import contains_link_or_address, reply_text_problem
from shoppilot.tools.context import audit, get_ctx
from shoppilot.tools.email import escalate_to_human, send_customer_email
from shoppilot.tools.orders import get_order, load_own_order, search_policy, track_shipment
from shoppilot.tools.refunds import Reason, issue_refund, propose_refund

log = get_logger(__name__)

GRAPH_VERSION = "support-v1"  # bump when a node or edge changes (goes into the LangSmith metadata)


def last_customer_text(state: TicketState) -> str:
    for message in reversed(state.get("messages", [])):
        if getattr(message, "type", None) == "human":
            return str(message.content)
    return ""


def _one_line(text: Any, limit: int) -> str:
    return " ".join(str(text).split())[:limit]


# --- triage ---
class Triage(BaseModel):
    """What the model must return for triage."""

    intent: Intent
    order_ref: str | None = None


def triage(state: TicketState) -> dict[str, Any]:
    """Sort the message: which intent, which order number. Calls no tools."""
    text = last_customer_text(state)
    if not text:
        return {"intent": "other", "order_ref": None}

    flags = injection_flags(text)
    if flags:
        audit("suspicious_text", flags=flags)  # a signal for the audit log only: the real defence is the code after the model
    try:
        raw = get_llm().with_structured_output(Triage, method="json_schema").invoke(
            [
                SystemMessage(load_prompt("triage")["system"]),
                HumanMessage(wrap_untrusted(text)),  # cleaned, and the customer cannot close the data tag
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


# --- gather_facts ---
# What we ask the policy knowledge base, per intent. Fixed text: the customer's words never go into the query.
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
    """Read the order, the shipment and the policy. Plain code. Never guesses a missing fact.

    Errors it can set: NO_ORDER_REF, ORDER_NOT_FOUND (also for another customer's order), BUDGET_EXCEEDED, NO_POLICY_FOUND.
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


# --- decide ---
MAX_DECIDE_ATTEMPTS = 2  # the first answer, plus one retry after we tell the model what was wrong

Action = Literal["refund", "reply", "escalate"]


class Decision(BaseModel):
    """What the model PROPOSES. It never decides money: the policy engine does, and the engine wins."""

    action: Action
    amount_pkr: int | None = Field(default=None, gt=0, le=100_000)
    reason: Reason | None = None
    evidence_ids: list[str] = Field(default_factory=list, max_length=8)
    summary: str = Field(default="", max_length=300)


def _proposal(action: Action, summary: str) -> dict[str, Any]:
    """A proposal made by code, without a model call."""
    return Decision(action=action, summary=summary[:300]).model_dump()


def check_decision(decision: Decision, state: TicketState) -> str | None:
    """Return a problem text for the model, or None when the proposal is fine.
    The text never repeats what the model wrote, so a hostile string cannot travel into the next answer."""
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
    facts = remove_delimiters(json.dumps(state.get("facts", []), ensure_ascii=False))  # data cannot close its own tag
    return (
        f"Intent: {state.get('intent', 'other')}\n"
        f"{wrap_untrusted(last_customer_text(state))}\n"
        f"<facts>\n{facts}\n</facts>"
    )


def decide(state: TicketState) -> dict[str, Any]:
    """The model proposes one action. No model call when the facts already decide it:
    missing or foreign order -> reply; facts incomplete or no policy -> escalate; retry budget used up -> escalate.
    A wrong answer is sent back to the model once, then the ticket goes to a human."""
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


# --- rules ---
# What `rules` stores when no money is involved, or when the engine could not be asked.
NO_RULING: dict[str, Any] = {
    "tier": "none",
    "allowed_amount": 0,
    "reasons": [],
    "policy_refs": [],
    "proposed_amount": None,
    "overridden": False,
}


def _refund_evidence(state: TicketState, reason: str) -> list[str]:
    """Evidence for the engine. Only a damage claim needs it: the customer's own words count as the description."""
    if reason != "damaged":
        return []
    description = last_customer_text(state).strip()[:300]
    return [description] if description else []


def rules(state: TicketState) -> dict[str, Any]:
    """The policy engine decides, not the model. Only a refund proposal needs it.

    The ruling is final (tier, allowed amount, reasons, policy sections). If the engine says less than the model
    proposed, or says no, the engine wins and the difference is written to audit_log (policy_override).
    If the engine cannot be asked, no money moves: the proposal becomes an escalation.
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


# --- approval_gate ---
def approval_gate(state: TicketState) -> dict[str, Any]:
    """Pause at a manager or owner refund and wait for a human (Step M). Plain code.

    auto tier -> nothing to wait for. Manager or owner tier -> create the approval row and call interrupt().
    On resume this node starts again from its first line, so everything before interrupt() must be safe to repeat.
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


# --- execute ---
def execute(state: TicketState) -> dict[str, Any]:
    """Move the money, only for a refund that is auto or approved by a human. No model call.

    The key ticket:order:refund makes a replay safe. issue_refund runs the policy engine and the approval check again.
    Any error stops here and goes to state["errors"].
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


# --- verify ---
def verify(state: TicketState) -> dict[str, Any]:
    """Read the order again and check that the refund really shows on it. Plain code.

    There is no retry on purpose: a second issue_refund cannot fix a missing refund, and any doubt about
    money goes to a human (after_verify sends it to escalate).
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


# --- escalate ---
def _escalation_summary(state: TicketState) -> str:
    """One paragraph for the staff member, built by code from the state.
    The customer's own words are not copied in (staff read them in the ticket)."""
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
    """The safe exit. Marks the ticket as escalated and writes a summary to audit_log. No model call."""
    result = escalate_to_human.invoke({"summary": _escalation_summary(state)})
    update: dict[str, Any] = {"escalated": True}
    if not result.get("ok"):
        code = str(result.get("error", "TOOL_ERROR"))
        log.warning("escalate: the ticket could not be marked: %s", code)
        update["errors"] = [*state.get("errors", []), code]
    return update


# --- reply ---
MAX_DETAILS_CHARS = 500
ESCALATED_DETAILS = "Thank you for your message. A member of our team will look at it and reply to you soon."
GENERAL_FALLBACK = "Thank you for your message. Our team will review it and get back to you."


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
    """The policy section to quote: the engine's refs first, then the policy facts."""
    refs = (state.get("ruling") or {}).get("policy_refs") or [
        f["data"]["section"] for f in state.get("facts", []) if f.get("source") == "search_policy"
    ]
    text = _one_line(", ".join(str(r) for r in refs[:2]), 200)
    return text if text and not contains_link_or_address(text) else "our returns policy"


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
        return ReplyPlan("need_verification")  # the same text for a missing and a foreign order: nothing leaks
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
    facts = remove_delimiters(json.dumps(state.get("facts", []), ensure_ascii=False))
    return (
        f"{wrap_untrusted(outcome, 'outcome')}\n"
        f"{wrap_untrusted(last_customer_text(state))}\n"
        f"<facts>\n{facts}\n</facts>"
    )


def _write_details(state: TicketState, plan: ReplyPlan) -> str:
    """The model writes the details. If it fails or breaks a rule, the fixed fallback text is used."""
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
    if problem := reply_text_problem(text, max_chars=MAX_DETAILS_CHARS):
        log.warning("reply: the model text broke a rule (%s), using the fixed text", problem)
        return plan.fallback
    return text


def reply(state: TicketState) -> dict[str, Any]:
    """Write the customer email from a template plus details and queue it. The last node of every path.

    Code picks the template from what REALLY happened, so the email can never promise an action that was not done.
    The key ticket:reply:template makes a replay send the same email once.
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


# --- edges ---
HANDLED_INTENTS = {"order_status", "refund", "exchange"}


def after_triage(state: TicketState) -> Literal["gather_facts", "escalate"]:
    """Only the three handled intents read facts. Anything else goes to a human."""
    return "gather_facts" if state.get("intent") in HANDLED_INTENTS else "escalate"


def after_rules(state: TicketState) -> Literal["approval_gate", "reply", "escalate"]:
    """Route by what the model proposed AND what the engine ruled. A missing proposal is never a reason to act."""
    action = (state.get("proposal") or {}).get("action")
    if action == "reply":
        return "reply"
    if action != "refund":
        return "escalate"
    tier = (state.get("ruling") or {}).get("tier")
    if tier == "deny":
        return "reply"  # a polite denial, no human needed
    if tier in ("auto", "manager", "owner"):
        return "approval_gate"  # auto passes straight through the gate
    return "escalate"


def after_approval(state: TicketState) -> Literal["execute", "reply", "escalate"]:
    """Only an auto tier or a recorded human approval reaches execute. A rejection is answered; the rest goes to a person."""
    status = (state.get("approval") or {}).get("status")
    if status in ("auto", "approved"):
        return "execute"
    if status == "rejected":
        return "reply"
    return "escalate"


def after_verify(state: TicketState) -> Literal["reply", "escalate"]:
    return "reply" if state.get("verified") else "escalate"


def build_support_graph(checkpointer: Any = None) -> Any:
    """Wire the nodes into the graph and compile it. Use thread_id = ticket id in the config.
    Pass a checkpointer (InMemorySaver in tests, the Postgres saver in the API) so interrupt() and resume work."""
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
