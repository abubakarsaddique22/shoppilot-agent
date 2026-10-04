# ShopPilot

E-commerce operations agent (LangGraph + LangSmith + FastAPI + AWS). Full plan: ShopPilot_Agentic_AI_Blueprint_A_to_Z.pdf. Requirements: docs/PRD.md.

## Requirements

- Python 3.12 (managed by uv, see `.python-version`)
- [uv](https://docs.astral.sh/uv/) (Windows: `winget install astral-sh.uv`)
- Docker (for Postgres with pgvector)
- `make` (Windows: `scoop install make` or `choco install make`). Without make, run the `uv run ...` commands from the Makefile directly.

## Quick start (Windows cmd)

```bat
copy .env.example .env
```

Edit `.env` and fill in `SHOP_LLM_API_KEY` and `LANGSMITH_API_KEY` with your own keys. Then:

```bat
make setup
make test
make lint
make up
make run
```

`make setup` runs `uv sync` and installs pre-commit hooks. `make up` starts Postgres (pgvector) in Docker. `make run` serves the API at http://127.0.0.1:8000/docs

On macOS or Linux use `cp .env.example .env` instead of `copy`.

Logs: `make demo-logs` (sample logs), `make logs`, `make logs-errors`. See docs/logging.md.

## Make targets

`setup`, `up`, `down`, `seed`, `ingest`, `run`, `test`, `lint`, `eval`, `logs`, `logs-errors`, `demo-logs`.
`seed`, `ingest` and `eval` need steps E, G and U of the blueprint to be built first.

## Status

Foundation (steps A to D) in progress. See the blueprint for the A to Z plan.
