# Operations runbook

Day-to-day operation of the deployed platform: starting and inspecting the
stack, checking health, provisioning the profile catalogue, keeping workers
healthy, engaging the kill switch, reading the logs and querying the state
database.

Everything below assumes the host repository `/Users/mac/Projects/Trading` and
the compose file `deploy/docker-compose.yml`.

| Surface | Address |
| --- | --- |
| Dashboard (and `/api/*` through its rewrite) | `http://127.0.0.1:3031` and `https://tristeubadingview.duckdns.org` |
| Aggregated JSON API (debugging surface) | `http://127.0.0.1:3030` |
| Container names | `trading-realtime`, `trading-dashboard` |

---

## 1. Compose commands

```bash
cd /Users/mac/Projects/Trading

# start / rebuild / wait for both healthchecks
docker compose -f deploy/docker-compose.yml up -d --build --wait

# state of the two containers (and of their healthchecks)
docker compose -f deploy/docker-compose.yml ps

# follow the supervisor logs (and the dashboard's)
docker compose -f deploy/docker-compose.yml logs -f trading-realtime
docker compose -f deploy/docker-compose.yml logs -f trading-dashboard

# stop (volumes and therefore profiles, trades and snapshots are kept)
docker compose -f deploy/docker-compose.yml down

# restart one container without rebuilding
docker compose -f deploy/docker-compose.yml restart trading-realtime

# rebuild ONE image
docker compose -f deploy/docker-compose.yml build trading-realtime
```

`down -v` deletes the named volumes `deploy_trading-state` and
`deploy_trading-cache`: it is the only way to lose the profiles, their snapshots,
their trade databases and their OHLCV caches. A rebuild (`up -d --build`) and a
`down` do **not** touch them.

---

## 2. Health checks

```bash
# the engine: must answer {"status": "ok", ..., "profiles_running": N} with N > 0
curl -fsS http://127.0.0.1:3030/api/health | python3 -m json.tool

# the same document through the dashboard origin (the /api/* rewrite)
curl -fsS http://127.0.0.1:3031/api/health

# the dashboard UI
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3031/

# the container healthchecks themselves
docker inspect --format '{{.State.Health.Status}}' trading-realtime
docker inspect --format '{{.State.Health.Status}}' trading-dashboard
```

What to read in `GET /api/health`:

| Field | Meaning when it is wrong |
| --- | --- |
| `status` | `degraded` means the API serves but **no** profile is running |
| `profiles_running` | `0` → nothing is trading; see `profiles_stopped`, `profiles_blocked`, `profiles_error` and the events |
| `profiles_healthy` | lower than `profiles_running` → at least one worker is failing its REST reads |
| `profiles_queued` | **always `0`**. The field is kept on the wire for compatibility with older dashboards; there is no fleet cap and no queue, so no profile can ever wait for a slot |
| `kill_switch_engaged` | `true` → the kill switch is engaged, nothing will start |
| per-profile `state` / `state_reason` (`GET /api/profiles`) | a profile that is not `running` is `stopped`, `blocked` or `error` — there is no fourth outcome. `blocked` names a safety gate a live profile did not pass |

The deploy pipeline asserts exactly these numbers after a deploy:
`status == "ok"` and `profiles_running > 0` within 60 s, `3031/` answering 200 and
`3031/api/health` answering 200.

---

## 3. Provisioning the profile catalogue

`config/profiles.json` is the declarative catalogue (32 entries: 30 paper, 2
live). Applying it is an **idempotent upsert**: catalogue-owned profiles are
created or refreshed, operator-created profiles are never touched, and a live
entry whose gates are unmet is reported in `refused_live` instead of being
created.

Run it **inside** the container, where `TB_OPERATOR_TOKEN` already travels
through `deploy/.env` (`env_file:` in the compose file) — no secret on a command
line, in a shell history or in a log:

```bash
# 1. dry run: prints created / updated / skipped / pruned / refused_live, changes nothing
docker exec trading-realtime trading realtime provision --api-url http://127.0.0.1:8080 --dry-run

# 2. apply
docker exec trading-realtime trading realtime provision --api-url http://127.0.0.1:8080

# 3. prune: the ONLY form that deletes catalogue-foreign profiles (operator ones included)
docker exec trading-realtime trading realtime provision --api-url http://127.0.0.1:8080 --prune

# 4. force: skip the catalogue-vs-platform coherence refusal (--dry-run ignores it)
docker exec trading-realtime trading realtime provision --api-url http://127.0.0.1:8080 --force
```

