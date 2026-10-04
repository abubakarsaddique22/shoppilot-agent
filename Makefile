.PHONY: setup up down seed ingest run test lint eval logs logs-errors demo-logs
PY=.venv/bin/python

setup:
	python3 -m venv .venv
	$(PY) -m pip install -U pip
	$(PY) -m pip install -e ".[dev]"
	.venv/bin/pre-commit install

up:
	docker compose up -d db
down:
	docker compose down
seed:
	$(PY) scripts/seed_mockshop.py
ingest:
	$(PY) scripts/ingest_policies.py
run:
	.venv/bin/uvicorn shoppilot.api.main:app --reload
test:
	.venv/bin/pytest --cov=shoppilot
lint:
	.venv/bin/ruff check . && .venv/bin/mypy src
eval:
	$(PY) scripts/run_eval.py

# --- logs ---
logs:            ## app.log (INFO+) dekho
	$(PY) scripts/view_logs.py
logs-errors:     ## sirf errors + traceback
	$(PY) scripts/view_logs.py --errors
demo-logs:       ## sample logs/errors generate karo
	$(PY) scripts/demo_logging.py
