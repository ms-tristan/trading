/**
 * Typed client of the frozen monitoring JSON API.
 *
 * The dashboard consumes exactly the contract exposed by
 * `src/trading_platform/web/routes.py`; this module never redesigns it. Every
 * read call answers one payload type of {@link ./types}, and every failure is
 * normalised into an {@link ApiError} carrying a `kind`:
 *
 * * `'http'` — the server answered a non-2xx status. The message is the server's
 *   own `{"error": "..."}` text when the body carries one (verbatim, which is
 *   what the kill-switch panel displays for a 403), else `HTTP <status>`;
 * * `'network'` — the request never produced a response (unreachable server,
 *   aborted request, DNS/TLS failure);
 * * `'malformed'` — a 2xx response whose body is not valid JSON or does not
 *   match the expected shape.
 *
 * Base URLs are never hard-coded here: a Server Component passes
 * `serverApiBaseUrl()`, a Client Component passes nothing and relies on the
 * same-origin Next.js rewrite (no CORS, no absolute URL in the browser).
 */

import type {
  EquityPayload,
  HealthPayload,
  KillSwitchPayload,
  MetricsPayload,
  OrdersPayload,
  PositionsPayload,
  ProfilesPayload,
  ProfileSnapshot,
  TradesPayload,
} from './types';

/** Header carrying the operator token on the single mutating route. */
export const OPERATOR_TOKEN_HEADER = 'X-Operator-Token';

/** How a request failed. */
export type ApiErrorKind = 'http' | 'network' | 'malformed';

/** Options accepted by every call of this module. */
export interface RequestOptions {
  /** Absolute origin used by Server Components; empty means same origin. */
  baseUrl?: string;
  /** Caller-owned cancellation signal (the polling hook aborts on unmount). */
  signal?: AbortSignal;
  /** Fetch implementation seam for tests. Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
}

/** Options of the single mutating call. */
export interface MutatingRequestOptions extends RequestOptions {
  /** Operator token sent as the `X-Operator-Token` header. Never logged. */
  operatorToken: string;
}

/** Error raised by every call of this module. */
export class ApiError extends Error {
  /** How the request failed. */
  readonly kind: ApiErrorKind;

  /** HTTP status of an `'http'` failure, `null` otherwise. */
  readonly status: number | null;

  /** Path that was requested (no base URL, no query, no secret). */
  readonly path: string;

