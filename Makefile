# ---------------------------------------------------------------------------
# Trading platform -- operator and developer entry points.
#
# Every target below wraps a command documented in docs/testing-policy.md, so a
# human, a work-package agent and CI all run exactly the same thing.
# ---------------------------------------------------------------------------
VENV      := .venv
PY        := $(VENV)/bin/python
PIP       := $(VENV)/bin/pip
PYTEST    := $(PY) -m pytest
DASHBOARD := dashboard
API_URL   ?= http://127.0.0.1:8080
AREA      ?= tests

.DEFAULT_GOAL := help

.PHONY: help venv install lint format typecheck test test-scoped check-python check \
        dashboard-install dashboard-dev dashboard-lint dashboard-typecheck \
        dashboard-test dashboard-check realtime status provision strategies images clean

help: ## Show the documented entry points
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtualenv from requirements-dev.txt (Python 3.14)
	test -x $(PY) || python3 -m venv $(VENV)
	$(PIP) install -r requirements-dev.txt

install: venv ## Install the development environment (no dependency resolution for the package)
	$(PIP) install -e . --no-deps

lint: ## ruff check + ruff format --check (full gate)
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

format: ## Apply ruff formatting in place
	$(VENV)/bin/ruff format .
	$(VENV)/bin/ruff check --fix .

typecheck: ## mypy src (full gate)
	$(VENV)/bin/mypy src

test: ## Full Python gate: pytest with the 80 % coverage threshold
	$(PYTEST)

test-scoped: ## Scoped run for one work package: make test-scoped AREA=tests/engine
	$(PYTEST) $(AREA) -q --no-cov

check-python: lint typecheck test ## The full documented Python gate

check: check-python dashboard-check ## Both full gates (what CI runs)

dashboard-install: ## npm ci in dashboard/
	cd $(DASHBOARD) && npm ci

dashboard-dev: ## Run the dashboard dev server on 127.0.0.1:3000
	cd $(DASHBOARD) && npm run dev

dashboard-lint: ## eslint in dashboard/
	cd $(DASHBOARD) && npm run lint

dashboard-typecheck: ## tsc --noEmit in dashboard/
	cd $(DASHBOARD) && npm run typecheck

dashboard-test: ## vitest with the coverage thresholds in dashboard/
	cd $(DASHBOARD) && npm run test:coverage

dashboard-check: ## The full documented dashboard gate
	cd $(DASHBOARD) && npm run lint && npm run typecheck && npm run test:coverage && npm run build

realtime: ## Run the supervisor + aggregated API locally (127.0.0.1:8080)
	$(PY) -m trading_platform realtime run --host 127.0.0.1 --port 8080

status: ## Print the health and the ranked profiles of a running engine
	$(PY) -m trading_platform realtime status --api-url $(API_URL)

provision: ## Apply config/profiles.json to a running engine (dry run)
	$(PY) -m trading_platform realtime provision --api-url $(API_URL) --dry-run

strategies: ## Print the discovered strategy catalogue
	$(PY) -m trading_platform strategies list

images: ## Build both container images (same commands as CI)
	docker build -f deploy/Dockerfile.realtime -t trading-realtime:local .
	docker build -f deploy/Dockerfile.dashboard -t trading-dashboard:local .

clean: ## Remove local caches (never the venv, never state)
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage htmlcov
	rm -rf $(DASHBOARD)/.next $(DASHBOARD)/coverage
