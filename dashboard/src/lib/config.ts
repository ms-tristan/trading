/**
 * Runtime configuration of the dashboard.
 *
 * Two origins coexist on purpose:
 *
 * * a **Server Component** runs inside the Node server and must reach the Python
 *   monitoring server directly, so it uses {@link serverApiBaseUrl} (absolute
 *   origin, `API_ORIGIN`, default `http://127.0.0.1:8080`);
 * * a **Client Component** runs in the browser and must stay same-origin, so it
 *   passes no base URL at all and the Next.js rewrite (see `next.config.ts`)
 *   proxies `/api/*` to the Python server.
 *
 * The polling cadence mirrors `monitoring.refresh_seconds` of the Python
 * configuration. That setting is not part of the frozen JSON API, so the
 * dashboard carries its own default (2000 ms) and accepts an override through
 * `NEXT_PUBLIC_POLL_INTERVAL_MS`.
 */

/**
 * Default origin of the Python monitoring server.
 *
 * Kept in sync with `DEFAULT_API_ORIGIN` in `next.config.ts`: the build
 * configuration cannot be imported from application code without pulling it into
 * the runtime bundle.
 */
export const DEFAULT_API_ORIGIN = 'http://127.0.0.1:8080';

/** Polling cadence of the live views, in milliseconds (2 s, as documented). */
export const DEFAULT_POLL_INTERVAL_MS = 2000;

/** Environment variable overriding {@link DEFAULT_POLL_INTERVAL_MS}. */
export const POLL_INTERVAL_ENV_VAR = 'NEXT_PUBLIC_POLL_INTERVAL_MS';

/**
 * The per-profile detail payloads (equity curve, trades, orders, positions,
 * metrics) are far heavier than the live snapshot, so they are refreshed on a
 * slower cadence: `5 x pollIntervalMs` (10 s by default).
 */
export const DETAIL_POLL_MULTIPLIER = 5;

type EnvLike = Record<string, string | undefined>;

/** Read an environment variable from `env` (defaults to `process.env`). */
function readEnv(env: EnvLike | undefined, name: string): string | undefined {
  const source: EnvLike = env ?? (typeof process === 'undefined' ? {} : process.env);
  const value = source[name];
  return typeof value === 'string' ? value : undefined;
}

/**
 * Resolve the absolute origin a Server Component must call.
 *
 * `API_ORIGIN` wins when it carries a value; surrounding whitespace and trailing
 * slashes are removed. Blank or absent falls back to
 * {@link DEFAULT_API_ORIGIN}.
 */
export function serverApiBaseUrl(env?: EnvLike): string {
  const raw = readEnv(env, 'API_ORIGIN');
  if (raw === undefined) {
    return DEFAULT_API_ORIGIN;
  }
  const trimmed = raw.trim().replace(/\/+$/, '');
  return trimmed === '' ? DEFAULT_API_ORIGIN : trimmed;
}

/**
 * Resolve the polling cadence, in milliseconds.
 *
 * Only a positive, finite number is accepted; everything else (unset, blank,
 * `abc`, `0`, `-1`, `Infinity`) falls back to
 * {@link DEFAULT_POLL_INTERVAL_MS}.
 */
export function resolvePollIntervalMs(env?: EnvLike): number {
  const raw = readEnv(env, POLL_INTERVAL_ENV_VAR);
  if (raw === undefined) {
    return DEFAULT_POLL_INTERVAL_MS;
  }
  const parsed = Number(raw.trim());
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return DEFAULT_POLL_INTERVAL_MS;
  }
  return parsed;
}

/**
 * Resolve the polling cadence of a per-profile detail view from the live
 * cadence: {@link DETAIL_POLL_MULTIPLIER} times slower, with the default cadence
 * used as the fallback for an unusable input.
 */
export function resolveDetailPollIntervalMs(pollIntervalMs: number): number {
  if (!Number.isFinite(pollIntervalMs) || pollIntervalMs <= 0) {
    return DEFAULT_POLL_INTERVAL_MS * DETAIL_POLL_MULTIPLIER;
  }
  return pollIntervalMs * DETAIL_POLL_MULTIPLIER;
}
