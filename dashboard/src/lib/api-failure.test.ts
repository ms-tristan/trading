import { describe, expect, it } from 'vitest';

import { ApiError } from './api';
import {
  failureMessage,
  failureReport,
  MONITORING_API_BAD_RESPONSE,
  MONITORING_API_ERROR,
  MONITORING_API_FAILED,
  MONITORING_API_NOT_FOUND,
  MONITORING_API_REFUSED,
  MONITORING_API_UNREACHABLE,
  PROXY_UNREACHABLE_STATUSES,
} from './api-failure';

const CANDLES_PATH = '/api/profiles/btc-paper/candles';

describe('failureReport', () => {
  it('maps exactly the three proxy statuses to unreachable', () => {
    expect(PROXY_UNREACHABLE_STATUSES).toEqual([502, 503, 504]);
  });

  it.each([502, 503, 504])(
    'reads an HTTP %i as the monitoring API being unreachable, never as a raw status',
    (status) => {
      const report = failureReport(
        new ApiError('http', `HTTP ${status}`, { status, path: CANDLES_PATH }),
      );

      expect(report.reason).toBe('unreachable');
      expect(report.headline).toBe(MONITORING_API_UNREACHABLE);
      expect(report.headline).toBe('The monitoring API is unreachable');
      expect(report.detail).toContain(`HTTP ${status}`);
      expect(report.detail).toContain(CANDLES_PATH);
      expect(report.message).toBe(`${report.headline}: ${report.detail}`);
      // The headline must never be the proxy status itself.
      expect(report.headline).not.toMatch(/50[234]/);
      expect(report.headline).not.toMatch(/HTTP/);
    },
  );

  it('keeps the proxy status as a detail, after the headline', () => {
    const report = failureReport(
      new ApiError('http', 'HTTP 502', { status: 502, path: CANDLES_PATH }),
    );

    expect(report.detail).toBe(`HTTP 502 · ${CANDLES_PATH}`);
    expect(report.message).toBe(`The monitoring API is unreachable: HTTP 502 · ${CANDLES_PATH}`);
  });

  it('reads a fetch connection failure as unreachable', () => {
    const report = failureReport(new TypeError('Failed to fetch'));

    expect(report.reason).toBe('unreachable');
    expect(report.headline).toBe(MONITORING_API_UNREACHABLE);
    expect(report.detail).toBe('Failed to fetch');
  });

  it('reads a normalised network failure as unreachable and keeps its raw text', () => {
    const report = failureReport(
      new ApiError('network', 'network error calling /api/profiles: fetch failed', {
        path: '/api/profiles',
      }),
    );

    expect(report.reason).toBe('unreachable');
    expect(report.headline).toBe(MONITORING_API_UNREACHABLE);
    expect(report.detail).toBe('network error calling /api/profiles: fetch failed');
    expect(report.headline).not.toMatch(/fetch|network/i);
  });

  it('reads a timed-out network failure as a timeout, with the unreachable headline', () => {
    const report = failureReport(
      new ApiError('network', 'network error calling /api/health: request timed out', {
        path: '/api/health',
      }),
    );

    expect(report.reason).toBe('timeout');
    expect(report.headline).toBe(MONITORING_API_UNREACHABLE);
    expect(report.detail).toContain('request timed out');
  });

  it('detects a bare TimeoutError by name', () => {
    const report = failureReport(new DOMException('the operation was aborted', 'TimeoutError'));

    expect(report.reason).toBe('timeout');
    expect(report.headline).toBe(MONITORING_API_UNREACHABLE);
  });

  it('detects a TimeoutError name on an arbitrary error', () => {
    const error = new Error('request aborted');
    error.name = 'TimeoutError';

    expect(failureReport(error).reason).toBe('timeout');
    expect(failureReport(error).headline).toBe(MONITORING_API_UNREACHABLE);
  });

  it('reads a rejected credential as refused', () => {
    for (const status of [401, 403]) {
      const report = failureReport(new ApiError('http', 'forbidden', { status, path: '/api/profiles' }));

      expect(report.reason).toBe('refused');
      expect(report.headline).toBe(MONITORING_API_REFUSED);
      expect(report.detail).toContain(`HTTP ${status}`);
      expect(report.detail).toContain('forbidden');
    }
  });

  it('reads a missing resource as not-found', () => {
    const report = failureReport(
      new ApiError('http', 'HTTP 404', { status: 404, path: '/api/profiles/ghost' }),
    );

    expect(report.reason).toBe('not-found');
    expect(report.headline).toBe(MONITORING_API_NOT_FOUND);
    expect(report.detail).toBe('HTTP 404 · /api/profiles/ghost');
  });

  it('reads any other HTTP status as a plain monitoring error', () => {
    const report = failureReport(
      new ApiError('http', 'HTTP 500', { status: 500, path: '/api/profiles/btc-paper' }),
    );

    expect(report.reason).toBe('unknown');
    expect(report.headline).toBe(MONITORING_API_ERROR);
    expect(report.detail).toBe('HTTP 500 · /api/profiles/btc-paper');
  });

  it('reads an unreadable response as a bad response', () => {
    const report = failureReport(
      new ApiError('malformed', 'unexpected response from /api/health: body is not valid JSON', {
        path: '/api/health',
      }),
    );

    expect(report.reason).toBe('malformed');
    expect(report.headline).toBe(MONITORING_API_BAD_RESPONSE);
    expect(report.detail).toBe('unexpected response from /api/health: body is not valid JSON');
  });

  it('does not print an error status for a 2xx with an unreadable body', () => {
    const report = failureReport(
      new ApiError('malformed', 'unexpected response from /api/health: body is not valid JSON', {
        status: 200,
        path: '/api/health',
      }),
    );

    expect(report.reason).toBe('malformed');
    expect(report.detail).not.toContain('HTTP');
  });

  it('falls back to the generic failure headline for a plain error', () => {
    const report = failureReport(new Error('boom'));

    expect(report.reason).toBe('unknown');
    expect(report.headline).toBe(MONITORING_API_FAILED);
    expect(report.detail).toBe('boom');
    expect(report.message).toBe('The monitoring API request failed: boom');
  });

  it('never throws on a non-Error value and still returns usable copy', () => {
    for (const value of [undefined, 'oops', null, 42, { weird: true }]) {
      const report = failureReport(value);

      expect(report.headline.length).toBeGreaterThan(0);
      expect(report.detail.length).toBeGreaterThan(0);
      expect(report.message).toBe(`${report.headline}: ${report.detail}`);
      expect(report.reason).toBe('unknown');
    }

    expect(failureReport('oops').detail).toBe('oops');
    expect(failureReport(undefined).detail).toBe('Unexpected error');
    expect(failureReport(null).detail).toBe('Unexpected error');
  });

  it('never throws on an out-of-contract ApiError kind', () => {
    const failure = new ApiError('weird' as 'http', 'boom', { path: '/api/health' });

    const report = failureReport(failure);

    expect(report.reason).toBe('unknown');
    expect(report.headline).toBe(MONITORING_API_FAILED);
    expect(report.detail).toBe('boom · /api/health');
  });

  it('appends the requested path only once', () => {
    const failure = new ApiError('network', 'network error calling /api/profiles: fetch failed', {
      path: '/api/profiles',
    });

    const report = failureReport(failure);

    expect(report.detail.split('/api/profiles')).toHaveLength(2);
    expect(report.message.split('/api/profiles')).toHaveLength(2);
  });

  it('appends the path of an ApiError whose message does not carry it', () => {
    const report = failureReport(new ApiError('network', 'failed to fetch', { path: '/api/health' }));

    expect(report.detail).toBe('failed to fetch · /api/health');
  });
});

