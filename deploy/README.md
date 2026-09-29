# Local deployment — multi-profile trading platform

This folder holds **everything needed to run the platform** on the machine,
behind the host nginx. Two containers, one public URL:

| Item | Value |
| --- | --- |
| Public URL | <https://tristeubadingview.duckdns.org> |
| Authentication | HTTP Basic (`auth_basic`), realm `Trading` |
| Dashboard container | `trading-dashboard` (image `trading-dashboard:latest`), Next.js standalone server |
| Realtime container | `trading-realtime` (image `trading-realtime:latest`), supervisor + N freqtrade workers + aggregated JSON API |
| Dashboard listen | `0.0.0.0:3000` inside the container, published on **`127.0.0.1:3031`** |
| API listen | `0.0.0.0:8080` inside the container, published on **`127.0.0.1:3030`** (debugging only) |
| TLS termination | **host nginx**, never a container |
| Protection | nginx `limit_req` + fail2ban jail `nginx-auth` (shared) |

---

## 1. The two containers

```
deploy/
├── Dockerfile.realtime    # freqtradeorg/freqtrade:2026.8 + this package (pip install --no-deps .)
├── Dockerfile.dashboard   # node:24-alpine, multi-stage, Next.js standalone output
├── docker-compose.yml     # both services, their ports, their volumes and their healthchecks
├── README.md              # this document
└── .env                   # TB_OPERATOR_TOKEN — git-ignored, never in an image
```

```bash
docker compose -f deploy/docker-compose.yml up -d --build --wait   # build, start, wait for health
docker compose -f deploy/docker-compose.yml logs -f                # follow both
docker compose -f deploy/docker-compose.yml ps                     # state + healthchecks
docker compose -f deploy/docker-compose.yml down                   # stop (volumes kept)
docker compose -f deploy/docker-compose.yml down -v                # stop AND wipe state
```

### `trading-realtime` — the supervisor, its fleet and the JSON API

