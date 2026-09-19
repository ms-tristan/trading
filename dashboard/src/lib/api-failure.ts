/**
 * Operator-facing vocabulary of a failed monitoring request.
 *
 * The dashboard used to surface whatever the transport produced: when the
 * Docker stack was stopped, the Next.js rewrite answered `502` and the chart
 * panel printed that bare status to the operator. This module is the single
 * translation layer between a failure and the copy the operator reads:
 *
 * * the **headline** says what happened in monitoring terms — `502`, `503` and
 *   `504`, a connection failure and a timeout all read as the monitoring API
 *   being unreachable;
 * * the **detail** keeps the underlying cause — raw status, server text and
 *   requested path — so nothing is hidden and support can still diagnose;
 * * the **message** is the two joined with `': '`, ready for a banner.
 *
 * It is the only place that knows this mapping: every panel, outage view and
 * lifecycle action imports {@link failureReport} / {@link failureMessage}
 * instead of inventing its own copy. The module never performs a request and
 * never throws: any value at all can be passed in.
 */

import { ApiError, errorMessage } from './api';

/** Monitoring-level cause of a failure. */
export type FailureReason =
  | 'unreachable'
  | 'timeout'
  | 'refused'
  | 'not-found'
  | 'malformed'
  | 'unknown';

/** A failure translated for the operator. */
export interface FailureReport {
  /** Monitoring-level cause, for callers that need to branch on it. */
  readonly reason: FailureReason;
  /** What the operator reads first — never a raw status. */
  readonly headline: string;
  /** The underlying cause, raw status included — never hidden. */
  readonly detail: string;
  /** `${headline}: ${detail}` (or the headline alone when there is no detail). */
  readonly message: string;
}

/** Headline of every failure that means "the monitoring API is not answering". */
export const MONITORING_API_UNREACHABLE = 'The monitoring API is unreachable';

/** Headline of a failure the monitoring API refused (401/403). */
export const MONITORING_API_REFUSED = 'The monitoring API refused the request';

/** Headline of a failure the monitoring API has no resource for (404). */
export const MONITORING_API_NOT_FOUND = 'The monitoring API has no such resource';

/** Headline of a 2xx response this dashboard cannot read. */
export const MONITORING_API_BAD_RESPONSE = 'The monitoring API returned an unexpected response';

/** Headline of a server-side error status (5xx other than the proxy statuses). */
export const MONITORING_API_ERROR = 'The monitoring API answered an error';

/** Headline of a failure with no identifiable monitoring meaning. */
export const MONITORING_API_FAILED = 'The monitoring API request failed';

/**
 * Statuses the reverse proxy answers when the monitoring API itself is down.
 *
 * `502` is what the operator actually saw, `503`/`504` are the neighbouring
 * gateway failures and mean exactly the same thing to them.
 */
export const PROXY_UNREACHABLE_STATUSES: readonly number[] = [502, 503, 504];

/** How a timeout announces itself when it is not an {@link ApiError}. */
const TIMEOUT_NAME = 'TimeoutError';

/** Message shapes produced by `AbortSignal.timeout` and by fetch timeouts. */
const TIMEOUT_MESSAGE = /timed?\s*-?\s*out|timeout/i;

/**
 * Whether `failure` looks like a timeout.
 *
 * Only non-null objects are inspected: a bare string is never trusted to carry
 * a `name`, and `null` must not be dereferenced.
 */
function isTimeout(failure: unknown): boolean {
  if (failure === null || typeof failure !== 'object') {
    return false;
  }
  const candidate = failure as { name?: unknown; message?: unknown };
  if (candidate.name === TIMEOUT_NAME) {
    return true;
  }
  return typeof candidate.message === 'string' && TIMEOUT_MESSAGE.test(candidate.message);
}

/** Resolve the monitoring-level cause and the headline of `failure`. */
function resolve(failure: unknown): { reason: FailureReason; headline: string } {
  if (failure instanceof ApiError) {
    if (failure.kind === 'http') {
      const { status } = failure;
      if (status !== null && PROXY_UNREACHABLE_STATUSES.includes(status)) {
        return { reason: 'unreachable', headline: MONITORING_API_UNREACHABLE };
      }
      if (status === 401 || status === 403) {
        return { reason: 'refused', headline: MONITORING_API_REFUSED };
      }
      if (status === 404) {
        return { reason: 'not-found', headline: MONITORING_API_NOT_FOUND };
      }
      return { reason: 'unknown', headline: MONITORING_API_ERROR };
    }
    if (failure.kind === 'network') {
      // A timeout is a special case of "no response", and reads the same to the
      // operator: the monitoring API did not answer.
      return {
        reason: isTimeout(failure) ? 'timeout' : 'unreachable',
        headline: MONITORING_API_UNREACHABLE,
      };
    }
    if (failure.kind === 'malformed') {
      return { reason: 'malformed', headline: MONITORING_API_BAD_RESPONSE };
    }
  }
  // Not an ApiError (or an out-of-contract kind): classify what is classifiable.
  if (isTimeout(failure)) {
    return { reason: 'timeout', headline: MONITORING_API_UNREACHABLE };
  }
  if (failure instanceof TypeError) {
    // This is how `fetch` signals a connection failure: `Failed to fetch`.
    return { reason: 'unreachable', headline: MONITORING_API_UNREACHABLE };
  }
  return { reason: 'unknown', headline: MONITORING_API_FAILED };
}

/**
 * Build the detail line: the raw status when there is one, then the server's
 * own text (already secret-scrubbed by {@link ./api}), then the requested path —
 * each part at most once.
 */
function failureDetail(failure: unknown): string {
  const raw = errorMessage(failure);
  const status = failure instanceof ApiError ? failure.status : null;
  const path = failure instanceof ApiError ? failure.path.trim() : '';
  const parts: string[] = [];
  // A 2xx carrying an unreadable body is not an error status: keep it out.
  if (status !== null && status >= 400) {
    parts.push(`HTTP ${status}`);
  }
  if (raw !== '' && raw !== `HTTP ${status}`) {
    parts.push(raw);
  }
  if (path !== '' && !parts.some((part) => part.includes(path))) {
    parts.push(path);
  }
  return parts.join(' · ');
}

/**
 * Translate any failure into the copy an operator reads: a monitoring-level
 * headline plus the underlying detail. Never throws.
 */
export function failureReport(failure: unknown): FailureReport {
  const { reason, headline } = resolve(failure);
  const detail = failureDetail(failure);
  return {
    reason,
    headline,
    detail,
    message: detail === '' ? headline : `${headline}: ${detail}`,
  };
}

/** Operator-facing message of any failure (`=== failureReport(failure).message`). */
export function failureMessage(failure: unknown): string {
  return failureReport(failure).message;
}
