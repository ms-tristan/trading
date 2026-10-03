# Testing and coverage policy

This document is the **mechanically enforceable** test policy of the repository.
It is not advisory: the thresholds below live in `pyproject.toml` and in
`dashboard/vitest.config.ts`, so the documented commands *fail* when a threshold
is missed. Nothing here is a work-package deliverable — the policy files are
shared by every work package and no implementer may edit them.

> Scope of this document: what must be run, what must pass, and what a scoped
> work-package run is allowed to do. The architecture itself is documented in
> `docs/architecture.md`, the strategies in `docs/strategies.md` and the runbook
> in `docs/operations.md`.

---

## 1. Environment bootstrap (once per checkout)

```bash
cd /Users/mac/Projects/Trading
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e . --no-deps
```

* Python is the host interpreter, **3.14** on this machine (macOS arm64);
  `requires-python` is `>=3.12` and every dependency must stay installable on
  3.14.
* `requirements-dev.txt` pulls `freqtrade==2026.8` — the same engine version the
  realtime container runs — plus `pytest`, `pytest-cov`, `pytest-asyncio`,
  `ruff` and `mypy`.
* The virtualenv is `.venv/` and is **git-ignored**: never commit it, never
  commit `data/`, `*.db`, `.next/` or `node_modules/`.
* `make install` performs the same three steps. `make venv` only creates the
  virtualenv and installs the requirements.
* The editable install is a convenience (the `trading` console script);
  `[tool.pytest.ini_options] pythonpath = ["src"]` makes the package importable
  from a plain checkout even without it.

---

