# ShopPilot evaluation report (Step U)

Date: 2026-10-07. Model for every run: `groq/openai/gpt-oss-120b`. Dataset: 60 cases (blueprint Table 26), built by `scripts/build_cases_v1.py`.
Every case starts from the same seeded MockShop with a fixed clock, and the real graph, tools, policy engine and approvals service run. Only the policy search is replaced by fixed text (`policy search fixed`), and the human decisions are scripted.
Prompts: `triage` v1, `reply` v1, `decide` v1 first and v2 later.
All numbers below were measured in this repository. A dash means "not measured".

## Experiment log (blueprint Table 28)

| Configuration | Cases | Task success | Wrong refunds | Trajectory | p95 latency | Cost per ticket |
|---|---|---|---|---|---|---|
| Baseline: single ReAct agent, 3 read tools (Step J, dataset v0: status and policy tickets only, so not comparable with the rows below) | 15 | 1.00 (15/15) | - | - | - | - |
| Full graph (triage, decide, policy engine, approval gate, verify, guardrails), smoke subset, triage via function calling | 10 | 0.80 | 0 | 0.80 | 39.4 s | - |
| Same, triage via `json_schema` | 10 | 1.00 | 0 | 1.00 | 27.8 s | - |
| Same, full dataset (`decide` prompt v1) | 60 | 0.95 | 0 | 0.95 | 24.9 s | - |
| Same, full dataset, `decide` prompt v2 (second attempt, see the note) | 60 | 1.00 | 0 | 1.00 | 27.7 s | - |

Note on the v2 row: the first attempt of the v2 run (01:49 UTC) scored 0.48 and is kept at the bottom of this file as **invalid**. It used only 488 input and 109 output tokens per case instead of about 2000 and 450, so most model calls did not happen. I did not check the cause (a rate limit or a network problem is likely). The second attempt (02:08 UTC) has the same label and the same configuration as far as I know, and gave the row above. Both are reported so nobody has to guess.

Not measured separately: the effect of each safety layer on its own (engine override, approval gate and verify node, guardrails). They were built together, so I cannot say how much each one contributed.
Cost per ticket is not computed: the Groq free tier has no price, so `--price-in` and `--price-out` were left at 0. Token use per case was 2118 in and 490 out (v1) and 2079 in and 454 out (v2).

## Results of the two valid full runs (60 cases, 1 run per case)

| Metric | `decide` v1 | `decide` v2 | Target |
|---|---|---|---|
| Task success | 0.95 | 1.00 | >= 0.90 |
| Wrong-refund cases | 0 | 0 | 0 (hard gate) |
| Approval-bypass cases | 0 | 0 | 0 (hard gate) |
| Correct escalation | 1.00 | 1.00 | >= 0.95 |
| Trajectory | 0.95 | 1.00 | >= 0.85 |
| Tool calls per ticket | 3.9 | 4.1 | <= 6 |
| Injection resistance (8 attack cases) | 1.00 | 1.00 | 1.00 |
| p95 latency, automatic tickets | 24.9 s | 27.7 s | < 15 s (missed both times) |
| Reply quality (LLM judge) | not run | not run | >= 4.0 |

Success by category, v1: order status 1.00, refund within auto tier 0.80, refund needing approval 0.88, refund denied 1.00, exchange and product 1.00, inventory and listing 1.00, adversarial 1.00, failure and edge cases 1.00.
Success by category, v2: 1.00 in every category.

Router (Step O, 20 mixed inputs): accuracy 0.95 (19 of 20), target 0.95. The one miss asked for sales numbers and late orders in one sentence. The router sent it to `unclear`, which is the human triage queue, so no wrong agent ran. I did not change the label to make it 20 of 20.

## What I found

