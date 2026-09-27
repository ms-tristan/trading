/**
 * Typed HTTP client of the platform monitoring API.
 *
 * Server components and client components use the very same functions; only the
 * URL differs:
 *
 * * on the server the request goes to the absolute `${API_ORIGIN}${path}` with
 *   `cache: "no-store"`, because a server component has no origin of its own;
 * * in the browser it goes to the **same-origin relative path**, so the Next
 *   rewrite of `next.config.ts` proxies it to the API. The API disables CORS, so
 *   an absolute cross-origin call from the browser would be blocked.
 *
 * Failure handling: `fetchJson` throws an `ApiError` for a non-2xx status, an
 * unreadable body or a body that is not valid JSON - it never resolves to
 * `Infinity`/`NaN`. `fetchWithFallback` never throws: it answers the last known
 * value of the path, then the caller's default, and hands the `ApiError` back so
 * the page can render an `ErrorBanner` above the data it did manage to show.
 *
 * Every function of this module returns the **view model** of `./types.ts`, not
 * the raw JSON: `./api-wire.ts` transcribes the wire payloads of the platform
 * API and maps them, so a field name lives in exactly one place.
 */

import {
  toAccountResponse,
  toCatalogueApplyResponse,
  toDashboardSettings,
  toEventsResponse,
  toHealthStatus,
  toKillSwitchResponse,
  toProfileActionResponse,
  toProfileDetail,
  toProfileDetailEnvelope,
  toProfilesResponse,
  toStrategiesResponse,
} from "./api-wire";
import type {
  AccountResponse,
  ApiWindow,
  CatalogueApplyRequest,
  CatalogueApplyResponse,
  DashboardSettings,
  EventsResponse,
  HealthStatus,
  KillSwitchResponse,
  ProfileAction,
  ProfileActionResponse,
  ProfileCreateRequest,
  ProfileDetail,
  ProfileMode,
  ProfileState,
  ProfileUpdateRequest,
  ProfilesResponse,
  SettingsUpdateRequest,
  StrategiesResponse,
} from "./types";

/** Origin of the Python API, baked at build time by `deploy/Dockerfile.dashboard`. */
export const API_ORIGIN = process.env.API_ORIGIN ?? "http://127.0.0.1:8080";

/** Header carrying the operator token of every mutating call. */
export const OPERATOR_TOKEN_HEADER = "X-Operator-Token";

/** Error raised by {@link fetchJson}: carries the HTTP status and the path. */
export class ApiError extends Error {
  readonly status: number;
  readonly path: string;

  constructor(message: string, status: number, path: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.path = path;
  }
}

/**
 * Last successful body of every path, per server process.
 *
 * A transient API failure therefore degrades to the previous payload instead of
 * an empty page.
 */
const lastKnownBodies = new Map<string, unknown>();

/**
 * `JSON.parse` reviver dropping every non-finite number.
 *
 * `JSON.parse("1e999")` yields `Infinity` without raising, so a numeric field
 * could reach a component as `Infinity`. Non-finite numbers are collapsed to `0`
 * here, which is what makes "`fetchJson` never returns `Infinity`/`NaN`" true.
 */
function reviveFiniteNumber(_key: string, value: unknown): unknown {
  return typeof value === "number" && !Number.isFinite(value) ? 0 : value;
}

/** Absolute on the server, same-origin relative in the browser. */
export function resolveRequestUrl(path: string): string {
  return typeof window === "undefined" ? `${API_ORIGIN}${path}` : path;
}

/** Build a query string from defined, non-empty values only. */
function queryString(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const serialised = search.toString();
  return serialised === "" ? "" : `?${serialised}`;
}

/**
 * Fetch and parse one API path.
 *
 * @throws {ApiError} on a network failure, a non-2xx status or an unparsable body.
 */
export async function fetchJson<T>(path: string, init?: RequestInit): Promise<T> {
  const url = resolveRequestUrl(path);
  const options: RequestInit = { ...init };

  if (typeof window === "undefined") {
    // A dashboard that shows the last minute of trading is never cached.
    options.cache = "no-store";
  }

  let response: Response;
  try {
    response = await fetch(url, options);
  } catch (cause) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    throw new ApiError(`Request to ${path} failed: ${reason}`, 0, path);
  }

  if (!response.ok) {
    throw new ApiError(
      `Request to ${path} failed with status ${response.status}`,
      response.status,
      path,
    );
  }

  let body: string;
  try {
    body = await response.text();
  } catch (cause) {
    const reason = cause instanceof Error ? cause.message : String(cause);
    throw new ApiError(`Response of ${path} could not be read: ${reason}`, response.status, path);
  }

  // A 204 answers no content at all: there is nothing to parse, and that is not
  // a failure of the call.
  if (body.trim() === "") {
    return undefined as T;
  }

  let payload: T;
  try {
    payload = JSON.parse(body, reviveFiniteNumber) as T;
  } catch {
    throw new ApiError(`Response of ${path} is not valid JSON`, response.status, path);
  }

  lastKnownBodies.set(path, payload);
  return payload;
}

