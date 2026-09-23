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

A profile is **one row of the `profiles` table** of the SQLite state store
(§1.1), loaded from there at every boot and validated by
`config.models.ProfileConfig`, which rejects any unknown key
(`extra="forbid"`). There is no profiles JSON document any more: the field table
below is the schema of that row, not of a file.

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
| `warmup_candles` | the **frame budget**: number of past candles the runner asks the stream for on every decision, and therefore the window the strategy is warmed up on (§2.2) |
| `poll_interval_seconds` | polling cadence specific to the profile |
| `risk` | `RiskLimitsConfig` block (§3) |
| `entry_lookback_candles` | live-only catch-up window: the entry decision may act on a crossover that occurred within the last N candles (0, the default, keeps the historical behaviour: only the last row decides); ignored by the backtest, which already reads every row; 0 <= N <= 200 |
| `history_candles` | **optional per-profile override** of the live stream window, in candles (`>= 1`); `None`, the default, means "use `realtime.history_candles`", so an absent override reproduces the previous behaviour exactly. This is what lets one intraday profile be fed tens of thousands of candles without forcing every other profile to ask for as many. Declared **last** in the model, so no serialised profile changes shape (§2.2, §8) |

The complete profile set is the `profiles` table of the state store. The two
other sections of the retired document are settings, and they are rows of the
`meta` table as well: `realtime` (`RealtimeConfig`: state base, directories, CSV
or cache provider, `start_at` anchor, delays, reconnections, benchmark) and
`monitoring` (`MonitoringConfig`: `host`, `port`, `refresh_seconds`,
`request_timeout_seconds`, `max_request_bytes`). An unknown key is rejected
everywhere, because the models forbid extra fields.

### 1.1 Where the configuration lives

**SQLite is the single source of truth**, for the profile set *and* for the
engine settings:

| Surface | Storage |
| --- | --- |
| the profile set | the `profiles` table of `realtime.state_db` |
| the engine and monitoring settings | the `meta` key `platform_settings` of the same database |
| positions, orders, fills, equity, candles, wallet | the tables of the same schema |

`POST /api/profiles` and `DELETE /api/profiles/{id}` write that table, not a
file: a profile created through the dashboard is durable, survives a restart, and
is **not** rebuilt from git by the next deployment. This is deliberate — the
platform used to keep the profile set in a committed JSON document that the
monitoring API rewrote, so every deploy silently dropped the profiles that existed
only on the deployed host.

**The bootstrap surface is exactly two keys.** The path of the state database
**cannot live inside the database it locates**, so `realtime.state_db` (default
`data/realtime/state.db`) and `realtime.logs_dir` (default `data/realtime/logs`)
are resolved *before* the store can be opened:

```
state_db  <-  --state-db,  else TB_REALTIME_STATE_DB,  else data/realtime/state.db
logs_dir  <-  --logs-dir,  else TB_REALTIME_LOGS_DIR,  else data/realtime/logs
```

**Every other setting is seeded into SQLite on first initialisation and read
from SQLite on every later boot**: `poll_interval_seconds`,
`stream_poll_timeout_seconds`, `max_stream_reconnects`,
`reconnect_backoff_seconds`, `reconcile_interval_seconds`, `data_dir`,
`cache_dir`, `csv_dir`, `format`, `allow_network`, `history_candles`,
`start_at`, `risk_free_rate`, `benchmark_variant`, `kill_switch_file`,
`platform_initial_balance`, `platform_max_total_notional`,
`platform_max_daily_loss`, and the whole `monitoring` section (`host`, `port`,
`refresh_seconds`, `request_timeout_seconds`, `max_request_bytes`) — no
exception. Deleting the state database and restarting re-seeds the built-in
defaults, which is why a host that never customises anything behaves exactly as
it did before this storage change: the seed *is* the model default.

The settings are updatable at runtime through the store (`set_meta` writes the
document, and the next read observes the new value); nothing needs to be edited
on disk, and there is no file to edit. `--host` and `--port` stay per-invocation
options: they are never persisted, so `realtime serve --port 0` cannot re-point
the engine.

