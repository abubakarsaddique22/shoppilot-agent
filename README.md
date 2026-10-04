# ShopPilot

E-commerce operations agent (LangGraph + LangSmith + FastAPI + AWS). Full plan: ShopPilot_Agentic_AI_Blueprint_A_to_Z.pdf

## Quick start
```bash
cp .env.example .env
make setup
make demo-logs      # generate sample logs
make logs           # view logs
make logs-errors    # errors + tracebacks only
make run            # start the API (http://127.0.0.1:8000/docs)
```
See docs/logging.md for how logging works.
