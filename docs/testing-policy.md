# Testing & Coverage Policy

**Status:** binding for every contributor and every automated agent working in this repository.
**Owner:** the repository planning authority (planning/integration phase). This file is *not* owned
by any work package — every package may read it, no package may modify it.

---

## 1. Non-negotiable rules

1. **No network access in tests.** No test may download market data, hit an exchange API, or call
   `ccxt`. All OHLCV data used by tests is generated synthetically
   (`trading_backtest.data.synthetic`) or read from a CSV fixture checked into `tests/fixtures/`.
2. **No real Freqtrade dependency in tests.** `freqtrade` is an *optional* extra. The suite must be
   green with only `.[dev]` installed.
   * The guard goes **inside the test body** (`pytest.importorskip("freqtrade")`), never at module
     level, whenever the same file also carries assertions that must run *without* the extra: a
     module-level guard skips the whole module, offline assertions included.
   * Simulating the missing extra in-process means `monkeypatch.setitem(sys.modules, "freqtrade",
     None)` **plus** dropping the cached `freqtrade.*` submodules through `monkeypatch` — a cached
     submodule is served straight from `sys.modules` and the simulation silently stops biting. Those
     purges must stay inside `monkeypatch` (or a subprocess): permanently deleting `freqtrade.*`
     makes the next import build a **second** `IStrategy` class, and `issubclass`/`isinstance`
     checks against the first one (Freqtrade's own `StrategyResolver` included) then fail.
   * From pytest 9.1 on, `importorskip` skips on `ModuleNotFoundError` only: a simulation whose
     blocker raises plain `ImportError` errors out instead of skipping.
3. **Determinism.** Any test involving randomness passes an explicit seed. Two runs of the full
   command must produce identical results.
4. **`pyproject.toml` is shared and frozen.** Packages must never edit it. All pytest, coverage,
   ruff and mypy configuration lives there.
5. **`--cov-fail-under` is wired into `pyproject.toml`** (`[tool.pytest.ini_options] addopts` and
   `[tool.coverage.report] fail_under`). It is inert until coverage is requested with `--cov`,
   which is why scoped runs below stay fast.

---

## 2. Environment setup

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

`uv` users:

```bash
UV_CACHE_DIR=.uv-cache uv venv --python 3.11 .venv
UV_CACHE_DIR=.uv-cache uv pip install --python .venv/bin/python -e ".[dev]"
```

Tests import the package through `pythonpath = ["src"]` (see `pyproject.toml`), so they also run
from a clean checkout **without installing the project at all**.

---

## 3. The FULL command — must be green before any push

```bash
.venv/bin/python -m pytest tests --cov=trading_backtest --cov-report=term-missing --cov-report=xml
```

This is the exact command run by CI (`.github/workflows/ci.yml`) and by `make test-cov`.
It **fails (exit code 1)** when total coverage of the `trading_backtest` package is below the
threshold below — the gate is mechanical, not a convention.

Equivalent commands:

```bash
make test-cov
```

The complete pre-push gate (all three must pass):

```bash
make lint        # ruff check . && ruff format --check .
make type-check  # mypy (configuration in pyproject.toml)
make test-cov    # pytest + coverage gate
```

---

## 4. Scoped commands — what an implementer runs while working

Work packages run concurrently in the same checkout. **Never run the full suite with coverage while
other packages are still being written**: their modules are incomplete and the coverage gate would
fail for reasons unrelated to your code.

```bash
# one file
.venv/bin/python -m pytest tests/test_engine.py -q

# one area (glob)
.venv/bin/python -m pytest tests/test_validation_*.py -q

# one test, verbose
.venv/bin/python -m pytest tests/test_monte_carlo.py::test_deterministic_seed -vv

# skip slow tests
.venv/bin/python -m pytest tests/test_walk_forward.py -q -m "not slow"
```

Rules for scoped runs:

- Run **only the test files inside your own scope**.
- Do **not** add `--cov` to a scoped run.
- Do **not** run `pytest` with no path argument until your package is finished.
- A scoped run that needs a module owned by another package means the dependency was declared wrong
  — report it instead of editing the other package's files.

Tests marked `network` are skipped by default and must stay skipped:

```bash
# opt-in only (never used in CI)
.venv/bin/python -m pytest tests -m network --run-network
```

---

## 5. Coverage rule

### 5.1 How coverage is measured

```bash
.venv/bin/python -m pytest tests --cov=trading_backtest --cov-report=term-missing --cov-report=xml
```

* `--cov=trading_backtest` restricts measurement to the shipped package (`src/trading_backtest`).
* `tests/` is never counted.
* `*/__main__.py` is excluded (`[tool.coverage.run] omit`).
* A machine-readable `coverage.xml` is written for CI/reporting; it is git-ignored.

### 5.2 Threshold — **85 %**

```toml
[tool.pytest.ini_options]
addopts = "-ra --strict-markers --strict-config --cov-fail-under=85"

[tool.coverage.report]
fail_under = 85
```

* Measurement is **line coverage** of `trading_backtest` (`branch = false`).
* **Total coverage must be ≥ 85.00 %.** Below that, `pytest` exits non-zero and the build is red.
* Ambition: every module ≥ 85 %; a module below 85 % must be compensated by others *and* justified
  in the work-package report. No module may ship at 0 %.
* Deviation from 85 % requires an explicit, written decision recorded in the repository; silently
  lowering `fail_under` is forbidden.
* Thin-but-critical wrappers (e.g. optional `ccxt`/`freqtrade` adapters) must be tested through
  their failure paths (`pytest.importorskip` / monkeypatched imports) rather than excluded.

### 5.3 What counts as a tested behaviour

Every public function/class declared in a work-package contract must have at least:

1. a nominal-path test,
2. a boundary/edge test (empty input, single row, all-NaN, no trades, zero volatility…),
3. an error-path test when the contract declares an exception.

---

## 6. Test layout

`tests/` mirrors the package layout and is split by ownership (one owner per file):

```
tests/conftest.py                     # shared fixtures (synthetic OHLCV, tmp cache, AppConfig)
tests/test_core_*.py                  # shared domain models & errors
tests/test_config.py                  # typed configuration layer
tests/test_data_*.py                  # cache, loader, validation, synthetic generator
tests/test_strategy_*.py              # indicators, base, basic strategy, registry
tests/test_engine.py                  # backtest engine
tests/test_validation_*.py            # IS/OOS split, walk-forward, robustness, Monte Carlo
tests/test_metrics*.py                # performance metrics, drawdown
tests/test_reporting.py               # report builder / markdown / writer
tests/test_cli.py                     # CLI end-to-end, offline, via CliRunner
tests/test_freqtrade_config.py        # config/*.json validity
tests/test_packaging.py               # Makefile / Dockerfile / requirements consistency
tests/test_docs.py                    # documentation & policy enforcement
tests/fixtures/                       # small CSV fixtures (no market data downloads)
```

Shared fixtures live **only** in `tests/conftest.py` (owned by one package). Other packages may add
test files but must not edit an existing one.

---

## 7. CI parity

`.github/workflows/ci.yml` runs, on Python 3.11:

| Step | Command |
| --- | --- |
| Lint | `ruff check .` and `ruff format --check .` |
| Type-check | `mypy src` |
| Tests + coverage | the FULL command of §3 |

CI installs `.[dev]` only. A red CI on coverage is a **blocking** failure, not a warning.
