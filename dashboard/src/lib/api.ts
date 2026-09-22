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
  Candle,
  CandlesPayload,
  CatalogPayload,
  CatalogSymbol,
  ControlPayload,
  CreateProfileBody,
  CreateProfilePayload,
  DeletePayload,
  EquityPayload,
  HealthPayload,
  KillSwitchPayload,
  LifecyclePayload,
  OperatorTokenCheckPayload,
  MetricsPayload,
  OrdersPayload,
  OrphanReport,
  PositionsPayload,
  ProfileControl,
  ProfilesPayload,
  ProfileSnapshot,
  TradesPayload,
  WalletSnapshot,
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
  method: 'GET' | 'POST' | 'DELETE';
  baseUrl?: string;
  signal?: AbortSignal;
  fetchImpl?: typeof fetch;
  headers?: Record<string, string>;
  body?: string;
  /** Query string appended to the request URL only (never to the error path). */
  query?: string;
  validate?: (payload: unknown) => boolean;
  /** Secret scrubbed from every message this request may raise. */
  secret?: string;
}

async function performRequest<T>(path: string, init: RequestInit_): Promise<T> {
  const url = apiUrl(init.query === undefined ? path : `${path}${init.query}`, init.baseUrl);
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

/**
 * An optional number-or-null key: absent is accepted (an older server does not
 * emit it), present must match the documented type.
 */
function isOptionalNumberOrNull(value: unknown): boolean {
  return value === undefined || isNumberOrNull(value);
}

/** An optional string-or-null key, tolerated exactly like the numeric one. */
function isOptionalStringOrNull(value: unknown): boolean {
  return value === undefined || isStringOrNull(value);
}

function isArrayOf(value: unknown, item: (entry: unknown) => boolean): boolean {
  return Array.isArray(value) && value.every((entry) => item(entry));
}

/**
 * The shared platform wallet of `GET /api/profiles` and `GET /api/health`.
 *
 * `source` is one of the two documented origins and `mode` one of the two run
 * modes: anything else is not a wallet snapshot, so the body is refused as
 * `'malformed'` instead of being rendered as a half-known wallet.
 */
export function isWalletSnapshot(value: unknown): value is WalletSnapshot {
  return (
    isRecord(value) &&
    isString(value.name) &&
    isRunMode(value.mode) &&
    isNumberOrNull(value.initial_balance) &&
    isNumberOrNull(value.cash) &&
    isNumberOrNull(value.equity) &&
    isNumberOrNull(value.deployed) &&
    isNumberOrNull(value.realized_pnl) &&
    isNumberOrNull(value.unrealized_pnl) &&
    isNumberOrNull(value.total_exposure) &&
    isNumber(value.profiles) &&
    (value.source === 'local' || value.source === 'venue') &&
    isStringOrNull(value.updated_at)
  );
}

/**
 * The optional `wallet` key of a payload: absent (`undefined`) and explicit
 * `null` both mean "no wallet reported", anything else must be a wallet.
 */
function isOptionalWallet(value: unknown): boolean {
  return value === undefined || value === null || isWalletSnapshot(value);
}

/**
 * Default the shared-wallet key to `null` when the producer did not emit it.
 *
 * The guard tolerates the absence so that an older monitoring server stays a
 * valid producer; this normalisation makes the absence explicit, so a consumer
 * never has to tell `undefined` from `null`.
 */
function withWallet<T extends { wallet?: WalletSnapshot | null }>(payload: T): T {
  if (payload.wallet !== undefined) {
    return payload;
  }
  return { ...payload, wallet: null };
}

/** One closure entry of the startup safety sweep report. */
function isOrphanClosure(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.profile_id) &&
    isString(value.symbol) &&
    isNumberOrNull(value.quantity) &&
    isString(value.side) &&
    isNumberOrNull(value.price)
  );
}

/** One unclosable orphan entry of the startup safety sweep report. */
function isOrphanFailure(value: unknown): boolean {
  return (
    isRecord(value) &&
    isString(value.profile_id) &&
    isString(value.symbol) &&
    isNumberOrNull(value.quantity) &&
    isString(value.error)
  );
}

