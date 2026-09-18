# Real-time multi-profile

This document describes the real-time platform delivered with the skeleton:

- **`trading_platform.realtime`** (layer 6) — engine: market streams, broker,
  execution gateway, risk, persistence, orchestrator, observability;
- **`trading_platform.web`** (layer 7) — HTTP transport using the **standard
  library only** and a static dashboard;
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
| `stake_amount` | amount committed per entry (default: the whole available balance) |
| `exchange` | name of the execution venue |
| `enabled` | a disabled profile is persisted but never started |
| `warmup_candles` | number of past candles the strategy receives on every decision |
| `poll_interval_seconds` | polling cadence specific to the profile |
| `risk` | `RiskLimitsConfig` block (§3) |

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

### 2.1 Real-time / backtest equivalence

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

### 2.2 Mandatory reuse

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
  handed to Freqtrade unchanged**.

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
4. **Global kill switch** (`KillSwitch`), in three forms: file
   (`realtime.kill_switch_file`), environment, and API (`POST /api/kill-switch`).
   It stops every profile, **cancels nothing silently**, and it is persisted in
   the state: a restart does not reset it. A kill switch forced by a file or by
   the environment cannot be released through the API.
5. **Unforgeable paper/live separation.** A paper profile can **never** be routed
   to a real broker: the mode is part of the identity of the profile and of every
   persisted order, and the broker checks its own mode.

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
- A **simulated** venue has no memory: after a restart, its cash starts again
  from the initial balance while the position is restored from the store. The
  orchestrator therefore **reseeds the paper broker's cash** with the last
  persisted equity point (`PaperBroker.restore_cash`) before the first candle,
  without which the position would be counted twice and the dashboard would show
  an equity that the persisted curve contradicts. A **real** venue is never
  reseeded: it publishes its own balance (`CcxtBroker.fetch_balance`).

## 5. Web API reference

Server `http.server.ThreadingHTTPServer`, JSON everywhere, UTC ISO-8601
timestamps, **no NaN and no Infinity** in any payload. The only static assets
served are `app.js` and `styles.css` (fixed allow-list: any path traversal
answers 404). The dashboard **polls** `GET /api/profiles` every 2 seconds
(`monitoring.refresh_seconds`) and draws the equity curves with inline
`canvas`/SVG, without any library.

| Method and route | 200 response | Errors |
| --- | --- | --- |
| `GET /` | HTML dashboard | 404 |
| `GET /static/{asset}` | `app.js` or `styles.css` | 404 (unknown asset or traversal) |
| `GET /api/health` | `{status, version, uptime_seconds, profiles_total, profiles_running, kill_switch, checked_at}` | — |
| `GET /api/profiles` | `{profiles: [ProfileSnapshot…], generated_at}` | — |
| `GET /api/profiles/{id}` | `ProfileSnapshot` | 404 `{error}` |
| `GET /api/profiles/{id}/equity` | `{points: [{timestamp, equity, cash, position_value}…]}` | 404 |
| `GET /api/profiles/{id}/trades` | `{trades: [...], count}` | 404 |
| `GET /api/profiles/{id}/orders` | `{orders: [...]}` | 404 |
| `GET /api/profiles/{id}/positions` | `{positions: [...]}` | 404 |
| `GET /api/profiles/{id}/metrics` | `{metrics: {...}, benchmark: {...} \| null, generated_at}` | 404 |
| `POST /api/kill-switch` | body `{engage: bool, reason: str}` → `{kill_switch, reason, changed_at}` | 400 malformed body, 403 missing/invalid token, 403 server in read-only mode |

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

# engine + dashboard
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

## 7. What is NOT proven

- The dashboard **polls** over HTTP every 2 seconds: there is **neither
  WebSocket nor ASGI** (the "standard library only" decision), hence no *push*,
  and the display latency is bounded by the polling cadence.
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
  is written in §2.1, and the `market_data.candles_skipped` event says exactly
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