The same command works from the host venv against the published port, provided
the token is exported in that shell (never passed as an argument):

```bash
export TB_OPERATOR_TOKEN=...      # from deploy/.env, never typed into a commit
.venv/bin/python -m trading_platform realtime provision --api-url http://127.0.0.1:3030 --dry-run
```

Profiles created through the dashboard are owned by the operator (`source =
operator`) and are never removed by a plain apply — only `--prune` (or
`DELETE /api/profiles/{id}?force=true` for a catalogue id) deletes anything.

After applying, check the fleet:

```bash
curl -fsS http://127.0.0.1:3030/api/profiles | python3 -m json.tool | head -40
docker exec trading-realtime trading realtime status --api-url http://127.0.0.1:8080
```

---

## 4. The fleet has no cap and no queue

**Every enabled profile runs.** The supervisor starts one `freqtrade trade`
worker per enabled profile, in `priority` descending then `id` ascending order,
and nothing holds a profile back: there is no fleet cap, no slot accounting and
no stagger gate.

Consequences an operator has to know:

* a profile that is not `running` is in exactly one of three other states —
  `stopped` (disabled, stopped by the operator, stopped by the kill switch, or
  waiting for its restart backoff), `blocked` (a live profile that did not pass
  the live-trading gate) or `error` (the restart budget is spent). Nothing is
  ever "waiting for a slot";
* `GET /api/settings` no longer carries the fleet-cap setting or the gradual-start
  setting. Both were removed from `config/platform.json`, from the compose
  environment and from `POST /api/settings`; a `POST` that still sends one of
  those keys simply ignores it (unknown body keys are dropped);
* `GET /api/health` still carries `profiles_queued`, pinned to the constant `0`,
  so an older dashboard reading that field keeps working. The slot-usage pair
  that used to accompany it is gone from the payload;
* the effective settings the engine acts on are therefore:
  `snapshot_interval_seconds` and `profile_api_port_base`, plus the
  `config/platform.json` values that have no environment override.

Because all enabled profiles start together, the container's memory footprint is
the sum of every worker. On this host that is the whole catalogue, and the
figure that used to justify a cap — **~390 MiB RSS per worker** — no longer
bounds anything. Check the real numbers before and after a deploy rather than
scaling that figure by hand:

```bash
docker stats --no-stream trading-realtime
sysctl -n hw.memsize | awk '{printf "%.2f GiB\n", $1/1024/1024/1024}'
```

`snapshot_interval_seconds` (`TB_SNAPSHOT_INTERVAL_SECONDS`, default 60) is
still a run-time setting: a shorter interval gives finer equity curves and more
REST traffic per worker. It is also the poll cadence the health rule of §5 is
counted in.

---

## 5. The worker crash loop, and why workers are no longer restarted on a slow read

### What the loop was

The workers were **healthy**. Their `GET /balance` took **12 to 20 s** because
CoinGecko rate-limits the whole fleet behind one IP — the platform ran 22
workers, and each `freqtrade` worker refreshed a USD fiat conversion on every
API request. Our REST client gave up at **10 s**, so a slow answer was recorded
as a read failure. Three consecutive timeouts declared a worker unhealthy and
the supervisor killed and restarted it. A restarted worker immediately joined
the same queue and hit the same rate limit, so the fleet restarted forever and
never finished its warm-up.

### What changed

1. **The generated freqtrade configuration no longer asks for a fiat
   conversion.** `engine/config_builder.py` now writes
   `"fiat_display_currency": ""`. Freqtrade treats an empty conversion currency
   as "no conversion requested" and never builds the converter — from
   `freqtrade/rpc/rpc.py`, `RPC.__init__`:

   ```python
   if self._config.get("fiat_display_currency"):
       self._fiat_converter = CryptoToFiatConverter(self._config)
   ```

   With `_fiat_converter` left as `None`, `_rpc_balance` takes its own fallback
   and answers `value = 0` without calling CoinGecko at all:

   ```python
   value = (
       self._fiat_converter.convert_amount(total, stake_currency, fiat_display_currency)
       if self._fiat_converter
       else 0
   )
   ```

   The consequence for the operator: `GET /balance`, `GET /profit` and
   `GET /daily` still answer with their **full field set**, and their `fiat_*`
   values are simply `0`. No field disappears from the payload.

