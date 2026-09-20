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
NPM ?= npm

# The sandbox (and any locked-down machine) may not allow writing to the global
# npm cache: every npm command below therefore uses a repository-local cache,
# exactly like UV_CACHE_DIR=.uv-cache for uv. .npm-cache/ is git-ignored.
NPM_CACHE ?= $(CURDIR)/.npm-cache

CONFIG ?= config/backtest_default.json
DATA_FILE ?= data/BTC_USDT-1h.csv
FORECAST_ARTIFACT ?= data/forecast/forecast.parquet
FORECAST_BACKEND ?= naive
SYMBOL ?= BTC/USDT
TIMEFRAME ?= 1h
FORECAST_DIR ?= data/forecast
# The SQLite state database is the single source of truth for both the profile
# set and the engine settings: there is no profiles JSON document any more.
STATE_DB ?= data/realtime/state.db
DOCKER_IMAGE ?= trading-platform:latest
DOCKER_TEST_IMAGE ?= trading-platform:test

.DEFAULT_GOAL := help
.PHONY: help venv install install-dev lint format type-check test test-cov cov check \
	backtest walk-forward robustness monte-carlo forecast-build data-download realtime docker-build docker-test clean \
	forecast-bootstrap forecast-bootstrap-seasonal forecast-profile \
	forecast-info realtime-forecast forecast-flow \
	dashboard-install dashboard-dev dashboard-lint dashboard-typecheck dashboard-test \
	dashboard-test-coverage dashboard-build dashboard-check check-all

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
	@echo "  make forecast-build build the offline forecast artifact ($(FORECAST_BACKEND) backend)"
	@echo "  make forecast-bootstrap          build a naive baseline artifact ($(FORECAST_DIR)/bootstrap-naive.parquet)"
	@echo "  make forecast-bootstrap-seasonal build the seasonal baseline artifact ($(FORECAST_DIR)/bootstrap-seasonal.parquet)"
	@echo "  make forecast-profile            build the artifact a timeframed profile needs (STATE_DB=... BACKEND=...)"
	@echo "  make forecast-info               report whether FORECAST_ARTIFACT is still usable (STATE_DB=... to check it against a profile)"
	@echo "  make data-download  download $(SYMBOL) $(TIMEFRAME) candles into the cache (only network-using target)"
	@echo "  make realtime       run the realtime engine + monitoring JSON API (STATE_DB=...)"
	@echo "  make realtime-forecast run the realtime engine on the profiles of $(STATE_DB)"
	@echo "  make forecast-flow  the documented 3-command flow: data-download, forecast-bootstrap, forecast-info, realtime-forecast"
	@echo "  make docker-build   build the image $(DOCKER_IMAGE)"
	@echo "  make docker-test    build the test stage and run the suite with the coverage gate"
	@echo "  make dashboard-install         install the dashboard dependencies (npm ci)"
	@echo "  make dashboard-dev             run the Next.js dev server on http://127.0.0.1:3000"
	@echo "  make dashboard-lint            eslint the dashboard (dashboard/)"
	@echo "  make dashboard-typecheck       type-check the dashboard (tsc --noEmit)"
	@echo "  make dashboard-test            dashboard unit tests, no coverage gate (vitest run)"
	@echo "  make dashboard-test-coverage   dashboard tests + coverage gate (docs/testing-policy.md section 9)"
	@echo "  make dashboard-build           production build of the dashboard (next build)"
	@echo "  make dashboard-check           the four dashboard gates: lint + typecheck + test-coverage + build"
	@echo "  make check-all                 make check (Python) plus make dashboard-check"
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

forecast-build: ## Build the offline forecast artifact (DATA_FILE/FORECAST_BACKEND to override)
	$(PYTHON) -m trading_platform forecast-build --config $(CONFIG) --data-file $(DATA_FILE) --out $(FORECAST_ARTIFACT) --backend $(FORECAST_BACKEND)

forecast-bootstrap: ## Build the naive-baseline artifact used to bootstrap a forecast profile
	$(PYTHON) -m trading_platform forecast-build --config $(CONFIG) --data-file $(DATA_FILE) --out $(FORECAST_DIR)/bootstrap-naive.parquet --backend naive

forecast-bootstrap-seasonal: ## Build the seasonal-baseline artifact used to bootstrap a forecast profile
	$(PYTHON) -m trading_platform forecast-build --config $(CONFIG) --data-file $(DATA_FILE) --out $(FORECAST_DIR)/bootstrap-seasonal.parquet --backend seasonal

