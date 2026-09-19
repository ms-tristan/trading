# Real-time multi-profile

This document describes the real-time platform delivered with the skeleton:

- **`trading_platform.realtime`** (layer 6) — engine: market streams, broker,
  execution gateway, risk, persistence, orchestrator, observability;
- **`trading_platform.web`** (layer 7) — HTTP transport, **standard library
  only**: a pure JSON API (no HTML, no static asset);
- **`trading_platform.cli`** (layer 8) — the three commands
  `realtime run`, `realtime serve` and `realtime check`.

It complements [`docs/architecture.md`](architecture.md) (§3 for the layers,
§4.10 and §4.11 for the inventory of frozen interfaces, §6.3 for the error tree)
and [`docs/usage.md`](usage.md) (copy-pasteable examples).

---

## 1. What is a profile?

A **profile** is a self-contained trading strategy: `asset + strategy +
timeframe + paper/live + risk limits`. Every profile is configured, run and
monitored **independently** of the others; N profiles run inside the same
process, on the same persistent state, sharing neither positions nor counters.

One thing **is** shared: the USDT cash. Every profile funds its orders from
**one** platform wallet, and the `allocation` of a profile is the part of that
wallet attributed to it (§9). Nothing else is pooled.

A profile is described by one JSON object of the profiles file (see
`config/profiles.example.json`) and validated by `config.models.ProfileConfig`,
which rejects any unknown key (`extra="forbid"`):

| `ProfileConfig` field | Role |
| --- | --- |
| `id` | profile identity (`^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$`): primary key of the state and of the log records |
| `symbol` | traded pair, for example `BTC/USDT` |
| `timeframe` | `1m`, `5m`, `15m`, `30m`, `1h`, `4h` or `1d` |
| `strategy` | name in the `strategy.registry.get_strategy` registry (never reimplemented) |
| `params` | strategy parameters (same conventions as `AppConfig`) |
| `mode` | `paper` (simulated) or `live` (real, subject to a condition, see §3) |
| `initial_balance` | initial capital of the profile |
| `allocation` | the profile's share of the **one shared platform wallet** (optional: when it is absent, `initial_balance` **is** the allocation) |
| `stake_amount` | amount committed per entry (default: the whole available balance) |
| `exchange` | name of the execution venue |
| `enabled` | a disabled profile is persisted but never started |
| `warmup_candles` | number of past candles the strategy receives on every decision |
| `poll_interval_seconds` | polling cadence specific to the profile |
| `risk` | `RiskLimitsConfig` block (§3) |
| `entry_lookback_candles` | live-only catch-up window: the entry decision may act on a crossover that occurred within the last N candles (0, the default, keeps the historical behaviour: only the last row decides); ignored by the backtest, which already reads every row; 0 <= N <= 200 |
| `forecast` | path of the **offline forecast artifact** the profile consumes (built by `trading forecast-build`, see [`forecasting.md`](forecasting.md)); `null` by default. It is **required** by a `timesfm` profile and refused loudly at startup when it is missing, corrupt, stale or built for another symbol/timeframe; a `basic` profile never reads it, and `"forecast": null` is exactly the historical behaviour |

The complete document carries three root keys: `profiles`, `realtime`
(`RealtimeConfig`: state base, directories, CSV or cache provider, `start_at`
anchor, delays, reconnections, benchmark) and `monitoring`
(`MonitoringConfig`: `host`, `port`, `refresh_seconds`,
`request_timeout_seconds`, `max_request_bytes`). Any other root key is
rejected.

## 2. One execution path for paper and live

There is **one** `ExecutionGateway` and **one** `ProfileRunner`. Paper and live
differ only in three things:

1. **the injected broker** — `PaperBroker` (simulated venue: execution at the
   reference price adjusted for slippage, in-house fees, deterministic partial
   fills driven by an explicit seed) or `CcxtBroker` (real venue, **lazy**
   `import ccxt`, idempotent on `clientOrderId`);
2. **the live gate** (`LiveTradingGate`) — a `live` profile is armed only if the
   environment carries exactly
   `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`;
3. **the configuration**.

The gateway contains **no** "if paper / if live" branch: it routes to the
injected `Broker`. The paper broker is therefore exercised by exactly the same
tests as the shared life cycle.

### 2.1 One shared wallet for every profile

The broker owns **no** cash any more. `PaperBroker` **delegates** every cash
movement to the one `PlatformWallet` injected into it (§9), and reaches it
through that injected seam only — never through a module-level global. A paper
venue therefore spends the same USDT ledger as every other profile of the
process, and the platform — not the profile — is the accounting entity.

The wallet is thread-safe: the profiles run in **separate threads** and every
read-modify-write of the cash (`cash = cash - notional - fee`) happens under one
re-entrant lock, so two fills can neither lose an update nor spend the same unit
twice, and the durable write happens inside the same critical section.