The container runs
`python -m trading_platform realtime run --state-db /app/data/realtime/state.db
--host 0.0.0.0 --port 8080` (that is the image's `ENTRYPOINT` + `CMD`), as the
unprivileged user `ftuser` of the base image, with `restart: unless-stopped` and a
healthcheck that queries `GET /api/health` on port 8080. The Python server is a
**pure JSON API**: it serves no HTML page and no static asset; the dashboard is
the only user interface.

That one process does three things (§1bis):

1. it is the **supervisor**: it schedules the profiles, generates one freqtrade
   configuration per running profile and spawns one `freqtrade trade` **child
   process** for each of them. It never trades itself;
2. it **polls** every worker over that worker's private freqtrade REST API
   (`127.0.0.1:8101 + index`, inside the container only) and writes minute
   snapshots and engine events into the state database;
3. it **serves** the aggregated JSON API the dashboard reads, on port 8080.

Its port is published on the loopback interface **for debugging only** — nothing
in the browser ever reaches it directly, and the per-profile worker ports are not
published at all.

### `trading-dashboard` — the Next.js UI

The container runs the standalone Next.js server (`node server.js`) on port
3000, as the non-root user `nextjs` (uid 1001). Its healthcheck uses Node's
global `fetch` against `GET http://127.0.0.1:3000/` — the image carries no
`curl` and no `wget`.

The browser always talks to the dashboard's own origin. `next.config.ts`
declares `rewrites()` mapping `/api/:path*` to `API_ORIGIN`, so:

* **no CORS**: the request is same-origin for the browser, which never sees the
  Python host name;
* **no absolute URL** in the client bundle;
* the **`X-Operator-Token` header flows through untouched** (the rewrite is a
  transparent proxy, not a request rebuild).

`API_ORIGIN` is **baked into the image at build time**: `rewrites()` is resolved
when the Next configuration is evaluated. Composed here it is
`http://trading-realtime:8080` (the compose service name); the default outside
Docker is `http://127.0.0.1:8080`. Changing it means **rebuilding the image**:

```bash
docker compose -f deploy/docker-compose.yml build trading-dashboard
docker compose -f deploy/docker-compose.yml up -d trading-dashboard
```

### Dashboard branding assets

The dashboard ships its own icon set, and it is part of the image like every
other page: three files under `dashboard/src/app/` (the Next.js 16 **App Router
file-based metadata** convention) are emitted as the document's icon tags.

| File | Format | Emitted as |
| --- | --- | --- |
| `dashboard/src/app/icon.svg` | SVG, 32x32 viewBox, `#020617` background and `#16A34A` mark | `<link rel="icon">` |
| `dashboard/src/app/favicon.ico` | real multi-resolution ICO carrying 16x16, 32x32 and 48x48 entries | the `/favicon.ico` request |
| `dashboard/src/app/apple-icon.png` | PNG, 180x180, opaque (no alpha) | `<link rel="apple-touch-icon">` |

Two consequences worth knowing:

* **no hand-written `<head>` tag and no extra dependency**: the App Router
  discovers the three files by their names and generates the tags, so the icons
  cannot drift out of sync with the build and no icon package is installed;
* **the files are served by the dashboard origin itself**: `/favicon.ico`,
  `/icon.svg` and `/apple-icon.png` answer `200` on `127.0.0.1:3031` (and therefore
  behind nginx too), which is what lets the browser and the iOS home-screen
  bookmark render the branding without any extra nginx rule.

### Volumes (state survives a restart)

| Volume | Path | Content | Used by |
| --- | --- | --- | --- |
| `trading-state` | `/app/data/realtime` | the SQLite state database (`state.db`), the generated per-profile freqtrade configs (`profiles/<id>/config.json`), their trade databases (`profiles/<id>/tradesv3.sqlite`), their OHLCV caches (`profiles/<id>/data/`), the per-profile logs (`logs/<id>.log`) and the `KILL_SWITCH` file | `trading-realtime` |
| `trading-cache` | `/app/data/cache` | reserved for a **future shared OHLCV cache**; created and owned by `ftuser`, written by no code path today | `trading-realtime` |

The dashboard is **stateless**: it owns no volume, no database and no durable
file. Restarting or rebuilding it loses nothing, and its operator token lives
only in the browser's `sessionStorage` (never on disk, never rendered back).

### The SQLite state database is the source of truth

The profile set and the platform settings live in **one SQLite database**:
`/app/data/realtime/state.db`, inside the `trading-state` volume, with schema
version `2` (`PRAGMA user_version`). It owns six tables and three indexes
(`idx_profile_snapshots_ts`, `idx_events_ts`, `idx_profile_trades_open`).

| What | Where |
| --- | --- |
| Profiles and their state, ports, pids | the `profiles` table, one row per profile |
| Platform settings | the `settings` table, one `platform_settings` row holding the whole settings document |
| Minute snapshots (portfolio value, cash, profit, trades, health) | the `profile_snapshots` table |
| Trades of every profile, open and closed, mirrored from its worker | the `profile_trades` table |
| Daily profit rows of every profile, mirrored from its worker | the `profile_daily` table |
| Engine journal (start, stop, crash, restart, error, kill switch, legacy archive) | the `events` table |

Consequences an operator must know:

* **profiles created through the dashboard** are written to that database (with
  `source = operator`), not to a file in this repository, and the declarative
  catalogue of `config/profiles.json` is applied to it explicitly
  (`POST /api/catalogue/apply`, or the provisioning command of §4);
* **deleting the state volume is the only way to lose them**
  (`docker compose -f deploy/docker-compose.yml down -v`), and a rebuild of the
  images (`up -d --build`) keeps them, because a named volume survives a rebuild;
* **no file of the checkout is written by any container**: there is no bind mount
  of `deploy/`, so nothing the runtime does needs write access on the host and the
  image never needs `chmod` on a host directory;
* **a foreign database is archived, never parsed.** If the volume still holds a
  `state.db` from an earlier platform (a different schema), the boot renames it to
  `state.db.legacy-<UTC timestamp>` in the same directory and creates a fresh
  database; the archive is never deleted automatically and an `events` row
  records it. A database of *this* platform at revision `1` is not foreign: it is
  **migrated in place** to revision `2`, the file is never archived and no data is
  lost. This is what the deployment this repository replaces leaves behind,
  and it is why the first boot of the new stack is deterministic.

The settings are resolved at boot with this precedence, weakest first: the
defaults the supervisor was constructed with, then the keys actually present in
`config/platform.json`, then the persisted `platform_settings` row of the
`settings` table, then the `TB_*` environment variables — the environment always
wins, because it is the only input an operator changes without writing to the
state volume. An operator change made through `POST /api/settings` is written as
that one `platform_settings` row and survives a restart; the route accepts
`snapshot_interval_seconds` only, because that is the only setting of the
document whose value is genuinely a site preference. The only thing that stays
outside the database is the **path** of the database itself: it comes from
`--state-db` (the image passes `/app/data/realtime/state.db`), from
`TB_REALTIME_STATE_DB`, or from the model default. A host that never customises
anything behaves exactly as documented.

### Secrets

`deploy/.env` carries the **operator token** of the aggregated API — the token
every mutating route and the dashboard's kill switch have to present. It is
git-ignored **and** excluded from the build context by `.dockerignore`, so it
never enters an image layer; the dashboard never reads it, the operator types it
and the browser keeps it in `sessionStorage` only.

Exchange credentials are a different matter and follow a different rule: they
come **only** from the environment (`TB_LIVE_EXCHANGE_KEY` /
`TB_LIVE_EXCHANGE_SECRET`), they are never written into a configuration file,
never logged and never returned by the API. No value of any secret is documented
here — the file names and the variable names are, and nothing else. The shipped
stack configures neither key, so every profile runs in `paper` mode.

---

## 1bis. The freqtrade fleet inside `trading-realtime`

### One worker per running profile

For every profile the supervisor schedules, it writes a **generated freqtrade
configuration** to `/app/data/realtime/profiles/<id>/config.json` (mode `0600`)
and starts one `freqtrade trade` child process with that configuration, its own
`--userdir` (`profiles/<id>`), its own `--db-url`
(`sqlite:///profiles/<id>/tradesv3.sqlite`), its own `--logfile`
(`logs/<id>.log`) and `--strategy-path /app/user_data/strategies`. The image
therefore contains the ten strategies and the three catalogue documents
(`config/platform.json`, `config/strategies.json`, `config/profiles.json`), and
none of them is bind-mounted: they are baked at build time, and the *state* of the
profiles lives in the volume.

Each worker gets its own private REST API, bound to `127.0.0.1` **inside the
container**, on `TB_PROFILE_API_PORT_BASE` (default `8101`) plus its index in the
id-sorted profile catalogue. Those ports are deterministic — the same profile
always gets the same port — and they are never published, so no worker surface is
reachable from the host or from the network.

### The profile catalogue

`config/profiles.json` declares the 22 profiles of the platform: 20 `paper`
profiles (1000 USDT of dry-run wallet each) and 2 `live` profiles (250 USDT of
reference capital each). Every strategy of the ten has at least two profiles, so
two grids of the same rule set are always comparable side by side.

The catalogue is **declarative**: applying it is idempotent, it never touches
profiles created through the dashboard, and a `live` entry whose preconditions are
unmet is reported as refused instead of being created (§4). Adding a profile is a
one-entry data edit; adding a strategy is a file in `user_data/strategies/` plus
one metadata entry in `config/strategies.json` (the metadata is optional — the
file alone is enough).

### Every enabled profile runs

**The fleet is not capped and has no queue.** The supervisor starts one
`freqtrade trade` worker per enabled profile, in `priority` descending then `id`
ascending order, and nothing holds a profile back. A profile that is not
`running` is `stopped`, `blocked` (live-trading gate unmet, §below) or `error`
(restart budget spent) — it is never "waiting for a slot".

There is therefore no setting to raise or lower:

* `deploy/docker-compose.yml` does not define a fleet-cap variable and does not
  define a gradual-start variable. Its `trading-realtime` service environment
  carries `TB_LOG_LEVEL` and `TB_SNAPSHOT_INTERVAL_SECONDS` only;
* `config/platform.json` does not carry either setting. The document holds the
  nine keys listed in `tests/contract/test_config_documents.py`;
* `POST /api/settings` accepts `snapshot_interval_seconds` only, and
  `GET /api/settings` does not publish the two removed settings. `GET /api/health`
  still carries `profiles_queued`, pinned to the constant `0`, so an older
  dashboard reading it keeps working.

The consequence to plan for: **all enabled profiles start together**, so the
container's memory footprint is the sum of every worker. Measure it, do not
estimate it:

```bash
docker stats --no-stream trading-realtime
```

The historical figure of **~390 MiB RSS per worker** (8 instances = 3.13 GiB,
linear) is what an earlier revision measured on this host; it is not a bound on
anything now that every profile starts. Check `docker stats` before and after a
deploy instead of scaling it by hand.

### Live-trading preconditions

A profile with `mode: "live"` is **never started** unless all three hold:

| Gate | Check |
| --- | --- |
| Explicit acknowledgement | `TB_ALLOW_LIVE_TRADING` equals exactly `I_UNDERSTAND_THE_RISK` |
| Exchange key | `TB_LIVE_EXCHANGE_KEY` present in the environment |
| Exchange secret | `TB_LIVE_EXCHANGE_SECRET` present in the environment |

Otherwise the profile stays `blocked`, with a reason naming the missing
precondition, visible in the API and in the dashboard. This stack configures none
of them, so its two live catalogue entries are refused (`refused_live`) — which is
the intended state of the deployment, not a defect.

### Why the deploy smoke test holds

`.github/workflows/deploy.yml` asserts, right after
`up -d --build --wait`, that `GET http://127.0.0.1:3030/api/health` reports
`status == "ok"` and `profiles_running > 0` within 60 s (twelve retries five
seconds apart), that `GET http://127.0.0.1:3031/` answers 200 and that
`GET http://127.0.0.1:3031/api/health` answers 200. The first boot makes that
true in this order:

1. the container starts; the supervisor opens `/app/data/realtime/state.db` and,
   if the volume holds a database of another platform, **archives** it as
   `state.db.legacy-<timestamp>` and creates a fresh one (an unrelated schema can
   therefore never abort the boot);
2. it seeds the **22-profile catalogue** and computes the schedule;
3. it computes the schedule — every enabled profile, sorted by `priority`
   descending then `id` ascending — and spawns one `freqtrade trade` worker for
   each: nothing caps the fleet and nothing queues a profile;
4. the API starts serving as soon as the supervisor is up, and `/api/health`
   reports `status == "ok"` with `profiles_running > 0` as soon as **one** worker
   is alive and healthy — which is what the smoke test waits for.

The `paper` profiles need no credential, so a fresh volume reaches that state
without any operator action; the two `live` entries are refused by design and do
not block anything.

---

## 2. nginx: the same pattern as `culia` / `tristeub`

Three blocks were added to `/opt/homebrew/etc/nginx/nginx.conf`:

1. **a rate-limit zone** and **an upstream to the dashboard**:

   ```nginx
   limit_req_zone $binary_remote_addr zone=trading_per_ip:10m rate=100r/s;

   upstream trading_dashboard {
     server 127.0.0.1:3031;
     keepalive 16;
   }
   ```

   nginx talks to the **dashboard** only. The dashboard proxies `/api/*` to
   `http://trading-realtime:8080` over the compose network; the aggregated API on
   `127.0.0.1:3030` is published for local debugging and is *not* an upstream.

2. **the port-80 vhost** (public via PF `80 -> 8088`): ACME challenge served from
   `/opt/homebrew/var/www/certbot`, then `301` to HTTPS;

3. **the TLS vhost** on `8446` (public via PF `443 -> 8445`, SNI routing by the
   `stream` block, `proxy_protocol`) with the Let's Encrypt certificate, the
   security headers, `auth_basic`, `limit_req zone=trading_per_ip burst=100 nodelay`
   and `proxy_pass http://trading_dashboard;`.

The certificate was obtained by the same method as the other domains:

```bash
certbot certonly --webroot -w /opt/homebrew/var/www/certbot \
  -d tristeubadingview.duckdns.org \
  --config-dir /opt/homebrew/etc/letsencrypt \
  --non-interactive --agree-tos --keep-until-expiring --key-type ecdsa
```

A backup `nginx.conf.bak-trading-<timestamp>` was written before the change,
and `nginx -t` is run before every `nginx -s reload`.

### What nginx must forward

The dashboard is an ordinary HTTP/1.1 origin: the vhost needs the usual
`Host`, `X-Real-IP`, `X-Forwarded-For` and `X-Forwarded-Proto` headers, and the
`keepalive 16` upstream keeps the polling cheap. No WebSocket upgrade is
required (live updates are documented polling, not a socket), and no CORS header
is ever needed: the browser only ever calls its own origin, `/api/*` included.

### State, errors and information leakage

nginx exposes only the dashboard, which exposes only the JSON API paths it
needs. Both containers are published on `127.0.0.1`, the per-profile worker ports
never leave the container, and the aggregated API stays **behind**
authentication. The operator token (mutations) is a second barrier, independent
of Basic auth: without it, `POST /api/kill-switch` returns `403` (a missing header
returns `401`).

---

## 3. fail2ban

No dedicated jail was added: the existing `[nginx-auth]` jail
(`/opt/homebrew/etc/fail2ban/jail.local`) watches **the whole nginx error
log** with the `nginx-http-auth` filter, so it already covers this vhost —
that is exactly what protects `culia.duckdns.org` and `tristeub.duckdns.org`
(`maxretry = 5`, `findtime = 10m`, `bantime = 1h`, banning via the pf anchor
`com.apple/f2b/nginx-auth`, ports 80 and 443).

Verified during deployment: an attempt with a wrong password produced a line

```
user "tristeub": password mismatch, client: <ip>, server: tristeubadingview.duckdns.org, request: "GET / HTTP/2.0"
```

and the fail2ban log grew by 120 bytes within the second — so the jail does
count failures **on this route**. The pattern is unchanged by the platform
migration: authentication still happens in nginx, before any request reaches a
container.

> **Worth knowing**: the daemon currently runs started by hand
> (`fail2ban-server --async`, root) and not by launchd — `launchctl print
> system/homebrew.mxcl.fail2ban` answers `not running`, which is the
> **pre-existing** situation on this machine, shared with the other protected
> vhosts. The plist `/Library/LaunchDaemons/homebrew.mxcl.fail2ban.plist`
> exists (with `RunAtLoad`); after a machine reboot, check with
> `sudo fail2ban-client status nginx-auth` that the jail is up again.

---

## 4. Operations

```bash
# the dashboard, locally (the same UI nginx serves, without TLS/Basic auth)
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:3031/

# the aggregated API, locally (debugging surface -- the dashboard does this itself)
curl -s http://127.0.0.1:3030/api/health
curl -s http://127.0.0.1:3030/api/profiles | python3 -m json.tool
curl -s http://127.0.0.1:3030/api/settings | python3 -m json.tool

# the same JSON through the dashboard's own origin (the rewrite in action)
curl -s http://127.0.0.1:3031/api/health

# container and healthcheck state
docker compose -f deploy/docker-compose.yml ps
docker inspect --format '{{.State.Health.Status}}' trading-dashboard
docker inspect --format '{{.State.Health.Status}}' trading-realtime

# the fleet: how many workers, how much memory
docker stats --no-stream trading-realtime
docker exec trading-realtime sh -c 'ls -l /app/data/realtime/profiles | head'

# vhost and certificate
nginx -t
certbot certificates --config-dir /opt/homebrew/etc/letsencrypt

# logs: the supervisor (stdout) and one profile's worker
docker logs --tail 50 trading-realtime
docker logs --tail 50 trading-dashboard
docker exec trading-realtime tail -20 /app/data/realtime/logs/momentum-btc-1h.log

# global kill switch (file form -- the API form needs the operator token)
docker exec trading-realtime touch /app/data/realtime/KILL_SWITCH
docker exec trading-realtime rm /app/data/realtime/KILL_SWITCH
```

The log level is set by `TB_LOG_LEVEL` (default `INFO`) in
`deploy/docker-compose.yml`. The full runbook — snapshots, the state database,
cap changes, troubleshooting — is `docs/operations.md`.

### Provisioning the profile catalogue on the deployed stack

The declarative catalogue is applied from **inside** the engine container,
against the engine's own API:

```bash
# 1. dry run first -- creates nothing, prints what would be created or skipped
docker exec trading-realtime trading realtime provision --api-url http://127.0.0.1:8080 --dry-run

# 2. apply -- creates the missing profiles, idempotently
docker exec trading-realtime trading realtime provision --api-url http://127.0.0.1:8080

# 3. prune -- the ONLY form that deletes a profile outside the catalogue
docker exec trading-realtime trading realtime provision --api-url http://127.0.0.1:8080 --prune
```

Two reasons for running it *in* the container rather than from the host:

* `TB_OPERATOR_TOKEN` already travels into `trading-realtime` through
  `deploy/.env` (`env_file:` in the compose file), so the command authenticates
  with no secret on the command line, in no shell history and in no log;
* `http://127.0.0.1:8080` is the in-container API port. The host-side
  `127.0.0.1:3030` is the **debugging** surface of the same server: the command
  works there too (`.venv/bin/python -m trading_platform realtime provision
  --api-url http://127.0.0.1:3030`, with `TB_OPERATOR_TOKEN` exported in that
  shell), but the container is where the token already is.

**Provisioning is idempotent, so re-running after a deploy creates nothing.** A
profile whose id is already in `state.db` is skipped, never recreated and never
modified, and profiles created through the dashboard (`source = operator`) are
never touched by an apply — only by an explicit `--prune` or `DELETE
/api/profiles/{id}?force=true`. After a first boot the catalogue is already
seeded; a later apply normally reports every entry as `skipped`.

**A live entry is refused, and that is the gate working.** This stack configures
no `TB_ALLOW_LIVE_TRADING` and no exchange credential, so the two live catalogue
entries are reported in `refused_live` and never created. A live profile the
broker cannot authenticate would otherwise land in a state where its orders
cannot be placed at all.

### Working on the dashboard outside Docker

```bash
make dashboard-install          # npm ci, with the repository-local npm cache
API_ORIGIN=http://127.0.0.1:8080 make dashboard-dev   # http://127.0.0.1:3000
make dashboard-check            # lint + typecheck + test:coverage + build
```

The dev server needs a reachable API: either the realtime container
(`127.0.0.1:3030`, see above) or a local `make realtime` on
`127.0.0.1:8080`, which is the `API_ORIGIN` default.

---

## 4bis. Automatic deploys (GitHub Actions)

Every push/merge to `main` rebuilds and restarts the whole stack automatically.
The `deploy` job of `.github/workflows/deploy.yml` runs on a self-hosted
GitHub Actions runner installed on this Mac — the deployment is a
`docker compose` stack bound to `127.0.0.1` behind the host nginx, so the job
must run on the host itself:

1. fast-forwards the **host repository** (`/Users/mac/Projects/Trading`) to the
   exact merged commit with `git merge --ff-only`, so the build context is the
   revision being deployed. It deploys from the host copy rather than from the
   runner's own checkout, and that is a requirement, not a shortcut: the images
   build with `context: ..` (the parent of `deploy/`, i.e. whichever repository
   sits beside it), and `trading-realtime` declares `env_file: - .env`, which
   compose resolves **relative to the compose file** — a runner checkout has no
   `deploy/.env`, and a `--env-file <host path>` does *not* satisfy a
   service-level `env_file`. A dirty or diverged host tree therefore fails the
   deploy instead of being forced through;
2. verifies `deploy/.env` is readable — it is git-ignored and mode 600, and the
   realtime container needs `TB_OPERATOR_TOKEN` from it;
3. runs `docker compose -f deploy/docker-compose.yml up -d --build --wait` —
   **both** containers are rebuilt together, because the supervisor and the
   Next.js dashboard ship from separate images and deploying only one would
   leave the pair disagreeing. The named volumes `deploy_trading-state` and
   `deploy_trading-cache` persist across the rebuild, and `--wait` blocks until
   both images' healthchecks pass;
4. prunes dangling images, then smoke-tests the host surfaces: `GET
   /api/health` on `127.0.0.1:3030` must report `status: ok` with at least one
   running profile, and the dashboard must answer on `127.0.0.1:3031` both on
   `/` and through its `/api/*` proxy.

The deploy **restarts every freqtrade worker**, because it restarts the
supervisor container; the whole fleet is scheduled again from the state database
in one pass, so every enabled profile starts again together, and the snapshot
history keeps the before/after in one table. The reason the smoke assertions hold
on a first boot — legacy database archived, catalogue seeded, workers spawned,
API answering as soon as one worker is healthy — is §1bis.

### Profiles created through the UI survive a deploy

A profile created from the dashboard is written to the `profiles` table of
`/app/data/realtime/state.db`, inside the `trading-state` volume. That volume is
**never** touched by a deployment: the build rebuilds the images, and
`docker compose up -d --build` keeps the named volumes, so a profile created
through the UI is still running after the next deploy.

This is what changed. The superseded design kept the profile set in a committed
JSON document that the monitoring API rewrote on disk: a deploy rebuilt that file
from the merged commit and silently dropped every UI-created profile. The
database is now the only state the engine reads, and a deployment cannot
overwrite it. The declarative catalogue of `config/profiles.json` is *applied*
to that database, idempotently and without ever deleting an operator profile.

The consequence to accept is the other side of the same coin: the profile set an
operator builds by hand is not versioned in git, so a **fresh volume** starts
from the catalogue and nothing else. A backup of the platform's configuration is
a copy of the `trading-state` volume (`docs/operations.md` §7).

Pull requests never touch the runner: the job is gated on a push to `main`.
`ci.yml` already runs the full Python and dashboard gates on every pull request,
so the deploy job deliberately does not repeat them — branch protection is what
guarantees a commit reaching `main` was green first. `cancel-in-progress` is
**disabled**: interrupting a half-finished `up -d --build` could leave the stack
mid-rebuild, which is worse than waiting.

Operational notes:

- **Register the runner once** (repo admin): generate a token at
  <https://github.com/ms-tristan/trading/settings/actions/runners> → "New
  self-hosted runner" → macOS / ARM64, then run
  `~/actions-runner-trading/register.sh` (it takes the token interactively or
  through `ACTIONS_RUNNER_TOKEN`). Start it with `./svc.sh install && ./svc.sh
  start`, which loads the launchd agent so it survives reboots.
- The job labels are `[self-hosted, macOS, ARM64]`. The runner's own name and
  labels are set by `.github/runner/register-runner.sh` — if the runner is ever
  re-created with other labels, keep both in sync.
- Rollback = `git revert` on `main`: the revert push triggers a fresh deploy of
  the previous code.
- To require a manual go/no-go before each deploy, add a protection rule to the
  `production` environment (Settings → Environments) — the job already declares
  `environment: production`.
- A deploy restarts the profiles, so it is also what clears a stale `error`
  state left in `state.db` by an earlier run of the fleet.

---

## 5. What this deployment is not

- **no per-user authentication**: a single shared Basic auth pair, plus an
  operator token — "trusted network" level;
- **no TLS in a container**: everything goes through nginx, which is the only
  TLS terminator; neither the dashboard nor the API is exposed directly;
- **two containers, not one**: `trading-dashboard` and `trading-realtime` run
  side by side, and the realtime container itself hosts the supervisor *and* its
  freqtrade workers as child processes. The dashboard is a **second
  trusted-network surface** — it renders whatever the operator token authorises
  (the kill switch, the profile lifecycle) and it must be protected by the same
  nginx authentication, never published on a public interface;
- **no shared state between the containers**: the dashboard holds no database
  and no volume; everything it shows comes from the JSON API at request time;
- **an uncapped fleet**: every enabled profile of the catalogue runs, so the
  container's memory footprint is the sum of all its workers — measure it with
  `docker stats`, do not estimate it (§1bis);
- **no live trading by default**: the two live catalogue entries stay `blocked`
  (in the API) and `refused_live` (in provisioning) until an operator provides
  `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK` and both exchange credentials in
  the environment — none of which is configured here;
- **no shared OHLCV cache yet**: each worker caches its own candles under its
  profile directory; the `trading-cache` volume is reserved for that feature;
- **no high availability**: two long-lived containers, one supervisor process and
  a single-writer SQLite database;
- **no exercise against a real exchange**: the test suite covers the supervisor,
  the config builder and the REST client through test doubles only.