**No migration is performed.** A previously declared profile is *not* imported
from any document, and no importer exists: the host starts with an **empty
profile set** and the operator re-creates the profiles through the dashboard
(§4, §8). The engine boots cleanly with zero profiles.

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

**Two different bounds: the frame budget and the stream window.** The frame a
runner warms its strategy up on is built from
`stream.history(symbol, timeframe, count=warmup_candles)`, so
**`warmup_candles` bounds what the strategy sees**: the candles a strategy
declares it needs (`Strategy.required_candles`, see
[`docs/strategies.md`](strategies.md) §3.1) have to fit inside that frame, and a
profile whose strategy needs more can never warm up at all. **`history_candles`
bounds what the stream itself asks the venue for**:
`PollingMarketStream.next_candle` polls
`[now - history_candles * candle_delta(timeframe), now]` on every attempt. The
knob is a **realtime-level** setting (`RealtimeConfig.history_candles`, 300 by
default) shared by every profile, and the profile carries an **optional
per-profile override** (`ProfileConfig.history_candles`, `None` by default,
`>= 1`): one profile can therefore be served the tens of thousands of candles an
intraday warm-up needs — 40321 of them on `1m` with the default `momentum`
parameters — without forcing every other profile to ask for as many. An absent
override therefore reproduces the previous behaviour **byte-for-byte**: the
profile is served `realtime.history_candles`, which is why the field is declared
**last** in `ProfileConfig`: no serialised profile changes shape.

A profile that asks for **more warm-up candles than its stream window holds**
(`warmup_candles > history_candles`) is reported as a **WARNING** everywhere and
is never refused (§8). The reason is the sentence above: the frame the strategy
receives is bounded by `warmup_candles`, so a smaller stream window does not by
itself silence the profile — but the operator asked for more history than the
engine is configured to serve, and that must be visible rather than inferred.

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

### 3.1 The idle-poll bound: a bound is never equal to the wait it wraps

**The bound applied around any call that may legitimately idle must be strictly
greater than the longest wait that call can take.** An idle poll is healthy, not
a hang: `next_candle` answers `None` when no new **closed** candle exists yet
(§2.2; `market_data.candles_skipped` says how many candles were skipped) and the
stream waits its own cadence before asking again. A bound that cuts that wait is
not a bound — it is a **race**: it killed every profile on its first idle poll
(`profile_crashed error="TimeoutError"`) and crash-looped the container (79
restarts of the shipped 30 s / 10 s configuration).

A stream declares its longest legitimate wait through the read-only member
`MarketStream.max_wait_seconds` (`realtime/stream.py`):

| stream | `max_wait_seconds` |
| --- | --- |
| `PollingMarketStream` | its `poll_interval_seconds`, or its whole retry backoff series when that is longer |
| `CcxtProMarketStream` | its read timeout, or that same retry backoff series when that is longer |
| `CompositeMarketStream` | the longest wait of its children |
| `ReplayMarketStream` | `0.0` (a replay never idles) |

The runner no longer derives its bound from `stream_poll_timeout_seconds` alone:
it is `max(stream_poll_timeout_seconds, max_wait_seconds)` plus a proportional
margin and a small floor, so the shipped pair — profile
`poll_interval_seconds = 30.0`, `stream_poll_timeout_seconds = 10.0` — is bounded
by ~31.6 s instead of ~10.55 s, and the 30 s idle wait completes. A single
`realtime run --once` tick budgets, per profile, the same bound that profile's
runner applies — the stream's declared wait included, the retry backoff series
and all — plus one stream timeout for the shutdown, so it cannot cut an idle poll
either.

