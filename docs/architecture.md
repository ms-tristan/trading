# Architecture

This document describes how the platform is built: the supervisor and its fleet,
the state database and its schema, the port allocation, the snapshot model, the
health and restart policy, and the gates that protect live trading.

The platform is **one container, one supervisor process, N freqtrade worker
processes, one SQLite database, one JSON API** — plus a stateless Next.js
dashboard that renders that API.

```
trading-realtime container
├── python -m trading_platform realtime run      <- the supervisor (pid 1's child)
│   ├── config + catalogue loaders               config/platform.json, config/*.json
│   ├── SQLite state store                       /app/data/realtime/state.db
│   ├── engine/config_builder.py                 one config.json per profile (0600)
│   ├── engine/supervisor.py                     fleet + subprocesses + restarts
│   ├── engine/client.py                         httpx client of each worker
│   ├── engine/poller.py                         snapshots + events
│   └── api/                                     FastAPI, http://0.0.0.0:8080
├── freqtrade trade --config .../profiles/<id>/config.json      (workers 1..N)
└── freqtrade trade --config .../profiles/<id>/config.json
```

---

## 1. The supervisor

`src/trading_platform/engine/supervisor.py` owns the fleet. **The supervisor
never trades**: it holds no position, it places no order and it never talks to an
exchange. Its whole job is to keep the right freqtrade processes alive and to
report what they say.

On boot, `python -m trading_platform realtime run` performs this sequence:

1. **load the platform settings** — the documented defaults, the keys present in
   `config/platform.json`, and the `TB_*` environment overrides, which win (§3.3
   resolves the whole order);
2. **open the state database** at `--state-db` (the image passes
   `/app/data/realtime/state.db`, `TB_REALTIME_STATE_DB` overrides the default)
   and verify its schema version; a foreign database is archived first, a
   database of this platform at revision `1` is migrated in place (§3);
3. **seed the state** — on a fresh database, the profiles of
   `config/profiles.json`; the settings are **not** seeded key by key, they are
   resolved at every boot by the precedence of §3.3;
4. **compute the schedule** — every enabled profile, sorted by `priority`
   descending then `id` ascending; the first `max_running_profiles` (default
   **6**) get a worker, the rest are marked `queued` with a `state_reason` naming
   the cap;
5. **generate one freqtrade configuration per scheduled profile** under
   `<state_dir>/profiles/<id>/config.json`, mode `0600` (§5);
6. **spawn one `freqtrade trade` subprocess per scheduled profile** — at most one
   new worker per `worker_start_stagger_seconds` (default **10**, §2) — and record
   its `pid`, its private API port and its start time in the `profiles` table;
7. **poll** every running profile on the snapshot interval and write snapshots
   and events (§4, §6);
8. **serve** the aggregated JSON API on `--host`/`--port`.

Scheduling continues while the platform runs: when a worker dies and its profile
is restarted the restart keeps its slot; when the operator stops a profile (or a
profile reaches the terminal state `error`) its slot is released and the next
`queued` profile — same order, still subject to the stagger gate of §2 — is
promoted automatically.

On `SIGTERM`/`SIGINT` the supervisor terminates every child gracefully (SIGTERM,
then SIGKILL after 15 s) before exiting, so a container stop never leaves an
orphan freqtrade process behind.

### Why a supervisor and not one process per profile

* **One aggregated surface.** N freqtrade REST APIs on N ports would have to be
  polled, merged and ranked by the dashboard. The supervisor does that once, in
  Python, and publishes one document.
* **One source of truth.** The catalogue, the fleet cap, the ranking and the
  kill switch are decisions that only make sense globally; a per-profile process
  cannot enforce them.
* **Bounded memory.** A worker is expensive (§11 of the README): only the
  supervisor knows how many are running, so it is the only place where the cap
  can be enforced.

---

## 2. The fleet and the workers

Each worker is a real `freqtrade trade` process:

```
freqtrade trade
  --config    <state_dir>/profiles/<id>/config.json
  --userdir   <state_dir>/profiles/<id>
  --db-url    sqlite:///<state_dir>/profiles/<id>/tradesv3.sqlite
  --logfile   <state_dir>/logs/<id>.log
  --strategy-path /app/user_data/strategies        (when that path exists,
                                                    else the repository-relative
                                                    user_data/strategies)
```

* the **`--config`** file is generated per profile (§5) and is the only place
  where the profile's strategy, timeframe, pair list and wallet are expressed;
* the **`--userdir`** keeps each profile's freqtrade state — including its own
  **OHLCV cache** under `data/` — in its own directory. Two workers therefore
  never share a candle file, which is what makes concurrent downloads safe;
* the **`--db-url`** gives each profile its own trade database, so a repair or a
  deletion of one profile's trades never touches another's;
* the **`--logfile`** is the per-profile log the runbook reads.

The supervisor talks to each worker over that worker's **private freqtrade REST
API** (`engine/client.py`, async httpx, HTTP Basic with the credentials it
generated): `GET /ping`, `/balance`, `/profit`, `/count`, `/status`, `/show_config`,
`/daily`, `/performance`, `/trades?limit=N`. It reads the real response keys
freqtrade 2026.8 returns and derives one metric itself:

```
portfolio_value = balance.total          (stake-currency valuation, holdings included)
cash            = balance.<stake>.free
positions_value = portfolio_value - cash
profit_abs      = profit.profit_all_coin
profit_pct      = (portfolio_value - initial_capital) / initial_capital
```

`profit_pct` is computed **by the platform**, not taken from freqtrade: freqtrade
measures its own percentage against its tradable balance, while the platform
measures it against the profile's declared `initial_capital` — the number the
operator actually put in the profile.

### Worker states

| State | Meaning |
| --- | --- |
| `running` | a worker process is alive and its last REST read succeeded |
| `queued` | enabled and eligible, but beyond the fleet cap or waiting for the stagger gate; `state_reason` names which |
| `stopped` | not running on purpose (operator action, disabled profile, kill switch) |
| `blocked` | refused by a safety gate; `state_reason` names the missing precondition (live trading) |
| `error` | the supervisor stopped restarting it; `last_error` carries the reason |

### Gradual start: `worker_start_stagger_seconds`

A cold start of the whole fleet is the one moment where memory and CPU peak
together. `worker_start_stagger_seconds` (integer, default **10**, minimum `0`,
in `config/platform.json` and in `PlatformSettings`, overridable through
`TB_WORKER_START_STAGGER_SECONDS`) bounds how fast the supervisor fills its slots:

* the supervisor starts **at most one new worker per stagger interval**; after a
  worker starts, the gate closes for that interval;
* every other profile that is **eligible to run but has to wait** stays in the
  state `queued` with a `state_reason` of exactly the form
  `queued: starting workers gradually (N of M slots in use)`, where N is the
  number of workers alive and M is `max_running_profiles`;
* a stagger of `0` restores the immediate behaviour: every eligible profile is
  started on the same pass;
* staggering only **delays promotions, it never reorders them** — the scheduling
  order stays `priority` descending, then `id` ascending.

The setting exists because one running worker costs about **390 MiB RSS**, the
Docker VM has **12 GiB** on a 16 GiB host that already swaps heavily, and a
simultaneous cold start of many workers spiked memory and CPU together and made
the guest unresponsive (reproduced twice). `deploy/docker-compose.yml` therefore
sets `TB_MAX_RUNNING_PROFILES=6` and `TB_WORKER_START_STAGGER_SECONDS=10`.

---

## 3. The state database

SQLite, one file, one writer: `/app/data/realtime/state.db` inside the
`trading-state` volume. The schema version is tracked with **`PRAGMA user_version
= 2`**. The database owns **six tables** — `profiles`, `profile_snapshots`,
`profile_trades`, `profile_daily`, `settings`, `events` — and three indexes:
`idx_profile_snapshots_ts`, `idx_events_ts` and `idx_profile_trades_open`.