/**
 * Report of the startup safety sweep (`GET /api/orphans` and the
 * `orphaned_positions` key of `GET /api/health`).
 *
 * Every field is required and both arrays are validated entry by entry: a
 * report the dashboard cannot read in full is refused as `'malformed'` rather
 * than half-rendered, because the operator must never read a truncated list of
 * what was — or was not — flattened at the venue.
 */
export function isOrphanReport(value: unknown): value is OrphanReport {
  return (
    isRecord(value) &&
    isNumber(value.found) &&
    isNumber(value.orphaned) &&
    isNumber(value.closed_count) &&
    isNumber(value.failed_count) &&
    isArrayOf(value.closed, isOrphanClosure) &&
    isArrayOf(value.failed, isOrphanFailure) &&
    isStringOrNull(value.swept_at)
  );
}

/**
 * The optional `orphaned_positions` key of a health payload: absent
 * (`undefined`) and explicit `null` both mean "never swept", anything else must
 * be a full report.
 */
function isOptionalOrphanReport(value: unknown): boolean {
  return value === undefined || value === null || isOrphanReport(value);
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
    isStringOrNull(value.checked_at) &&
    isOptionalWallet(value.wallet) &&
    isOptionalOrphanReport(value.orphaned_positions)
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
    isStringOrNull(value.updated_at) &&
    // The attributed breakdown of the shared wallet: validated only when the
    // producer emits it, so a payload of an older server still validates and is
    // still rendered (with the em dash placeholder where a value is absent).
    isOptionalNumberOrNull(value.allocation) &&
    isOptionalNumberOrNull(value.deployed) &&
    isOptionalNumberOrNull(value.realized_pnl) &&
    isOptionalNumberOrNull(value.unrealized_pnl) &&
    isOptionalStringOrNull(value.last_block_reason)
  );
}