Because the two values meet at boot, the platform checks the pair when the
profiles are wired to the realtime settings and logs a **WARNING** — event
`profile_poll_interval_exceeds_stream_timeout`, naming both values — whenever a
profile's `poll_interval_seconds` is longer than `stream_poll_timeout_seconds`.
The trigger is that pair alone: a stream that declares a longer wait only because
its retry backoff series is longer is not a misconfiguration and stays silent.
The record also carries the declared wait and the bound the runner really applies
(`stream_max_wait_seconds`, `tick_bound_seconds`), so the mismatch can be read
straight off the log.
It is a **warning, never a refusal to boot**: refusing would turn a
misconfiguration into an outage. The 30 s / 10 s pair legitimately fires it, a
profile whose poll interval is not longer stays silent, and with the bound above
the warning no longer announces a crash: lowering `poll_interval_seconds` is
**not** needed to keep the platform up.

## 4. Persistence, restart and reconciliation

- The state lives in **a single SQLite file** (`realtime.state_db`, default
  `data/realtime/state.db`, ignored by git), in WAL mode, one connection per
  calling thread, **every write inside a transaction**. That one file is the
  whole durable state of the platform: the profile set, the engine settings
  (§1.1), the positions, the orders, the fills, the equity curve, the candle
  history and the shared wallet.
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
- Reconciliation **compares like with like**. The two sides do not describe the
  same window: `ExecutionGateway.reconcile` hands `Broker.reconcile` the profile's
  **complete durable history** (`store.list_orders(profile_id, limit=RECONCILE_ORDER_LIMIT)`,
  terminal orders included), while a venue only ever reports the orders it
  currently works. So an order is compared only when the venue could legitimately
  still know it: an order whose state is still **working** (`PENDING`,
  `SUBMITTED`, `PARTIALLY_FILLED`) is always compared, a **terminal** order
  (filled, cancelled, rejected) is compared only when the venue's view also knows
  its `client_order_id`, and a **terminal order the venue no longer holds is
  dropped from the comparison entirely — it is not a divergence** and is never
  reported as `only_locally`. The three-way classification is otherwise unchanged:
  `only_at_venue` (the venue holds an order the local state ignores),
  `only_locally` (the local state believes in a *working* order the venue ignores)
  and `mismatched` (both sides know the identifier but disagree on its state — a
  terminal local state against a venue that still holds the order lands here,
  never in `only_locally`). The rule lives in the single shared comparison helper
  `_reconcile_orders`, so `PaperBroker` and `CcxtBroker` behave identically. A
  difference of `filled_quantity` with an identical state stays in `details`
  (`quantity_mismatches`) and does not degrade anything. The gateway still
  **reports and never repairs**: `ok=False` is forwarded to the orchestrator,
  which alone marks the profile `degraded`.
- A reconciliation discrepancy on an **in-memory** venue is therefore no longer
  produced by the profile's own history: a paper venue that restarted with an
  empty book while the store still holds filled orders reconciles **healthy**
  (`ok=True`, `matched=0`). A genuine divergence — a *working* order only the
  venue holds, a *working* order only the local state knows, or a state
  disagreement between two known orders — still degrades the profile, and nothing
  is resubmitted in any case. This is the documented behaviour of D7, not a data
  loss.
- The store is **single-writer**: a second orchestrator on the same file fails
  with `StateStoreError` (a `flock` lock taken on `<base>.lock`). This lock is
  **advisory**: it protects two `SqliteStateStore` instances, not a third-party
  process that would write into the file while bypassing the store. A newer
  schema version also raises `StateStoreError` instead of writing blindly.
- The stored schema version is now **4**, and the `v3 -> v4` step is the first
  **non-additive** one. It rewrites the rows of the `profiles` table, dropping
  exactly the keys the current `ProfileConfig` does not declare
  (`ProfileConfig.model_fields`), preserving every other key byte-for-byte and
  never touching `updated_at`. That is deliberate: a field is removed by a
  release, and the migration that prunes it must already be in place for the
  **next** removal, so the declared field set of the current model is the
  contract — never a hard-coded list of removed names. The rewrite runs inside the
  **same transaction** that bumps the stored `schema_version`, so a crash migrates
  nothing; it is **idempotent**; and it is a **no-op on a clean database** — a row
  that carries no undeclared key is never rewritten, so its `payload` and its
  `updated_at` stay byte-for-byte identical (the deployed database, already
  repaired by hand, opens on version `4` without a single row changing). A row
  whose payload is not valid JSON — or is not a JSON object — is left
  **untouched**: the migration never mangles what it cannot read, and the
  tolerant read below quarantines it instead. A dropped key is accounted for by
  its **name** alone; the value it held is never echoed.
