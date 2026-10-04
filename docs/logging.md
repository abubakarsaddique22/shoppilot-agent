# How logging works

| File | What it contains | Format |
|---|---|---|
| `logs/app.log` | Every log (INFO+): request, tool call, warning, error | JSON, one line = one event |
| `logs/error.log` | Only ERROR / CRITICAL / uncaught exceptions + full traceback | Human-readable block |
| console | In the terminal during development | short colored lines |

Every line carries `request_id`, `ticket_id` and `thread_id`. Emails (`a***@x.com`) and secrets (`api_key=***`) are masked.

## Ways to view logs
```bash
python scripts/view_logs.py                  # last 30 events
python scripts/view_logs.py --errors         # errors + tracebacks only
python scripts/view_logs.py --request-id ab12cd34
python scripts/view_logs.py --ticket T-1042
python scripts/view_logs.py --raw-errors     # print error.log directly
python scripts/view_logs.py -f               # live follow
```

## In code
```python
from shoppilot.core.logging import get_logger, bind_context
log = get_logger(__name__)
with bind_context(ticket_id="T-1042"):
    log.info("refund proposed", extra={"amount_pkr": 5400})
try: ...
except Exception: log.exception("refund failed")   # traceback goes to error.log
```
Custom exceptions: `src/shoppilot/core/errors.py` (OrderNotFound, PolicyDenied, ToolTimeout ...).
The API always returns errors as `{"error": {"code","message","request_id"}}`; the traceback stays in the logs only.