One `StateStore` instance is shared by the API thread pool — a dashboard page
load fires several requests at once — and by the poller, which writes a tick from
the event loop. The single connection therefore issues every statement under an
internal lock, so two threads can never use it at the same time; the WAL journal
still lets the readers of another connection work while the poller writes.

### 3.1 `profiles` — one row per profile

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | TEXT PRIMARY KEY | profile identifier, e.g. `momentum-btc-1h` |
| `name` | TEXT | human label shown by the dashboard |
| `strategy` | TEXT | strategy id (`config/strategies.json`), e.g. `momentum` |
| `timeframe` | TEXT | freqtrade timeframe, e.g. `1h` |
| `mode` | TEXT | `paper` or `live` |
| `exchange` | TEXT | exchange name, e.g. `binance` |
| `pairs` | TEXT | JSON array of pairs, e.g. `["BTC/USDT"]` |
| `initial_capital` | REAL | starting cash (dry-run wallet for paper, reference capital for live) |
| `max_open_trades` | INTEGER | concurrent trades allowed for this profile |
| `priority` | INTEGER | scheduling rank: higher runs first when the fleet is full |
| `enabled` | INTEGER | `1`/`0`; a disabled profile is never scheduled |
| `source` | TEXT | `catalogue` (from `config/profiles.json`) or `operator` (API-created) |
| `state` | TEXT | `running`, `queued`, `stopped`, `blocked`, `error` |
| `state_reason` | TEXT | why the profile is in that state (cap, kill switch, live gate) |
| `api_port` | INTEGER | private freqtrade REST port of this profile's worker |
| `api_username` | TEXT | generated REST user of that worker |
| `api_password` | TEXT | generated REST password of that worker |
| `pid` | INTEGER | process id of the running worker, `NULL` when stopped |
| `started_at` | TEXT | ISO-8601 UTC start of the current run |
| `last_error` | TEXT | last fatal error seen for this profile |
| `created_at` | TEXT | ISO-8601 UTC creation timestamp |
| `updated_at` | TEXT | ISO-8601 UTC timestamp of the last write |

### 3.2 `profile_snapshots` — the history of every profile

| Column | Type | Meaning |
| --- | --- | --- |
| `profile_id` | TEXT | the profile this row belongs to |
| `ts` | TEXT | ISO-8601 UTC timestamp, rounded down to the minute |
| `portfolio_value` | REAL | total stake-currency valuation |
| `cash` | REAL | free stake-currency balance |
| `positions_value` | REAL | `portfolio_value − cash` |
| `profit_abs` | REAL | absolute profit since the profile started |
| `profit_pct` | REAL | profit against the profile's `initial_capital` |
| `realized_profit_abs` | REAL | profit of the closed trades |
| `unrealized_profit_abs` | REAL | profit of the open trades |
| `open_trades` | INTEGER | trades currently open |
| `closed_trades` | INTEGER | trades closed since the profile started |
| `win_rate` | REAL | share of winning closed trades |
| `profit_factor` | REAL | gross profit / gross loss |
| `max_drawdown_pct` | REAL | worst drawdown reported by the worker |
| `healthy` | INTEGER | `1` when the last poll of this profile succeeded |

Primary key: **`(profile_id, ts)`**. Index: **`idx_profile_snapshots_ts`** on
`profile_snapshots(ts)` — the cross-profile time series the aggregated equity
curve reads.

### 3.3 `settings` — platform settings

| Column | Type | Meaning |
| --- | --- | --- |
| `key` | TEXT PRIMARY KEY | setting document name — the platform uses one key, `platform_settings` |
| `value` | TEXT | JSON-encoded value: the whole settings document |
| `updated_at` | TEXT | ISO-8601 UTC timestamp of the last write |

There is **no per-key seeding**. `POST /api/settings` writes **one document row**
into this table under the key `platform_settings`, whose `value` is the entire
JSON settings document and whose `updated_at` moves on every write — so an
operator change survives a restart. `GET /api/settings` renders the **effective**
values (what the engine actually acts on) plus the path of the state database.

At boot the effective settings are resolved from four inputs, from weakest to
strongest:

1. the settings the supervisor was **constructed with** — the documented defaults;
2. the keys **actually present in `config/platform.json`**;
3. the **persisted `platform_settings` row**, when there is one;
4. the **`TB_*` environment variables** — they always win, because the environment
   is the only input an operator changes without writing to the state volume
   (`TB_MAX_RUNNING_PROFILES`, `TB_SNAPSHOT_INTERVAL_SECONDS`,
   `TB_WORKER_START_STAGGER_SECONDS`, `TB_PROFILE_API_PORT_BASE`, …). That is the
   documented way to change the fleet cap in Docker.

`POST /api/settings` accepts `max_running_profiles`, `snapshot_interval_seconds`
and `worker_start_stagger_seconds` (§2).

### 3.4 `events` — the engine journal

| Column | Type | Meaning |
| --- | --- | --- |
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | monotonic event id |
| `ts` | TEXT | ISO-8601 UTC timestamp |
| `profile_id` | TEXT | the profile concerned, `NULL` for a platform-wide event |
| `level` | TEXT | `info`, `warning` or `error` |
| `kind` | TEXT | event kind: `start`, `stop`, `crash`, `restart`, `cap_reached`, `kill_switch`, `legacy_db_archived`, … |
| `message` | TEXT | human-readable, English, credential-free description |

Index: **`idx_events_ts`** on `events(ts DESC)` — the read model behind
`GET /api/events?limit=` and the operations page of the dashboard always wants the
newest rows first.

### 3.5 `profile_trades` and `profile_daily` — the per-profile read model

Two tables carry what each worker reported about its own trades, so that the
profile detail endpoint answers from the state database instead of from a live
worker and therefore also answers for a profile that is stopped:

| Table | Content | Index |
| --- | --- | --- |
| `profile_trades` | one row per trade of a profile as its worker reported it, open and closed; the read behind `open_trades` and `recent_trades` of `GET /api/profiles/{id}` | `idx_profile_trades_open` |
| `profile_daily` | one row per daily profit report of a profile; the read behind `daily` of `GET /api/profiles/{id}` | — |

Both are scoped by `profile_id` and are written by the poller alongside the
snapshots, so they follow the same lifecycle as `profile_snapshots`: they grow
with the fleet and are dropped with the state volume.

### 3.6 Schema versioning and the legacy-database archive

The schema version lives in **`PRAGMA user_version`**, and the supported value is
**`2`**.

On boot the store inspects the file before touching it:

* **no file, or an empty file** → create the six tables and their indexes, set
  `user_version = 2`;
* **`user_version = 1` with the platform's `profiles` columns** → the database is
  **migrated in place**: the two new tables (`profile_trades`, `profile_daily`)
  and their index are created and the version is stamped `2`. The file is **never
  archived, never renamed and no data is lost**;
* **`user_version = 2`** → normal start;
* **a `profiles` table that is not the platform's, or a `user_version` that is
  neither `1` nor `2`** → the file is a **foreign schema** (typically the state
  database of the platform this one replaces, which the `trading-state` volume
  still holds). It is **renamed** to

  ```
  state.db.legacy-<UTC timestamp>      e.g. state.db.legacy-20260927T173000Z
  ```

  in the same directory with its `-wal`, `-shm` and `-journal` sidecars, and a
  **fresh** database is created next to it. The old
  file is never parsed, never migrated and never deleted, and an event of kind
  `legacy_db_archived` records what happened. This is what makes the first boot
  of a deployment deterministic: an unrelated `state.db` cannot abort the
  container, and it cannot be silently overwritten either.

---

## 4. The snapshot model

The poller (`src/trading_platform/engine/poller.py`) runs every
`snapshot_interval_seconds` (default **60**, `TB_SNAPSHOT_INTERVAL_SECONDS`):

1. for every **running** profile, read the worker (`/balance`, `/profit`,
   `/count`, `/show_config`) and write **one snapshot row**, with `ts` **rounded
   down to the minute**, as an `INSERT OR REPLACE` on the `(profile_id, ts)`
   primary key. A repeated poll inside the same minute therefore *replaces* the
   row instead of appending a duplicate, and a restart never creates a second row
   for a minute that already has one;