- **One unreadable profile row can no longer take the platform down.** The read
  **quarantines**: `load_profiles()` skips a row it cannot decode or validate,
  logs it at `WARNING` with the **sanitised** reason — the field path and the
  rule, never the offending value, because a profile row is exactly where an
  operator might have hand-written a credential — and keeps loading every other
  profile. `SqliteStateStore.load_profile_failures()` answers `profile_id ->
  sanitised reason` for the failures of the **most recent** read; it never raises
  and answers `{}` when there is nothing to report. The read is tolerant by
  default — `load_profiles(*, strict=False)` — and `load_profiles(strict=True)` is
  the **loud** path, kept for callers that genuinely want validation to fail: it
  raises `StateStoreError` as it always did. A profile whose `strategy` is no
  longer registered does not abort the boot either: it is reported as failed
  (`ProfileStatus.ERROR`) while every other profile keeps running. The previous
  behaviour was not survivable — **one** stale row raised `StateStoreError`, so
  the orchestrator never started, the monitoring API never bound, and the
  dashboard showed `The monitoring API is unreachable`: one row, the whole
  platform.
- **One** shared wallet holds the USDT cash, and it is the only thing that can
  fund an order (§9). It is a single row of the `wallet` table — added by schema
  version **3**, with `wallet_id = 1` enforced by a `CHECK` — written through
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
- **An empty platform is a legal platform.** With the profile set living in the
  state store, the engine boots with **zero** profiles: it opens the store, seeds
  or adopts the settings, restores the wallet, opens the monitoring API and idles.
  `GET /api/profiles` answers `[]`, `GET /api/health` reports
  `profiles_total: 0`, `realtime check` exits `0`, and `POST /api/profiles`
  creates the first profile and **starts it immediately**, without a restart. The
  retired loader used to refuse an empty profile set with the error
  `the profiles file declares no profile`, which would now mean a platform that
  cannot be brought up at all from an empty database — so that refusal is gone
  with the file that produced it.
- **The "cannot delete the last profile" guard is removed** — a *recorded
  decision*, not an oversight. It existed because an engine was believed to need
  at least one profile; it does not: with the state store as the source of truth,
  deleting the last profile simply leaves the same legal empty platform described
  above, and re-creating a profile from the dashboard takes one request. The
  **flatten-before-remove safety of `DELETE /api/profiles/{id}` is untouched**:
  the open exposure is still closed through the execution gateway *before*
  anything is removed, and a position that cannot be flattened still aborts the
  whole call and changes nothing (§8). What the removed guard leaves behind is
  the path that does **not** go through the delete route at all — a row removed
  with `sqlite3`, a database restored from a backup taken before the profile
  existed — and that path is covered by the next paragraph.

### 4.1 Orphaned positions are flattened at startup

A position is **durable state independent of the `profiles` table**: its row
carries the `profile_id` it belongs to, but nothing ties the two tables together.
When a profile disappears **without** going through `Orchestrator.delete_profile`,
nothing ever tracks its open position again — no stop-loss, no exit management, no
reconciliation — and the exposure stays open at the venue with no owner. That is
an **orphan**, and the platform sweeps it.

**The sweep.** It runs at engine boot, from `_prepare_runners`, at the one point
where the whole durable position set and the whole loaded profile set are visible
together — after the store is opened, the settings adopted and the profiles
loaded, and after the shared wallet is restored, but **before any runner exists**.
That ordering is the safety property: nothing can be trading while the sweep
closes a position.

**What it does.** Every position whose `profile_id` matches **no** loaded profile
is orphaned, and is **closed at the venue through the execution gateway**, in
**both** `paper` and `live` mode. Closing means routing a real closing order
through the injected broker, never deleting the SQLite row: removing the row would
leave the exchange position open, which is exactly the hazard being fixed.

