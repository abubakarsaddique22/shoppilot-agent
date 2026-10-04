# ShopPilot: Product Requirements Document (PRD)

Version 1.0 | Status: Draft for build | Owner: Abubakar

## 1. Summary

ShopPilot is an approval-aware, multi-agent E-commerce Operations Agent for a Shopify-style online store. It reads customer tickets, looks up orders and courier status, applies the store's refund rules, asks a human for approval when money or risk is involved, executes the action, replies to the customer and writes an audit record. The same system also drafts purchase orders for low stock, drafts product listings and sends a daily operations report.

This is a portfolio project built on a Shopify development store (with a built-in MockShop fallback) using only fake data. It is not connected to any live store.

**Core principle: the LLM proposes, code decides.** The model never decides money movement. A deterministic policy engine, validators, role checks and human approvals do.

## 2. Problem

Small store owners and support staff spend hours every day on the same repetitive work: "where is my order?", "my order is late, I want a refund", "item arrived damaged", reorder decisions and writing product descriptions. A normal chatbot only answers with policy text. The store needs a system that does the work end to end, safely.

## 3. Users

| User | Needs | How they use ShopPilot |
|---|---|---|
| Store owner | Visibility, control over money, daily summary | Approves high-value refunds (owner tier), reads daily report, reviews purchase-order drafts |
| Support staff | Fewer repetitive tickets, good escalation summaries | Runs tickets, reviews escalations, cannot approve money |
| Manager | Fast, safe approvals, ideally from a phone | Approves or rejects manager-tier refunds in the approval inbox |
| Customer | Quick, correct, polite answers | Sends an email; receives a reply and, if eligible, a refund |
| Admin | Operate the system | Triggers reports, manages users and roles |

Roles in the system: viewer, support, manager, owner, admin.

## 4. Tasks in Scope (Version 1)

1. **Order status**: find the order, check courier tracking, answer the customer.
2. **Refund or replacement**: evaluate against policy, execute within limits, or ask for approval.
3. **Product question**: answer from policy knowledge base and product data, with citations.
4. **Low-stock purchase order**: propose reorder quantity, save a draft for human sending.
5. **Product listing**: write title, bullets, description and tags; save as a draft, never published.
6. **Daily report**: sales, refunds, late orders, low stock, three recommended actions, by email and S3.

## 5. Non-Goals

- No real payments outside the Shopify refund API (and MockShop for tests).
- No deleting data (orders, customers, products, audit rows).
- No live store in version 1. Development store and MockShop only.
- No real customer data. Fake data only, because free LLM tiers and LangSmith traces store prompts.
- The agent never publishes products, never emails suppliers, never changes live prices by itself.
- No automatic cash-on-delivery money movement. COD refunds create a payout task for staff.
- No local LLM or heavy embedding model on the small EC2 instance.
- No autonomy level L3 (act, review later) in version 1.

## 6. Why an Agent (and Not a Workflow or RAG Chain)

| Pattern | Fit for ShopPilot |
|---|---|
| RAG chain | Used only as one tool, `search_policy`, to quote store policy |
| Workflow (fixed graph) | Used for the daily report: fetch data, summarise, send |
| Agent | Used for refund tickets: the next step depends on what the order, tracking and policy tools return |
| Multi-agent | A supervisor routes to Support, Inventory/Listing and Reports because they need different tools, permissions and prompts |

Rule applied: use the simplest pattern that works. Add agents only where tools or permissions really differ.

## 7. Success Criteria

All numbers are measured on the LangSmith evaluation dataset (about 60 cases) and the red-team suite. No number is published unless it was measured.

| Metric | Target | Gate |
|---|---|---|
| Task success rate | at least 0.90 | CI fails if it drops more than 3 points from baseline |
| Wrong-refund rate | exactly 0 | Hard gate |
| Policy-violating actions | exactly 0 | Hard gate |
| Approval bypass (money moved without required approval) | exactly 0 | Hard gate |
| Correct escalation (escalates when it should, and not when it should not) | at least 0.95 | |
| Trajectory score (required tools called, forbidden tools not called) | at least 0.85 | |
| Tool calls per ticket (average) | at most 6 | |
| Reply quality (rubric judge, 1 to 5) | at least 4.0, calibrated on 20 human-scored replies | |
| Injection resistance (red-team) | 100 percent of at least 20 cases | CI |
| p95 time for an automatic ticket (human waiting time excluded) | under 15 seconds | |
| Cost per ticket | tracked and trending down | |
| Router accuracy on 20 mixed inputs | at least 95 percent | |
| Test coverage on policy, tools and guardrails | at least 80 percent | CI |

## 8. Autonomy Levels

