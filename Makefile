# Agora — common dev commands

.PHONY: help install fmt lint type test test-fast cov audit up down logs db-reset migrate api demo eval-routing eval-routing-llm sync-doc-counts reshare-up reshare-down reshare-logs reshare-probe reshare-smoke clean

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
	@echo "  eval-routing-llm  run LLM-augmented eval (--no-write); requires Vertex/ADC env (see CLAUDE.md)"
	@echo "  sync-doc-counts  rewrite test count + ADR count in docs to match runtime truth"
	@echo "  reshare-up   bring up mod-rs sandbox (Postgres + reshare-init-db + mod-rs)"
	@echo "  reshare-down tear down mod-rs sandbox (keeps agora postgres running)"
	@echo "  reshare-logs tail mod-rs container logs"
	@echo "  reshare-probe run scripts/reshare_probe.py against localhost:8081"
	@echo "  reshare-smoke run HttpReShareClient smoke test against localhost:8081"
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
	# Normalize Windows-shaped backslash paths in the baseline before the
	# hook compares against forward-slash `git ls-files` output. Idempotent
	# on Linux/CI where the baseline is already forward-slash.
	python scripts/normalize_secrets_baseline.py
	# Scan for new secrets vs the committed baseline. Update baseline with:
	#   detect-secrets scan --baseline .secrets.baseline
	# NUL-delimited so filenames with spaces survive xargs splitting.
	# WARNING: do NOT run `detect-secrets scan --baseline ...` on Windows.
	# v1.5.0 + Python 3.14 silently drops entries (e.g. docs/runbook.md
	# Basic-Auth Credentials) that Linux CI keeps — the rescan returns
	# zero findings due to a platform filter difference and the baseline
	# round-trip loses real entries. For doc edits that shift secret line
	# numbers, prefer adding `<!-- pragma: allowlist secret -->` on the
	# offending line over rebaselining. See docs/lessons.md 2026-05-04
	# entry. If you must regenerate the baseline, do it on Linux/WSL.
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
# gemini-2.5-flash. Re-verified 2026-05-04 against committed baseline.
eval-routing:
	python -m agora.evals.routing

# Score the LLM-augmented RoutingAgent. Requires Vertex/ADC plumbing —
# without GOOGLE_GENAI_USE_VERTEXAI=true the google-genai SDK silently
# falls back to public-Gemini API-key auth and 401s every call (the seam
# catches it and runs rules-only — looks successful with the wrong
# numbers). See CLAUDE.md known-gaps routing block for the full
# checklist (ADC + quota project + API enablement + Studio
# click-through). This target asserts the four env vars exist and
# passes --no-write so a misconfigured run can't overwrite the
# committed baseline. Drop --no-write only after a clean run.
eval-routing-llm:
	@if [ -z "$$GOOGLE_GENAI_USE_VERTEXAI" ] || [ -z "$$GOOGLE_CLOUD_PROJECT" ] || [ -z "$$GOOGLE_CLOUD_LOCATION" ] || [ -z "$$AGORA_ROUTING_LLM_ENABLED" ]; then \
		echo "ERROR: missing env. Required:"; \
		echo "  GOOGLE_GENAI_USE_VERTEXAI=true"; \
		echo "  GOOGLE_CLOUD_PROJECT=<project-id>"; \
		echo "  GOOGLE_CLOUD_LOCATION=us-central1"; \
		echo "  AGORA_ROUTING_LLM_ENABLED=1"; \
		echo "Recommended also:"; \
		echo "  AGORA_ROUTING_LLM_MODEL=gemini-2.5-flash"; \
		echo "  AGORA_ROUTING_LLM_TIMEOUT_SECS=30"; \
		exit 1; \
	fi
	python -m agora.evals.routing --llm --no-write

# Rewrite test count + ADR count in docs (README, CLAUDE.md, PRD-00,
# solution.md) to match runtime truth (pytest --collect-only +
# `ls docs/adr/`). The pytest gate `tests/test_doc_counts.py` asserts a
# clean run, so any drift surfaces in CI as a red triple-gate. See
# `scripts/sync_doc_counts.py` for the registry of doc locations.
sync-doc-counts:
	python scripts/sync_doc_counts.py --fix

# ── ReShare local sandbox ───────────────────────────────────────────────────
# Requires Docker with Compose v2 (docker compose, not docker-compose).
# Pulls ghcr.io/openlibraryenvironment/mod-rs:2.19.0-rc17 on first run.
# mod-rs Grails cold-start takes 90 s+; watch progress with `make logs`.

reshare-up:
	docker compose --profile reshare up -d
	@echo ""
	@echo "Sandbox starting.  Follow logs: make reshare-logs"
	@echo "mod-rs healthcheck turns green in ~2 min (Grails cold start)."
	@echo "Then run: make reshare-probe"

reshare-down:
	docker compose --profile reshare down --remove-orphans
	@echo "mod-rs containers removed. agora-postgres still running."

reshare-logs:
	docker compose --profile reshare logs -f mod-rs

reshare-probe:
	python scripts/reshare_probe.py

# Run the existing HttpReShareClient smoke test (tests/test_reshare_http_smoke.py)
# against the local mod-rs sandbox.  Complements reshare-probe: the probe finds
# the body shapes; reshare-smoke verifies the client factory wiring end-to-end.
reshare-smoke:
	AGORA_TEST_RESHARE_URL=http://localhost:8081 \
	  RESHARE_TENANT=consortium-a \
	  RESHARE_USER=admin \
	  RESHARE_PASSWORD=admin \
	  python -m pytest tests/test_reshare_http_smoke.py -v

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage
	find . -type d -name __pycache__ -exec rm -rf {} +