/** Last successfully fetched body of `path`, or `undefined`. */
export function lastKnown<T>(path: string): T | undefined {
  return lastKnownBodies.get(path) as T | undefined;
}

/** Drop every memorised body (used by the tests). */
export function forgetLastKnown(): void {
  lastKnownBodies.clear();
}

/**
 * Fetch a path and never throw.
 *
 * The fallback chain is: the fresh body, then the last known body of the path,
 * then `fallback`. The returned `error` is `null` on success.
 */
export async function fetchWithFallback<T>(
  path: string,
  fallback: T,
): Promise<{ data: T; error: ApiError | null }> {
  try {
    const data = await fetchJson<T>(path);
    if (data === undefined || data === null) {
      return { data: lastKnown<T>(path) ?? fallback, error: null };
    }
    return { data, error: null };
  } catch (cause) {
    const error =
      cause instanceof ApiError
        ? cause
        : new ApiError(cause instanceof Error ? cause.message : String(cause), 0, path);
    return { data: lastKnown<T>(path) ?? fallback, error };
  }
}

/**
 * Fetch one path, map the wire body onto the view model and never throw.
 *
 * The read counterpart of `fetchWithFallback` for the pages: it keeps the
 * documented fallback chain (fresh body, then the last known *raw* body of the
 * path, then `fallback`) and runs `map` on whichever body it answers with, so a
 * page never renders a raw payload and never has to know a wire field name.
 */
export async function fetchMapped<T>(
  path: string,
  fallback: T,
  map: (raw: unknown) => T,
): Promise<{ data: T; error: ApiError | null }> {
  try {
    const raw = await fetchJson<unknown>(path);
    if (raw === undefined || raw === null) {
      const known = lastKnown<unknown>(path);
      return { data: known === undefined ? fallback : map(known), error: null };
    }
    return { data: map(raw), error: null };
  } catch (cause) {
    const error =
      cause instanceof ApiError
        ? cause
        : new ApiError(cause instanceof Error ? cause.message : String(cause), 0, path);
    const known = lastKnown<unknown>(path);
    return { data: known === undefined ? fallback : map(known), error };
  }
}

/** Merge the operator token into the headers of a mutating call. */
function operatorInit(token: string, init: RequestInit = {}): RequestInit {
  const headers: Record<string, string> = {
    ...((init.headers as Record<string, string> | undefined) ?? {}),
    [OPERATOR_TOKEN_HEADER]: token,
  };
  return { ...init, headers };
}