In **live** mode the wallet *is* the venue's account: `CcxtBroker` already
fetches that balance, the wallet **mirrors** it, and it is **read-only** — it is
never debited locally, and a local debit is refused with `WalletError` instead of
inventing a balance. `source: "venue"` in the API payload says so (§5, §9).

### 2.2 Real-time / backtest equivalence

The execution contract is the one of `strategy.engine`: **the decision is
evaluated at the close of candle `t` and executed at the open of `t+1`**,
slippage always working against the trader. In real time, the close of `t`
**is** the trigger instant: the stream emits only closed candles
(`timestamp <= now - candle_delta(timeframe)`) and the reference price of a
decision is the **close of `t`** — that is, the open of `t+1` at the granularity
of one candle. Accepted consequence: a live profile reacts **one timeframe
boundary after the signal**, exactly like the backtest engine, and the two
engines do not produce the same numbers (only the **signals** are the contract).

**Live streams emit the most recent closed candle, never an old candle.**
`PollingMarketStream` and `CcxtProMarketStream` skip the candles whose close was
already known when the engine started — or during an outage — and report it
through the structured event `market_data.candles_skipped`. This is not an
implementation detail: the engine warms the strategy up on `history()` (a window
that ends **now**) and then appends the emitted candle to that window; emitting
the oldest candle of the lookback window produced a single-row frame
(`warmup_incomplete`) and, once the frame had grown long enough, made the engine
decide on a candle several days old **while filling it at the current day's
price**. Deterministic replay of a past window is the job of
`ReplayMarketStream` (`realtime run --once`, tests), never that of a live
stream.

### 2.3 Mandatory reuse

The real-time engine **implements no formula**. It assembles:

- the strategies of the registry (`strategy.registry.get_strategy` + `Strategy.run`);
- the data (`data.loader` providers and `data.cache`) and
  `data.validation.ensure_ohlcv` for **every** frame entering the engine;
- the metrics and benchmarks (`metrics.compute_metrics`,
  `metrics.compute_benchmark`, `metrics.compare_benchmark`, `benchmark_alpha`,
  `validation.validate_benchmark`) through the `realtime.monitor` read model;
- the domain models (`core.models.TradeRecord`, `BacktestResult`);
- the pydantic configuration layer;
- the Freqtrade bridge (`strategy.freqtrade_adapter.make_freqtrade_strategy`),
  exposed by `realtime.strategies.freqtrade_strategy_for(profile)` (lazy import,
  returns `None` when the extra is absent): **the same profile definition can be
  handed to Freqtrade unchanged**;
- the **feature-injection seam** (`strategy.features.resolve_features` /
  `attach_features`), reached through `realtime.features.resolve_profile_features`:
  a profile that declares a `forecast` artifact receives it exactly as it would in
  a backtest (§3.1).

## 3. The safety model

1. **Explicit live opt-in.** A `live` profile requires both `mode: "live"` **and**
   `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`; otherwise
   `LiveTradingForbiddenError`.
2. **Secrets from the environment only.** `TB_LIVE_API_KEY` /
   `TB_LIVE_API_SECRET` / `TB_LIVE_API_PASSWORD`, or, per profile,
   `TB_PROFILE_<ID>_API_KEY` / `_API_SECRET`. The configuration models have
   **no** credential field: a profile containing `api_key` fails loudly
   (`ConfigError`). `ExchangeCredentials.__repr__` and every structured log
   entry mask secrets.
3. **Per-profile limits, evaluated before the call to the broker**
   (`RiskLimitsConfig` → `RiskLimits`): `max_position_notional`,
   `max_order_notional`, `max_open_positions`, `max_daily_loss`,
   `max_drawdown_pct`, `max_daily_trades`. The evaluation order is frozen and the
   **first** failure wins; every rejection is logged with its reason, and the
   profile is not watermarked (a rejection is a decision, not a crash).
4. **The shared-wallet funding check, then the optional platform-wide caps.**
   Every entry is funded from the one shared wallet (§9) and the risk manager
   verifies the ledger **before** the broker is called. The check **always**
   applies — even on a configuration that declares no platform cap at all — and
   it refuses an order the wallet cannot fund, with the limit name
   `platform_wallet` and the reason
   `platform wallet cannot fund order: requires 12.50 USDT, available 3.00 USDT`.
   On top of it, two **optional** keys of the `realtime` block cap the whole
   platform, in addition to the per-profile limits above:
   `realtime.platform_max_total_notional` (the exposure aggregated over every
   profile) refuses an entry with the limit name `platform_max_total_notional`
   and the reason
   `platform exposure 1200.00 exceeds platform_max_total_notional 1000.00`, and
   `realtime.platform_max_daily_loss` (the daily loss aggregated over every
   profile) refuses an order with the limit name `platform_max_daily_loss` and
   the reason
   `platform daily loss 60.00 exceeds platform_max_daily_loss 50.00`. An absent
   key means "not enforced" — never a cap of `0`, and never a missing funding
   check.
   The **evaluation order is frozen**, and the first failure wins:
   `kill_switch`, `platform_wallet`, `platform_max_total_notional`,
   `platform_max_daily_loss`, `max_order_notional`, `max_position_notional`,
   `max_open_positions`, `max_daily_loss`, `max_drawdown_pct`,
   `max_daily_trades`. The two platform exposure checks and the funding check are
   entry checks: an order that **closes** a position is never refused for lack of
   cash or for lack of exposure budget, while the two loss caps and every
   per-profile limit keep applying to it. A refusal leaves **nothing**
   half-applied: no order row, no fill, no position and **no cash movement** — the
   reason is what `last_block_reason` of the `ProfileSnapshot` carries (§5, §9).