## 2. The Python gate (must pass before any push)

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m pytest
```

`make check-python` runs exactly those four commands, in that order.

| Step | Threshold | Where it is enforced |
| --- | --- | --- |
| `ruff check .` | zero findings | `[tool.ruff]` / `[tool.ruff.lint]` in `pyproject.toml` |
| `ruff format --check .` | zero reformats | `[tool.ruff]` |
| `mypy src` | zero errors | `[tool.mypy]` in `pyproject.toml` |
| `python -m pytest` | **coverage of `trading_platform` >= 80 %** | `addopts` in `[tool.pytest.ini_options]` |

The coverage gate is part of `addopts`:

```toml
addopts = "-ra --cov=trading_platform --cov-report=term-missing --cov-fail-under=80"
```

so a bare `.venv/bin/python -m pytest` **is** the gate: there is no separate
coverage command to remember, and a run that drops below 80 % exits non-zero.

### What the Python suite must cover

| Area | Module under test | Mandatory cases |
| --- | --- | --- |
| Configuration | `trading_platform/config.py` | defaults from `config/platform.json`, the `TB_SNAPSHOT_INTERVAL_SECONDS` / `TB_PROFILE_API_PORT_BASE` overrides, the live-trading gate (both refusals and the accepted case) |
| Domain models | `trading_platform/models.py` | serialisation of every documented JSON shape, the supported-timeframe set, the window helpers |
| Aggregation | `trading_platform/metrics.py` | portfolio/profit/win-rate aggregation, `profit_pct = (portfolio_value - initial_capital) / initial_capital`, ranking by portfolio value across both modes before filtering |
| Catalogue | `trading_platform/profiles/catalogue.py` | metadata load, a strategy file with **no** metadata entry (title derived from the class name), the 22 profile entries |
| State store | `trading_platform/profiles/store.py` | schema version 1, upserts, snapshots (`INSERT OR REPLACE` per minute), events, settings, and the **legacy-database archive path** (a foreign `state.db` is renamed `state.db.legacy-<ts>` and a fresh database is created) |
| Engine | `trading_platform/engine/config_builder.py` | every key freqtrade 2026.8 validates, per-timeframe `process_throttle_secs`, file mode `0600`, no `telegram` block |
| Engine | `trading_platform/engine/client.py` | the documented freqtrade REST response keys, HTTP Basic auth, the platform's own `profit_pct` calculation, the documented read timeout |
| Engine | `trading_platform/engine/supervisor.py` | scheduling order (priority DESC, id ASC) with every enabled profile started, deterministic API ports, the live refusal, the proven-gone restart rule, restart backoff, graceful SIGTERM of every child |
| Engine | `trading_platform/engine/poller.py` | minute-rounded snapshots, "no invented snapshot for a stopped profile", the health rule (`UNHEALTHY_THRESHOLD` / `UNHEALTHY_PING_THRESHOLD` / `WORKER_STARTUP_GRACE_SECONDS`), engine events |
| API | `trading_platform/api/**` | every route with `fastapi.testclient.TestClient`, `401` on a missing operator token, `403` on a wrong one, ranking stability, `404`/`409`/`422` cases |
| CLI | `trading_platform/__main__.py` | `realtime run`, `realtime provision` (token read from the environment, never argv), `realtime status`, `strategies list` |
| Strategies | `user_data/strategies/*.py` | each shipped strategy resolved by freqtrade's own `StrategyResolver`, indicators + entry/exit trends run over a deterministic seeded OHLCV frame, the entry column is only `0`/`1`, no NaN in the signal columns, at least one entry signal, the declared timeframe is in freqtrade's own supported set |

**Hard rule: the suite never spawns a real `freqtrade` process and never talks to
a real exchange.** The process launcher and the REST client are injected
(`ProcessLauncher`, `client_factory`) and replaced by test doubles; the strategy
test is the only place that imports freqtrade, and it only loads strategy
classes.

---

## 3. The dashboard gate (must pass before any push)

```bash
cd dashboard
npm ci
npm run lint
npm run typecheck
npm run test:coverage
npm run build
```

`make dashboard-check` runs the last four commands after `npm ci`.

| Step | Command | Threshold |
| --- | --- | --- |
| Lint | `npm run lint` (`eslint .`) | zero errors |
| Types | `npm run typecheck` (`tsc --noEmit`) | zero errors |
| Tests | `npm run test:coverage` (`vitest run --coverage`) | lines >= 70 %, statements >= 70 %, functions >= 70 %, **branches >= 60 %** |
| Build | `npm run build` (`next build`) | success with `output: "standalone"` |

The thresholds live in `dashboard/vitest.config.ts`
(`test.coverage.thresholds`), so a run under the threshold exits non-zero.

### What the dashboard suite must cover

* `src/lib/api.ts` — `API_ORIGIN`, `fetchJson` (absolute origin for server
  components, same-origin for the browser), `ApiError`, the last-known-value
  fallback, and the mutating calls that carry `X-Operator-Token`;
* `src/lib/ranking.ts` and `src/lib/format.ts` — the sort/rank helpers and the
  USDT / percent / duration formatters;
* `src/lib/states.ts` — the profile-state badge mapping (label, colour token,
  attention flag);
* the SVG chart components (`Sparkline`, `EquityChart`, `DailyBars`) **including
  their accessible table fallback**, and the sortable table header buttons that
  expose `aria-sort`.

The dashboard tests run in `jsdom` and must not reach the network: `fetch` is
mocked per test.

---

## 4. Scoped runs (work packages)

A work package **never** runs the full suite and **never** runs the coverage
gate. It runs only its own area:

```bash
.venv/bin/python -m pytest tests/<area> -q --no-cov      # Python area
cd dashboard && npx vitest run src/<path>.test.tsx       # dashboard area
```

* `--no-cov` is required: the coverage flags are in `addopts`, and a scoped run
  cannot reach 80 % of the whole package.
* A dashboard scope is checked with `npx vitest run <path>` and, when types are
  touched, `npx tsc --noEmit`.
* Lint/format are checked on the package's own files only:
  `.venv/bin/ruff check <paths>` and `.venv/bin/ruff format --check <paths>`.

---

## 5. Continuous integration

`.github/workflows/ci.yml` runs on **pull requests and manual dispatch only**
(never on a push to `main`: `deploy.yml` owns that) on the self-hosted runner
labelled `[self-hosted, macOS, ARM64]`, in **one sequential job** with a
40-minute timeout and `concurrency: ci-<ref>` (`cancel-in-progress: true`):

1. checkout;
2. create/restore `.venv` from `requirements-dev.txt`, with `actions/cache`
   keyed on the hash of `requirements.txt` + `requirements-dev.txt`;
3. the full documented Python gate (§2);
4. the full documented dashboard gate (§3) in `dashboard/`;
5. `docker build -f deploy/Dockerfile.realtime -t trading-realtime:ci .` and
   `docker build -f deploy/Dockerfile.dashboard -t trading-dashboard:ci .`.

The job is deliberately sequential: the self-hosted machine is shared with the
running trading stack, so two parallel gates would compete for CPU and memory.
The same sequence, run locally with `make check` plus `make images`, is what
proves a revision before it is pushed.