The mode and the exchange of an orphan are recovered from the `meta` table of
the state store, under `profile_state:<id>` and `profile_exchange:<id>` — the durable
evidence of how that profile traded — because the profile itself is gone. A
`profile_state:<id>` of `live` therefore closes through the real broker, subject
to the same live gate as any live profile (§3); an absent or unreadable
`profile_state:<id>` closes in the **safer** direction, at the venue the position
was opened on.

**Every closure is logged** with `profile_id`, `symbol`, `quantity`, `side` and
`price` (the structured event `orphaned_position_closed`), and the sweep itself is
logged as `orphaned_positions_swept` with the counts.

**A position that cannot be closed is NOT deleted.** The row stays, the failure is
logged at `ERROR` (`orphaned_position_close_failed`, carrying the verbatim venue
error) and it is **surfaced** — in the report below and on the dashboard — because
an unclosable exposure must stay visible rather than be silently forgotten.

**Idempotent across restarts.** The sweep only ever acts on positions, and a
position that was closed is closed: a restart finds no open position for that
profile and does nothing. A failure is retried on the next boot rather than
hidden, and a successful closure is never repeated. `closed_count` counts
closures, never rows inspected.

**Where the operator sees it.** The result is published through the monitoring
JSON API, on `GET /api/health` (the additive `orphaned_positions` key) and on the
dedicated read route `GET /api/orphans` (§5). Both carry the same object:

```
{"found": 3, "orphaned": 2, "closed_count": 1, "failed_count": 1,
 "closed": [{"profile_id": "test1", "symbol": "BTC/USDT", "quantity": 0.5,
             "side": "sell", "price": 61234.5}],
 "failed": [{"profile_id": "eth-scratch", "symbol": "ETH/USDT",
             "quantity": 1.25, "error": "BrokerUnavailableError: ..."}],
 "swept_at": "2024-05-01T12:00:00+00:00"}
```

`found` counts the durable positions the sweep inspected, `orphaned` those that
matched no loaded profile, `closed_count`/`failed_count` the two outcomes,
`closed` and `failed` the per-position detail, and `swept_at` the ISO-8601 stamp
of the sweep — `null` when the platform was never swept, which is how a consumer
tells "no orphan found" from "never looked". A `failed_count > 0` makes
`GET /api/health` answer `status: degraded`, exactly like an engaged kill switch:
a failure to close is reported as loudly as a success. The dashboard renders the
same object as an operator-facing warning banner. `GET /api/orphans` is
read-only and requires no operator token.

## 5. Web API reference

Server `http.server.ThreadingHTTPServer`, JSON everywhere, UTC ISO-8601
timestamps, **no NaN and no Infinity** in any payload. The server serves **no
HTML page and no static asset**: every path outside the table below — `GET /`
and `GET /static/{asset}` included — answers the documented JSON 404
`{"error": "not found: <path>"}`, exactly like any other unknown route.

| Method and route | 200 response | Errors |
| --- | --- | --- |
| `GET /api/health` | `{status, version, uptime_seconds, profiles_total, profiles_running, kill_switch, checked_at, wallet, orphaned_positions, profile_failures}` | — |
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
| `GET /api/orphans` | the `orphaned_positions` report of the startup safety sweep (§4.1), on its own route | — |
| `GET /api/operator-token` | `{valid: bool, reason: str, read_only?: bool}` — answers whether the `X-Operator-Token` header of the request authorises mutations | never 403 (see below) |
| `POST /api/profiles/{id}/pause` | `{profile: ProfileSnapshot, paused: true}` | 400, 403, 404, 409, 503 |
| `POST /api/profiles/{id}/resume` | `{profile: ProfileSnapshot, paused: false}` | 400, 403, 404, 409, 503 |
| `DELETE /api/profiles/{id}` | `{profile_id, deleted: true}` | 400, 403, 404, 409, 503 |
| `POST /api/profiles` | body `{profile_id, symbol, timeframe, strategy, mode, initial_balance?, params?, warmup_candles?, history_candles?}` → `201 {profile: ProfileSnapshot}` | 400 malformed body / unknown strategy / unsupported timeframe / a profile that can never warm up, 403, 409 duplicate |

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