| Level | Behaviour | ShopPilot actions |
|---|---|---|
| L0 Read only | Reads data and explains, no side effects | Order status, tracking, policy questions, sales summary |
| L1 Draft | Prepares the action; a human presses send | Purchase orders, product listings, price changes, outgoing replies at first |
| L2 Act within limits | Acts alone below hard limits, everything logged | Refund up to PKR 3,000 that matches policy, order status replies |
| L3 Act, review later | Acts, human samples results | Not used in version 1. Unlock only after weeks of clean evaluation data |

## 9. Refund Approval Tiers

Values are examples and live in YAML (`configs/settings.dev.yaml`), so the owner can change them without a code release.

| Tier | Condition | Who decides | Agent behaviour |
|---|---|---|---|
| auto | Within 14 days of delivery, or late by more than 5 days; amount up to PKR 3,000; no earlier refund on the order; customer not flagged; prepaid order | Agent within hard limits | Refund, send confirmation, write audit row |
| manager | PKR 3,001 to 15,000, or partial or unusual case, or cash-on-delivery, or damage claim | Manager in approval inbox | `interrupt()`, wait, then act on the decision |
| owner | Above PKR 15,000, or two or more refunds by the customer in 90 days, or flagged customer | Owner in approval inbox | `interrupt()` with full evidence summary |
| deny | Outside the window without a defect, already refunded, order not yet due, non-refundable category | Policy engine | Polite denial citing the policy, offer an alternative, no human needed |

**Hard invariants that no approval can override:**
- A refund never exceeds amount paid minus earlier refunds.
- One open refund per order.
- Blocked or flagged customers always escalate.
- After a human approves, the agent re-checks the rules. Approval is evidence of intent, not permission to skip validation.

## 10. Budgets and Hard Limits (per run)

| Limit | Default | On breach |
|---|---|---|
| Read tool calls per ticket | 6 | Stop and escalate with partial facts |
| Write tool calls per ticket | 2 | Stop and escalate |
| Retries of decide or verify | 2 | Escalate |
| Model time per ticket | 45 seconds | Escalate |
| Automatic refund per ticket | PKR 3,000 | Move to manager tier |
| Automatic refunds per day (whole store) | PKR 30,000 | All refunds move to manager tier until next day, owner notified |
| Outgoing emails per ticket | 3 | Escalate |
| Approval wait | 48 hours | Escalate to owner, then close with a customer notice |

## 11. Functional Requirements

**FR1 Ticket handling.** Tickets arrive from the web UI, an email webhook or a scheduler. Each ticket gets a stable `ticket_id`, used as the LangGraph `thread_id`.

**FR2 Routing.** A supervisor routes each item to support, inventory, listing or reports with a confidence score. Low confidence goes to a human triage queue. Scheduler events skip the model.

**FR3 Support agent.** Nodes: triage, gather_facts, decide, rules, approval_gate, execute, verify, reply, escalate. Model calls only in triage, decide, verify and reply.

**FR4 Policy engine.** `evaluate_refund()` is a pure Python function returning tier, allowed amount, reasons and policy references. The engine result always overrides the model. Disagreements are logged.

**FR5 Policy knowledge base.** `search_policy(query, k)` returns chunks with source and section. Below the similarity threshold it returns "no policy found" and the agent escalates instead of inventing a rule.

**FR6 Tools.** Every tool has pydantic input and output, a timeout, a role check and an audit row. Write tools take an idempotency key and re-run the policy engine as the last line of defence. Tools return the smallest useful result.

**FR7 Human-in-the-loop.** Manager and owner tiers pause the graph with `interrupt()`. The decision endpoint resumes with `Command(resume=...)` on the same thread. Supports approve, reject and edit amount downward. Expired approvals escalate after 48 hours. State survives a server restart.

**FR8 Inventory and listing agents.** Inventory proposes reorder quantity as a purchase-order draft. Listing writes a draft product in the brand voice. Each agent has its own tool allow-list (Support cannot create purchase orders; Inventory cannot issue refunds).

**FR9 Scheduled jobs.** Daily report and low-stock alert run through the same graph path with a deterministic `thread_id` (for example `report-2026-10-03`), so reruns are idempotent.

**FR10 API.** Versioned FastAPI under `/v1` with JWT login, roles, rate limits, SSE streaming of node events, webhook endpoint and health and ready probes.

**FR11 Web UI.** Plain HTML, CSS and JavaScript: login, ticket inbox, ticket detail with live agent timeline and evidence panel, approval queue, reports, and a customer-email simulator. All untrusted text is inserted with `textContent`.

**FR12 Audit.** Every action and every approval decision is written to an append-only `audit_log` with actor and timestamp.