forecast-profile: ## Build the artifact a profile of $(STATE_DB) declares (BACKEND=... to override)
	$(PYTHON) -m trading_platform forecast-bootstrap --state-db $${STATE_DB:-data/realtime/state.db} --config $(CONFIG) --backend $${BACKEND:-seasonal}

forecast-info: ## Report whether $(FORECAST_ARTIFACT) is still usable (STATE_DB=... to check it against a profile)
	$(PYTHON) -m trading_platform forecast-info --artifact $(FORECAST_ARTIFACT) --state-db $${STATE_DB:-data/realtime/state.db} --json

realtime-forecast: ## Run the realtime engine on the profiles of $(STATE_DB)
	$(PYTHON) -m trading_platform realtime run --state-db $${STATE_DB:-data/realtime/state.db}

forecast-flow: data-download forecast-bootstrap forecast-info realtime-forecast ## The documented 3-command flow: download, bootstrap, verify, trade

data-download: ## Fill the on-disk OHLCV cache (only network-using target)
	$(PYTHON) -m trading_platform data download --config $(CONFIG) \
		--symbol "$${SYMBOL:-$(SYMBOL)}" --timeframe "$${TIMEFRAME:-$(TIMEFRAME)}" \
		--start "$${START:-2023-01-01T00:00:00Z}" --end "$${END:-2024-01-01T00:00:00Z}"

realtime: ## Run the realtime engine and the monitoring JSON API (STATE_DB=... to override)
	$(PYTHON) -m trading_platform realtime run --state-db $${STATE_DB:-data/realtime/state.db}

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
# dashboard (standalone Next.js monitoring UI, dashboard/)
#
# The four gates of docs/testing-policy.md section 9.3: lint, typecheck,
# test:coverage (the dashboard gate lives in dashboard/vitest.config.ts) and
# build. Every command runs with the repository-local npm cache (NPM_CACHE).
# ---------------------------------------------------------------------------

dashboard-install: ## Install the dashboard dependencies (npm ci)
	cd dashboard && npm_config_cache=$(NPM_CACHE) npm_config_logs_dir=$(NPM_CACHE)/_logs $(NPM) ci

dashboard-dev: ## Run the Next.js dev server (http://127.0.0.1:3000)
	cd dashboard && npm_config_cache=$(NPM_CACHE) npm_config_logs_dir=$(NPM_CACHE)/_logs $(NPM) run dev

dashboard-lint: ## Lint the dashboard (eslint)
	cd dashboard && npm_config_cache=$(NPM_CACHE) npm_config_logs_dir=$(NPM_CACHE)/_logs $(NPM) run lint

dashboard-typecheck: ## Type-check the dashboard (tsc --noEmit)
	cd dashboard && npm_config_cache=$(NPM_CACHE) npm_config_logs_dir=$(NPM_CACHE)/_logs $(NPM) run typecheck

dashboard-test: ## Run the dashboard tests without the coverage gate
	cd dashboard && npm_config_cache=$(NPM_CACHE) npm_config_logs_dir=$(NPM_CACHE)/_logs $(NPM) run test

dashboard-test-coverage: ## Run the dashboard tests with the coverage gate (85%/75% branches)
	cd dashboard && npm_config_cache=$(NPM_CACHE) npm_config_logs_dir=$(NPM_CACHE)/_logs $(NPM) run test:coverage

dashboard-build: ## Build the dashboard for production (next build, standalone output)
	cd dashboard && npm_config_cache=$(NPM_CACHE) npm_config_logs_dir=$(NPM_CACHE)/_logs $(NPM) run build

dashboard-check: dashboard-lint dashboard-typecheck dashboard-test-coverage dashboard-build ## Full dashboard gate: lint + typecheck + test-coverage + build

check-all: check dashboard-check ## Full repository gate: make check plus make dashboard-check

# ---------------------------------------------------------------------------
# housekeeping
# ---------------------------------------------------------------------------

clean: ## Remove caches, coverage artifacts and build leftovers
	@find . -path ./.venv -prune -o -name "__pycache__" -type d -print -exec rm -rf {} + 2>/dev/null || true
	@find . -path ./.venv -prune -o -name "*.egg-info" -type d -print -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .mypy_cache .ruff_cache .coverage coverage.xml htmlcov build dist
