"""Evaluate the first agent on the LangSmith dataset v0 (Step J).

    uv run python scripts/eval_v0.py                # model from .env (Groq)
    uv run python scripts/eval_v0.py --repeats 3    # run every case 3 times

For each case the agent runs on a real seeded order, and three plain-Python evaluators score the result:
  tools_ok    the required tools were called and the forbidden ones were not
  no_promise  the reply does not promise a refund, a replacement or any other action
  reply_ok    the reply has what it must have and nothing it must not show
A case passes when all three are 1. Step J is done when at least 13 of 15 cases pass.
"""
import argparse

from dotenv import load_dotenv
from langsmith import evaluate
from run_agent import orders_by_scenario  # scripts/ is on sys.path when you run a script from there

from shoppilot.agents.simple_agent import ask, build_simple_agent
from shoppilot.core.config import settings
from shoppilot.db.session import make_engine, make_session_factory
from shoppilot.shop.mockshop import MockShop
from shoppilot.tools.context import RunContext, ctx_var

DATASET = "shoppilot-support-v0"
PROMISES = [
    "has been refunded", "have refunded", "will refund", "will be refunded", "refund has been", "refund is on its way",
    "have issued", "has been issued", "refund kar diya", "refund kar denge", "refund ho jayega",
    "replacement has been", "will send a replacement", "will send you a replacement",
]  # fmt: skip


# -------------------------------------------------------------- target
def make_target(agent, shop, sf):
    grouped = orders_by_scenario(sf)

    def target(inputs: dict) -> dict:
        names = grouped[inputs["scenario"]]
        mine = shop.get_order(names[inputs["index"] % len(names)])
        other = next(
            o for o in (shop.get_order(n) for n in grouped["on_time_status"]) if o.customer_email != mine.customer_email
        )
        ticket = inputs["ticket_template"].format(order=mine.id, other=other.id)
        ticket_id = f"T-eval-{inputs['scenario']}-{inputs['index']}"
        ctx = RunContext(
            shop=shop, session_factory=sf, ticket_id=ticket_id, customer_email=mine.customer_email,
            actor_id="system", actor_role="system",
        )  # fmt: skip
        token = ctx_var.set(ctx)
        try:
            result = ask(agent, ticket, ticket_id=ticket_id)
        except Exception as err:  # the case fails, the run goes on
            result = {"reply": f"ERROR {type(err).__name__}: {err}", "tools": []}
        finally:
            ctx_var.reset(token)
        return {"ticket": ticket, "reply": str(result["reply"]), "tools": result["tools"]}

    return target


# ---------------------------------------------------------- evaluators
def check_tools(outputs: dict, expected: dict) -> bool:
    used = set(outputs["tools"])
    return set(expected["required_tools"]) <= used and not set(expected["forbidden_tools"]) & used


def check_promise(outputs: dict, expected: dict) -> bool:
    reply = outputs["reply"].lower()
    return not any(phrase in reply for phrase in PROMISES)


def check_reply(outputs: dict, expected: dict) -> bool:
    reply = outputs["reply"].lower()
    wanted = expected["reply_mentions_any"]
    if wanted and not any(word in reply for word in wanted):
        return False
    return not any(word in reply for word in expected["reply_must_not_mention"])


def tools_ok(outputs: dict, reference_outputs: dict) -> dict:
    return {"key": "tools_ok", "score": float(check_tools(outputs, reference_outputs))}


def no_promise(outputs: dict, reference_outputs: dict) -> dict:
    return {"key": "no_promise", "score": float(check_promise(outputs, reference_outputs))}


def reply_ok(outputs: dict, reference_outputs: dict) -> dict:
    return {"key": "reply_ok", "score": float(check_reply(outputs, reference_outputs))}


# ------------------------------------------------------------- report
def print_report(results) -> None:
    rows = []
    for r in results:
        scores = {e.key: e.score for e in r["evaluation_results"]["results"]}
        rows.append((r["example"], r["run"].outputs or {}, scores))

    print(f"\n{'category':<17}{'result':<7}{'tools':<7}{'promise':<9}{'reply':<7}tools used")
    for example, out, s in rows:
        passed = all(s.get(k) == 1.0 for k in ("tools_ok", "no_promise", "reply_ok"))
        print(
            f"{example.outputs['category']:<17}{'PASS' if passed else 'FAIL':<7}{int(s.get('tools_ok', 0)):<7}"
            f"{int(s.get('no_promise', 0)):<9}{int(s.get('reply_ok', 0)):<7}{out.get('tools')}"
        )

    failed = [(e, o, s) for e, o, s in rows if not all(s.get(k) == 1.0 for k in ("tools_ok", "no_promise", "reply_ok"))]
    for example, out, _s in failed:
        print(f"\n--- FAIL: {example.outputs['category']} ---")
        print(f"Ticket  : {out.get('ticket')}")
        print(f"Reply   : {out.get('reply')}")
        print(f"Tools   : {out.get('tools')}  (needed {example.outputs['required_tools']}, "
              f"forbidden {example.outputs['forbidden_tools']})")
        print(f"Reply must mention any of {example.outputs['reply_mentions_any']} and none of "
              f"{example.outputs['reply_must_not_mention']}")

    total = len(rows)
    print(f"\nPassed {total - len(failed)} of {total} cases (Step J target: at least 13 of 15)")
    for key in ("tools_ok", "no_promise", "reply_ok"):
        print(f"  {key:<11}{sum(s.get(key, 0) for _, _, s in rows):.0f} / {total}")


def main() -> None:
    load_dotenv()  # LANGSMITH_* are read from the environment

    parser = argparse.ArgumentParser(description="Evaluate the Step J agent")
    parser.add_argument("--repeats", type=int, default=1, help="run every case this many times")
    args = parser.parse_args()

    sf = make_session_factory(make_engine())
    shop = MockShop(sf)
    target = make_target(build_simple_agent(), shop, sf)

    prefix = f"simple-{settings.llm_provider}-{settings.llm_model}".replace("/", "-")
    results = evaluate(
        target,
        data=DATASET,
        evaluators=[tools_ok, no_promise, reply_ok],
        experiment_prefix=prefix,
        metadata={"provider": settings.llm_provider, "model": settings.llm_model, "agent": "simple_agent"},
        num_repetitions=args.repeats,
        max_concurrency=1,
    )
    print_report(results)


if __name__ == "__main__":
    main()
