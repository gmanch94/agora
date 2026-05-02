# Agora — common dev commands

.PHONY: help install fmt lint type test test-fast cov up down logs db-reset migrate api demo chaos clean

help:
	@echo "Common targets:"
	@echo "  install     pip install -e .[dev,adk]"
	@echo "  fmt         ruff format"
	@echo "  lint        ruff check"
	@echo "  type        mypy"
	@echo "  test        pytest (all)"
	@echo "  test-fast   pytest -m 'not slow and not integration'"
	@echo "  cov         pytest with coverage"
	@echo "  up          docker compose up -d"
	@echo "  down        docker compose down"
	@echo "  logs        docker compose logs -f"
	@echo "  db-reset    drop + recreate agora db, run alembic upgrade head"
	@echo "  migrate     alembic upgrade head"
	@echo "  api         run FastAPI app locally"
	@echo "  demo        run scripted happy-path demo"
	@echo "  chaos       run saga compensation chaos test"
	@echo "  clean       remove caches"

install:
	pip install -e ".[dev,adk]"

fmt:
	ruff format src tests

lint:
	ruff check src tests

type:
	mypy src

test:
	pytest

test-fast:
	pytest -m "not slow and not integration"

cov:
	pytest --cov=src/agora --cov-report=term-missing --cov-report=html

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f

db-reset:
	docker compose exec -T postgres psql -U agora -d postgres -c "DROP DATABASE IF EXISTS agora;"
	docker compose exec -T postgres psql -U agora -d postgres -c "CREATE DATABASE agora;"
	alembic upgrade head

migrate:
	alembic upgrade head

api:
	uvicorn agora.api.app:app --reload --host 0.0.0.0 --port 8000

demo:
	python -m agora.demos.happy_path

chaos:
	python -m agora.demos.chaos

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