2. **The read timeout was raised above the measured worst case.**
   `DEFAULT_TIMEOUT_SECONDS` in `engine/client.py` is **45.0 s**. The values the
   constant is sized against are the worst `GET /balance` durations recorded on
   the live deployment while 22 workers shared one CoinGecko rate limit:
   **19.74 s**, **19.77 s** and **11.94 s**. They are recorded in the source
   comment of the constant, not re-measured for this document; a worker that is
   merely slow now answers instead of timing out.

3. **A worker is restarted only when it is proven gone.** The health rule is
   counted in poll cycles (`snapshot_interval_seconds`, 60 s by default) and it
   is defined by five named constants in `engine/supervisor.py` and
   `engine/client.py`:

   | Constant | Value | What it does |
   | --- | --- | --- |
   | `DEFAULT_TIMEOUT_SECONDS` | `45.0` s | per-request read timeout of every worker call |
   | `WORKER_STARTUP_GRACE_SECONDS` | `90.0` s | a freshly spawned worker is not judged at all: a failed read inside this window is not even counted |
   | `UNHEALTHY_THRESHOLD` | `10` | consecutive failed reads that merely **start** the death check |
   | `UNHEALTHY_PING_THRESHOLD` | `10` | consecutive failed `GET /ping` that **prove** the worker is gone |
   | `RESTART_BACKOFF_SECONDS` | `(5, 15, 45)` s | backoff before a restart, saturating at 45 s |

   Reaching `UNHEALTHY_THRESHOLD` no longer restarts anything by itself. The
   supervisor then asks two questions, and accepts only two proofs of death: the
   process handle is gone, or the worker's own liveness endpoint `GET /ping` has
   failed `UNHEALTHY_PING_THRESHOLD` consecutive times. A worker that answers
   `{"status": "pong"}` is alive and is **not** restarted, however many
   `/balance` reads failed. A `GET /ping` that raises counts as a failed ping,
   never as a crash.

   The restart budget is unchanged: at most **5 restarts within 15 minutes**,
   then the profile goes to the terminal state `error`.

### How to read the difference in the journal

The loop showed a `restart` event per profile again and again, on a cadence set
by the supervisor's own poll interval (`snapshot_interval_seconds`, 60 s by
default): three consecutive timed-out reads at the old 10 s timeout crossed the
old three-failure threshold within a few poll cycles, and the 5 s backoff did not
slow the cycle down. After the fix that pattern must be gone — a restart is now
preceded by `UNHEALTHY_THRESHOLD` failed reads **and** `UNHEALTHY_PING_THRESHOLD`
failed pings, so it takes a genuinely dead worker:

```bash
# restart events, newest first: a repeating ~2-minute cadence is the loop
docker exec trading-realtime python -c "
import sqlite3
con = sqlite3.connect('/app/data/realtime/state.db')
for row in con.execute(\"SELECT ts, kind, profile_id, message FROM events WHERE kind IN ('restart','crash','error') ORDER BY ts DESC LIMIT 40\"):
    print(row)
"
```

If a `restart` row keeps reappearing for the same profile with roughly the same
gap between rows, the worker is answering `/ping` but failing its reads: read
`/app/data/realtime/logs/<id>.log` (§7) for the underlying failure instead of
restarting the fleet again.

---

## 6. Kill switch

Two equivalent forms, one meaning: **stop everything and start nothing**.

```bash
# through the API (requires the operator token; the dashboard's kill switch does this)
curl -fsS -X POST http://127.0.0.1:3030/api/kill-switch \
  -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' -d '{"engaged": true}'

# ... and release it
curl -fsS -X POST http://127.0.0.1:3030/api/kill-switch \
  -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' -d '{"engaged": false}'

# out-of-band, if the API itself is unreachable
docker exec trading-realtime touch /app/data/realtime/KILL_SWITCH   # engage
docker exec trading-realtime rm    /app/data/realtime/KILL_SWITCH   # release

# verify
docker exec trading-realtime ls -l /app/data/realtime/KILL_SWITCH
curl -fsS http://127.0.0.1:3030/api/settings | python3 -m json.tool
```

Semantics to rely on:

* engaging it stops every worker, sets every running profile to `stopped` with
  the reason `kill_switch`, and creates `/app/data/realtime/KILL_SWITCH`;
* while the file exists, **no worker starts** — not even after a container
  restart, because the file lives in the `trading-state` volume;
* releasing it removes the file and resumes scheduling; every enabled profile
  that is not stopped by the operator starts again on the next pass, and stopped
  profiles stay stopped until they are started;
* the state is reported by `GET /api/settings` (`kill_switch_engaged`) and by
  `GET /api/health` (`kill_switch_engaged`).

