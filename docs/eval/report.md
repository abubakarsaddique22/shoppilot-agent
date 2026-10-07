# ShopPilot evaluation report (Step U)

Date: 2026-10-07. Model for every run: `groq/openai/gpt-oss-120b`. Dataset: 60 cases (blueprint Table 26), built by `scripts/build_cases_v1.py`.
Every case starts from the same seeded MockShop with a fixed clock, and the real graph, tools, policy engine and approvals service run. Only the policy search is replaced by fixed text (`policy search fixed`), and the human decisions are scripted.
All numbers below were measured in this repository. A dash means "not measured".

## Experiment log (blueprint Table 28)

| Configuration | Cases | Task success | Wrong refunds | Trajectory | p95 latency | Cost per ticket |
|---|---|---|---|---|---|---|
| Baseline: single ReAct agent, 3 read tools (Step J, dataset v0: status and policy tickets only, so not comparable with the rows below) | 15 | 1.00 (15/15) | - | - | - | - |
| Full graph (triage, decide, policy engine, approval gate, verify, guardrails), smoke subset, triage via function calling | 10 | 0.80 | 0 | 0.80 | 39.4 s | - |
| Same, triage via `json_schema` | 10 | 1.00 | 0 | 1.00 | 27.8 s | - |
| Same, full dataset (`decide` prompt v1) | 60 | 0.95 | 0 | 0.95 | 24.9 s | - |

Not measured separately: the effect of each safety layer on its own (engine override, approval gate and verify node, guardrails). They were built together, so I cannot say how much each one contributed.
Cost per ticket is not computed: the Groq free tier has no price, so `--price-in` and `--price-out` were left at 0. Token use per case was 2118 in and 490 out on the full run.

## Result of the full run (60 cases, 1 run per case)

| Metric | Value | Target |
|---|---|---|
| Task success | 0.95 | >= 0.90 |
| Wrong-refund cases | 0 | 0 (hard gate) |
| Approval-bypass cases | 0 | 0 (hard gate) |
| Correct escalation | 1.00 | >= 0.95 |
| Trajectory | 0.95 | >= 0.85 |
| Tool calls per ticket | 3.9 | <= 6 |
| Injection resistance (8 attack cases) | 1.00 | 1.00 |
| p95 latency, automatic tickets | 24.9 s | < 15 s (missed) |
| Reply quality (LLM judge) | not run | >= 4.0 |

Success by category: order status 1.00, refund within auto tier 0.80, refund needing approval 0.88, refund denied 1.00, exchange and product 1.00, inventory and listing 1.00, adversarial 1.00, failure and edge cases 1.00.

Router (Step O, 20 mixed inputs): accuracy 0.95 (19 of 20), target 0.95. The one miss asked for sales numbers and late orders in one sentence. The router sent it to `unclear`, which is the human triage queue, so no wrong agent ran. I did not change the label to make it 20 of 20.

## What I found

1. **Structured output through function calling failed on Groq.** In the first smoke run, 2 of 10 tickets failed in `triage`: the model named the tool after the schema and Groq rejected the call as a tool that was not in the request. The graph did the safe thing and escalated to a human, so no money moved, but refund tickets were left unfinished (smoke success 0.80). Switching `triage` to `method="json_schema"` removed the error (smoke success 1.00).
2. **The three failures of the full run are all "too careful".** In `auto-05`, `auto-09` and `approval-02` the model chose to reply instead of proposing a refund, and the engine never saw a proposal. No wrong money moved, but the customer did not get a refund the engine would have allowed. Reading `decide.yaml` v1 suggests two causes: the prompt told the model to reply when the customer gives no reason and the order is not late, while the engine also refunds a delivered order within 14 days, and in two cases the model applied the policy limit itself instead of letting the engine decide. This is a reading of the prompt, I have not yet checked the traces.
3. **A guardrail caught the model 5 times.** During the 60 runs the reply check logged `MONEY_CLAIM` five times: the model wrote text promising money, and the code replaced it with the fixed template.
4. **Outcome checks found what text quality would not.** The three failed replies were polite and cited policy, and still wrong. A judge that only reads the email would have passed them.

## Limitations (honest)

- One run per case (`repeats 1`). The model is not deterministic, so a single pass proves little. Mean and worst case over several runs are still to do.
- `decide` prompt v2 (refund also for a delivered order the customer wants to return, and no self-applied limits) is written but **not measured yet**. The baseline in `data/eval/baseline.json` (0.95) belongs to prompt v1.
- p95 latency of 24.9 s misses the 15 s target. Each ticket makes about three model calls to a large reasoning model. Not yet investigated.
- The LLM judge for reply quality was not run and has not been calibrated against hand-scored replies.
- Policy search used fixed text, not the Postgres knowledge base (`--real-kb`).
- The dataset has 60 cases, and 8 of them are attacks, so the 1.00 injection resistance covers a small sample.

## Run blocks (added by `scripts/run_eval.py --report`)

### graph-v2-gptoss120b

2026-10-07 01:21 UTC | model `groq/openai/gpt-oss-120b` | 60 runs | subset full | repeats 1 | policy search fixed

| Metric | Value | Target |
|---|---|---|
| task_success | 0.950 | >= 0.9 |
| correct_escalation | 1.000 | >= 0.95 |
| trajectory | 0.950 | >= 0.85 |
| tool_calls_avg | 3.933 | <= 6.0 |
| reply_quality | n/a | >= 4.0 |
| injection_resistance | 1.000 | >= 1.0 |
| latency_p95_s | 24.886 | <= 15.0 |
| wrong_refund_cases | 0 | 0 |
| approval_bypass_cases | 0 | 0 |
| tokens in / out per case | 2118 / 490 | tracked |
| cost per ticket | 0.0000 | tracked |

Success by category: failure_edge 1.00, refund_auto 0.80, refund_denied 1.00, adversarial 1.00, order_status 1.00, exchange_product 1.00, inventory_listing 1.00, refund_approval 0.88
