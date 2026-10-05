"""Logging + exceptions ka demo: python scripts/demo_logging.py  ->  phir  python scripts/view_logs.py"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))  # bina install ke bhi chale
from shoppilot.core.errors import OrderNotFound, PolicyDenied
from shoppilot.core.logging import bind_context, get_logger, setup_logging

setup_logging("DEBUG", "logs")
log = get_logger("demo")

with bind_context(request_id="req-demo01", ticket_id="T-1042", thread_id="T-1042"):
    log.debug("debug detail: state loaded")
    log.info("ticket received from ali.raza@gmail.com", extra={"channel": "email"})
    log.info("config check api_key=sk-SECRET123 token: abc999")        # masked ho jayega
    log.warning("model and policy engine disagree", extra={"model_tier": "auto", "engine_tier": "manager"})
    try:
        raise OrderNotFound("order 88731 not found", details={"order_ref": "88731"})
    except OrderNotFound as exc:
        log.error("tool get_order failed: %s", exc.code, exc_info=exc, extra={"result": exc.to_result()})
    try:
        raise PolicyDenied("outside 14 day window", details={"days": 20})
    except PolicyDenied:
        log.warning("refund denied by policy")
    try:
        {}["missing_key"]
    except KeyError:
        log.exception("unexpected bug while building reply")           # traceback error.log mein
print("Done. Ab chalao:  python scripts/view_logs.py   |   python scripts/view_logs.py --errors")
