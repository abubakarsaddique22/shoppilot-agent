.PHONY: setup up down seed ingest run test lint eval eval-recreate logs logs-errors demo-logs

# Uses uv, so the same commands work on Windows, macOS and Linux.
# Install uv once: https://docs.astral.sh/uv/   (Windows: winget install astral-sh.uv)
RUN=uv run

setup:
	uv sync
	$(RUN) pre-commit install

up:
	docker compose up -d db
down:
	docker compose down
seed:
	$(RUN) python scripts/seed_mockshop.py
ingest:
	$(RUN) python scripts/ingest_policies.py
run:
	$(RUN) uvicorn shoppilot.api.main:app --reload
test:
	$(RUN) pytest --cov=shoppilot
lint:
	$(RUN) ruff check .
	$(RUN) mypy src
eval:
	$(RUN) python scripts/run_eval.py
eval-recreate:   ## cases dobara banao, LangSmith dataset naya upload karo AUR poora eval bhi chalao (~14 min)
	$(RUN) python scripts/build_cases_v1.py
	$(RUN) python scripts/run_eval.py --recreate-dataset

# --- logs ---
logs:            ## app.log (INFO+) dekho
	$(RUN) python scripts/view_logs.py
logs-errors:     ## sirf errors + traceback
	$(RUN) python scripts/view_logs.py --errors
demo-logs:       ## sample logs/errors generate karo
	$(RUN) python scripts/demo_logging.py
