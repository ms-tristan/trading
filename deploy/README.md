# Local deployment — real-time dashboard

This folder holds **everything needed to run the real-time platform** (layers 6
and 7) on the machine, behind the host nginx. Two containers, one public URL:

| Item | Value |
| --- | --- |
| Public URL | <https://tristeubadingview.duckdns.org> |
| Authentication | HTTP Basic (`auth_basic`), realm `Trading` |
| Dashboard container | `trading-dashboard` (image `trading-dashboard:latest`), Next.js standalone server |
| Realtime container | `trading-realtime` (image `trading-realtime:latest`), Python engine + monitoring JSON API |
| Dashboard listen | `0.0.0.0:3000` inside the container, published on **`127.0.0.1:3031`** |
| API listen | `0.0.0.0:8080` inside the container, published on **`127.0.0.1:3030`** (debugging only) |
| TLS termination | **host nginx**, never a container |
| Protection | nginx `limit_req` + fail2ban jail `nginx-auth` (shared) |

---

## 1. The two containers

```
deploy/
├── Dockerfile.realtime    # python:3.11-slim + the package + the `exchange` extra (ccxt)
├── Dockerfile.dashboard   # node:24-alpine, multi-stage, Next.js standalone output
├── docker-compose.yml     # both services, their volumes and their healthchecks
├── README.md              # this document
└── .env                   # TB_OPERATOR_TOKEN — git-ignored, never in an image
```

```bash
docker compose -f deploy/docker-compose.yml up -d --build   # build and start both
docker compose -f deploy/docker-compose.yml logs -f         # follow the logs
docker compose -f deploy/docker-compose.yml down            # stop (volumes kept)
docker compose -f deploy/docker-compose.yml down -v         # stop AND wipe state
```

### `trading-realtime` — the engine and its JSON API

