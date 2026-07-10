.PHONY: install install-drive install-frontend test lint typecheck check run-weekly run-monthly api frontend clean

install:
	pip install -e ".[dev]"

install-drive:
	pip install -e ".[dev,drive]"

install-frontend:
	pip install -e ".[dev,frontend]"

test:
	pytest

lint:
	ruff check src tests

format:
	ruff format src tests

typecheck:
	mypy src

check: lint typecheck test

run-weekly:
	project-health-weekly

run-monthly:
	project-health-monthly

api:
	uvicorn project_health_agent.api.main:app --reload --port 8000

frontend:
	streamlit run frontend/app.py

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info