5. **Global kill switch** (`KillSwitch`), in three forms: file
   (`realtime.kill_switch_file`), environment, and API (`POST /api/kill-switch`).
   It stops every profile, **cancels nothing silently**, and it is persisted in
   the state: a restart does not reset it. A kill switch forced by a file or by
   the environment cannot be released through the API.
6. **Unforgeable paper/live separation.** A paper profile can **never** be routed
   to a real broker: the mode is part of the identity of the profile and of every
   persisted order, and the broker checks its own mode.

### 3.1 The forecast startup guard

A forecast-driven profile is **refused at startup**, never started to run inert.
`realtime.features.resolve_profile_features(profile, required=True)` loads the
declared artifact **once**, through the project's single loader
(`strategy.features.resolve_features`), attaches it to the strategy in
`resolve_strategy` through the shared `attach_features` seam, and then runs
`realtime.features.check_profile_forecast`. Four checks run in order, and each
raises `ForecastArtifactError` with an actionable message:

1. a feature bundle with no forecast behind a declared path is a caller bug, and
   still surfaces loudly;
2. **symbol mismatch** — the artifact was built for another instrument;
3. **timeframe mismatch** — the artifact was built on another candle duration;
4. **staleness** — the artifact's coverage can no longer reach the profile's
   decision horizon. This is the reason the guard exists.

A `timesfm` profile that declares **no** `forecast` path at all fails just as
loudly, because a profile that starts and then never trades, silently and
forever, is the exact failure this contract removes:

```
profile 'btc-timesfm-paper' uses strategy 'timesfm', which needs a forecast artifact,
but declares no 'forecast' path; build one with 'trading forecast-build' and add
'"forecast": "<artifact>.parquet"' to the profile
```

The staleness refusal, verbatim (one sentence, wrapped here for readability):

```
the forecast artifact data/forecast/forecast.parquet is stale for profile 'btc-timesfm-paper':
its last origin is 2026-09-01T00:00:00+00:00 and the profile decision horizon only stays covered
until 2026-09-02T00:00:00+00:00 (now is 2026-09-03T00:00:00+00:00); coverage must reach the 1h
decision horizon, so rebuild it with 'trading forecast-build --symbol BTC/USDT --timeframe 1h'
and point the profile at the new file
```

The bound is derived from the profile's **own** declarations rather than from a
constant: the guard reads `min_lead` and `forecast_age` from the merged strategy
parameters and `horizon` / `stride` / `timeframe` from the artifact metadata, so
`age_bound = min(forecast_age, horizon − 1 − min_lead)` and the artifact stays
usable until `last_origin + (age_bound − 1) × stride × candle_delta(timeframe)`.
The comparison is `now > usable_until`: the boundary instant itself **passes**.

Because the guard runs inside `resolve_strategy`, and every entry point of a
profile — `ProfileRunner.start`, `ProfileRunner.run` / `run_once`,
`realtime run`, `realtime run --once`, `realtime check` and the orchestrator —
goes through it, the refusal happens **before the first candle**. The error is a
`TradingBacktestError`, so the CLI error surface already renders it; it is never
wrapped and never swallowed. A profile that declares no forecast (every `basic`
profile of the platform, including the two shipped examples) keeps the exact
previous behaviour: its bundle is empty, nothing is loaded, nothing is checked.

Ask the same question **before** starting the engine with
`trading forecast-info --profiles <file> [--profile <id>]`, which reuses this very
guard and prints the same message (§6).

## 4. Persistence, restart and reconciliation

- The state lives in **a single SQLite file** (`realtime.state_db`, default
  `data/realtime/state.db`, ignored by git), in WAL mode, one connection per
  calling thread, **every write inside a transaction**.
- Every write is **idempotent**: UPSERT on the natural key, and the order
  identifier is **deterministic** — `new_client_order_id(profile_id, symbol,
  candle_timestamp, sequence)` — so the same decision always produces the same
  `client_order_id`. A restart between submission and fill **never** duplicates
  an order.
- The **last processed candle** is persisted per profile: a restart neither
  replays a candle nor skips one.