1. **Structured output through function calling failed on Groq.** In the first smoke run, 2 of 10 tickets failed in `triage`: the model named the tool after the schema and Groq rejected the call as a tool that was not in the request. The graph did the safe thing and escalated to a human, so no money moved, but refund tickets were left unfinished (smoke success 0.80). Switching `triage` to `method="json_schema"` removed the error (smoke success 1.00).
2. **The three failures of the v1 run were all "too careful", and the v2 prompt fixed them.** In `auto-05`, `auto-09` and `approval-02` the model chose to reply instead of proposing a refund, and the engine never saw a proposal. No wrong money moved, but the customer did not get a refund the engine would have allowed. Reading `decide.yaml` v1 suggested two causes: it told the model to reply when the customer gives no reason and the order is not late, while the engine also refunds a delivered order within 14 days, and the model applied the policy limit itself. Prompt v2 allows a refund proposal for a delivered order the customer wants to return, and tells the model to leave windows and limits to the engine. With v2 all 60 cases passed (refund within auto tier 0.80 to 1.00, refund needing approval 0.88 to 1.00). The cases that must be denied now end with the engine saying `deny` (`denied-01` to `denied-04`), where before the model refused on its own. I have not looked at the traces, so the explanation is still a reading of the prompt that the result supports.
3. **A guardrail caught the model 5 times** during the v1 run. The reply check logged `MONEY_CLAIM` five times: the model wrote text promising money, and the code replaced it with the fixed template.
4. **Outcome checks found what text quality would not.** The three failed replies of the v1 run were polite and cited policy, and still wrong. A judge that only reads the email would have passed them.
5. **The same Groq error appeared in the inventory agent** (the quantity proposal) at least once in the v2 run. The code fell back to the calculated quantity and still wrote the draft, as designed. The `listing`, `supervisor`, `decide` and `reply` model calls still use function calling and can fail the same way. They have not been changed.

## Limitations (honest)

- **The v2 prompt was tuned on the same 60 cases it was then measured on.** The result shows that the three known failures are fixed, not that v2 is right on tickets it has not seen. A fresh set of 10 to 15 new cases is still to do.
- One run per case (`repeats 1`). The model is not deterministic, so a single pass proves little. Mean and worst case over several runs are still to do.
- The invalid first attempt shows that a run can also fail for reasons outside the code, so the CI gate can give a false alarm.
- p95 latency of 24.9 s (v1) and 27.7 s (v2) misses the 15 s target. Each ticket makes about three model calls to a large reasoning model. Not yet investigated.
- The LLM judge for reply quality was not run and has not been calibrated against hand-scored replies.
- Policy search used fixed text, not the Postgres knowledge base (`--real-kb`).
- The dataset has 60 cases, and 8 of them are attacks, so the 1.00 injection resistance covers a small sample.
- The baseline in `data/eval/baseline.json` is the v2 run (1.00, label `graph-v3-decide-v2`). The gate fails below 0.97, which is two failed cases out of 60.

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

### graph-v3-decide-v2 (first attempt, INVALID: most model calls did not happen, cause not checked)

2026-10-07 01:49 UTC | model `groq/openai/gpt-oss-120b` | 60 runs | subset full | repeats 1 | policy search fixed

| Metric | Value | Target |
|---|---|---|
| task_success | 0.483 | >= 0.9 |
| correct_escalation | 0.205 | >= 0.95 |
| trajectory | 0.500 | >= 0.85 |
| tool_calls_avg | 1.733 | <= 6.0 |
| reply_quality | n/a | >= 4.0 |
| injection_resistance | 1.000 | >= 1.0 |
| latency_p95_s | 21.923 | <= 15.0 |
| wrong_refund_cases | 0 | 0 |
| approval_bypass_cases | 0 | 0 |
| tokens in / out per case | 488 / 109 | tracked |
| cost per ticket | 0.0000 | tracked |

Success by category: failure_edge 0.50, refund_auto 0.30, refund_denied 0.12, adversarial 1.00, order_status 0.20, exchange_product 1.00, inventory_listing 1.00, refund_approval 0.12

### graph-v3-decide-v2 (second attempt, valid)

2026-10-07 02:08 UTC | model `groq/openai/gpt-oss-120b` | 60 runs | subset full | repeats 1 | policy search fixed

| Metric | Value | Target |
|---|---|---|
| task_success | 1.000 | >= 0.9 |
| correct_escalation | 1.000 | >= 0.95 |
| trajectory | 1.000 | >= 0.85 |
| tool_calls_avg | 4.100 | <= 6.0 |
| reply_quality | n/a | >= 4.0 |
| injection_resistance | 1.000 | >= 1.0 |
| latency_p95_s | 27.733 | <= 15.0 |
| wrong_refund_cases | 0 | 0 |
| approval_bypass_cases | 0 | 0 |
| tokens in / out per case | 2079 / 454 | tracked |
| cost per ticket | 0.0000 | tracked |

Success by category: failure_edge 1.00, refund_auto 1.00, refund_denied 1.00, adversarial 1.00, order_status 1.00, exchange_product 1.00, inventory_listing 1.00, refund_approval 1.00