2. for a **non-running** profile, write a snapshot only when its state or its
   value actually changed — so `queued`, `blocked`, `stopped` and `error`
   profiles appear in the history when something happened to them and stay
   silent otherwise;
3. **never invent a snapshot for a stopped profile.** No row is written for the
   minutes in which a profile was not running, and no value is carried forward.
   The equity curves of the dashboard therefore show gaps for the periods a
   profile was down, which is the honest reading — a flat line would claim the
   capital was still being managed;
4. record the engine events of the interval (`start`, `stop`, `crash`,
   `restart`, `cap_reached`, `kill_switch`, `legacy_db_archived`) in `events`.

The aggregated equity curve of `GET /api/account` is built from
`profile_snapshots` over the requested window (`24h`, `7d`, `30d`, `all`) — the
reason the `idx_profile_snapshots_ts` index exists. Retention follows
`equity_retention_days` in `config/platform.json`.

---

## 5. Generated freqtrade configuration

Written by `src/trading_platform/engine/config_builder.py` to
`<state_dir>/profiles/<id>/config.json` with **mode `0600`** — it holds generated
credentials, so the file is readable by the `ftuser` process only.

The builder emits every key freqtrade 2026.8 validates; a missing key aborts the
worker, so the list is exhaustive on purpose: `max_open_trades`,
`stake_currency`, `stake_amount`, `tradable_balance_ratio` (`0.99`),
`fiat_display_currency`, `dry_run`, `dry_run_wallet` (paper profiles only),
`trading_mode` (`"spot"`), `margin_mode` (`""`), `timeframe`, `unfilledtimeout`,
`entry_pricing`, `exit_pricing`, `exchange`, `pairlists` (`StaticPairList`),
`api_server`, `initial_state` (`"running"`), `internals`, `bot_name`.

Points an operator or a debugger should know:

* **`dry_run` follows the profile mode.** A `paper` profile gets `dry_run: true`
  and `dry_run_wallet = initial_capital` — the profile's starting cash *is* the
  freqtrade dry-run wallet. A `live` profile gets `dry_run: false` and reaches
  `stake_amount` through the normal `max_open_trades` sizing;
* **each worker gets its own REST credentials.** `api_server` is enabled, bound
  to `127.0.0.1` on the profile's allocated port, with a generated
  `username`/`password` pair, a `jwt_secret_key` of at least 32 characters
  (`secrets.token_hex(32)`) and its own `ws_token`. They are stored in the
  `profiles` row, never logged and never returned by the API;
* **no `telegram` block is emitted**: freqtrade 2026.8 requires `telegram.token`
  whenever the key is present, so the platform simply does not configure it;
* **`internals.process_throttle_secs` follows the timeframe** — 5m → 5 s,
  15m → 15 s, 1h → 30 s, 4h and 1d → 60 s. A worker on a slow grid has no reason
  to wake up more often;
* real exchange keys are injected into the `exchange` block **at run time, from
  the environment only**, for live profiles that passed the gates of §8.

---

## 6. Port allocation

Every profile owns a **deterministic private REST port**:

```
api_port = profile_api_port_base + index
```

where `profile_api_port_base` defaults to **8101**
(`TB_PROFILE_API_PORT_BASE`) and `index` is the profile's position in the
**catalogue list sorted by `id` ascending** — not in the scheduling order, not in
creation order. The rule is therefore stable across restarts, across deploy
rounds and across machines, and it never depends on which profiles happen to be
running: a profile that is queued today and promoted tomorrow keeps the port it
already had.

All of those ports are bound by the generated configuration to
**`127.0.0.1` inside the container** (`api_server.listen_ip_address`) and are
never published by compose. Only two ports cross the container boundary:

| Port | Service | Published |
| --- | --- | --- |
| 8080 | aggregated JSON API of the supervisor | `127.0.0.1:3030` (loopback, debugging) |
| 3000 | dashboard | `127.0.0.1:3031` (the nginx upstream) |

---

## 7. Health and restart policy