**FR13 Evaluation.** LangSmith dataset of about 60 cases with outcome, trajectory and policy evaluators, an LLM judge for tone only, and a CI regression gate.

## 12. Non-Functional Requirements

- **Safety:** policy engine outside the prompt; typed model output; idempotent writes; recipient allow-list for email; trust boundary around untrusted text.
- **Security:** hashed passwords (argon2 or bcrypt), role checks in the API and again inside tools (role from signed state, never from model output), no secrets in Git or images, secrets from SSM in production.
- **Reliability:** timeouts, retry with backoff, fallback model, bounded loops, graceful escalation, Postgres checkpointer with a kill-and-resume test.
- **Observability:** LangSmith traces tagged with environment, ticket id, role and prompt version; structured JSON logs with request id; cost per ticket stored with the ticket.
- **Privacy:** fake data only; mask emails in logs; never log API keys or full customer messages.
- **Cost:** free tiers only; AWS Budget alert of 1 to 5 USD from day one; stop EC2 after each session.
- **Accessibility:** semantic HTML, visible focus, `aria-live` timeline, mobile-friendly approval page, colour never used alone.

## 13. Architecture (short)

Triggers (web UI, email webhook, scheduler) call the FastAPI service. FastAPI authenticates, then starts or resumes a LangGraph run. The supervisor routes to Support, Inventory/Listing or Reports. Agents never touch the outside world directly: every tool call passes through one guarded tool layer (typed schemas, timeouts, idempotency keys, role checks, policy engine, audit row). Data lives in one Postgres database (MockShop tables, app tables, pgvector, checkpoints). See `docs/architecture.md` for diagrams.

## 14. Technology Choices

Python 3.12, LangGraph + LangChain, LangSmith, Gemini or Groq free tier (provider is a config value), PostgreSQL 16 with pgvector in Docker, FastAPI + Uvicorn, vanilla HTML/CSS/JS, Caddy, Docker Compose, GitHub Actions, AWS (EC2, S3, IAM, SSM, CloudWatch, Budgets). Each choice gets a short ADR in `docs/adr/`.

## 15. Milestones

| Week | Steps | Deliverable |
|---|---|---|
| 1 | A, B, C, D | Working repo, PRD with targets, budget alert on, tracing key tested |
| 2 | E, F, G, H, I | `make seed` and `make ingest` work; tools pass unit tests |
| 3 | J, K, L | Agent answers order-status tickets; 15 evaluation cases |
| 4 | M, N, O, P | Refund with approval works end to end; daily report runs |
| 5 to 6 | Q, R, S, T | Secured API with Swagger docs; clickable UI; 20 attack tests pass |
| 7 | U, V, W | Evaluation report, green CI badge, cost per ticket measured |
| 8 | X, Y | AWS setup and deployment, backups |
| 9 | Z | README with results, demo video, freelancing profile |

## 16. Risks and Mitigations

| Risk | Mitigation |
|---|---|
| Wrong or double refund | Policy engine, re-check inside tool, idempotency keys, hard gate in CI |
| Prompt injection from emails, notes or reviews | Delimited untrusted text, typed output, argument validation, allow-lists, 20+ red-team cases in CI |
| Weak free model fails tool calls | Test several models in LangSmith experiments; stronger model only for decide |
| Surprise AWS bill | Budget alert first, stop and cleanup routine, tags, weekly bill check |
| Free-tier limits change | Record current limits in `docs/costs.md` before starting |
| Shopify API blocks progress | MockShop behind the same `ShopBackend` interface |
| Real data leaks into free LLMs or traces | Fake data only, hidden inputs in traces if real data ever appears |

## 17. Definition of Done (Version 1)

- Order status, refund, exchange, restock draft, listing draft and daily report all work end to end from the UI.
- Evaluation shows task success at least 0.90, wrong-refund rate 0, approval bypass 0, and the table is shown in `docs/eval/report.md`.
- Approval inbox works; the graph survives a restart while waiting; expired approvals escalate.
- Roles tested; red-team suite passes in CI; no secrets in Git; untrusted text is never rendered as HTML.
- At least 80 percent coverage on policy, tools and guardrails; green CI badge.
- IAM least privilege verified by commands; private encrypted S3 bucket; secrets in SSM; budget alert tested.
- One-command deploy, tagged images, rollback tested, backup restored once.
- README with results and diagrams, demo video, gig descriptions.

## 18. Open Questions

- Which free LLM (Gemini or Groq) calls tools most reliably? Decide after the first two models are compared in Step J.
- Email delivery for demos: SES (sandbox) or Gmail SMTP with an app password?
- HTTPS: free subdomain, or HTTP plus login for a private demo?
