# Agora — common dev commands

.PHONY: help install fmt lint type test test-fast cov audit up down logs db-reset migrate api demo eval-routing clean

help:
	@echo "Common targets:"
	@echo "  install     pip install -e .[dev,adk]"
	@echo "  fmt         ruff format"
	@echo "  lint        ruff check"
	@echo "  type        mypy"
	@echo "  test        pytest (all)"
	@echo "  test-fast   pytest -m 'not slow and not integration'"
	@echo "  cov         pytest with coverage"
	@echo "  audit       security scan (bandit + pip-audit + detect-secrets)"
	@echo "  up          docker compose up -d"
	@echo "  down        docker compose down"
	@echo "  logs        docker compose logs -f"
	@echo "  db-reset    drop + recreate agora db, run alembic upgrade head"
	@echo "  migrate     alembic upgrade head"
	@echo "  api         run FastAPI app locally"
	@echo "  demo        run scripted happy-path demo"
	@echo "  eval-routing run RoutingAgent eval harness (rules-only); rewrite evals/routing/baseline-rules.json"
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

audit:
	# Static analysis of source tree. Tests dir excluded via [tool.bandit]
	# in pyproject.toml.
	bandit -r src/agora/ -q
	# Audit installed env for known CVEs. Network-bound (PyPI advisory DB).
	pip-audit
	# Scan for new secrets vs the committed baseline. Update baseline with:
	#   detect-secrets scan --baseline .secrets.baseline
	# NUL-delimited so filenames with spaces survive xargs splitting.
	git ls-files -z | xargs -0 detect-secrets-hook --baseline .secrets.baseline

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

# Score the rules-baseline RoutingAgent against the committed eval set
# (evals/routing/scenarios.json) and rewrite evals/routing/baseline-rules.json
# (the rules floor — split from the LLM-augmented baseline.json in #50).
# See ADR-0014 for the gating policy. CI runs the floor check via
# .github/workflows/routing-eval-floor.yml; PR-2 (LLM tie-breaker)
# shipped in #48-#51 with top-1 0.9500 / mean Spearman 0.8889 against
# gemini-2.5-flash. For LLM-augmented runs invoke the module directly:
#   python -m agora.evals.routing --llm
eval-routing:
	python -m agora.evals.routing

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