  constructor(
    kind: ApiErrorKind,
    message: string,
    details: { status?: number | null; path?: string } = {},
  ) {
    super(message);
    this.name = 'ApiError';
    this.kind = kind;
    this.status = details.status ?? null;
    this.path = details.path ?? '';
  }
}

/** Human-readable message of any thrown value (never `undefined`/`NaN`). */
export function errorMessage(error: unknown): string {
  if (error instanceof Error) {
    const message = typeof error.message === 'string' ? error.message.trim() : '';
    return message === '' ? 'Unexpected error' : message;
  }
  if (typeof error === 'string') {
    const message = error.trim();
    return message === '' ? 'Unexpected error' : message;
  }
  return 'Unexpected error';
}

/**
 * Build the URL of `path` against `baseUrl`.
 *
 * An empty `baseUrl` keeps the path relative, which is the browser case: the
 * request stays same-origin and the Next.js rewrite proxies it to the Python
 * server.
 */
export function apiUrl(path: string, baseUrl?: string): string {
  const normalized = path.startsWith('/') ? path : `/${path}`;
  const base = (baseUrl ?? '').trim().replace(/\/+$/, '');
  return base === '' ? normalized : `${base}${normalized}`;
}

/** Replace `secret` with `***` in `message` (belt and braces: no leak ever). */
function redact(message: string, secret: string | undefined): string {
  if (secret === undefined || secret === '') {
    return message;
  }
  return message.split(secret).join('***');
}

function networkError(path: string, cause: unknown, secret?: string): ApiError {
  const detail = cause instanceof Error ? cause.message.trim() : '';
  const message = detail === '' ? `network error calling ${path}` : `network error calling ${path}: ${detail}`;
  return new ApiError('network', redact(message, secret), { path });
}

/** Extract the server's documented `{"error": "..."}` text, when present. */
function httpMessage(status: number, raw: string): string {
  const trimmed = raw.trim();
  if (trimmed !== '') {
    try {
      const payload: unknown = JSON.parse(trimmed);
      if (isRecord(payload) && typeof payload.error === 'string' && payload.error.trim() !== '') {
        return payload.error;
      }
    } catch {
      // A non-JSON error body falls back to the status line below.
    }
  }
  return `HTTP ${status}`;
}

interface RequestInit_ {
  method: 'GET' | 'POST';
  baseUrl?: string;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  headers?: Record<string, string>;
  body?: string;
  validate?: (payload: unknown) => boolean;
  /** Secret scrubbed from every message this request may raise. */
  secret?: string;
}

async function performRequest<T>(path: string, init: RequestInit_): Promise<T> {
  const url = apiUrl(path, init.baseUrl);
  const impl = init.fetchImpl ?? (typeof fetch === 'function' ? fetch : undefined);
  if (impl === undefined) {
    throw new ApiError('network', `network error calling ${path}: fetch is unavailable`, { path });
  }

  let response: Response;
  try {
    response = await impl(url, {
      method: init.method,
      headers: init.headers,
      body: init.body,
      cache: 'no-store',
      signal: init.signal,
    });
  } catch (error) {
    throw networkError(path, error, init.secret);
  }

  let raw: string;
  try {
    raw = await response.text();
  } catch (error) {
    throw networkError(path, error, init.secret);
  }

  if (!response.ok) {
    throw new ApiError('http', redact(httpMessage(response.status, raw), init.secret), {
      status: response.status,
      path,
    });
  }

  let payload: unknown;
  const trimmed = raw.trim();
  if (trimmed === '') {
    throw new ApiError('malformed', `unexpected response from ${path}: empty body`, {
      status: response.status,
      path,
    });
  }
  try {
    payload = JSON.parse(trimmed);
  } catch {
    throw new ApiError('malformed', `unexpected response from ${path}: body is not valid JSON`, {
      status: response.status,
      path,
    });
  }

  if (init.validate !== undefined && !init.validate(payload)) {
    throw new ApiError(
      'malformed',
      `unexpected response from ${path}: payload does not match the expected shape`,
      { status: response.status, path },
    );
  }
  return payload as T;
}

/**
 * Perform one JSON `GET` and return the decoded body.
 *
 * Exported for the pages that need a route without a dedicated helper; the
 * payload helpers below are the normal entry point.
 */
export async function requestJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  return performRequest<T>(path, {
    method: 'GET',
    baseUrl: options.baseUrl,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
  });
}

// ---------------------------------------------------------------------------
// shape validation (a 2xx body that does not match the contract is 'malformed')
// ---------------------------------------------------------------------------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isString(value: unknown): boolean {
  return typeof value === 'string';
}

function isStringOrNull(value: unknown): boolean {
  return value === null || typeof value === 'string';
}

function isNumberOrNull(value: unknown): boolean {
  return value === null || typeof value === 'number';
}

function isNumber(value: unknown): boolean {
  return typeof value === 'number';
}

function isBoolean(value: unknown): boolean {
  return typeof value === 'boolean';
}

function isArrayOf(value: unknown, item: (entry: unknown) => boolean): boolean {
  return Array.isArray(value) && value.every((entry) => item(entry));
}

function isHealthPayload(value: unknown): boolean {
  return (
    isRecord(value) &&
    (value.status === 'ok' || value.status === 'degraded') &&
    isString(value.version) &&
    isNumberOrNull(value.uptime_seconds) &&
    isNumber(value.profiles_total) &&
    isNumber(value.profiles_running) &&
    isBoolean(value.kill_switch) &&
    isStringOrNull(value.checked_at)
  );
}

function isCounters(value: unknown): boolean {
  return (
    isRecord(value) &&
    isNumber(value.candles_processed) &&
    isNumber(value.orders_submitted) &&
    isNumber(value.orders_filled) &&
    isNumber(value.orders_rejected) &&
    isNumber(value.stream_reconnects) &&
    isNumber(value.risk_rejections) &&
    isNumber(value.errors)
  );
}

function isProfileHealth(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.profile_id) &&
    isString(value.status) &&
    isStringOrNull(value.last_candle_at) &&
    isNumberOrNull(value.lag_seconds) &&
    isStringOrNull(value.last_error) &&
    isNumber(value.reconnect_count) &&
    isCounters(value.counters)
  );
}