---

## 7. Logs

| What | Where |
| --- | --- |
| Supervisor + API (stdout of the container) | `docker compose -f deploy/docker-compose.yml logs -f trading-realtime` |
| Dashboard | `docker compose -f deploy/docker-compose.yml logs -f trading-dashboard` |
| One profile's freqtrade worker | `/app/data/realtime/logs/<id>.log` inside the container, e.g. `docker exec trading-realtime tail -50 /app/data/realtime/logs/momentum-btc-1h.log` |
| Engine journal (structured, queryable) | the `events` table of the state database (§8) |

```bash
# verbosity of the supervisor (INFO by default)
docker exec trading-realtime printenv TB_LOG_LEVEL

# the newest worker logs, one line each
docker exec trading-realtime sh -c 'ls -lt /app/data/realtime/logs | head'

# everything the engine logged today, newest first
docker exec trading-realtime python -c "
import sqlite3
con = sqlite3.connect('/app/data/realtime/state.db')
for row in con.execute('SELECT ts, level, kind, profile_id, message FROM events ORDER BY ts DESC LIMIT 30'):
    print(row)
"
```

---

## 8. Reading the state database

One SQLite file: `/app/data/realtime/state.db` (host path: the
`deploy_trading-state` volume; local runs: `data/realtime/state.db`). Its schema
is documented in `docs/architecture.md` §3. Reading it is always safe; **do not
write to it while the supervisor runs**, and never `VACUUM` it under a live
engine.

The container image is not guaranteed to ship the `sqlite3` command-line tool,
so the guaranteed path is Python (always present):

```bash
# the schema version: must print 2
docker exec trading-realtime python -c "import sqlite3; print(sqlite3.connect('/app/data/realtime/state.db').execute('PRAGMA user_version').fetchone()[0])"

# the fleet at a glance: state, ports
docker exec trading-realtime python -c "
import sqlite3
con = sqlite3.connect('/app/data/realtime/state.db')
rows = con.execute('SELECT state, COUNT(*) FROM profiles GROUP BY state ORDER BY state').fetchall()
print('states:', rows)
print('enabled:', con.execute('SELECT COUNT(*) FROM profiles WHERE enabled = 1').fetchone()[0])
"

# the ranked running profiles
docker exec trading-realtime python -c "
import sqlite3
con = sqlite3.connect('/app/data/realtime/state.db')
for row in con.execute('SELECT id, strategy, timeframe, mode, state, api_port, pid FROM profiles ORDER BY priority DESC, id'):
    print(row)
"

# the newest snapshot of every profile
docker exec trading-realtime python -c "
import sqlite3
con = sqlite3.connect('/app/data/realtime/state.db')
for row in con.execute('''
    SELECT s.profile_id, s.ts, ROUND(s.portfolio_value, 2), ROUND(s.profit_pct * 100, 2), s.open_trades
    FROM profile_snapshots s
    JOIN (SELECT profile_id, MAX(ts) AS ts FROM profile_snapshots GROUP BY profile_id) m
      ON m.profile_id = s.profile_id AND m.ts = s.ts
    ORDER BY s.portfolio_value DESC
'''):
    print(row)
"

# the equity history of one profile, last two hours
docker exec trading-realtime python -c "
import sqlite3
con = sqlite3.connect('/app/data/realtime/state.db')
for row in con.execute(\"SELECT ts, portfolio_value, profit_pct FROM profile_snapshots WHERE profile_id = 'momentum-btc-1h' ORDER BY ts DESC LIMIT 120\"):
    print(row)
"

# the engine settings
docker exec trading-realtime python -c "
import sqlite3
con = sqlite3.connect('/app/data/realtime/state.db')
for key, value, updated_at in con.execute('SELECT key, value, updated_at FROM settings ORDER BY key'):
    print(key, '=', value, '(', updated_at, ')')
"
```

With the `sqlite3` CLI installed (`brew install sqlite3`), the same reads are:

```bash
sqlite3 /app/data/realtime/state.db "PRAGMA user_version;"
sqlite3 -header -column /app/data/realtime/state.db "SELECT id, state, api_port FROM profiles ORDER BY id;"
sqlite3 -header -column /app/data/realtime/state.db "SELECT ts, level, kind, message FROM events ORDER BY ts DESC LIMIT 20;"
sqlite3 -header -column /app/data/realtime/state.db "SELECT COUNT(*), MIN(ts), MAX(ts) FROM profile_snapshots;"
```