The container runs
`python -m trading_platform realtime run --state-db /app/data/realtime/state.db`
(that is the image's `CMD`): the engine **and** the monitoring server. It runs as
an unprivileged user (`appuser`), with `restart: unless-stopped` and a healthcheck
that queries `GET /api/health` on port 8080. Since the standalone dashboard
landed, the Python server is a **pure JSON API**: it serves no HTML page and no
static asset any more (`GET /` answers the same JSON 404 as any unknown route).
Its port is published on the loopback interface **for debugging only** — nothing
in the browser ever reaches it directly.

The container starts with an **empty profile set**: the profile set and the engine
settings are read from the SQLite state database, and a fresh `trading-state`
volume holds no profile yet. The operator creates the profiles from the dashboard
(`POST /api/profiles`) and they take effect immediately, without a restart.
`realtime.start_at` is `null`: the engine follows the wall clock, it does **not**
replay history.

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

### Volumes (state survives a restart)

| Volume | Path | Content | Used by |
| --- | --- | --- | --- |
| `trading-state` | `/app/data/realtime` | SQLite database (`state.db`), kill-switch file, JSON logs | `trading-realtime` |
| `trading-cache` | `/app/data/cache` | OHLCV cache (avoids re-downloading the warm-up window) | `trading-realtime` |

The dashboard is **stateless**: it owns no volume, no database and no durable
file. Restarting or rebuilding it loses nothing, and its operator token lives
only in the browser's `sessionStorage` (never on disk, never rendered back).

### The SQLite state database is the source of truth

The profile set and the engine settings live in **one SQLite database**:
`/app/data/realtime/state.db`, inside the `trading-state` volume.

| What | Where |
| --- | --- |
| Profiles | the `profiles` table, one row per profile |
| Engine and monitoring settings | the `meta` key/value table, under the `platform_settings` key |
| Positions, orders, fills, equity, candles, wallet | the tables of the same schema |

Consequences an operator must know:

* **the operator creates profiles through the dashboard** (`POST /api/profiles`)
  and they are written to that database, not to a file in this repository;
* **deleting the state volume is the only way to lose them**
  (`docker compose -f deploy/docker-compose.yml down -v`), and a rebuild of the
  images (`up -d --build`) keeps them, because a named volume survives a rebuild;
* **no file of the checkout is written by any container any more**. There is no
  bind mount of `deploy/`: with the committed JSON profile document gone, nothing
  the runtime does needs to write on the host, and the image therefore never
  needs `chmod` on a host directory.

The settings are seeded from the built-in configuration defaults the first time
the database is opened, and read back from the database on every later boot. The
only thing that stays outside the database is the **path** of the database
itself, which cannot live inside the database it locates: it comes from
`--state-db` (the image passes `/app/data/realtime/state.db`), from
`TB_REALTIME_STATE_DB`, or from the model default. A host that never customises
anything behaves exactly as it did before.

Since the profile set is no longer versioned in git, a deployment **cannot**
overwrite it: the old failure mode — a profile created through the UI and
silently destroyed by the next deploy, because the deploy rebuilt a committed
file — is gone with the file.

### Secrets

`deploy/.env` carries the **operator token** of the monitoring API — the token
the dashboard's kill switch has to present. It is git-ignored **and** excluded
from the build context by `.dockerignore`, so it never enters an image layer;
the dashboard never reads it, the operator types it and the browser keeps it in
`sessionStorage` only. Exchange keys (unused here, all profiles are `paper`)
come only from the environment — see `docs/realtime.md` §3.

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
   `http://trading-realtime:8080` over the compose network; the Python port on
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
`keepalive 16` upstream keeps the 2 s polling cheap. No WebSocket upgrade is
required (live updates are documented polling, not a socket), and no CORS header
is ever needed: the browser only ever calls its own origin, `/api/*` included.

### State, errors and information leakage

nginx exposes only the dashboard, which exposes only the JSON API paths it
needs. Both containers are published on `127.0.0.1` and the monitoring API stays
**behind** authentication. The operator token (mutations) is a second barrier,
independent of Basic auth: without it, `POST /api/kill-switch` returns `403`.

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
count failures **on this route**. The pattern is unchanged by the dashboard
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

# the Python API, locally (debugging surface -- the dashboard does this itself)
curl -s http://127.0.0.1:3030/api/health
curl -s http://127.0.0.1:3030/api/profiles | python3 -m json.tool

# the same JSON through the dashboard's own origin (the rewrite in action)
curl -s http://127.0.0.1:3031/api/health

# container and healthcheck state
docker compose -f deploy/docker-compose.yml ps
docker inspect --format '{{.State.Health.Status}}' trading-dashboard
docker inspect --format '{{.State.Health.Status}}' trading-realtime

# vhost and certificate
nginx -t
certbot certificates --config-dir /opt/homebrew/etc/letsencrypt

# structured JSON logs (console + durable file)
docker logs --tail 50 trading-realtime
docker logs --tail 50 trading-dashboard
docker exec trading-realtime tail -20 /app/data/realtime/logs/realtime-*.log

# global kill switch (the only state change the dashboard can perform)
docker exec trading-realtime touch /app/data/realtime/KILL_SWITCH
docker exec trading-realtime rm /app/data/realtime/KILL_SWITCH
```

The log level is set by `TB_LOG_LEVEL` (default `INFO`) in
`deploy/docker-compose.yml`.

### Working on the dashboard outside Docker

```bash
make dashboard-install          # npm ci, with the repository-local npm cache
API_ORIGIN=http://127.0.0.1:8080 make dashboard-dev   # http://127.0.0.1:3000
make dashboard-check            # lint + typecheck + test:coverage + build
```

The dev server needs a reachable monitoring API: either the realtime container
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
   **both** containers are rebuilt together, because the Python engine and the
   Next.js dashboard ship from separate images and deploying only one would
   leave the pair disagreeing. The named volumes `deploy_trading-state` and
   `deploy_trading-cache` persist across the rebuild, and `--wait` blocks until
   both images' healthchecks pass;
4. prunes dangling images, then smoke-tests the host surfaces: `GET
   /api/health` on `127.0.0.1:3030` must report `status: ok` with at least one
   running profile, and the dashboard must answer on `127.0.0.1:3031` both on
   `/` and through its `/api/*` proxy.

### Profiles created through the UI survive a deploy

A profile created from the dashboard is written to the `profiles` table of
`/app/data/realtime/state.db`, inside the `trading-state` volume. That volume is
**never** touched by a deployment: the build rebuilds the images, and
`docker compose up -d --build` keeps the named volumes, so a profile created
through the UI is still running after the next deploy.

This is what changed. The superseded design kept the profile set in a committed
JSON document that the monitoring API rewrote on disk: a deploy rebuilt that file
from the merged commit and silently dropped every UI-created profile — the
`test1` scratch profile was destroyed exactly that way. The document is gone, and
with it the failure mode: the database is now the only source of truth the engine
reads, and a deployment cannot overwrite it.

The consequence to accept is the other side of the same coin: the profile set is
no longer versioned in git, so it is no longer reproduced on another machine. A
fresh host starts with an **empty** platform — the API answers, `GET
/api/profiles` returns `[]` — and the operator re-creates the profiles from the
dashboard. A backup of the platform's configuration is a copy of the
`trading-state` volume.

Pull requests never touch the runner: the job is gated on
`github.event_name == 'push'`. `ci.yml` already runs the full Python and
dashboard gates on every pull request, so the deploy job deliberately does not
repeat them — branch protection is what guarantees a commit reaching `main`
was green first. `cancel-in-progress` is **disabled**: interrupting a
half-finished `up -d --build` could leave the stack mid-rebuild, which is worse
than waiting.

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
- A deploy restarts the profiles, so it is also what clears a stale
  `last_error` left in `state.db` by an earlier engine version.

---

## 5. What this deployment is not

- **no per-user authentication**: a single shared Basic auth pair, plus an
  operator token — "trusted network" level;
- **no TLS in a container**: everything goes through nginx, which is the only
  TLS terminator; neither the dashboard nor the API is exposed directly;
- **two containers, not one**: the platform now runs `trading-dashboard` and
  `trading-realtime` side by side. The dashboard is a **second trusted-network
  surface** — it renders whatever the operator token authorises (the kill
  switch) and it must be protected by the same nginx authentication, never
  published on a public interface;
- **no shared state between the containers**: the dashboard holds no database
  and no volume; everything it shows comes from the JSON API at request time;
- **no live trading**: every profile this stack runs is `paper`; `live` mode would
  require `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`, exchange keys in the
  environment and a `mode: live` profile — none of which is configured here;
- **no high availability**: two long-lived containers and a single-writer
  SQLite database;
- **no exercise against a real exchange for live mode**: the test suite covers
  the live broker only through test doubles (see `docs/realtime.md` §7).