- On startup, the orchestrator **reconciles** the local state against the
  execution venue (`Broker.reconcile()`) and marks the profile `degraded` in case
  of a discrepancy. Fills are reconciled at a bounded interval
  (`realtime.reconcile_interval_seconds`).
- The store is **single-writer**: a second orchestrator on the same file fails
  with `StateStoreError` (a `flock` lock taken on `<base>.lock`). This lock is
  **advisory**: it protects two `SqliteStateStore` instances, not a third-party
  process that would write into the file while bypassing the store. A newer
  schema version also raises `StateStoreError` instead of writing blindly.
- A reconciliation discrepancy on an **in-memory** venue is expected after a
  restart: the paper broker does not know the orders it has not seen, so the
  profile starts again as `degraded` — the local state itself is intact and
  nothing is resubmitted. This is the documented behaviour of D7, not a data
  loss.
- **One** shared wallet holds the USDT cash, and it is the only thing that can
  fund an order (§9). It is a single row of the `wallet` table — schema version
  **3**, `wallet_id = 1` enforced by a `CHECK` — written through
  `StateStore.save_wallet` after every accepted fill and restored **once** at
  startup (`PlatformWallet.restore`), before the first candle of the first
  profile. A restart therefore **never** resets the cash. When the store holds no
  row yet, the wallet starts from `realtime.platform_initial_balance`, or, when
  that optional key is absent, from the **sum of the profiles' allocations**
  (§9), which is exactly the total cash the profiles used to own separately.
  Opening an older database simply migrates it: schema version `2` gains the
  empty `wallet` table and **keeps every row** it already had, and the wallet is
  then initialised as above — a deployed database opens, migrates and loses
  nothing.
- The old **per-profile reseed is gone**: `PaperBroker` no longer owns a balance
  of its own, so there is no simulated cash to reseed from the last equity point.
  The broker debits and credits the injected wallet, and the wallet is the
  persisted truth. A **real** venue is never reseeded locally either: the wallet
  *mirrors* the account the venue publishes (`CcxtBroker.fetch_balance`,
  best-effort, once at startup) and stays read-only (§9).

## 5. Web API reference

Server `http.server.ThreadingHTTPServer`, JSON everywhere, UTC ISO-8601
timestamps, **no NaN and no Infinity** in any payload. The server serves **no
HTML page and no static asset**: every path outside the table below — `GET /`
and `GET /static/{asset}` included — answers the documented JSON 404
`{"error": "not found: <path>"}`, exactly like any other unknown route.

| Method and route | 200 response | Errors |
| --- | --- | --- |
| `GET /api/health` | `{status, version, uptime_seconds, profiles_total, profiles_running, kill_switch, checked_at, wallet}` | — |
| `GET /api/profiles` | `{profiles: [ProfileSnapshot…], generated_at, wallet}` | — |
| `GET /api/profiles/{id}` | `ProfileSnapshot` | 404 `{error}` |
| `GET /api/profiles/{id}/equity` | `{points: [{timestamp, equity, cash, position_value}…]}` | 404 |
| `GET /api/profiles/{id}/trades` | `{trades: [...], count}` | 404 |
| `GET /api/profiles/{id}/orders` | `{orders: [...]}` | 404 |
| `GET /api/profiles/{id}/positions` | `{positions: [...]}` | 404 |
| `GET /api/profiles/{id}/metrics` | `{metrics: {...}, benchmark: {...} \| null, generated_at}` | 404 |
| `GET /api/kill-switch` | `{kill_switch, reason, changed_at}` | — |
| `POST /api/kill-switch` | body `{engage: bool, reason: str}` → `{kill_switch, reason, changed_at}` | 400 malformed body, 403 missing/invalid token, 403 server in read-only mode |
| `GET /api/profiles/{id}/candles?limit=N` | `{candles: [{profile_id, timestamp, open, high, low, close, volume, closed}…], count}` (oldest first) | 404 unknown profile, 400 malformed `limit` |
| `GET /api/catalog` | `{symbols: [{symbol, base, quote}…], strategies: [...], timeframes: [...], modes: [...]}` | — |
| `GET /api/control` | `{engine_running, read_only, mutable, profiles: [{profile_id, paused, running}…]}` | — |
| `POST /api/profiles/{id}/pause` | `{profile: ProfileSnapshot, paused: true}` | 400, 403, 404, 409, 503 |
| `POST /api/profiles/{id}/resume` | `{profile: ProfileSnapshot, paused: false}` | 400, 403, 404, 409, 503 |
| `DELETE /api/profiles/{id}` | `{profile_id, deleted: true}` | 400, 403, 404, 409, 503 |
| `POST /api/profiles` | body `{profile_id, symbol, timeframe, strategy, mode, initial_balance?, params?}` → `201 {profile: ProfileSnapshot}` | 400 malformed body / unknown strategy / unsupported timeframe, 403, 409 duplicate |