`orphaned_positions` is the **additive** report of the startup safety sweep of
§4.1, present on `GET /api/health` and served on its own by `GET /api/orphans`:

```
{"found": int, "orphaned": int, "closed_count": int, "failed_count": int,
 "closed": [{"profile_id": str, "symbol": str, "quantity": num|null,
             "side": "buy"|"sell", "price": num|null}],
 "failed": [{"profile_id": str, "symbol": str, "quantity": num|null,
             "error": str}],
 "swept_at": str|null}
```

`found` is how many durable positions the sweep looked at and `orphaned` how many
of them matched no loaded profile. `closed_count` and `failed_count` are the two
outcomes; `closed` and `failed` carry one entry per position, and a `quantity` or
a `price` the venue did not report as a finite number is rendered `null` rather
than dropped — the closing order still happened. `swept_at` is `null` when the
platform was never swept, which is how a consumer distinguishes "no orphan found"
from "never looked" (the dashboard then renders no warning at all). A
`failed_count > 0` makes `status` **`degraded`**, exactly like an engaged kill
switch, so an unclosable position is as loud as a closed one. `GET /api/orphans`
is read-only, needs no operator token, and answers the same object — a platform
that was never swept answers `swept_at: null` with empty lists rather than a
`404`.

`profile_failures` is the **additive** answer to "what could not be loaded": an
object `{profile_id: sanitised reason}` carrying the failures of the most recent
profile read (§4). The key is **always present** and is never `null`: it is `{}`
when nothing failed, so a consumer distinguishes "no profile failed" from "the key
is missing". A reason is never a value — it is the sanitised field path and the
rule — because a profile row is exactly where an operator might have hand-written
a credential. An outage that used to leave the dashboard at
`The monitoring API is unreachable` can therefore no longer be invisible: a
platform that quarantined a row says so here while every other profile keeps
running.

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

`GET /api/operator-token` answers one question: **does the token in this
request's `X-Operator-Token` header authorise mutations?** It exists because
every mutating route answers the same `403 missing or invalid operator token`
whether the header was absent, wrong, or the server simply disables mutations,
and an operator who pasted a token has no way to tell those apart. The route
therefore never answers `403` -- a wrong token is a successful answer to the
question, so it is a `200` carrying `valid: false` and one of four fixed
reasons:

| `reason` | Meaning |
| --- | --- |
| `valid operator token` | `valid: true`; the header matches the configured token |
| `no operator token was supplied` | the request carried no `X-Operator-Token` header |
| `the supplied operator token does not match this server` | a token was sent and it is wrong |
| `no operator token is configured on this server` | `TB_OPERATOR_TOKEN` is unset, or the server runs read-only; `read_only` is also reported |

The answer is a boolean and a fixed reason string only: the configured token is
never echoed, and the comparison stays constant-time. The route is `GET` only --
its answer depends on a request header, which a header-free `HEAD` could not
supply without misreporting the caller's credentials -- so `HEAD` and every
other verb answer `405`.

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
trading realtime check --state-db data/realtime/state.db --json

# engine + JSON monitoring API
trading realtime run --state-db data/realtime/state.db
trading realtime run --state-db data/realtime/state.db --host 127.0.0.1 --port 8080

# a single deterministic tick (realtime.start_at anchor), then exit 0
trading realtime run --state-db data/realtime/state.db --once --json