A worker is considered **healthy** while its REST reads succeed. The supervisor
keeps a per-profile failure counter:

| Condition | Reaction |
| --- | --- |
| 3 consecutive failed reads | the profile is **unhealthy** (snapshot `healthy = 0`, state stays `running` until the restart decision) |
| unhealthy, restart budget available | the worker is terminated and **restarted with exponential backoff: 5 s, 15 s, then 45 s** between attempts |
| **5 restarts within 15 minutes** | the supervisor gives up on the profile: state **`error`**, `last_error` carries the last failure, and the slot is released for the next `queued` profile |
| success after a restart | the counter and the backoff reset; the event journal records `restart` |

The policy is intentionally impatient at the read level (three failures is a few
seconds on any timeframe) and patient at the process level (45 s of backoff
before giving up on a flapping worker), because the usual causes are an exchange
outage or a rate limit, and both pass.

Supervisor shutdown is the other half of the policy: on `SIGTERM`/`SIGINT` every
child receives `SIGTERM`, and a child still alive after **15 s** receives
`SIGKILL`. Containers stop in bounded time and never leave an orphan worker.

---

## 8. Live-trading safety gates

Live trading is refused by default. A profile whose `mode` is `live` is started
only when **all** of the following hold:

| Gate | Check |
| --- | --- |
| Explicit acknowledgement | `TB_ALLOW_LIVE_TRADING == "I_UNDERSTAND_THE_RISK"` (exact comparison) |
| Exchange key | `TB_LIVE_EXCHANGE_KEY` is present in the environment |
| Exchange secret | `TB_LIVE_EXCHANGE_SECRET` is present in the environment |

When a gate is missing, the profile's state is **`blocked`** and `state_reason`
names the missing precondition — it is never started, and the failure is visible
in `GET /api/profiles`, in `make status` and on the profile page of the
dashboard. `POST /api/catalogue/apply` behaves the same way: a live catalogue
entry whose gates are unmet is reported in `refused_live` and **not created**,
rather than created in a state where its orders could not be authenticated.

Three further rules complete the model:

* **credentials come from the environment only.** No key is ever written into a
  committed file, into `config/profiles.json`, into a generated configuration
  file (those hold only the *generated* REST credentials of the worker), into a
  log line or into an API response;
* **the kill switch wins over everything.** When `<state_dir>/KILL_SWITCH` exists
  (created by `POST /api/kill-switch {"engaged": true}`), every worker is stopped,
  every running profile becomes `stopped` with the reason `kill_switch`, and no
  worker starts while the file is present — including after a container restart,
  because the file lives in the `trading-state` volume;
* **the supervisor cannot trade.** It never places an order; starting a live
  profile only means spawning a freqtrade worker configured with the credentials
  of the environment.

---

## 9. The aggregated JSON API

FastAPI (`src/trading_platform/api/`), all routes prefixed `/api`, no CORS (the
dashboard proxies `/api/*` from its own origin). Read routes are public; mutating
routes require `X-Operator-Token` equal to `TB_OPERATOR_TOKEN` — a **missing**
header is `401`, a **wrong** one is `403`.

| Route | Purpose |
| --- | --- |
| `GET /api/health` | `status` (`ok` when at least one profile runs, `degraded` otherwise), version, uptime, profile counters, slot usage, kill-switch flag |
| `GET /api/account?window=` | aggregated paper / live / combined performance and the equity curve |
| `GET /api/profiles` | the ranked profile list (default sort: portfolio value descending; `rank` is assigned across both modes before filtering) |
| `GET /api/profiles/{id}` | one profile with its equity curve, daily profit, open and recent trades and strategy |
| `GET /api/strategies` | the discovered strategy catalogue with its aggregated figures |
| `GET /api/events?limit=&since=` | the engine journal |
| `GET /api/settings` | effective platform settings and the state database path |
| `POST /api/profiles` · `PATCH` · `DELETE` | operator-owned profile lifecycle |
| `POST /api/profiles/{id}/actions` | `start`, `stop`, `restart` |
| `POST /api/catalogue/apply` | idempotent upsert of `config/profiles.json` |
| `POST /api/kill-switch` | engage or release the global kill switch |
| `POST /api/settings` | change the fleet cap, the snapshot interval and the worker start stagger at run time |