Backup and inspection from the host:

```bash
# consistent copy of a live database (safe while the supervisor runs)
docker exec trading-realtime python -c "
import sqlite3
src = sqlite3.connect('/app/data/realtime/state.db')
dst = sqlite3.connect('/app/data/realtime/state.db.backup')
src.backup(dst); dst.close(); src.close()
"
docker cp trading-realtime:/app/data/realtime/state.db.backup ./state.db.backup
rm -f ./state.db.backup   # it is git-ignored, but do not leave state lying around
```

A file named `state.db.legacy-<timestamp>` in the same directory is the **archived
foreign database** of a previous platform: it was renamed, never parsed, and it
can be deleted once it has been inspected.

---

## 9. Troubleshooting

| Symptom | Likely cause and what to do |
| --- | --- |
| `status: "degraded"`, `profiles_running: 0` | the fleet has not started a worker yet (wait for the first poll), the kill switch is engaged, or every profile is disabled. Check `GET /api/settings`, then the `events` table for `kill_switch` rows |
| `profiles_running` lower than `profiles_total` | some workers failed to start or died. Every enabled profile is supposed to run, so compare `profiles_total` with `profiles_running` and read the per-profile `state`: `stopped`, `blocked` or `error`. Then check `docker stats` for memory pressure |
| a profile `stopped` with the reason `restart_backoff: …` | normal for a few seconds: the worker is dead and waits for its backoff (5 s, 15 s or 45 s) before the restart |
| a worker restarted every ~2 minutes | the worker answers `GET /ping` but fails its reads, so the new rule must **not** restart it — check `docker stats`, the worker log and the disk before looking at the supervisor |
| all workers restarted at once after a deploy | expected: a deploy restarts every worker. The restart resets the in-memory health counters and clears a stale `error` state |
| a profile in state `error` | five restarts inside 15 minutes: read `last_error` and `/app/data/realtime/logs/<id>.log`, fix the cause, then `POST /api/profiles/{id}/actions {"action": "restart"}` |
| a live profile in state `blocked` | a safety gate is unmet: see `state_reason`; `TB_ALLOW_LIVE_TRADING` must equal `I_UNDERSTAND_THE_RISK` and both exchange credentials must be in the environment |
| `GET /api/profiles` answers `401`/`403` on a mutation | the `X-Operator-Token` header is missing (`401`) or wrong (`403`); the token is `TB_OPERATOR_TOKEN` of `deploy/.env` |
| the container restarts in a loop | read `docker compose -f deploy/docker-compose.yml logs --tail 100 trading-realtime`; a foreign `state.db` is archived automatically, so a loop usually means a bad `config/platform.json` or a missing catalogue file |
| `3031` answers but `/api/*` is stale | the dashboard proxies to `http://trading-realtime:8080` over the compose network; check that `trading-realtime` is healthy |
| no new snapshots | the snapshot interval is longer than expected (`GET /api/settings`) or the supervisor is stopped; snapshots only exist for profiles that were running |
| a worker keeps dying on a slow grid | the generated config throttles per timeframe (4h/1d → 60 s); check the worker log for exchange rate limits |

---

## 10. Restart, upgrade, rollback

```bash
# restart the engine only (volumes, profiles and trades are kept)
docker compose -f deploy/docker-compose.yml restart trading-realtime

# deploy the current checkout
docker compose -f deploy/docker-compose.yml up -d --build --wait
curl -fsS http://127.0.0.1:3030/api/health | python3 -m json.tool

# rollback: revert the commit on main; the push triggers a fresh deploy
git revert <sha> && git push origin main
```

An upgrade never touches the `trading-state` volume, so profiles, snapshots,
trade databases and the kill-switch file survive a rebuild. A deploy **restarts
every worker**, which resets the in-memory health counters and clears a stale
`error` state; snapshots keep the before/after history in one table. Every
enabled profile comes back in the same scheduling pass, so a deploy is
immediately followed by the full fleet's cold start — watch `docker stats` on a
deploy if the host is tight on memory.

A database of this platform written by an earlier revision (`user_version = 1`,
current `profiles` columns) is **migrated in place** on the next boot: the two
new tables and their index are created, the version is stamped `2`, the file is
never archived, never renamed and no data is lost. A file that is not this
platform's schema, or a `user_version` that is neither `1` nor `2`, is still
incompatible: the boot archives it as `state.db.legacy-<timestamp>` and starts
fresh — the previous file is recoverable from the volume, and the event journal
records the archive.
