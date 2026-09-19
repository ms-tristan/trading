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
├── profiles.json          # the deployed profiles (paper only)
└── .env                   # TB_OPERATOR_TOKEN — git-ignored, never in an image
```

```bash
docker compose -f deploy/docker-compose.yml up -d --build   # build and start both
docker compose -f deploy/docker-compose.yml logs -f         # follow the logs
docker compose -f deploy/docker-compose.yml down            # stop (volumes kept)
docker compose -f deploy/docker-compose.yml down -v         # stop AND wipe state
```

### `trading-realtime` — the engine and its JSON API

The container runs `python -m trading_platform realtime run`: the engine **and**
the monitoring server. It runs as an unprivileged user (`appuser`), with
`restart: unless-stopped` and a healthcheck that queries `GET /api/health` on
port 8080. Since the standalone dashboard landed, the Python server is a **pure
JSON API**: it serves no HTML page and no static asset any more (`GET /` answers
the same JSON 404 as any unknown route). Its port is published on the loopback
interface **for debugging only** — nothing in the browser ever reaches it
directly.

Two **paper** profiles run on real market data (Binance via ccxt):
`btc-paper` (BTC/USDT 1h) and `eth-paper` (ETH/USDT 15m).
`realtime.start_at` is `null`: the engine follows the wall clock, it does
**not** replay history.

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

### The profiles file is written by the API

`deploy/` itself is bind-mounted **read-write** at `/app/deploy`, because
`POST /api/profiles` and `DELETE /api/profiles/{id}` rewrite `profiles.json`
(the on-disk source of truth). Two host-side details matter:

1. **The directory must be writable by the container user** (`appuser`, uid
   1000), which is not the uid that owns the checkout:

   ```bash
   chmod o+w deploy
   ```

   The rewrite is atomic — a temp file in the same directory, then
   `os.replace` — and that rename fails with `EBUSY` when the target is a
   bind-mounted **file**, which is why the whole directory is mounted rather
   than `profiles.json` alone. The image still ships its own copy at
   `/app/deploy/profiles.json`; the mount shadows it at runtime.
2. **`deploy/.env` keeps mode 600**, so it stays readable by its owner only and
   the unprivileged container user cannot read it — granting the directory
   write access does not expose the operator token.

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
- **no live trading**: both profiles are `paper`; `live` mode would require
  `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`, exchange keys in the
  environment and a `mode: live` profile — none of which is configured here;
- **no high availability**: two long-lived containers and a single-writer
  SQLite database;
- **no exercise against a real exchange for live mode**: the test suite covers
  the live broker only through test doubles (see `docs/realtime.md` §7).