function isProfileSnapshot(value: unknown): value is ProfileSnapshot {
  return (
    isRecord(value) &&
    isString(value.profile_id) &&
    isString(value.symbol) &&
    isString(value.timeframe) &&
    isString(value.strategy) &&
    isString(value.mode) &&
    isString(value.status) &&
    isNumberOrNull(value.initial_balance) &&
    isNumberOrNull(value.equity) &&
    isNumberOrNull(value.cash) &&
    isNumberOrNull(value.position_value) &&
    isNumberOrNull(value.total_return) &&
    isNumber(value.n_trades) &&
    isNumber(value.open_positions) &&
    isProfileHealth(value.health) &&
    isStringOrNull(value.started_at) &&
    isStringOrNull(value.updated_at)
  );
}

function isProfilesPayload(value: unknown): boolean {
  return (
    isRecord(value) &&
    isArrayOf(value.profiles, isProfileSnapshot) &&
    isStringOrNull(value.generated_at)
  );
}

function isEquityPoint(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.timestamp) &&
    isNumberOrNull(value.equity) &&
    isNumberOrNull(value.cash) &&
    isNumberOrNull(value.position_value)
  );
}

function isEquityPayload(value: unknown): boolean {
  return isRecord(value) && isArrayOf(value.points, isEquityPoint);
}

function isPosition(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.profile_id) &&
    isString(value.symbol) &&
    isNumberOrNull(value.quantity) &&
    isNumberOrNull(value.average_price) &&
    isString(value.direction) &&
    isString(value.opened_at) &&
    isString(value.updated_at) &&
    isNumberOrNull(value.realized_pnl) &&
    isNumberOrNull(value.unrealized_pnl) &&
    isNumberOrNull(value.stop_price)
  );
}

function isPositionsPayload(value: unknown): boolean {
  return isRecord(value) && isArrayOf(value.positions, isPosition);
}

function isTrade(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.entry_time) &&
    isString(value.exit_time) &&
    isNumber(value.entry_price) &&
    isNumber(value.exit_price) &&
    isNumber(value.size) &&
    isString(value.direction) &&
    isNumber(value.pnl) &&
    isNumber(value.pnl_pct) &&
    isNumber(value.fees) &&
    isString(value.exit_reason) &&
    isNumber(value.duration_minutes) &&
    isNumberOrNull(value.stop_price) &&
    isNumberOrNull(value.take_profit_price) &&
    isString(value.params_id)
  );
}

function isTradesPayload(value: unknown): boolean {
  return isRecord(value) && isArrayOf(value.trades, isTrade) && isNumber(value.count);
}

function isOrder(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.client_order_id) &&
    isString(value.profile_id) &&
    isString(value.symbol) &&
    isString(value.side) &&
    isString(value.type) &&
    isNumberOrNull(value.quantity) &&
    isString(value.state) &&
    isString(value.mode) &&
    isString(value.created_at) &&
    isString(value.updated_at) &&
    isNumberOrNull(value.filled_quantity) &&
    isNumberOrNull(value.price) &&
    isNumberOrNull(value.average_fill_price) &&
    isStringOrNull(value.broker_order_id) &&
    isString(value.reject_reason)
  );
}

function isOrdersPayload(value: unknown): boolean {
  return isRecord(value) && isArrayOf(value.orders, isOrder);
}

function isMetricMap(value: unknown): boolean {
  return isRecord(value) && Object.values(value).every((entry) => isNumberOrNull(entry));
}

function isBenchmark(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.variant) &&
    isNumber(value.initial_balance) &&
    isNumber(value.final_balance) &&
    isNumber(value.n_periods) &&
    isString(value.timeframe) &&
    isMetricMap(value.metrics)
  );
}

function isMetricsPayload(value: unknown): boolean {
  return (
    isRecord(value) &&
    isMetricMap(value.metrics) &&
    (value.benchmark === null || isBenchmark(value.benchmark)) &&
    isStringOrNull(value.generated_at)
  );
}

function isKillSwitchPayload(value: unknown): boolean {
  return (
    isRecord(value) &&
    isBoolean(value.kill_switch) &&
    isString(value.reason) &&
    isStringOrNull(value.changed_at)
  );
}

/** Run `validate` and raise the documented `'malformed'` error otherwise. */
function expectShape<T>(
  payload: unknown,
  validate: (value: unknown) => boolean,
  path: string,
): T {
  if (!validate(payload)) {
    throw new ApiError(
      'malformed',
      `unexpected response from ${path}: payload does not match the expected shape`,
      { path },
    );
  }
  return payload as T;
}

