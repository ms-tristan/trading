# Local deployment — real-time dashboard

This folder holds **everything needed to run the real-time platform** (layers 6
and 7) on the machine, behind the host nginx:

| Item | Value |
| --- | --- |
| Public URL | <https://tristeubadingview.duckdns.org> |
| Authentication | HTTP Basic (`auth_basic`), realm `Trading` |
| Container | `trading-realtime` (image `trading-realtime:latest`) |
| Internal listen | `0.0.0.0:8080` inside the container, published on **`127.0.0.1:3030`** |
| TLS termination | **host nginx**, never the container |
| Protection | nginx `limit_req` + fail2ban jail `nginx-auth` (shared) |

---

## 1. The container

```
deploy/
├── Dockerfile.realtime   # python:3.11-slim + the package + the `exchange` extra (ccxt)
├── docker-compose.yml    # the service, its volumes and its healthcheck
├── profiles.json         # the deployed profiles (paper only)
└── .env                  # TB_OPERATOR_TOKEN — git-ignored, never in the image
```

```bash
docker compose -f deploy/docker-compose.yml up -d --build   # build and start
docker compose -f deploy/docker-compose.yml logs -f         # follow the logs
docker compose -f deploy/docker-compose.yml down            # stop (volumes kept)
docker compose -f deploy/docker-compose.yml down -v         # stop AND wipe state
```

The container runs `python -m trading_platform realtime run`: engine **and**
monitoring server. It runs as an unprivileged user (`appuser`), with
`restart: unless-stopped` and a healthcheck that queries `GET /api/health`.

Two **paper** profiles run on real market data (Binance via ccxt):
`btc-paper` (BTC/USDT 1h) and `eth-paper` (ETH/USDT 15m).
`realtime.start_at` is `null`: the engine follows the wall clock, it does
**not** replay history.

### Volumes (state survives a restart)

| Volume | Path | Content |
| --- | --- | --- |
| `trading-state` | `/app/data/realtime` | SQLite database (`state.db`), kill-switch file, JSON logs |
| `trading-cache` | `/app/data/cache` | OHLCV cache (avoids re-downloading the warm-up window) |

### Secrets

`deploy/.env` carries the **operator token** of the monitoring API (the
dashboard's "kill switch" button); it is git-ignored **and** excluded from the
build context by `.dockerignore`, so it never enters an image layer. Exchange
keys (unused here, all profiles are `paper`) come only from the environment —
see `docs/realtime.md` §3.

---

## 2. nginx: the same pattern as `culia` / `tristeub`

Three blocks were added to `/opt/homebrew/etc/nginx/nginx.conf`:

1. **a rate-limit zone** and **an upstream**:

   ```nginx
   limit_req_zone $binary_remote_addr zone=trading_per_ip:10m rate=100r/s;

   upstream trading_realtime {
     server 127.0.0.1:3030;
     keepalive 16;
   }
   ```

2. **the port-80 vhost** (public via PF `80 -> 8088`): ACME challenge served from
   `/opt/homebrew/var/www/certbot`, then `301` to HTTPS;

3. **the TLS vhost** on `8446` (public via PF `443 -> 8445`, SNI routing by the
   `stream` block, `proxy_protocol`) with the Let's Encrypt certificate, the
   security headers, `auth_basic`, `limit_req zone=trading_per_ip burst=100 nodelay`
   and the proxy to the upstream.

The certificate was obtained by the same method as the other domains:

```bash
certbot certonly --webroot -w /opt/homebrew/var/www/certbot \
  -d tristeubadingview.duckdns.org \
  --config-dir /opt/homebrew/etc/letsencrypt \
  --non-interactive --agree-tos --keep-until-expiring --key-type ecdsa
```

A backup `nginx.conf.bak-trading-<timestamp>` was written before the change,
and `nginx -t` is run before every `nginx -s reload`.

### State, errors and information leakage

nginx exposes only the dashboard: the container is published only on
`127.0.0.1`, and the monitoring API stays **behind** authentication.
The operator token (mutations) is a second barrier, independent of Basic
auth: without it, `POST /api/kill-switch` returns `403`.

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
count failures **on this route**.

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
# profile status, locally (without going through nginx)
curl -s http://127.0.0.1:3030/api/profiles | python3 -m json.tool

# vhost and certificate
nginx -t
certbot certificates --config-dir /opt/homebrew/etc/letsencrypt

# structured JSON logs (console + durable file)
docker logs --tail 50 trading-realtime
docker exec trading-realtime tail -20 /app/data/realtime/logs/realtime-*.log

# global kill switch (the dashboard can do NOTHING else)
docker exec trading-realtime touch /app/data/realtime/KILL_SWITCH
docker exec trading-realtime rm /app/data/realtime/KILL_SWITCH
```

The log level is set by `TB_LOG_LEVEL` (default `INFO`) in
`deploy/docker-compose.yml`.

---

## 5. What this deployment is not

- **no per-user authentication**: a single shared Basic auth pair, plus an
  operator token — "trusted network" level;
- **no TLS in the container**: everything goes through nginx, which is the only
  TLS terminator;
- **no live trading**: both profiles are `paper`; `live` mode would require
  `TB_ALLOW_LIVE_TRADING=I_UNDERSTAND_THE_RISK`, exchange keys in the
  environment and a `mode: live` profile — none of which is configured here;
- **no high availability**: one container, a single-writer SQLite database;
- **no exercise against a real exchange for live mode**: the test suite covers
  the live broker only through test doubles (see `docs/realtime.md` §7).