describe('failureMessage', () => {
  it('is always the message of the matching report', () => {
    const failures: unknown[] = [
      new ApiError('http', 'HTTP 502', { status: 502, path: CANDLES_PATH }),
      new ApiError('http', 'HTTP 503', { status: 503, path: '/api/health' }),
      new ApiError('http', 'HTTP 504', { status: 504, path: '/api/health' }),
      new ApiError('http', 'forbidden', { status: 403, path: '/api/kill-switch' }),
      new ApiError('http', 'HTTP 404', { status: 404, path: '/api/profiles/ghost' }),
      new ApiError('http', 'HTTP 500', { status: 500 }),
      new ApiError('network', 'network error calling /api/health: Failed to fetch', {
        path: '/api/health',
      }),
      new ApiError('network', 'network error calling /api/health: request timed out'),
      new ApiError('malformed', 'unexpected response from /api/health: empty body'),
      new TypeError('Failed to fetch'),
      new Error('boom'),
      new Error(''),
      'oops',
      null,
      undefined,
      0,
    ];

    for (const failure of failures) {
      const report = failureReport(failure);
      expect(failureMessage(failure)).toBe(report.message);
      expect(report.message.startsWith(report.headline)).toBe(true);
      if (report.detail !== '') {
        expect(report.message).toBe(`${report.headline}: ${report.detail}`);
      }
    }
  });
});