`wallet` is the **additive** platform-wide view of the one shared wallet (§9):

```
{"name": str, "mode": "paper"|"live", "initial_balance": num|null, "cash": num|null,
 "equity": num|null, "deployed": num|null, "realized_pnl": num|null,
 "unrealized_pnl": num|null, "total_exposure": num|null, "profiles": int,
 "source": "local"|"venue", "updated_at": str|null}
```

`source` is `"local"` for the simulated ledger and `"venue"` when the wallet
mirrors a real account (live mode, read-only). `profiles` counts the profiles
aggregated into the view. Both providers publish it: the engine reports the
in-process wallet, and the read-only adapter of `realtime serve` rebuilds it from
the persisted `wallet` row (§4), so the read-only surface shows the durable cash
and the attributed per-profile figures too. A provider whose store holds **no**
wallet row yet answers `"wallet": null`: the key is
always present, so a consumer never has to guess whether the wallet is missing or
simply empty, and no payload ever carries `NaN` or `Infinity` — a non-finite
number is rendered `null`.

A `ProfileSnapshot` carries every key it always carried — `profile_id`, `symbol`,
`timeframe`, `strategy`, `mode`, `status`, `initial_balance`, `equity`, `cash`,
`position_value`, `total_return`, `n_trades`, `open_positions`, `health`,
`started_at`, `updated_at` — plus, **additively**, the attributed figures
`allocation`, `deployed`, `realized_pnl`, `unrealized_pnl` (numbers or `null`) and
`last_block_reason` (a string or `null`). No existing key changes its name or its
type: `initial_balance` stays the configured capital of the profile, while
`allocation` is the share of the shared wallet its figures are attributed to, so
`cash` can never be read as "this profile's own pot" (§9). `last_block_reason` is
the reason of the **last refused order** of the profile — the exact message of
§3, `null` when no order was ever refused or the profile is not running.

`GET /api/profiles/{id}/candles` serves the persisted history of one profile,
**oldest first**. `limit` is optional: it defaults to **500** and is clamped to
**1000** (the store keeps the 1000 most recent candles of every profile). An
empty, non-numeric, zero or negative value answers
`400 {"error": "malformed query parameter: 'limit' must be a positive integer"}`;
any other query parameter is ignored. The route reads the same seam in both
modes -- the live engine in `realtime run`, the persisted state in
`realtime serve` -- so a chart keeps its history when the engine is stopped.

`GET /api/catalog` is the vocabulary of the dashboard pickers: the tradable
**spot** pairs of the exchange for the configured quote currency (fetched through
the existing exchange/`ccxt` seam, cached server-side with a TTL and backed by a
static table), the strategy names of
`trading_platform.strategy.registry.strategy_names()` (never hard-coded), the
keys of `SUPPORTED_TIMEFRAMES` ordered shortest to longest, and `["paper",
"live"]`. It answers identically with and without an engine and **never** answers
a `500`: any failure degrades to the static catalog.

`GET /api/control` is what the dashboard reads before enabling its controls:
`engine_running` (a lifecycle seam is attached), `read_only`, `mutable` (a
writable server **and** a controller **and** a configured operator token) and
`profiles`, the pause/run state of every profile. A failing controller answers
the same status with an empty profile list; it never turns this read into a
`500`.

The dashboard is a **separate Next.js application** (`dashboard/`): it owns its
own Node server, it renders the overview page and the per-profile page, and it
**polls** this same JSON API every 2 seconds (`monitoring.refresh_seconds`, the
single source of truth for the cadence). The browser therefore only ever talks to
its own origin — the `next.config.ts` rewrite proxies `/api/:path*` to this
server — so there is no CORS preflight, no absolute URL in the browser, and the
`X-Operator-Token` header flows through untouched. `trading_platform.web` serves
neither the dashboard nor any of its assets: the transport is JSON only, and the
read-only `GET /api/kill-switch` route is what the dashboard polls for the
emergency-stop state.

`realtime serve` opens the state database in schema read/write mode: if the file
does not exist yet, it is **created** (empty schema) and the dashboard shows a
platform with no profile. Run `realtime run --once` first to populate the state
before serving it.

Cross-cutting codes: `400` malformed request, `404` unknown route or profile,
`405` wrong method (with an `Allow` header), `500` with `{error}` — **never** a
trace on the network. The single operator token comes from `TB_OPERATOR_TOKEN`
(constant-time comparison); with no token configured, the mutating routes refuse
**everybody**. A server started by `realtime serve` is **read-only**:
`POST /api/kill-switch` answers 403.

## 6. The three commands

```bash
# static pre-flight: passes NO order and does NOT touch the network
trading realtime check --profiles config/profiles.example.json --json

# engine + JSON monitoring API
trading realtime run --profiles config/profiles.example.json
trading realtime run --profiles config/profiles.example.json --host 127.0.0.1 --port 8080

# a single deterministic tick (realtime.start_at anchor), then exit 0
trading realtime run --profiles config/profiles.example.json --once --json

# monitoring only, read-only, on the persisted state
trading realtime serve --profiles config/profiles.example.json --port 8080
```