/** JSON body plus the operator token. */
function jsonOperatorInit(token: string, body: unknown, method: string): RequestInit {
  return operatorInit(token, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// read endpoints
// ---------------------------------------------------------------------------

/** `GET /api/health` */
export async function getHealth(): Promise<HealthStatus> {
  return toHealthStatus(await fetchJson<unknown>("/api/health"));
}

/** `GET /api/account?window=...` */
export async function getAccount(window: ApiWindow = "24h"): Promise<AccountResponse> {
  return toAccountResponse(await fetchJson<unknown>(`/api/account${queryString({ window })}`), window);
}

/** Optional filters of `GET /api/profiles`. */
export interface ProfilesQuery {
  mode?: ProfileMode;
  state?: ProfileState;
}

/** `GET /api/profiles` - the API ranks the profiles by portfolio value, descending. */
export async function getProfiles(params: ProfilesQuery = {}): Promise<ProfilesResponse> {
  return toProfilesResponse(await fetchJson<unknown>(`/api/profiles${queryString({ ...params })}`));
}

/** `GET /api/profiles/{id}?window=...` */
export async function getProfile(id: string, window: ApiWindow = "24h"): Promise<ProfileDetail> {
  const raw = await fetchJson<unknown>(
    `/api/profiles/${encodeURIComponent(id)}${queryString({ window })}`,
  );
  return toProfileDetail(raw, window);
}

/** `GET /api/strategies` */
export async function getStrategies(): Promise<StrategiesResponse> {
  return toStrategiesResponse(await fetchJson<unknown>("/api/strategies"));
}

/** `GET /api/events?limit=...` */
export async function getEvents(limit = 50): Promise<EventsResponse> {
  return toEventsResponse(await fetchJson<unknown>(`/api/events${queryString({ limit })}`));
}

/** `GET /api/settings` */
export async function getSettings(): Promise<DashboardSettings> {
  return toDashboardSettings(await fetchJson<unknown>("/api/settings"));
}

// ---------------------------------------------------------------------------
// read endpoints with the fallback chain of the pages
//
// Every one of them answers the view model of `./types.ts` and never throws: the
// fresh payload when the API answers, the last known raw payload of the same
// path when it does not, and the caller's `fallback` when nothing is known.
// ---------------------------------------------------------------------------

/** `GET /api/health`, with the fallback chain of the pages. */
export function fetchHealth(
  fallback: HealthStatus,
): Promise<{ data: HealthStatus; error: ApiError | null }> {
  return fetchMapped("/api/health", fallback, toHealthStatus);
}

/** `GET /api/account?window=...`, with the fallback chain of the pages. */
export function fetchAccount(
  window: ApiWindow,
  fallback: AccountResponse,
): Promise<{ data: AccountResponse; error: ApiError | null }> {
  const path = `/api/account${queryString({ window })}`;
  return fetchMapped(path, fallback, (raw) => toAccountResponse(raw, window));
}

/** `GET /api/profiles`, with the fallback chain of the pages. */
export function fetchProfiles(
  fallback: ProfilesResponse,
  params: ProfilesQuery = {},
): Promise<{ data: ProfilesResponse; error: ApiError | null }> {
  const path = `/api/profiles${queryString({ ...params })}`;
  return fetchMapped(path, fallback, toProfilesResponse);
}

/** `GET /api/profiles/{id}?window=...`, with the fallback chain of the pages. */
export function fetchProfileDetail(
  id: string,
  window: ApiWindow,
  fallback: ProfileDetail,
): Promise<{ data: ProfileDetail; error: ApiError | null }> {
  const path = `/api/profiles/${encodeURIComponent(id)}${queryString({ window })}`;
  return fetchMapped(path, fallback, (raw) => toProfileDetail(raw, window));
}

/** `GET /api/strategies`, with the fallback chain of the pages. */
export function fetchStrategies(
  fallback: StrategiesResponse,
): Promise<{ data: StrategiesResponse; error: ApiError | null }> {
  return fetchMapped("/api/strategies", fallback, toStrategiesResponse);
}

/** `GET /api/events?limit=...`, with the fallback chain of the pages. */
export function fetchEvents(
  limit: number,
  fallback: EventsResponse,
): Promise<{ data: EventsResponse; error: ApiError | null }> {
  return fetchMapped(`/api/events${queryString({ limit })}`, fallback, toEventsResponse);
}

/** `GET /api/settings`, with the fallback chain of the pages. */
export function fetchSettings(
  fallback: DashboardSettings,
): Promise<{ data: DashboardSettings; error: ApiError | null }> {
  return fetchMapped("/api/settings", fallback, toDashboardSettings);
}

// ---------------------------------------------------------------------------
// mutating endpoints - every one of them carries X-Operator-Token
// ---------------------------------------------------------------------------

/** `POST /api/profiles/{id}/actions/{action}` */
export async function postAction(
  id: string,
  action: ProfileAction,
  token: string,
): Promise<ProfileActionResponse> {
  return toProfileActionResponse(
    await fetchJson<unknown>(
      `/api/profiles/${encodeURIComponent(id)}/actions/${encodeURIComponent(action)}`,
      operatorInit(token, { method: "POST" }),
    ),
  );
}

/** `POST /api/kill-switch` */
export async function postKillSwitch(engaged: boolean, token: string): Promise<KillSwitchResponse> {
  return toKillSwitchResponse(
    await fetchJson<unknown>("/api/kill-switch", jsonOperatorInit(token, { engaged }, "POST")),
  );
}

/** `PATCH /api/profiles/{id}` */
export async function patchProfile(
  id: string,
  body: ProfileUpdateRequest,
  token: string,
): Promise<ProfileDetail> {
  return toProfileDetailEnvelope(
    await fetchJson<unknown>(
      `/api/profiles/${encodeURIComponent(id)}`,
      jsonOperatorInit(token, body, "PATCH"),
    ),
  );
}

/** `POST /api/profiles` */
export async function createProfile(
  body: ProfileCreateRequest,
  token: string,
): Promise<ProfileDetail> {
  return toProfileDetailEnvelope(
    await fetchJson<unknown>("/api/profiles", jsonOperatorInit(token, body, "POST")),
  );
}

/** `DELETE /api/profiles/{id}` (optionally forcing a running profile) */
export function deleteProfile(
  id: string,
  token: string,
  force = false,
): Promise<{ deleted: string } | undefined> {
  return fetchJson<{ deleted: string } | undefined>(
    `/api/profiles/${encodeURIComponent(id)}${queryString({ force: force ? "true" : undefined })}`,
    operatorInit(token, { method: "DELETE" }),
  );
}

/** `POST /api/catalogue/apply` */
export async function applyCatalogue(
  token: string,
  body: CatalogueApplyRequest = {},
): Promise<CatalogueApplyResponse> {
  return toCatalogueApplyResponse(
    await fetchJson<unknown>("/api/catalogue/apply", jsonOperatorInit(token, body, "POST")),
  );
}

/** `POST /api/settings` */
export async function postSettings(
  body: SettingsUpdateRequest,
  token: string,
): Promise<DashboardSettings> {
  return toDashboardSettings(
    await fetchJson<unknown>("/api/settings", jsonOperatorInit(token, body, "POST")),
  );
}
