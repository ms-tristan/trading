# Trading platform — many profiles, one supervisor

A crypto trading platform that runs **many trading profiles at the same time**.
A profile is one strategy on one timeframe, on one exchange, in one mode, on one
pair, with its own starting cash. A single Python **supervisor** process owns the
fleet: it generates one freqtrade configuration per running profile, spawns one
`freqtrade trade` worker per running profile, polls each worker over its private
REST API, stores everything in one SQLite database and serves **one aggregated
JSON API**. A Next.js dashboard renders that API.

The trading engine is [freqtrade](https://www.freqtrade.io) 2026.8 — this
repository does not re-implement exchange connectivity, order management,
`dry_run` accounting or the dry-run wallet. It orchestrates freqtrade instances
and aggregates what they report.

```
 18 profiles in the catalogue   ->  one freqtrade worker per enabled profile
 (16 paper + 2 live)                (no cap, no queue)
```

## Contents

1. [Quick start](#1-quick-start)
2. [Architecture](#2-architecture)
3. [Profiles](#3-profiles)
4. [The strategies](#4-the-strategies)
5. [Run modes: paper and live](#5-run-modes-paper-and-live)
6. [Documented commands](#6-documented-commands)
7. [Environment variables](#7-environment-variables)
8. [State, logs and ports on disk](#8-state-logs-and-ports-on-disk)
9. [Local development loop](#9-local-development-loop)
10. [Deployment loop](#10-deployment-loop)
11. [Operational limits](#11-operational-limits)
12. [Documentation map](#12-documentation-map)

---

## 1. Quick start

```bash
# once per checkout: create the virtualenv and install the package
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pip install -e . --no-deps

# run the supervisor + the aggregated API on 127.0.0.1:8080
make realtime

# in another shell: what the platform is doing
make status          # health + the ranked profiles
make strategies      # the discovered strategy catalogue
```

`make install` performs the three bootstrap commands above. `make help` lists
every entry point.

---

## 2. Architecture

One supervisor, N freqtrade workers, one state database, one JSON API, one
dashboard:

```
                    host nginx: TLS, HTTP Basic auth, limit_req, fail2ban jail nginx-auth
                                        |
                                        v
   +------------------------------------+-------------------------------------+
   |                                                                            |
   |   127.0.0.1:3031                                            127.0.0.1:3030|
   |        |                                                          ^        |
   |        v                                                          |        |
   |  +---------------------+   next.config.ts rewrites /api/:path*    |        |
   |  | trading-dashboard   |------------------------------------------+        |
   |  | Next.js 16          |   (compose network: http://trading-realtime:8080)  |
   |  | listen 0.0.0.0:3000 |                                                   |
   |  +---------------------+                                                   |
   |                                                                            |
   |  +----------------------------------------------------------------------+  |
   |  | trading-realtime             `python -m trading_platform realtime run`|  |
   |  |                                                                      |  |
   |  |   +--------------------+        +------------------------------+     |  |
   |  |   | supervisor         |        | FastAPI JSON API  /api/*     |     |  |
   |  |   | - fleet scheduling |        | /api/health /api/profiles    |     |  |
   |  |   | - config builder   |        | /api/account /api/strategies |     |  |
   |  |   | - poller + metrics |        | /api/events /api/settings    |     |  |
   |  |   +---------+----------+        | /api/kill-switch (operator)  |     |  |
   |  |             |                   +---------------+--------------+     |  |
   |  |             | spawns / polls                    | reads/writes        |  |
   |  |             v                                   v                     |  |
   |  |   +-------------------+   REST    +---------------------------------+  |  |
   |  |   | freqtrade #1      |<----------| SQLite state database           |  |  |
   |  |   | freqtrade #2      |  :8101+   | /app/data/realtime/state.db     |  |  |
   |  |   | ...               |  loopback | tables: profiles,               |  |  |
   |  |   | freqtrade #6      |  in the   | profile_snapshots,              |  |  |
   |  |   +-------------------+  container| profile_trades, profile_daily,  |  |  |
   |  |        |   ^                      | settings, events                |  |  |
   |  |        |   |                      | (PRAGMA user_version 2)         |  |  |
   |  |        |   |                      +---------------------------------+  |  |
   |  |        |   | HTTP Basic to each worker's own /api/v1/               |  |
   |  |        v   |                                                          |  |
   |  |   generated per-profile config + tradesv3.sqlite + OHLCV cache        |  |
   |  +----------------------------------------------------------------------+  |
   |                                                                            |
   +----------------------------------------------------------------------------+

 Named volumes (the only durable state):
   trading-state -> /app/data/realtime   state.db, per-profile configs, trades DBs,
                                         per-profile OHLCV caches, logs, KILL_SWITCH
   trading-cache -> /app/data/cache      reserved for a future SHARED OHLCV cache
```

The division of labour is deliberate:

| Component | Responsibility | Never does |
| --- | --- | --- |
| supervisor (`src/trading_platform/engine/supervisor.py`) | fleet selection, one `freqtrade trade` subprocess per running profile, REST polling, restart policy | trades, owns no position |
| freqtrade worker | exchange connectivity, indicators, entries/exits, `dry_run` wallet, its own trade database | knows nothing about the other profiles |
| state store (`src/trading_platform/profiles/store.py`) | profiles, minute snapshots, settings, events | trades |
| API (`src/trading_platform/api`) | aggregated read models + operator mutations | trades |
| dashboard (`dashboard/`) | rendering, operator actions | holds no database, no state |

`docs/architecture.md` documents the design in full, including the state-database
schema and the health/restart policy.

---

## 3. Profiles

A profile is **strategy id + timeframe + mode + pair(s) + starting cash** plus a
few operational knobs (exchange, `max_open_trades`, `priority`, `enabled`).

Profiles come from two places, and the state database is the single source of
truth at run time:

* the **declarative catalogue** `config/profiles.json` (18 entries: 16 paper and
  2 live), applied to a running engine with `POST /api/catalogue/apply` (or
  `python -m trading_platform realtime provision`). Applying it is an idempotent
  upsert: catalogue-owned profiles are created or refreshed, operator-created
  profiles are never touched, and a live entry whose preconditions are unmet is
  reported in `refused_live` instead of being created;
* the **API**: `POST /api/profiles` creates an operator-owned profile,
  `PATCH /api/profiles/{id}` edits it, `DELETE /api/profiles/{id}` removes it
  (refused with `409` on a catalogue-owned id unless `?force=true`), and
  `POST /api/profiles/{id}/actions` starts, stops or restarts it. The dashboard
  drives exactly these routes.

One catalogue entry looks like this:

```json
{
  "id": "momentum-btc-1h",
  "name": "Momentum BTC 1h",
  "strategy": "momentum",
  "timeframe": "1h",
  "mode": "paper",
  "exchange": "binance",
  "pairs": ["BTC/USDT"],
  "initial_capital": 1000.0,
  "max_open_trades": 2,
  "priority": 100,
  "enabled": true
}
```

`strategy` references an `id` of `config/strategies.json`; `initial_capital` is
the dry-run wallet of a paper profile and the reference capital of a live one
(profit percentage is measured against it, not against freqtrade's own tradable
balance). Selection order for the fleet is `priority` descending, then `id`
ascending, so `priority` is how an operator makes sure a profile gets a worker
when the fleet is full.

---

## 4. The strategies

Every strategy is an ordinary freqtrade `IStrategy` (interface version 3, spot,
long only) living in `user_data/strategies/`. `config/strategies.json` carries
their catalogue metadata (title, summary, description, indicators, timeframes,
reference, risk notes).

| id | file | rule in one line |
| --- | --- | --- |
| `basic` | `BasicStrategy.py` | EMA(20) cross above EMA(50) with RSI(14) < 70 |
| `momentum` | `MomentumStrategy.py` | ROC(12) > 0 inside an EMA(50) > EMA(200) trend with ADX(14) > 20 |
| `rsi-reversion` | `RsiReversionStrategy.py` | RSI(14) crosses back above 30 while the close holds above SMA(200) |
| `bollinger` | `BollingerStrategy.py` | lower Bollinger(20, 2.0) touch with RSI(14) < 40 above SMA(200) |
| `macd` | `MacdStrategy.py` | MACD(12, 26, 9) histogram crosses above zero above EMA(200) |
| `donchian` | `DonchianStrategy.py` | close above the highest high of the previous 20 candles (Turtle breakout) |
| `keltner` | `KeltnerStrategy.py` | close above `EMA(20) + 2 × ATR(10)` while EMA(50) > EMA(200) |
| `supertrend` | `SupertrendStrategy.py` | entry on the Supertrend(10, 3.0) flip to uptrend, exit on the flip back |
| `dual-thrust` | `DualThrustStrategy.py` | breakout of `open ± 0.5 ×` the previous 4-candle range |
| `faber` | `FaberStrategy.py` | long while the close is above SMA(200) (Faber's tactical allocation) |

Five further strategies were added by the 2026 research programme — three improved
versions of the worst-performing rules and two full-size variants of the best
long-run trend profiles. They are additive: the ten above keep running, so an
improvement can be measured against a live baseline.

| id | file | rule in one line |
| --- | --- | --- |
| `keltner-breakout-v2` | `KeltnerBreakoutV2Strategy.py` | first close above `EMA(20) + 3 × ATR(10)` in an uptrend, trailed by Supertrend |
| `trend-ensemble-v2` | `TrendEnsembleV2Strategy.py` | hold while two of SMA(50/100/200) agree with price and SMA(50) rises |
| `vol-targeted-trend` | `VolTargetedTrendStrategy.py` | trade the SMA(200) trend only while realised volatility is below its median |
| `faber-all-in` | `FaberAllInStrategy.py` | `faber`'s rule, resized for a single all-in position |
| `donchian-all-in` | `DonchianAllInStrategy.py` | `donchian`'s breakout, resized, with an ATR chandelier exit |

`docs/strategies.md` documents each one: logic, parameters, suitable timeframes
and risk notes.

### Adding a strategy: the two-file recipe

1. drop the strategy file into `user_data/strategies/`, e.g.
   `user_data/strategies/MyIdeaStrategy.py`, with one class whose name equals the
   file stem (`class MyIdeaStrategy(IStrategy)` — freqtrade's resolver looks the
   class up by name inside that file);
2. add one entry to `config/strategies.json` (its `id` is what profiles
   reference).

Step 2 is **optional**: the catalogue loader scans `user_data/strategies/*.py`,
so a file with no metadata entry is still discovered and usable — its title is
derived from the class name and its description stays empty. The file alone is
enough. Nothing else has to change: the supervisor passes the strategy path to
every worker, and the API's strategy view is built from the discovered catalogue
at request time.

---

## 5. Run modes: paper and live

| | `paper` | `live` |
| --- | --- | --- |
| Engine | freqtrade `dry_run: true` | freqtrade `dry_run: false` |
| Capital | `dry_run_wallet = initial_capital` (the profile's starting cash) | `initial_capital` is only the reference the platform measures profit against |
| Credentials | none | `TB_LIVE_EXCHANGE_KEY` / `TB_LIVE_EXCHANGE_SECRET` from the environment |
| Extra gate | none | `TB_ALLOW_LIVE_TRADING` must equal exactly `I_UNDERSTAND_THE_RISK` |
| Default catalogue | 20 profiles, 1000 USDT each | 2 profiles, 250 USDT each |

A live profile is **never started** unless the acknowledgement variable holds the
exact sentence *and* both exchange credentials are present in the environment.
Otherwise the profile keeps the state `blocked` and `state_reason` names the
missing precondition — visible in `GET /api/profiles`, in `make status` and in
the dashboard. Real exchange keys are read from the environment only: they are
never written into a committed configuration file, never logged and never
returned by the API.

Paper mode is the default and the only mode a fresh deployment runs: the shipped
`deploy/.env` carries no exchange credential, so the two live catalogue entries
are refused (`refused_live`) until an operator provides them.

---

## 6. Documented commands

### Install

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt && .venv/bin/pip install -e . --no-deps
```

### Gates

Python gate (coverage of `trading_platform` must stay **>= 80 %**; the threshold
is part of the documented `pytest` command):

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/python -m pytest
```

Dashboard gate, in `dashboard/`:

```bash
npm ci && npm run lint && npm run typecheck && npm run test:coverage && npm run build
```

`docs/testing-policy.md` is the enforceable policy behind those commands.

### `make` targets

| Target | What it runs |
| --- | --- |
| `install` | the venv bootstrap above |
| `check-python` | the four Python gate commands, in order |
| `check` | `check-python` + `dashboard-check` (what CI runs) |
| `dashboard-install` | `npm ci` in `dashboard/` |
| `dashboard-check` | `npm run lint && npm run typecheck && npm run test:coverage && npm run build` |
| `realtime` | the supervisor + the aggregated API on `127.0.0.1:8080` |
| `status` | health and the ranked profiles of a running engine |
| `provision` | apply `config/profiles.json` to a running engine (dry run) |
| `strategies` | the discovered strategy catalogue |
| `images` | build both container images (same commands as CI) |
| `test-scoped` | one package's tests: `make test-scoped AREA=tests/<area>` |

### CLI

```bash
python -m trading_platform realtime run [--state-db PATH] [--host H] [--port P] [--max-running-profiles N]
python -m trading_platform realtime provision --api-url URL [--dry-run] [--prune] [--force]
python -m trading_platform realtime status --api-url URL
python -m trading_platform strategies list
```

The same commands are exposed through the console script `trading` once the
package is installed (`trading realtime status --api-url http://127.0.0.1:3030`).

`realtime provision` reads `TB_OPERATOR_TOKEN` **from the environment only** —
never from `argv`, so the token never lands in a shell history or in a process
list. `--dry-run` prints what would change without changing anything, `--prune`
additionally deletes profiles that the catalogue does not declare, and `--force`
skips the catalogue-vs-platform coherence refusal.

---

## 7. Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `TB_OPERATOR_TOKEN` | unset | operator token of the mutating API routes (`X-Operator-Token`); a missing header is `401`, a wrong one `403` |
| `TB_LOG_LEVEL` | `INFO` | log level of the supervisor and the API |
| `TB_SNAPSHOT_INTERVAL_SECONDS` | `60` | snapshot loop period (`config/platform.json` sets the default) |
| `TB_PROFILE_API_PORT_BASE` | `8101` | first private worker REST port; the profile at index *i* of the id-sorted catalogue uses `base + i` |
| `TB_REALTIME_STATE_DB` | `data/realtime/state.db` | path of the SQLite state database (the container passes `/app/data/realtime/state.db` on the command line) |
| `TB_ALLOW_LIVE_TRADING` | unset | must equal exactly `I_UNDERSTAND_THE_RISK` before any live profile may start |
| `TB_LIVE_EXCHANGE_KEY` | unset | live exchange API key, environment only |
| `TB_LIVE_EXCHANGE_SECRET` | unset | live exchange API secret, environment only |
| `TB_CONFIG_DIR` | `config` | directory holding `platform.json`, `strategies.json` and `profiles.json` (the image sets `/app/config`) |
| `TB_STRATEGIES_DIR` | `user_data/strategies` | directory the supervisor passes to every worker as `--strategy-path` (the image sets `/app/user_data/strategies`) |
| `TB_FREETRADE_BIN` | `freqtrade` | the freqtrade executable the supervisor spawns |

Every variable is an **environment override of a `config/platform.json`
default**; the file stays the documented default and the environment wins when it
is set.

---

## 8. State, logs and ports on disk

| Path (in the container) | Content |
| --- | --- |
| `/app/data/realtime/state.db` | the SQLite state database: `profiles`, `profile_snapshots`, `profile_trades`, `profile_daily`, `settings`, `events`, `PRAGMA user_version = 2` |
| `/app/data/realtime/KILL_SWITCH` | kill-switch marker; while the file exists no worker starts |
| `/app/data/realtime/logs/<id>.log` | the freqtrade log file of one profile |
| `/app/data/realtime/profiles/<id>/config.json` | the generated freqtrade configuration of one profile, mode `0600` |
| `/app/data/realtime/profiles/<id>/tradesv3.sqlite` | that worker's freqtrade trade database |
| `/app/data/realtime/profiles/<id>/data/` | that worker's own OHLCV cache (per-worker isolation) |
| `/app/data/cache/` | the `trading-cache` volume, reserved for a future shared OHLCV cache |

Ports:

| Port | Where | Published as |
| --- | --- | --- |
| 8080 | aggregated JSON API, inside `trading-realtime` | `127.0.0.1:3030` (loopback, debugging) |
| 3000 | dashboard, inside `trading-dashboard` | `127.0.0.1:3031` (loopback, the nginx upstream) |
| `8101 + index` | private freqtrade REST API of one running profile, bound to `127.0.0.1` **inside the container** | not published |

---

## 9. Local development loop

```bash
# 1. bootstrap once (idempotent)
make install

# 2. run the engine locally: supervisor + API on 127.0.0.1:8080
make realtime

# 3. inspect it from another shell
make status
make provision                 # dry run of the 18-profile catalogue
.venv/bin/python -m trading_platform strategies list

# 4. dashboard, outside Docker
make dashboard-install
API_ORIGIN=http://127.0.0.1:8080 make dashboard-dev     # http://127.0.0.1:3000

# 5. tight loop while working
make test-scoped AREA=tests/engine
.venv/bin/ruff check src/trading_platform/engine
.venv/bin/ruff format --check src/trading_platform/engine
```

`make realtime` spawns real `freqtrade trade` processes, because the host venv
installs freqtrade 2026.8 (`requirements-dev.txt`). The test suite never does:
the process launcher and the REST client are injectable and are replaced by test
doubles, so `pytest` runs without a worker, without a network and without an
exchange.

Before pushing, run the whole documented gate (`make check`) plus `make images`.

---

## 10. Deployment loop

Local deployment of the production-shaped stack:

```bash
docker compose -f deploy/docker-compose.yml up -d --build --wait
```

* `--build` rebuilds both images from this checkout (`context: ..`);
* `--wait` blocks until both healthchecks pass — the realtime image probes
  `GET http://127.0.0.1:8080/api/health`, the dashboard image probes `GET
  http://127.0.0.1:3000/`;
* the two named volumes (`trading-state`, `trading-cache`) survive the rebuild, so
  profiles, snapshots, trade databases and the OHLCV caches are kept;
* `deploy/.env` (git-ignored) is injected through `env_file:` — the images
  themselves contain no secret.

`.github/workflows/deploy.yml` does the same automatically on every push to
`main`, on the self-hosted runner, and then **smoke-tests the deployed stack**:

| Assertion | Contract |
| --- | --- |
| `GET http://127.0.0.1:3030/api/health` | `status == "ok"` and `profiles_running > 0`, within 60 s of container start |
| `GET http://127.0.0.1:3031/` | HTTP 200 (the dashboard UI) |
| `GET http://127.0.0.1:3031/api/health` | HTTP 200 (the dashboard's `/api/*` rewrite to the engine) |

**Why that holds on a first boot.** The container starts, the supervisor opens
`/app/data/realtime/state.db`; if that file carries a foreign schema (the
deployment this platform replaced left one behind) it is archived as
`state.db.legacy-<UTC timestamp>` and a fresh database is created. The 18-profile
catalogue is then seeded, the fleet scheduler starts **every enabled profile in
the same pass** and spawns one `freqtrade trade` worker for each — there is no
fleet cap and no queue. `/api/health` answers as soon as the API is
serving, and reports `profiles_running > 0` as soon as **one** worker is alive
and healthy — which is what the smoke test waits for, with twelve retries five
seconds apart.

Operational detail, the nginx front (TLS, Basic auth, `limit_req`, fail2ban),
the volumes and the rollback procedure are in `deploy/README.md`; the day-to-day
runbook is in `docs/operations.md`.

---

## 11. Operational limits

* **Every enabled profile runs.** The fleet is not capped and has no queue: the
  supervisor starts one `freqtrade trade` worker per enabled profile, in
  `priority` descending then `id` ascending order, in the same scheduling pass. A
  profile that is not `running` is `stopped`, `blocked` (live-trading gate unmet)
  or `error` (restart budget spent) — never "waiting for a slot".
* **Memory is the sum of every worker.** One running `freqtrade trade` process
  cost about **390 MiB RSS** (measured: 8 instances = 3.13 GiB, linear) on this
  host, and the Docker VM has **12 GiB** on a 16 GiB host that already swaps
  heavily. That figure is what an earlier revision measured while a cap was in
  place; it is not a bound now that the whole catalogue starts together. Measure
  the real footprint with `docker stats --no-stream trading-realtime` instead of
  scaling it by hand.
* **A worker is restarted only when it is proven gone.** A failed read does not
  restart anything: the supervisor waits out `WORKER_STARTUP_GRACE_SECONDS`
  (90 s) after a start, then requires `UNHEALTHY_THRESHOLD` (10) consecutive
  failed reads, and even then restarts only when the process handle is gone or
  `GET /ping` has failed `UNHEALTHY_PING_THRESHOLD` (10) consecutive times. The
  read timeout is `DEFAULT_TIMEOUT_SECONDS` (45 s), above the worst measured
  `GET /balance` (19.74 s). The generated freqtrade configuration sets
  `fiat_display_currency` to the empty string, so freqtrade never calls the
  CoinGecko price API and a `GET /balance` cannot be rate-limited into a timeout.
* **One supervisor, one writer.** The state database has a single writer (the
  supervisor process); two supervisors over the same file are unsupported.
* **One exchange account per mode.** Every profile of a mode trades the same
  freqtrade account context; a live profile uses the credentials of the
  environment, and paper profiles use the built-in dry-run wallet.
* **No high availability.** Two long-lived containers and one SQLite file: a
  restart is a short outage, and a deploy restarts every worker.

---

## 12. Documentation map

| Document | Content |
| --- | --- |
| `docs/architecture.md` | supervisor/fleet design, the full state-database schema, port allocation, the snapshot model, the health and restart policy, the live-trading safety gates |
| `docs/strategies.md` | the strategies: logic, parameters, suitable timeframes, risk notes |
| `docs/operations.md` | the runbook: compose commands, health checks, provisioning, the worker crash-loop fix, the kill switch, log locations, reading the state database |
| `docs/testing-policy.md` | the enforceable test and coverage policy (the gates quoted above) |
| `deploy/README.md` | the deployment: containers, volumes, nginx, TLS, fail2ban, automatic deploys |

Never commit a secret: `deploy/.env`, exchange keys and the operator token stay
out of the repository, out of the images and out of the logs.