function isProfilesPayload(value: unknown): boolean {
  return (
    isRecord(value) &&
    isArrayOf(value.profiles, isProfileSnapshot) &&
    isStringOrNull(value.generated_at) &&
    isOptionalWallet(value.wallet)
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

/** A run mode is one of the two documented values. */
function isRunMode(value: unknown): boolean {
  return value === 'paper' || value === 'live';
}

/** `GET /api/profiles/{id}/candles` — one persisted OHLCV candle. */
export function isCandle(value: unknown): value is Candle {
  return (
    isRecord(value) &&
    isString(value.profile_id) &&
    isString(value.timestamp) &&
    isNumberOrNull(value.open) &&
    isNumberOrNull(value.high) &&
    isNumberOrNull(value.low) &&
    isNumberOrNull(value.close) &&
    isNumberOrNull(value.volume) &&
    isBoolean(value.closed)
  );
}

/** Body of `GET /api/profiles/{id}/candles`. */
export function isCandlesPayload(value: unknown): value is CandlesPayload {
  return isRecord(value) && isArrayOf(value.candles, isCandle) && isNumber(value.count);
}

/** One entry of the catalog symbol list. */
export function isCatalogSymbol(value: unknown): value is CatalogSymbol {
  return (
    isRecord(value) &&
    isString(value.symbol) &&
    isString(value.base) &&
    isString(value.quote)
  );
}

/** Body of `GET /api/catalog`. */
export function isCatalogPayload(value: unknown): value is CatalogPayload {
  return (
    isRecord(value) &&
    isArrayOf(value.symbols, isCatalogSymbol) &&
    isArrayOf(value.strategies, isString) &&
    isArrayOf(value.timeframes, isString) &&
    isArrayOf(value.modes, isRunMode)
  );
}

/** One per-profile control entry of `GET /api/control`. */
export function isProfileControl(value: unknown): value is ProfileControl {
  return (
    isRecord(value) &&
    isString(value.profile_id) &&
    isBoolean(value.paused) &&
    isBoolean(value.running)
  );
}

/** Body of `GET /api/control`. */
export function isControlPayload(value: unknown): value is ControlPayload {
  return (
    isRecord(value) &&
    isBoolean(value.engine_running) &&
    isBoolean(value.read_only) &&
    isBoolean(value.mutable) &&
    isArrayOf(value.profiles, isProfileControl)
  );
}

/** Body of `GET /api/operator-token`. `read_only` is optional by contract. */
export function isOperatorTokenCheckPayload(value: unknown): value is OperatorTokenCheckPayload {
  return (
    isRecord(value) &&
    isBoolean(value.valid) &&
    typeof value.reason === 'string' &&
    (value.read_only === undefined || isBoolean(value.read_only))
  );
}

/** Body of a successful pause or resume call. */
export function isLifecyclePayload(value: unknown): value is LifecyclePayload {
  return isRecord(value) && isProfileSnapshot(value.profile) && isBoolean(value.paused);
}

/** Body of a successful `POST /api/profiles`: `{profile}`, never `paused`. */
export function isCreateProfilePayload(value: unknown): value is CreateProfilePayload {
  return isRecord(value) && isProfileSnapshot(value.profile);
}

/** Body of a successful `DELETE /api/profiles/{id}`. */
export function isDeletePayload(value: unknown): value is DeletePayload {
  return isRecord(value) && isString(value.profile_id) && value.deleted === true;
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

/** `GET /api/health` — platform status, version, uptime, kill switch, wallet. */
export async function fetchHealth(options: RequestOptions = {}): Promise<HealthPayload> {
  const path = '/api/health';
  const payload = await requestJson<unknown>(path, options);
  return withWallet(expectShape<HealthPayload>(payload, isHealthPayload, path));
}

/**
 * `GET /api/orphans` — the report of the startup safety sweep.
 *
 * `swept_at === null` means the platform was never swept; the caller then
 * renders no warning at all. The route is read-only and, like every other read
 * route, validates the full body: a report the dashboard cannot read in full is
 * refused instead of being shown truncated.
 */
export async function fetchOrphans(options: RequestOptions = {}): Promise<OrphanReport> {
  const path = '/api/orphans';
  const payload = await requestJson<unknown>(path, options);
  return expectShape<OrphanReport>(payload, isOrphanReport, path);
}

/** `GET /api/profiles` — the shared wallet and every configured profile. */
export async function fetchProfiles(options: RequestOptions = {}): Promise<ProfilesPayload> {
  const path = '/api/profiles';
  const payload = await requestJson<unknown>(path, options);
  return withWallet(expectShape<ProfilesPayload>(payload, isProfilesPayload, path));
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

/**
 * `GET /api/profiles/{id}/candles?limit=N` — the persisted candle window of one
 * profile, oldest first. `limit` is optional server-side and capped there; the
 * caller asks for the window it renders.
 */
export async function fetchCandles(
  profileId: string,
  limit: number,
  options: RequestOptions = {},
): Promise<CandlesPayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/candles`;
  const payload = await performRequest<unknown>(path, {
    method: 'GET',
    baseUrl: options.baseUrl,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    query: `?limit=${limit}`,
    validate: isCandlesPayload,
  });
  return payload as CandlesPayload;
}

/** `GET /api/catalog` — symbols, strategies, timeframes and modes of the pickers. */
export async function fetchCatalog(options: RequestOptions = {}): Promise<CatalogPayload> {
  const path = '/api/catalog';
  const payload = await requestJson<unknown>(path, options);
  return expectShape<CatalogPayload>(payload, isCatalogPayload, path);
}

/**
 * `GET /api/control` — engine/read-only status and the per-profile pause state.
 *
 * Read-only by design: this route needs no operator token and stays available in
 * `realtime serve` mode.
 */
export async function fetchControl(options: RequestOptions = {}): Promise<ControlPayload> {
  const path = '/api/control';
  const payload = await requestJson<unknown>(path, options);
  return expectShape<ControlPayload>(payload, isControlPayload, path);
}

/**
 * Check whether the token in `options.operatorToken` authorises mutations.
 *
 * `GET /api/operator-token` answers `200` whether the token is right or wrong —
 * the payload's `valid` flag and `reason` carry the verdict — so this call
 * resolves for a wrong token instead of throwing. That is deliberate: the
 * caller is asking a question, and "no, it is wrong" is an answer, not an
 * error. Only a transport failure or an unexpected shape rejects.
 *
 * The token is sent as the `X-Operator-Token` header, registered as the secret
 * to scrub from any message this call may raise, and never placed in the URL.
 */
export async function verifyOperatorToken(
  options: MutatingRequestOptions,
): Promise<OperatorTokenCheckPayload> {
  const path = '/api/operator-token';
  const payload = await performRequest<unknown>(path, {
    method: 'GET',
    baseUrl: options.baseUrl,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    headers: { [OPERATOR_TOKEN_HEADER]: options.operatorToken },
    validate: isOperatorTokenCheckPayload,
    secret: options.operatorToken,
  });
  return payload as OperatorTokenCheckPayload;
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

/**
 * Build the shared init of a mutating call: JSON content type, the operator
 * token header, and the token registered as the secret to scrub from every
 * message the request may raise.
 */
function mutationInit(
  options: MutatingRequestOptions,
  mutation: {
    method: 'POST' | 'DELETE';
    body?: string;
    query?: string;
    validate: (payload: unknown) => boolean;
  },
): RequestInit_ {
  return {
    method: mutation.method,
    baseUrl: options.baseUrl,
    signal: options.signal,
    fetchImpl: options.fetchImpl,
    headers: {
      'Content-Type': 'application/json',
      [OPERATOR_TOKEN_HEADER]: options.operatorToken,
    },
    body: mutation.body,
    query: mutation.query,
    validate: mutation.validate,
    secret: options.operatorToken,
  };
}

/**
 * `POST /api/profiles/{id}/pause` — stop opening new positions.
 *
 * The profile keeps managing the position it already holds: the stop loss stays
 * active and exits are still evaluated. The token is never logged, never part of
 * a URL and never rendered.
 */
export async function pauseProfile(
  profileId: string,
  options: MutatingRequestOptions,
): Promise<LifecyclePayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/pause`;
  const payload = await performRequest<unknown>(
    path,
    mutationInit(options, { method: 'POST', body: JSON.stringify({}), validate: isLifecyclePayload }),
  );
  return payload as LifecyclePayload;
}

/** `POST /api/profiles/{id}/resume` — let the profile open positions again. */
export async function resumeProfile(
  profileId: string,
  options: MutatingRequestOptions,
): Promise<LifecyclePayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}/resume`;
  const payload = await performRequest<unknown>(
    path,
    mutationInit(options, { method: 'POST', body: JSON.stringify({}), validate: isLifecyclePayload }),
  );
  return payload as LifecyclePayload;
}

/**
 * `DELETE /api/profiles/{id}` — flatten the profile and remove it.
 *
 * The server closes every open order and the open position at market before it
 * removes anything; a refused flattening leaves the profile in place and answers
 * an error the dashboard displays verbatim.
 */
export async function deleteProfile(
  profileId: string,
  options: MutatingRequestOptions,
): Promise<DeletePayload> {
  const path = `/api/profiles/${encodeURIComponent(profileId)}`;
  const payload = await performRequest<unknown>(
    path,
    mutationInit(options, { method: 'DELETE', validate: isDeletePayload }),
  );
  return payload as DeletePayload;
}

/**
 * `POST /api/profiles` — create a profile and start it.
 *
 * Optional fields are omitted from the body when the caller leaves them out, so
 * the server applies its own defaults. Duplicate id -> 409, unknown strategy or
 * unsupported timeframe -> 400: both are surfaced verbatim.
 *
 * The server answers `201 {profile}` — creation carries no `paused` field, unlike
 * pause/resume (`{profile, paused}`).
 */
export async function createProfile(
  body: CreateProfileBody,
  options: MutatingRequestOptions,
): Promise<CreateProfilePayload> {
  const path = '/api/profiles';
  const request: Record<string, unknown> = {
    profile_id: body.profile_id,
    symbol: body.symbol,
    timeframe: body.timeframe,
    strategy: body.strategy,
    mode: body.mode,
  };
  if (body.initial_balance !== undefined) {
    request.initial_balance = body.initial_balance;
  }
  if (body.params !== undefined) {
    request.params = body.params;
  }
  if (body.forecast !== undefined) {
    request.forecast = body.forecast;
  }
  const payload = await performRequest<unknown>(
    path,
    mutationInit(options, {
      method: 'POST',
      body: JSON.stringify(request),
      validate: isCreateProfilePayload,
    }),
  );
  return payload as CreateProfilePayload;
}