Payloads (exact keys):

- `realtime check` → `{command: "realtime-check", ok, config_path, state_db,
  state_db_writable, kill_switch, profiles: [{id, symbol, timeframe, strategy,
  mode, ok, issues, credentials_present, live_gate_allowed, risk}], issues}`.
  The **top-level `issues` key** carries the *platform* problems (unreadable
  document, non-writable state directory); the `issues` of each profile carry the
  *profile's* problems. Exit `1` as soon as one profile cannot start.
- `realtime run` / `run --once` → `{command: "realtime-run", ok, config_path,
  state_db, profiles: [ProfileSnapshot…], decisions: [TradeSignalDecision…],
  url}`. `url` is `null` with `--once` and `http://host:port/` otherwise; `--once`
  starts **no** server.
- `realtime serve` → `{command: "realtime-serve", ok, config_path, state_db,
  profiles: [ProfileSnapshot…], url}`.

In `--json` mode, the startup URL is announced on **stderr**: stdout contains
only a single JSON object. `SIGINT` stops the server and the engine cleanly, then
exits with code `0`.

**Logging.** `realtime run` and `realtime serve` install the layer's structured
JSON logs (`observability.configure_logging`): one stream on **stderr** and one
durable file `realtime-YYYYMMDD.log` under `realtime.logs_dir`. The level is set
by `TB_LOG_LEVEL` (`INFO` by default). Without that installation, the events of
the layer fall back on the interpreter's last-resort *handler*, which prints only
the **name** of the event at level `WARNING` and above — an operator then reads
`profile_crashed` without the error, without the profile and without the symbol,
and never sees a single `candle_processed`.

The clock: `realtime.start_at` arms a `ManualClock` (deterministic replay),
otherwise `SystemClock` is used. The `ManualClock` injected by the CLI **hands
control back to the event loop** after every `sleep`: the engine paces itself
only through `Clock.sleep`, so a non-cooperative manual clock would starve the
loop, its timers (`asyncio.wait_for`) would no longer fire and `SIGINT` would
never be delivered. Accepted trade-off: an **anchored** `run` replays history as
fast as the CPU allows (it is backfill), it is not a live follow-up of real time.

### 6.1 Operating a forecast profile: the three-command flow

A `timesfm` profile is inert without an artifact, and the startup guard refuses
it rather than letting it run silently (§3.1). The operational path is therefore
**download → build → declare → start → confirm**, and the first two steps are
automatable:

```bash
# 1. download the candles of the symbol/timeframe the profile declares
#    (the ONLY network-using step; any symbol, any supported timeframe)
make data-download SYMBOL=BTC/USDT TIMEFRAME=1h

# 2. build the artifact the profile declares, offline and deterministically
#    (the seasonal/naive backends only: no torch, no checkpoint download)
make forecast-profile TIMESFM_PROFILE=config/profiles.timesfm.example.json BACKEND=seasonal
#    ... or the equivalent CLI call, which is what the target runs
trading forecast-bootstrap --profiles config/profiles.timesfm.example.json --backend seasonal

# 2b. ask the guard itself whether what was just built is usable right now
make forecast-info PROFILE=config/profiles.timesfm.example.json
trading forecast-info --artifact data/forecast/btc-timesfm-paper-1h-seasonal.parquet \
    --profiles config/profiles.timesfm.example.json --profile btc-timesfm-paper

# 3. start the engine; the profile now really trades
make realtime-forecast          # == trading realtime run --profiles config/profiles.timesfm.example.json
```

The whole chain is also wired end to end as `make forecast-flow`, which runs
`data-download` → `forecast-bootstrap` → `forecast-info` → `realtime-forecast` in
that order. The artifact path is the profile's `forecast` key; keep the default
`data/forecast/<profile-id>-<timeframe>-<backend>.parquet` naming that
`forecast-bootstrap` writes, or point the key at wherever your artifact lives.

**Declare the profile.** The forecast profile is a normal `ProfileConfig` (§1),
so the only thing that distinguishes it is the `strategy` name, its `params` and
the `forecast` key:

```json
{
  "id": "btc-timesfm-paper",
  "symbol": "BTC/USDT",
  "timeframe": "1h",
  "strategy": "timesfm",
  "mode": "paper",
  "forecast": "data/forecast/btc-timesfm-paper-1h-seasonal.parquet"
}
```

The shipped example is `config/profiles.timesfm.example.json`. The `symbol`, the
`timeframe` and the `forecast` path must agree with the artifact's own metadata:
the guard refuses a mismatch at startup rather than trading another instrument's
forecast.

