# ---------------------------------------------------------------------------
# trading-platform -- developer tasks
#
# `make` (or `make help`) lists every target. The interpreter is the local
# virtualenv when it exists and python3.11 otherwise, so the same Makefile
# works on a fresh checkout, on a developer machine and inside the Docker image.
#
# The full test command (`make test-cov`) is defined once in
# docs/testing-policy.md section 3 and repeated here byte for byte: the coverage
# gate of 85 % is mechanical (pyproject.toml -> [tool.coverage.report].fail_under).
# ---------------------------------------------------------------------------

SHELL := /bin/bash
PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3.11; fi)

CONFIG ?= config/backtest_default.json
DOCKER_IMAGE ?= trading-platform:latest
DOCKER_TEST_IMAGE ?= trading-platform:test

.DEFAULT_GOAL := help
.PHONY: help venv install install-dev lint format type-check test test-cov cov check \
	backtest walk-forward robustness monte-carlo data-download realtime docker-build docker-test clean

# ---------------------------------------------------------------------------
# meta
# ---------------------------------------------------------------------------

help: ## Show this help (every target below is runnable)
	@echo "trading-platform -- available targets"
	@echo ""
	@echo "  make help           show this help"
	@echo "  make venv           create the .venv virtualenv with python3.11"
	@echo "  make install        install the runtime package: pip install -e ."
	@echo "  make install-dev    install the development extra: pip install -e \".[dev]\""
	@echo "  make lint           ruff check . and ruff format --check ."
	@echo "  make format         ruff format . and ruff check --fix ."
	@echo "  make type-check     mypy src"
	@echo "  make test           pytest tests -q (no coverage gate)"
	@echo "  make test-cov       the full command of docs/testing-policy.md (85% gate)"
	@echo "  make cov            the same run plus a browsable htmlcov/ report"
	@echo "  make check          lint + type-check + test-cov (pre-push gate)"
	@echo "  make backtest       single backtest on $(CONFIG)"
	@echo "  make walk-forward   walk-forward analysis on $(CONFIG)"
	@echo "  make robustness     parameter sweep on $(CONFIG)"
	@echo "  make monte-carlo    Monte Carlo simulation on $(CONFIG)"
	@echo "  make data-download  fill the OHLCV cache (needs the exchange extra)"
	@echo "  make realtime       run the realtime engine + monitoring dashboard (PROFILES=...)"
	@echo "  make docker-build   build the image $(DOCKER_IMAGE)"
	@echo "  make docker-test    build the test stage and run the suite with the coverage gate"
	@echo "  make clean          remove caches, coverage artifacts and build leftovers"

# ---------------------------------------------------------------------------
# environment
# ---------------------------------------------------------------------------

venv: ## Create the .venv virtualenv (python3.11)
	python3.11 -m venv .venv

install: ## Install the runtime package (pip install -e .)
	$(PYTHON) -m pip install -e .

install-dev: ## Install the package with the dev extra (pip install -e ".[dev]")
	$(PYTHON) -m pip install -e ".[dev]"

# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------

lint: ## Check style and formatting (ruff check . + ruff format --check .)
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .

format: ## Rewrite the sources with the ruff formatter, then fix the lint findings
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

type-check: ## Static type checking (mypy src)
	$(PYTHON) -m mypy src

test: ## Run the test-suite without the coverage gate
	$(PYTHON) -m pytest tests -q

test-cov: ## Run the test-suite with the coverage gate (docs/testing-policy.md section 3)
	$(PYTHON) -m pytest tests --cov=trading_platform --cov-report=term-missing --cov-report=xml

cov: ## Run the test-suite and write a browsable htmlcov/ report
	$(PYTHON) -m pytest tests --cov=trading_platform --cov-report=term-missing --cov-report=html

check: lint type-check test-cov ## Full pre-push gate: lint + type-check + test-cov

# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

backtest: ## Run a single backtest (CONFIG=... to override)
	$(PYTHON) -m trading_platform backtest --config $(CONFIG)

walk-forward: ## Run the walk-forward analysis
	$(PYTHON) -m trading_platform walk-forward --config $(CONFIG)

robustness: ## Run the parametric robustness sweep
	$(PYTHON) -m trading_platform robustness --config $(CONFIG)

monte-carlo: ## Run the Monte Carlo simulation
	$(PYTHON) -m trading_platform monte-carlo --config $(CONFIG)

data-download: ## Fill the on-disk OHLCV cache (only network-using target)
	$(PYTHON) -m trading_platform data download --config $(CONFIG) \
		--symbol "$${SYMBOL:-BTC/USDT}" --timeframe "$${TIMEFRAME:-1h}" \
		--start "$${START:-2023-01-01T00:00:00Z}" --end "$${END:-2024-01-01T00:00:00Z}"

realtime: ## Run the realtime engine and the monitoring dashboard (PROFILES=... to override)
	$(PYTHON) -m trading_platform realtime run --profiles $${PROFILES:-config/profiles.example.json}

# ---------------------------------------------------------------------------
# docker (reproducible environment)
# ---------------------------------------------------------------------------

docker-build: ## Build the docker image
	docker build -t $(DOCKER_IMAGE) .

docker-test: ## Build the docker test stage, then run the full suite with the coverage gate
	docker build --target test -t $(DOCKER_TEST_IMAGE) .
	docker run --rm --entrypoint python $(DOCKER_TEST_IMAGE) -m pytest tests \
		--cov=trading_platform --cov-report=term-missing --cov-report=xml --cov-fail-under=85

# ---------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------

clean: ## Remove caches, coverage artifacts and build leftovers
	@find . -path ./.venv -prune -o -name "__pycache__" -type d -print -exec rm -rf {} + 2>/dev/null || true
	@find . -path ./.venv -prune -o -name "*.egg-info" -type d -print -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov build dist
