# Operations runbook

Day-to-day operation of the deployed platform: starting and inspecting the
stack, checking health, provisioning the profile catalogue, changing the fleet
cap, engaging the kill switch, reading the logs and querying the state database.

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
| `profiles_running` | `0` → nothing is trading; see `profiles_queued` and the events |
| `profiles_healthy` | lower than `profiles_running` → at least one worker is failing its REST reads |
| `profiles_queued` | profiles held back by the fleet cap, or waiting for the stagger gate; `state_reason` names which |
| `engine_slots_used` / `engine_slots_total` | the fleet cap and how much of it is used |
| `kill_switch_engaged` | `true` → the kill switch is engaged, nothing will start |
| per-profile `state` / `state_reason` (`GET /api/profiles`) | `blocked` names a safety gate a live profile did not pass |

The deploy pipeline asserts exactly these numbers after a deploy:
`status == "ok"` and `profiles_running > 0` within 60 s, `3031/` answering 200 and
`3031/api/health` answering 200.

---

## 3. Provisioning the profile catalogue

`config/profiles.json` is the declarative catalogue (22 entries: 20 paper, 2
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

## 4. Raising the fleet cap

The cap is `max_running_profiles`. It exists because one running `freqtrade trade`
worker costs about **390 MiB RSS** (8 workers measured at 3.13 GiB, linear) and
the Docker VM has **12 GiB** on a 16 GiB host that already swaps heavily; the
default of **6** uses roughly **2.3 GiB** and leaves the rest for the supervisor,
the API, the dashboard and the build caches. Profiles beyond the cap are `queued`
with a `state_reason` naming the cap, and the next one is promoted automatically
when a slot frees.

Three ways to change it, in increasing order of permanence:

```bash
# 1. at run time, through the API (persisted in the settings table of state.db)
curl -fsS -X POST http://127.0.0.1:3030/api/settings \
  -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"max_running_profiles": 16}'

# 2. as an environment override, for one deployment (compose `environment:` or deploy/.env)
TB_MAX_RUNNING_PROFILES=16

# 3. as the documented default, in config/platform.json ("max_running_profiles": 16)
```

Precedence for every platform setting: the **environment**
(`TB_MAX_RUNNING_PROFILES`) wins at boot, then the value stored in the `settings`
table, then `config/platform.json`, then the built-in default. Raising the cap
starts queued profiles on the next scheduling pass — no restart needed when it is
done through the API. Before raising it, check the available memory:

```bash
docker stats --no-stream trading-realtime
sysctl -n hw.memsize | awk '{printf "%.2f GiB\n", $1/1024/1024/1024}'
```

Margin to keep: at least ~1 GiB for the supervisor, the dashboard, Docker itself
and the image builds. A cap that exceeds the available memory shows up as workers
being killed by the OOM killer and profiles landing in the state `error`.

The same arithmetic applies to `snapshot_interval_seconds`
(`TB_SNAPSHOT_INTERVAL_SECONDS`, default 60): a shorter interval gives finer
equity curves and more REST traffic per worker.

### `worker_start_stagger_seconds` — starting the fleet gradually

A third setting, `worker_start_stagger_seconds` (integer, default **10**, minimum
`0`, environment override `TB_WORKER_START_STAGGER_SECONDS`), controls how fast
the fleet fills its slots. It exists because a simultaneous cold start of many
workers spiked memory and CPU together and made the Docker VM unresponsive —
reproduced twice on this host. It is changed exactly like the cap, and
`POST /api/settings` accepts it:

```bash
# start at most one new worker every 30 s
curl -fsS -X POST http://127.0.0.1:3030/api/settings \
  -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"worker_start_stagger_seconds": 30}'

# 0 restores the immediate behaviour (every eligible profile starts at once)
curl -fsS -X POST http://127.0.0.1:3030/api/settings \
  -H "X-Operator-Token: $TB_OPERATOR_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"worker_start_stagger_seconds": 0}'
```

What it does, and what it does not do:

* the supervisor starts **at most one new worker per stagger interval**; after a
  worker starts, the gate closes for that interval;
* every other profile that is **eligible to run but has to wait** stays in the
  state `queued` with a `state_reason` of exactly the form
  `queued: starting workers gradually (N of M slots in use)`, where N is the
  number of workers alive and M is `max_running_profiles`;
* staggering only **delays promotions, it never reorders them**: the order stays
  priority descending, then id ascending;
* it therefore changes *when* a queued profile starts, never *whether* it does —
  a fleet that is genuinely beyond the cap stays queued until a slot frees.

---

## 5. Kill switch

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
* releasing it removes the file and resumes scheduling; queued profiles are
  promoted again and stopped profiles stay stopped until they are started;
* the state is reported by `GET /api/settings` (`kill_switch_engaged`) and by
  `GET /api/health` (`kill_switch_engaged`).

---

## 6. Logs

| What | Where |
| --- | --- |
| Supervisor + API (stdout of the container) | `docker compose -f deploy/docker-compose.yml logs -f trading-realtime` |
| Dashboard | `docker compose -f deploy/docker-compose.yml logs -f trading-dashboard` |
| One profile's freqtrade worker | `/app/data/realtime/logs/<id>.log` inside the container, e.g. `docker exec trading-realtime tail -50 /app/data/realtime/logs/momentum-btc-1h.log` |
| Engine journal (structured, queryable) | the `events` table of the state database (§7) |

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

## 7. Reading the state database

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

# the fleet at a glance: state, slots, ports
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

## 8. Troubleshooting

| Symptom | Likely cause and what to do |
| --- | --- |
| `status: "degraded"`, `profiles_running: 0` | the fleet has not started a worker yet (wait for the first poll), the kill switch is engaged, or every profile is disabled. Check `GET /api/settings`, then the `events` table for `cap_reached` / `kill_switch` rows |
| `profiles_running` below the cap, profiles `queued` | the cap is reached or the workers failed. Compare `engine_slots_used` with `engine_slots_total`; check `docker stats` for memory pressure |
| profiles `queued` with the reason `queued: starting workers gradually (N of M slots in use)` | normal right after a boot or a deploy: the supervisor starts at most one new worker per `worker_start_stagger_seconds` (10 s by default). Wait for the gate to reopen; lower the setting if the fleet must fill faster |
| a profile in state `error` | five restarts inside 15 minutes: read `last_error` and `/app/data/realtime/logs/<id>.log`, fix the cause, then `POST /api/profiles/{id}/actions {"action": "restart"}` |
| a live profile in state `blocked` | a safety gate is unmet: see `state_reason`; `TB_ALLOW_LIVE_TRADING` must equal `I_UNDERSTAND_THE_RISK` and both exchange credentials must be in the environment |
| `GET /api/profiles` answers `401`/`403` on a mutation | the `X-Operator-Token` header is missing (`401`) or wrong (`403`); the token is `TB_OPERATOR_TOKEN` of `deploy/.env` |
| the container restarts in a loop | read `docker compose -f deploy/docker-compose.yml logs --tail 100 trading-realtime`; a foreign `state.db` is archived automatically, so a loop usually means a bad `config/platform.json` or a missing catalogue file |
| `3031` answers but `/api/*` is stale | the dashboard proxies to `http://trading-realtime:8080` over the compose network; check that `trading-realtime` is healthy |
| no new snapshots | the snapshot interval is longer than expected (`GET /api/settings`) or the supervisor is stopped; snapshots only exist for profiles that were running |
| a worker keeps dying on a slow grid | the generated config throttles per timeframe (4h/1d → 60 s); check the worker log for exchange rate limits |

---

## 9. Restart, upgrade, rollback

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
`error` state; snapshots keep the before/after history in one table. The workers
come back one per `worker_start_stagger_seconds`, so a deploy does not spike the
guest's memory and CPU the way a simultaneous cold start does.

A database of this platform written by an earlier revision (`user_version = 1`,
current `profiles` columns) is **migrated in place** on the next boot: the two
new tables and their index are created, the version is stamped `2`, the file is
never archived, never renamed and no data is lost. A file that is not this
platform's schema, or a `user_version` that is neither `1` nor `2`, is still
incompatible: the boot archives it as `state.db.legacy-<timestamp>` and starts
fresh — the previous file is recoverable from the volume, and the event journal
records the archive.
