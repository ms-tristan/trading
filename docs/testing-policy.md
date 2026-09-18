# Testing & Coverage Policy

**Status:** binding for every contributor and every automated agent working in this repository.
**Owner:** the repository planning authority (planning/integration phase). This file is *not* owned
by any work package — every package may read it, no package may modify it.

---

## 1. Non-negotiable rules

1. **No network access in tests.** No test may download market data, hit an exchange API, or call
   `ccxt`. All OHLCV data used by tests is generated synthetically
   (`trading_platform.data.synthetic`) or read from a CSV fixture checked into `tests/fixtures/`.
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
.venv/bin/python -m pytest tests --cov=trading_platform --cov-report=term-missing --cov-report=xml
```

This is the exact command run by CI (`.github/workflows/ci.yml`) and by `make test-cov`.
It **fails (exit code 1)** when total coverage of the `trading_platform` package is below the
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
.venv/bin/python -m pytest tests --cov=trading_platform --cov-report=term-missing --cov-report=xml
```

* `--cov=trading_platform` restricts measurement to the shipped package (`src/trading_platform`).
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

* Measurement is **line coverage** of `trading_platform` (`branch = false`).
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

---

## 8. Addendum — the realtime and web layers (`trading_platform.realtime`, `trading_platform.web`)

**Status:** binding addendum, owned by the repository planning authority like the rest of this
file. Written **once**, before the work packages of `feat/realtime-multi-profile-platform` start:
no work package may modify this file.

Everything of §1–§7 applies unchanged. This section only fixes what is *specific* to the two new
layers.

### 8.1 The gate is NOT redefined

§3 (the FULL command) and §5.2 (`fail_under = 85`, measured on the **whole** `trading_platform`
package with `--cov=trading_platform`) are **unchanged**. The new modules are measured by the very
same run; there is no per-layer threshold, no exemption for the optional `ccxt` / `freqtrade`
adapters and no change to `pyproject.toml` — which stays frozen (§1.4).

### 8.2 Scoped commands while a package is in flight

Packages run concurrently in one checkout (§4). A realtime/web implementer runs **only** their own
files, **never** with `--cov`:

```bash
# one file, no coverage, fast
.venv/bin/python -m pytest tests/test_realtime_stream.py -q --no-cov

# one area
.venv/bin/python -m pytest tests/test_realtime_*.py -q --no-cov

# one test, verbose
.venv/bin/python -m pytest tests/test_realtime_risk.py::test_max_order_notional -vv --no-cov

# the CLI / end-to-end files
.venv/bin/python -m pytest tests/test_cli_realtime.py tests/test_realtime_e2e.py -q --no-cov
```

The FULL command of §3 is run **once** by the integration agent, at the end, on the frozen tree.

### 8.3 Offline and determinism rules for the new layers

1. **No network, no real exchange, no real order.** No test may reach a venue, and no test may
   place an order anywhere but in `PaperBroker` (simulated) or in a local fake implementing the
   `Broker` protocol.
2. **No `ccxt` / `freqtrade` import at module scope — neither in the sources nor in the tests.**
   The optional extras are imported *inside function bodies* only. A test that simulates the
   missing extra does it *in the test body*, through `monkeypatch.setitem(sys.modules, "ccxt",
   None)` (plus the purge of cached `ccxt.*` submodules, §1.2), never at module level.
3. **The wall clock is banned.** Every timestamp and every delay in the realtime layer goes through
   the injected `Clock`; a test injects `ManualClock` and advances it explicitly. `datetime.now()`,
   `datetime.utcnow()` and `time.time()` may appear in `SystemClock` only.
4. **Explicit seeds.** Every random draw (`PaperBroker` partial fills, jitter) takes an explicit
   seed; two runs of the same command produce byte-identical state and payloads.
5. **TCP port 0 only.** No test may bind a fixed port: the monitoring server is started on port `0`
   and the chosen port is read back from the server object before the first request.
6. **Bounded timeouts, always.** Every `await` in production code is wrapped in
   `asyncio.wait_for(..., timeout=<explicit>)`; stream reconnects use a bounded attempt budget and a
   bounded backoff. No test may hang: the suite must finish without an external timeout.
7. **No secret on disk, in a log or in a `repr()`.** Credentials come from the environment only, and
   a test asserts that a sentinel secret value never appears in `repr()`, in `to_dict()`, in the
   captured log records or in the SQLite file.

### 8.4 Test layout for the new layers (one owner per file)

Mirroring §6 — every file below has exactly **one** owning work package:

```
tests/test_realtime_models.py         # wp1  frozen domain models, errors, config, example profile
tests/test_realtime_stream.py         # wp2  Replay / Polling / Composite / ccxt-pro streams
tests/test_realtime_store.py          # wp3  SqliteStateStore, idempotency, schema/migration
tests/test_realtime_broker.py         # wp4  PaperBroker + CcxtBroker + credentials/redaction
tests/test_realtime_credentials.py    # wp4  env resolution, repr/log redaction
tests/test_realtime_risk.py           # wp5  every risk limit, kill switch, live gate
tests/test_realtime_gateway.py        # wp6  the single order lifecycle (paper == live path)
tests/test_realtime_runner.py         # wp7  ProfileRunner per-candle loop, warmup, health
tests/test_realtime_orchestrator.py   # wp7  N concurrent profiles, supervision, restart
tests/test_realtime_observability.py  # wp7  JSON logs, redaction filter, counters
tests/test_realtime_monitor.py        # wp8  read model, metrics reuse, benchmark block
tests/test_web_routes.py              # wp9  pure routing/request->response, status codes
tests/test_web_server.py             # wp9  ThreadingHTTPServer, port 0, read-only mode, assets
tests/test_cli_realtime.py            # wp10 `realtime run|serve|check` via CliRunner
tests/test_realtime_e2e.py            # wp10 deterministic end-to-end (`--once`) over SQLite
tests/test_realtime_docs.py           # wp10 documentation contract of the new layers
```