# monitoring only, read-only, on the persisted state
trading realtime serve --state-db data/realtime/state.db --port 8080
```

The three commands take the same storage option: `--state-db` is the path of the
SQLite state database, and it is the **only** thing the command needs to know
before the store exists (§1.1). `--profiles` / `-p` is kept as an **alias** of
`--state-db`, so a script written against the previous name keeps working while
pointing at a database instead of a document; the primary spelling is
`--state-db`. `--logs-dir` sets the durable log directory, and both options fall
back on `TB_REALTIME_STATE_DB` / `TB_REALTIME_LOGS_DIR` and then on the model
defaults.

Payloads (exact keys):

- `realtime check` → `{command: "realtime-check", ok, state_db,
  state_db_writable, kill_switch, profiles: [{id, symbol, timeframe, strategy,
  mode, ok, issues, credentials_present, live_gate_allowed, risk, warmup}],
  issues}`.
  The **top-level `issues` key** carries the *platform* problems (a non-writable
  state directory, an unusable store); the `issues` of each profile carry the
  *profile's* problems. The `warmup` block is the additive warm-up report of §8
  (`candles_per_day`, `required_candles`, `warmup_candles`, `history_candles`,
  `findings`). Exit `1` as soon as one profile cannot start. Zero
  profiles is a legal platform, so an empty `profiles` list answers `ok: true`.
- `realtime run` / `run --once` → `{command: "realtime-run", ok, state_db,
  profiles: [ProfileSnapshot…], decisions: [TradeSignalDecision…], url}`. `url` is
  `null` with `--once` and `http://host:port/` otherwise; `--once` starts **no**
  server.
- `realtime serve` → `{command: "realtime-serve", ok, state_db,
  profiles: [ProfileSnapshot…], url}`.

There is no `config_path` key any more: no command reads a configuration
document, and `state_db` is the storage the payload reports.

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
- A grid the platform can now **feed** is not a grid that was **validated**. The
  per-profile `history_candles` override and the warm-up contract make an
  intraday `momentum` profile *run*, and stop it from running *silently* when it
  cannot — they say nothing about whether it makes money. The validated grids,
  the cost of a large window and the honest verdict are in
  [`docs/strategies.md`](strategies.md) §3.1, §5 and §7: `4h` and `1d` are the
  deployable grids, `1h` was measured and degrades on the holdout, and
  `5m`/`15m`/`30m`/`1m` were never validated at all.

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
it was: nothing is orphaned and no profile that is still exposed is removed. Once
flat, the profile is stopped, removed from the running engine **and** deleted from
the `profiles` table; a store that cannot be written aborts with `400`, with the
engine still consistent.

**Deleting the last profile is allowed.** The old "refuse to remove the **last**
profile" guard is **gone** (§4, where the decision and its motivation are
recorded): with the state store as the source of truth an empty platform is a
legal one, the API keeps serving, and `POST /api/profiles` re-creates a profile
without a restart. The flatten-before-remove guarantee above is untouched: a
position that cannot be flattened still aborts the whole call. The path the guard
was never able to cover — a profile that disappears **without** going through
this route — is the one that leaves an orphan, and the next boot flattens it
(§4.1).

**Create validates against the catalog.** `POST /api/profiles` accepts
`{profile_id, symbol, timeframe, strategy, mode, initial_balance?, params?, warmup_candles?, history_candles?}`,
refuses an unknown field, a field of the wrong type, an unknown strategy (the
message names the available ones), an unsupported timeframe, a `warmup_candles` or
`history_candles` that is not a positive integer and a malformed identifier with
`400`, and a duplicate identifier with `409`. On success the profile is written to
the `profiles` table and **started immediately** in the running engine, and the
`201` body carries its `ProfileSnapshot`, so the dashboard refreshes without
guessing. It is the supported way to create the profiles of a freshly deployed,
empty host. The two warm-up keys are optional and purely additive: an absent
`warmup_candles` keeps the model default and an absent `history_candles` keeps the
realtime-level window, so a body written before this delivery behaves exactly as
it did.