// ---------------------------------------------------------------------------
// read routes
// ---------------------------------------------------------------------------

/** `GET /api/health` — platform status, version, uptime, kill switch. */
export async function fetchHealth(options: RequestOptions = {}): Promise<HealthPayload> {
  const path = '/api/health';
  const payload = await requestJson<unknown>(path, options);
  return expectShape<HealthPayload>(payload, isHealthPayload, path);
}

/** `GET /api/profiles` — every configured profile with its live snapshot. */
export async function fetchProfiles(options: RequestOptions = {}): Promise<ProfilesPayload> {
  const path = '/api/profiles';
  const payload = await requestJson<unknown>(path, options);
  return expectShape<ProfilesPayload>(payload, isProfilesPayload, path);
}

/** `GET /api/profiles/{id}` — one profile. */
export async function fetchProfile(
  profileId: string,
  options: RequestOptions = {},
): Promise<ProfileSnapshot> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}`;
  const payload = await requestJson<unknown>(path, options);
  return expectShape<ProfileSnapshot>(payload, isProfileSnapshot, path);
}

/** `GET /api/profiles/{id}/equity` — the equity curve, oldest first. */
export async function fetchEquity(
  profileId: string,
  options: RequestOptions = {},
): Promise<EquityPayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/equity`;
  const payload = await requestJson<unknown>(path, options);
  return expectShape<EquityPayload>(payload, isEquityPayload, path);
}

/** `GET /api/profiles/{id}/trades` — the closed round-trips. */
export async function fetchTrades(
  profileId: string,
  options: RequestOptions = {},
): Promise<TradesPayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/trades`;
  const payload = await requestJson<unknown>(path, options);
  return expectShape<TradesPayload>(payload, isTradesPayload, path);
}

/** `GET /api/profiles/{id}/orders` — the most recent orders. */
export async function fetchOrders(
  profileId: string,
  options: RequestOptions = {},
): Promise<OrdersPayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/orders`;
  const payload = await requestJson<unknown>(path, options);
  return expectShape<OrdersPayload>(payload, isOrdersPayload, path);
}

/** `GET /api/profiles/{id}/positions` — the open positions. */
export async function fetchPositions(
  profileId: string,
  options: RequestOptions = {},
): Promise<PositionsPayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/positions`;
  const payload = await requestJson<unknown>(path, options);
  return expectShape<PositionsPayload>(payload, isPositionsPayload, path);
}

/** `GET /api/profiles/{id}/metrics` — the frozen metric set and its benchmark. */
export async function fetchMetrics(
  profileId: string,
  options: RequestOptions = {},
): Promise<MetricsPayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/metrics`;
  const payload = await requestJson<unknown>(path, options);
  return expectShape<MetricsPayload>(payload, isMetricsPayload, path);
}

/** `GET /api/kill-switch` — the effective kill-switch state. */
export async function fetchKillSwitch(
  options: RequestOptions = {},
): Promise<KillSwitchPayload> {
  const path = '/api/kill-switch';
  const payload = await requestJson<unknown>(path, options);
  return expectShape<KillSwitchPayload>(payload, isKillSwitchPayload, path);
}

// ---------------------------------------------------------------------------
// mutating route
// ---------------------------------------------------------------------------

/**
 * `POST /api/kill-switch` — engage (`engage: true`) or release the kill switch.
 *
 * The request always carries `Content-Type: application/json` and the
 * `X-Operator-Token` header. The token is scrubbed from every message this call
 * may raise, is never logged and is never part of a URL.
 */
export async function postKillSwitch(
  body: { engage: boolean; reason: string },
  options: MutatingRequestOptions,
): Promise<KillSwitchPayload> {
  const path = '/api/kill-switch';
  const payload = await performRequest<unknown>(path, {
    method: 'POST',
    baseUrl: options.baseUrl,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    headers: {
      'Content-Type': 'application/json',
      [OPERATOR_TOKEN_HEADER]: options.operatorToken,
    },
    body: JSON.stringify({ engage: body.engage, reason: body.reason }),
    validate: isKillSwitchPayload,
    secret: options.operatorToken,
  });
  return payload as KillSwitchPayload;
}