`tests/conftest.py` stays owned by the core/data package: a realtime package that needs a shared
object defines a **local fake** implementing the protocol it depends on, in its own test file.

---

## 9. Addendum — the standalone dashboard (`dashboard/`)

**Status:** binding addendum, owned by the repository planning authority like the rest of this
file. Written **once**, before the work packages of `feat/nextjs-monitoring-dashboard` start: no
work package may modify this file.

Everything of §1–§8 applies unchanged; §1–§8 govern the Python suite. This section fixes what is
specific to the standalone Next.js dashboard delivered in `dashboard/`.

### 9.1 The Python gate is NOT redefined

`fail_under = 85` measured with `--cov=trading_platform` over the whole package (§5.2) stays
exactly as it is, and `pyproject.toml` stays frozen (§1.4). The dashboard is a **separate**
project with a **separate**, mechanical gate.

### 9.2 Threshold — **85 % lines / 85 % statements / 85 % functions / 75 % branches**

* measured over `dashboard/src/**/*.{ts,tsx}` (tests, `*.d.ts` and the root config files excluded);
* enforced by `coverage.thresholds` in `dashboard/vitest.config.ts` — `vitest run --coverage`
  exits non-zero below a threshold, so the gate is mechanical, not a convention;
* branch coverage is the single number below the line threshold because the Python policy itself
  measures **line** coverage only (`branch = false`, §5.2) and JSX produces trivially unreachable
  branches. Every one of the four numbers is a hard gate;
* a file this feature does not test may not be excluded to make the gate pass: the honest escape
  hatch is the `/* v8 ignore ... */` comment next to the statement, and it must be justified in
  the work-package report.

### 9.3 The FULL dashboard command — must be green before any push

```bash
export npm_config_cache=/Users/mac/Projects/Trading/.npm-cache
export npm_config_logs_dir=$npm_config_cache/_logs
cd dashboard
npm run lint          # eslint .
npm run typecheck     # tsc --noEmit
npm run test:coverage # vitest run --coverage, with the thresholds of §9.2
npm run build         # next build (production build, standalone output)
```

Equivalent commands: `make dashboard-check` (all four) and `make check-all` (the Python gate of
§3 **plus** the dashboard gate).

Non-negotiable rules for this project:

1. **No live server, ever.** Every dashboard test mocks the network (an injected `fetchImpl` or
   `vi.stubGlobal("fetch", ...)`); no test binds a port, no test reaches the Python monitoring
   server, no test reads the real `sessionStorage` of a browser.
2. **Deterministic.** Polling tests use fake timers and explicit cadences; timestamps are fixed
   reference values; no test depends on the wall clock or on a random identifier.
3. **The lockfile is committed.** CI installs with `npm ci`; a dependency that is not declared in
   `dashboard/package.json` (owned by the foundation package) may never be added by another
   package.
4. **No network at test time.** The remote font/asset story is solved at build time (self-hosted
   packages), never by a runtime CDN.

### 9.4 Scoped commands — what a dashboard implementer runs while working

Packages run concurrently in one checkout (§4). Export the cache first (see §9.6), then run
**only** your own files, and never the coverage gate or the production build:

```bash
cd dashboard
npx vitest run src/lib/format.test.ts          # one file
npx vitest run src/components/overview         # one directory
npx vitest run src/components/profile -t "positions"
npx eslint src/components/profile              # one area
npm run typecheck                              # whole project, cheap and safe
```

The FULL command of §9.3 is run **once** by the integration agent, at the end, on the frozen tree.
A scoped run that needs a file owned by another package means the dependency was declared wrong —
report it instead of editing the other package's files.

### 9.5 Dashboard test layout (one owner per file)

```
dashboard/src/lib/*.test.ts                     # wp-3  API client, formatting, operator token, polling
dashboard/src/components/ui/*.test.tsx          # wp-3  shared primitives and the kill-switch flow
dashboard/src/components/overview/*.test.tsx    # wp-4  profile cards, live header, empty/error states
dashboard/src/components/profile/*.test.tsx     # wp-5  positions / trades / orders / metrics tables
dashboard/src/components/charts/*.test.tsx      # wp-5  equity chart geometry, tooltip, empty series
dashboard/src/app/**/page.test.tsx              # wp-4, wp-5  page-level render contracts
```

### 9.6 Sandbox: the npm cache lives inside the repository

The agent sandbox only permits writes **inside** the repository, and the global npm cache
(`~/.npm`) is not writable: a bare `npm` command fails while writing its logs. Every npm/node
command therefore runs with a repository-local cache:

```bash
export npm_config_cache=/Users/mac/Projects/Trading/.npm-cache
export npm_config_logs_dir=$npm_config_cache/_logs
```

`.npm-cache/` is git-ignored, exactly like `.uv-cache/`.

