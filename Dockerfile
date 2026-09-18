# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# Reproducible environment for the trading-platform package.
#
#   base  -> runtime image: requirements.txt + the installed package
#   test  -> base + dev dependencies + the full offline suite (85 % coverage gate)
#
# Both stages only need pip: no apt-get, no exchange API, no market data
# download. Everything the suite touches is generated synthetically or read from
# tests/fixtures/.
#
#   docker build -t trading-platform:latest .            # base (+ test) image
#   docker build --target test -t trading-platform:test .# test stage only
#   docker run --rm trading-platform:latest backtest \
#       --config config/backtest_default.json --data-file data/btc.csv
# ---------------------------------------------------------------------------
FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 1. runtime dependencies (requirements.txt mirrors [project].dependencies)
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt

# 2. the package itself (+ the committed configurations, so the runtime image can
#    be used with --config config/backtest_default.json)
COPY pyproject.toml README.md ./
COPY src ./src
COPY config ./config
RUN python -m pip install --no-cache-dir .

# 3. never run as root -- and keep /app writable for the runtime user (reports/,
#    data/cache/ and the coverage artifacts written by the re-run below)
RUN useradd --create-home --shell /bin/bash appuser \
    && chown -R appuser:appuser /app
USER appuser

# ---------------------------------------------------------------------------
# Test stage: dev dependencies, the sources and the full suite.
# `docker build --target test` fails when the coverage gate of 85 % is not met.
# ---------------------------------------------------------------------------
FROM base AS test

USER root

COPY requirements-dev.txt ./
RUN python -m pip install --no-cache-dir -r requirements-dev.txt

# the suite also asserts the packaging files and the documentation, so they are
# copied as well (tests/test_packaging.py, tests/test_docs.py)
COPY src ./src
COPY tests ./tests
COPY config ./config
COPY docs ./docs
COPY .github ./.github
COPY requirements.txt requirements-dev.txt requirements-freqtrade.txt ./
COPY Makefile Dockerfile .dockerignore pyproject.toml README.md ./

RUN python -m pytest tests --cov=trading_platform --cov-report=term-missing --cov-fail-under=85

# the build-time run above wrote /app/.coverage as root: hand /app back to the
# runtime user, which re-runs the same command through `make docker-test`
RUN chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["python", "-m", "trading_platform"]