`GET /api/health` is the surface the container healthcheck and the deploy smoke
test use: it answers as soon as the API serves, and `profiles_running > 0` as
soon as one worker is alive and healthy.

`POST /api/settings` accepts `max_running_profiles`, `snapshot_interval_seconds`
and `worker_start_stagger_seconds`; what an operator writes there is what
`GET /api/settings` renders as effective, and it is what the engine acts on
(§3.3).

### The profile view: `sparkline`, `slot`, `worker_port`

Three additive fields complete the `ProfileView` the profile routes return:

| Field | Meaning |
| --- | --- |
| `sparkline` | the last **60** equity points of the profile, oldest first; `[]` when the profile has no snapshot yet |
| `slot` | the **1-based** position of the profile among the running profiles in scheduler order; `null` when it is not running |
| `worker_port` | the private worker REST port while the profile runs; `null` otherwise |

They are read-model conveniences, not new state: `slot` is derived from the same
`priority` descending, `id` ascending order the fleet scheduler uses, and
`sparkline` is a window over `profile_snapshots`.

### The profile detail payload comes from the state database

`GET /api/profiles/{id}` reads its three per-profile collections from the state
database, so they answer for a profile that is **stopped** as well — not only
while a worker is alive:

* `daily` — up to **30** daily rows, chronological, most recent last;
* `open_trades` — the trades currently open, **newest first**;
* `recent_trades` — the **20** most recent closed rows, newest first.

This is what `profile_trades` and `profile_daily` exist for (§3.5): the API never
has to reach a worker that may be gone.

---

## 10. Module map

| Path | Responsibility |
| --- | --- |
| `src/trading_platform/__main__.py` | CLI (`realtime run`, `realtime provision`, `realtime status`, `strategies list`) and the `trading` console script |
| `src/trading_platform/config.py` | platform settings, environment overrides, supported timeframes |
| `src/trading_platform/models.py` | domain models and the JSON shapes of the API |
| `src/trading_platform/metrics.py` | portfolio aggregation, profit percentage, ranking |
| `src/trading_platform/paths.py` | state, profile, log and catalogue path resolution |
| `src/trading_platform/logging_setup.py` | structured logging, `TB_LOG_LEVEL` |
| `src/trading_platform/profiles/catalogue.py` | discovery of `user_data/strategies/*.py` and the strategy metadata of `config/strategies.json` |
| `src/trading_platform/profiles/store.py` | the SQLite state store, schema version, snapshots, events, legacy archive |
| `src/trading_platform/engine/config_builder.py` | the generated per-profile freqtrade configuration |
| `src/trading_platform/engine/client.py` | the async freqtrade REST client |
| `src/trading_platform/engine/supervisor.py` | fleet scheduling, subprocesses, restarts, shutdown |
| `src/trading_platform/engine/poller.py` | the snapshot loop and the health counter |
| `src/trading_platform/api/` | FastAPI application, routes, schemas, operator-token security |

Layer direction is one-way: `api` → `engine` → `profiles` → `config`/`models`.
`engine` and `profiles` never import `api`, so the fleet can be exercised without
an HTTP server and the API can be tested without spawning a worker.

---

## 11. What the architecture does not do

* no high availability: one supervisor, one SQLite writer, a restart is an outage;
* no shared capital across profiles: every profile has its own dry-run wallet,
  and its own freqtrade trade database;
* no shared OHLCV cache yet: each worker caches its own candles under its profile
  directory (`/app/data/realtime/profiles/<id>/data`); the `trading-cache` volume
  (`/app/data/cache`) is created and owned for a future shared cache;
* no parameter sweep: `config/profiles.json` selects a **strategy id**, not a
  parameter set, so every profile of a strategy runs its published defaults;
* no message queue and no scheduler beyond the fleet: restart, promotion and
  snapshot logic all live in the supervisor process.