**Confirm it is trading.** "Started" is not "trading", and this is the whole
point of the delivery, so check the observable surface rather than the log line:

```bash
# a single deterministic tick, then exit 0 (no server)
trading realtime run --profiles config/profiles.timesfm.example.json --once --json

# or, against the running engine, the per-profile snapshot of the JSON API
curl -s http://127.0.0.1:8080/api/profiles | python -m json.tool
```

A trading profile shows up in three additive places of the snapshot payload
(§5): `n_trades` grows above `0`, `open_positions` reports the position it holds,
and `last_block_reason` stays `null` while the risk layer is not refusing orders.
A profile whose `n_trades` stays at `0` while its `status` is `running` is *not*
trading: read the strategy's `exit_code` / `forecast_*` diagnostics (they are
`NaN` only when no trajectory covers the bar) and remember that the entry gates
are deliberately strict — an artifact that is *wired* is not an artifact that is
*informative*. Read [`forecasting.md`](forecasting.md) §9 before drawing any
conclusion from a profitable one.

**When the guard refuses.** The message is the operational instruction: it names
the profile, the artifact, the covered window and the exact
`trading forecast-build --symbol … --timeframe …` rebuild command. Run
`trading forecast-info --profiles …` to reproduce it without touching the engine,
and `make forecast-info` for the same answer through the profiles file.

## 7. What is NOT proven

- The dashboard is a **separate Next.js application** that **polls** this JSON
  API over HTTP every 2 seconds: the Python transport has **neither WebSocket nor
  ASGI** (the "standard library only" decision), hence no *push*, and the display
  latency is bounded by the polling cadence.
- Authentication is limited to a **single operator token**: this is a
  **monitoring surface for a trusted network**, not an interface that can be
  exposed on the Internet.
- Fills are reconciled on a **bounded polling interval**: between two polls, the
  local state may lag behind the execution venue. In paper mode, the partial
  fills are **simulated deterministically** (explicit seed) and do **not** model
  a real order book.
- Decisions are taken on **closed** candles only: a live profile reacts one
  timeframe boundary after the signal, exactly like the backtest engine.
- Candles closed **before** the engine started (or during an outage) are
  **skipped**, not replayed: a live stream does not catch up candle by candle. A
  restart therefore resumes at the first candle that closes after it — the reason
  is written in §2.2, and the `market_data.candles_skipped` event says exactly
  how many candles were ignored and over which range.
- **No** funding, borrowing, leverage or exchange-specific order type is
  modelled (orders are market orders, possibly touched limits).
- **Live trading is implemented but it is NOT exercised against a real exchange
  by the test suite**: the tests cover the paper broker, the live gate, the risk
  limits and the error paths, never a real order.
- The SQLite store is **single-writer**: a second orchestrator on the same file
  raises `StateStoreError` (tested behaviour, not a guarantee of sharing).
- `realtime check` attests the **absence** of credentials, not their validity: it
  opens no connection, so an invalid key/secret pair will only be detected at the
  first real call to the broker.

## 8. Profile lifecycle and candle history

The dashboard drives four mutations with the **same** single operator token
(`X-Operator-Token`) as the kill switch. The read-only server of
`realtime serve` refuses all four with the documented `403`
`{"error": "mutations are disabled on this server"}`: it attaches **no**
controller, and a lifecycle route answers `403` whenever that seam is absent --
even on an otherwise writable server.

