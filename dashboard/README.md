# Trading monitor dashboard

Standalone Next.js application that renders the real-time monitoring surface of
the trading platform. It is a **pure consumer of the frozen JSON API** exposed by
the Python monitoring server (`src/trading_platform/web/`): it never redesigns the
contract, never talks to SQLite and never touches the engine.

- Next.js 16 (App Router, React 19, TypeScript strict)
- Tailwind CSS v4 with the tokens of `design-system/trading-monitor/MASTER.md`
- Vitest + React Testing Library for the unit and component tests
- No chart library, no state library, no UI kit: the foundation is deliberately
  dependency-light.

## Requirements

- Node.js >= 20.9.0 and npm
- A reachable monitoring server (`trading-platform realtime serve`, default
  `http://127.0.0.1:8080`)

## Getting started

```bash
# Sandboxed/CI machines whose global npm cache is not writable:
export npm_config_cache="$PWD/../.npm-cache"
export npm_config_logs_dir="$npm_config_cache/_logs"

npm install
npm run dev            # http://localhost:3000
```

## How the dashboard reaches the API

Two paths, on purpose:

| Caller | Base URL | Why |
| --- | --- | --- |
| Server Component | `API_ORIGIN` (default `http://127.0.0.1:8080`) | the Node server talks to the Python server directly, with `cache: 'no-store'` |
| Client Component | none (same origin) | the browser calls `/api/...` on its own origin and Next.js proxies it |

`next.config.ts` declares the proxy as a rewrite:

```ts
rewrites() {
  return [{ source: '/api/:path*', destination: `${apiOrigin()}/api/:path*` }];
}
```

`apiOrigin()` reads `API_ORIGIN` (trailing slashes removed) and falls back to
`http://127.0.0.1:8080`. The docker compose service sets
`API_ORIGIN=http://trading-realtime:8080`.

> **Build-time resolution.** The rewrite destination is resolved when
> `next.config.ts` is evaluated, i.e. **baked at build time**. With
> `output: 'standalone'` the destination lives in the generated routes manifest of
> the built server: changing `API_ORIGIN` therefore requires a rebuild, it cannot
> be changed by a runtime environment variable of the container.

Because the browser always talks to its own origin there is no CORS
configuration, no absolute URL in client code, and the `X-Operator-Token` header
of `POST /api/kill-switch` flows through the rewrite untouched.

## Live updates

HTTP polling, as documented for this platform (`monitoring.refresh_seconds`,
2 s default):

- the Server Component renders the first payload (no empty first paint, no
  duplicate request on mount);
- client islands poll with `usePolling` — 2 s for the live views,
  `5 x` slower for the heavy per-profile collections;
- a visible **Pause / Resume** control and a `checked at` timestamp are always on
  screen, and a failed poll shows a non-blocking banner while the last known good
  data stays visible.

## Environment variables

| Name | Default | Scope | Purpose |
| --- | --- | --- | --- |
| `API_ORIGIN` | `http://127.0.0.1:8080` | server, **build time** | origin proxied by the `/api/*` rewrite and used by Server Components |
| `NEXT_PUBLIC_POLL_INTERVAL_MS` | `2000` | browser | polling cadence in milliseconds (must be positive and finite; anything else is ignored) |

## Scripts

| Command | What it does |
| --- | --- |
| `npm run dev` | development server |
| `npm run build` | production build (standalone output) |
| `npm start` | serve the production build |
| `npm run lint` | ESLint (flat config, `eslint-config-next`) |
| `npm run typecheck` | `tsc --noEmit` |
| `npm test` | Vitest, single run |
| `npm run test:watch` | Vitest in watch mode |
| `npm run test:coverage` | Vitest with the v8 coverage gate (85 % lines/statements/functions, 75 % branches) |

Scoped commands while other work packages are writing files:

```bash
npx vitest run src/lib src/components src/app   # tests of one area only
npm run typecheck
npm run lint
```

The numeric thresholds and the full testing policy live in
`docs/testing-policy.md`.

## Design system

`design-system/trading-monitor/MASTER.md` is binding (dark-mode OLED palette,
Fira Sans body / Fira Code numerics and headings, density 8/10, motion 3/10); its
"Enterprise Gateway" landing-page pattern does not apply to this product
interior.

- every colour, spacing, shadow and radius token is declared once in the
  `@theme` block of `src/app/globals.css` and consumed as a Tailwind utility —
  **no component hard-codes a hex value**;
- fonts are self-hosted through `@fontsource` (no CDN request at runtime, no
  network fetch at build);
- colour never carries a meaning alone: every up/down, healthy/degraded or
  order-state signal pairs its tone with an icon and a text label;
- accessibility is non-negotiable: 4.5:1 contrast, visible focus rings, full
  keyboard operation, `aria-live` on live regions, `prefers-reduced-motion`
  respected, responsive at 375/768/1024/1440 with no horizontal page scroll.

## Operator token

`POST /api/kill-switch` is the only mutating call, so it is the only one that
needs the operator token. The token is kept in `sessionStorage` only (never
`localStorage`, never a cookie, never a URL), is written on submit, is cleared
from the DOM immediately, is never rendered back — the panel only shows whether a
token is stored — and is scrubbed from every error message. Engaging the
emergency stop always goes through a confirmation dialog that requires a reason,
because it is a destructive action.