**The warm-up contract is enforced, never silent.** A strategy declares how many
candles a frame must hold before it can emit **any** signal
(`Strategy.required_candles`, part of the frozen strategy contract; `0` for a
strategy that declares no warm-up). Three numbers decide whether a profile can be
fed, and `realtime.warmup` is the single arithmetic authority that compares them:
the candles the strategy **requires** on the profile's candle grid, the candles
the profile **asks for** (`warmup_candles`, the frame budget of §2.2) and the
candles the stream is configured to **serve** (`history_candles`, the realtime
setting or the profile's own override). Two findings come out of it, and they are
not the same thing:

* **`strategy-warmup-impossible` (`error`)** — `required_candles >
  warmup_candles`: the frame can *never* grow to the strategy's warm-up, so the
  profile would run for ever with zero signals. It is **refused where the profile
  is created**: `POST /api/profiles` answers the documented `400` with a message
  that names the strategy, the timeframe, the candles required (and the day
  lookback behind them), the candles the profile only ever asks the stream for,
  and the timeframes that **would** work with those parameters, cheapest grid
  first. The same sentence is used by every surface that reports it, so an
  operator who read it once recognises it everywhere.
* **`warmup-exceeds-history` (`warning`)** — `warmup_candles > history_candles`:
  the profile asks the stream for more candles than its window holds. It is
  **reported, never refused**: the frame the strategy receives is bounded by
  `warmup_candles` (§2.2), so a smaller stream window does not by itself silence
  the profile, and refusing it would refuse working configurations. It is logged
  at create (`profile_warmup_coherence`) and at start (`warmup_coherence`), both
  at `WARNING`, with the fix spelled out (raise `history_candles` — the realtime
  setting or the per-profile override — to at least `warmup_candles`, or lower
  `warmup_candles`). The stream the runner polls is built with the **effective**
  window — `cli.py`'s stream factory passes
  `profile.effective_history_candles(realtime.history_candles)` to
  `PollingMarketStream`, and the runner receives the same number — so the window
  the finding names is the window that actually polls.

**`realtime check` reports the same contract.** Every profile entry carries an
**additive** `warmup` key; the existing keys keep their names, their types and
their positions:

```
{"candles_per_day": num, "required_candles": int, "warmup_candles": int,
 "history_candles": int,
 "findings": [{"code": "strategy-warmup-impossible" | "warmup-exceeds-history",
               "severity": "error" | "warning", "message": str}]}
```

`candles_per_day` is the profile's grid (1440 on `1m`, 288 on `5m`, 24 on `1h`,
1 on `1d`), `history_candles` is the window the profile is effectively served
(its own override, or `realtime.history_candles`), and `findings` is `[]` for a
coherent profile. An **error** finding is also appended to that profile's
`issues`, and that is what keeps the documented semantics unchanged rather than
bending them: the profile reports `ok: false`, the payload reports `ok: false`
and `realtime check` exits `1`, exactly as for any other profile that cannot
start. A **warning** finding never reaches `issues`: it appears under
`warmup.findings` alone, and `ok` and the exit code are untouched — a profile
that is merely misconfigured on its window is still a profile that starts.

**Start-time semantics: never-warm-up is fatal, not-warm-yet is not.** At profile
start (`ProfileRunner._prepare`) the runner compares what the strategy requires
with what the profile asks the stream for, and the two outcomes are deliberately
different:

* when the requirement can **never** be satisfied with the configured
  `warmup_candles`, the runner logs `warmup_impossible` at `ERROR`, persists
  `ProfileStatus.ERROR` with the actionable message as its detail and raises, so
  the profile does **not** poll for ever with zero signals. At boot the
  orchestrator quarantines it — the failure is published through
  `profile_failures` on `GET /api/health` while every other profile keeps running
  — and a profile started later simply ends in `ERROR`;
* when the requirement is simply **not satisfied yet** but will be as candles
  accumulate, the tick logs `warmup_incomplete` at `WARNING` — naming the rows
  received, the candles required, `warmup_candles` and the grid — and returns
  without a decision. The profile **keeps running** and warms up on its own:
  every profile legitimately starts with a short frame, and ending it there would
  turn a normal warm-up into an outage.

**The `profiles` table is the source of truth.** Adding or removing a profile is
one transaction of the state store (an UPSERT on the natural key, or a `DELETE`),
so a failed write leaves the table untouched and the running engine consistent:
there is no document left half-rewritten, and nothing for a deployment to
overwrite.

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
not running, a refused flattening), `ConfigError` (`400`: an unknown strategy, an
unsupported timeframe, an unsupported value, a profile the store refuses) and --
for anything else -- the existing `500` boundary.

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