**Pause stops opening, never managing.** A paused profile keeps its stream, its
runner and its supervision: it stops **opening new positions**, while the static
stop of the open position and every exit signal keep being evaluated, so a
position is **never** left unmanaged. The flag is durable (it is written to the
store's `meta`, so a restart resumes paused) and it is exposed through
`GET /api/control` as `{profile_id, paused, running}` -- **not** through
`ProfileSnapshot`, whose shape is frozen. A paused profile therefore still
reports `status: "running"`, and its tick still publishes equity points and
candles: pause is not stop.

**Delete flattens before it removes.** `DELETE /api/profiles/{id}` closes every
open order and flattens the open position **at market** through the existing
execution gateway **before** anything is removed. When flattening fails, the
delete fails with an explicit error (`409`) and the profile stays exactly where
it was: nothing is orphaned and no profile that is still exposed is removed. The
delete also refuses to remove the **last** profile of the platform (an engine
with no profile cannot run). Once flat, the profile is stopped, removed from the
running engine **and** removed from the profiles configuration file; a file that
cannot be rewritten aborts with `400`, with the engine still consistent.

**Create validates against the catalog.** `POST /api/profiles` accepts
`{profile_id, symbol, timeframe, strategy, mode, initial_balance?, params?}`,
refuses an unknown field, a field of the wrong type, an unknown strategy (the
message names the available ones), an unsupported timeframe and a malformed
identifier with `400`, and a duplicate identifier with `409`. On success the
profile is persisted to the profiles configuration file and **started
immediately** in the running engine, and the `201` body carries its
`ProfileSnapshot`, so the dashboard refreshes without guessing.

**The profiles configuration file is the source of truth.** Adding or removing a
profile rewrites it **atomically** (temporary file + `os.replace`), so an
interrupted rewrite can never leave a truncated document behind.

**Candle history.** The engine persists every candle it processes in a bounded
`candles` table (one row per profile and timestamp, the 1000 most recent rows per
profile, pruned in the same transaction as the append). Until this delivery the
store persisted **no** candle at all -- only the *last processed candle*
watermark -- so §7's honesty list is corrected here: the store now keeps a
bounded candle history per profile, which is what the chart draws. What is
**not** corrected is the decision rule: decisions still only ever use **closed**
candles, and a candle closed before the engine started is still **skipped**, not
replayed (§2.2).

**Error mapping of the four mutations.** `MonitoringError` (`503`: no engine, or
a command that timed out), `ProfileError` (`409`: a duplicate, a profile that is
not running, a refused flattening, the last profile), `ConfigError` (`400`: an
unknown strategy, an unsupported timeframe, an unsupported value, a profiles file
that cannot be rewritten) and -- for anything else -- the existing `500`
boundary.

## 9. The shared platform wallet

There is **one** wallet and it is the **only** thing that can fund an order. It
holds the USDT cash of the whole platform, it is persisted as the single row of
the `wallet` table (§4) and it is restored **once**, so a restart never
resets it. The engine restores it at startup, before the first candle of the
first profile; a process that only **reads** the platform (the monitoring API
answers before the engine loop has booted, and a read-only composition never
boots at all) restores it on its first read instead, so the cash it reports is
always the durable one and never the configured initial balance. `PaperBroker`
owns no cash any more: it is **injected** with the
wallet and delegates every movement to it (never through a global, exactly like
the clock and the store). Two profiles entering at the same instant therefore
spend the same ledger, and the second one can be refused for lack of cash that
the first one just spent (§3).

**Per-profile figures are ATTRIBUTED, never the venue's.** A profile does not own
a pot: it is attributed a share of the shared wallet, its `allocation` (its own
`initial_balance` when the optional `allocation` key is absent, §1). Every
per-profile number the API publishes is that share, with `allocation` as the
capital base of the metrics and the reports:

```
equity = allocation + realized_pnl + unrealized_pnl
cash   = allocation - deployed    + realized_pnl
```

`deployed` is the **cost basis of the open positions at their average entry
price** (`|quantity x average_price|`, summed over the profile's positions), and
`unrealized_pnl` is their mark-to-market value minus that cost basis. The
existing keys keep their names and their types — `equity`, `cash`,
`position_value`, `total_return`, `initial_balance` — and only their meaning is
refined: `initial_balance` is still the configured capital of the profile, while
`cash` is the profile's own view of the shared ledger. That is why the dashboard
labels it *attributed* and shows the allocation beside it: reading `cash` as
"this profile's own pot" would be wrong, and the payload says which share it is.

**What is reported versus what the wallet holds.** The wallet object of §5 is the
platform-wide truth: `cash` is what is left to deploy, `equity` is `cash` plus the
mark-to-market value of everything the profiles hold, `deployed`, `realized_pnl`
and `unrealized_pnl` are the sums of the attributed per-profile figures, and
`total_exposure` is the summed notional the profiles hold right now (the absolute
mark-to-market value of every open position). One caveat is worth stating rather
than hiding: the attributed `cash` of a profile does not subtract the **entry
fee** of its still-open positions, while the wallet really paid it. So the sum of
the attributed cash of every profile equals the shared wallet cash **plus the
entry fees of the positions that are still open** — they meet again, to the cent,
once every position is closed.

**Live mode.** There, the wallet **is** the venue's account: `CcxtBroker` already
fetches that balance, the wallet mirrors it best-effort once at startup, and
`source` is `"venue"` in the payload. It is **read-only**: the venue account is
the source of truth, the wallet is never debited locally, and a local debit is
refused with `WalletError` instead of inventing a balance. Funding checks and
platform caps still read it — they are the same checks — but nothing is ever
written back to it. While the venue has reported nothing, the mirror falls back
to the configured initial balance, never to an invented `0.0`.

**Platform caps are optional; the funding check is not.** Absent
`realtime.platform_initial_balance`, the wallet starts at the **sum of the
allocations**, so a configuration written before the shared wallet existed keeps
its total cash; absent `realtime.platform_max_total_notional` and
`realtime.platform_max_daily_loss`, no platform-wide cap is enforced and the
per-profile limits stay the only ceilings. The **funding check is always
enforced**, on every configuration, because no order may spend cash the one
wallet does not hold — a refusal names its limit and its reason (§3) and is
published as the `last_block_reason` of the profile (§5). Nothing is
half-applied: a refused order leaves no order row, no fill, no position and no
cash movement behind.
