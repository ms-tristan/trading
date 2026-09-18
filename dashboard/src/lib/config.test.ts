import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_API_ORIGIN,
  DEFAULT_POLL_INTERVAL_MS,
  DETAIL_POLL_MULTIPLIER,
  POLL_INTERVAL_ENV_VAR,
  resolveDetailPollIntervalMs,
  resolvePollIntervalMs,
  serverApiBaseUrl,
} from './config';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('constants', () => {
  it('pin the documented defaults', () => {
    expect(DEFAULT_API_ORIGIN).toBe('http://127.0.0.1:8080');
    expect(DEFAULT_POLL_INTERVAL_MS).toBe(2000);
    expect(POLL_INTERVAL_ENV_VAR).toBe('NEXT_PUBLIC_POLL_INTERVAL_MS');
    expect(DETAIL_POLL_MULTIPLIER).toBe(5);
  });
});

describe('serverApiBaseUrl', () => {
  it('falls back to the documented origin', () => {
    expect(serverApiBaseUrl({})).toBe(DEFAULT_API_ORIGIN);
    expect(serverApiBaseUrl({ API_ORIGIN: undefined })).toBe(DEFAULT_API_ORIGIN);
  });

  it('reads API_ORIGIN and removes trailing slashes', () => {
    expect(serverApiBaseUrl({ API_ORIGIN: 'http://trading-realtime:8080' })).toBe(
      'http://trading-realtime:8080',
    );
    expect(serverApiBaseUrl({ API_ORIGIN: 'http://127.0.0.1:9000///' })).toBe(
      'http://127.0.0.1:9000',
    );
    expect(serverApiBaseUrl({ API_ORIGIN: '  http://127.0.0.1:9000/  ' })).toBe(
      'http://127.0.0.1:9000',
    );
  });

  it('ignores a blank value', () => {
    expect(serverApiBaseUrl({ API_ORIGIN: '' })).toBe(DEFAULT_API_ORIGIN);
    expect(serverApiBaseUrl({ API_ORIGIN: '   ' })).toBe(DEFAULT_API_ORIGIN);
  });

  it('reads process.env when no environment is injected', () => {
    vi.stubEnv('API_ORIGIN', 'http://stub-host:1234');
    expect(serverApiBaseUrl()).toBe('http://stub-host:1234');
    vi.stubEnv('API_ORIGIN', '');
    expect(serverApiBaseUrl()).toBe(DEFAULT_API_ORIGIN);
  });
});

describe('resolvePollIntervalMs', () => {
  it('defaults to two seconds', () => {
    expect(resolvePollIntervalMs({})).toBe(DEFAULT_POLL_INTERVAL_MS);
  });

  it('reads a positive override, including surrounding whitespace', () => {
    expect(resolvePollIntervalMs({ [POLL_INTERVAL_ENV_VAR]: '500' })).toBe(500);
    expect(resolvePollIntervalMs({ [POLL_INTERVAL_ENV_VAR]: ' 1500 ' })).toBe(1500);
    expect(resolvePollIntervalMs({ [POLL_INTERVAL_ENV_VAR]: '250.5' })).toBe(250.5);
  });

  it('ignores an unusable override', () => {
    for (const value of ['', '   ', 'abc', '0', '-1', 'Infinity', 'NaN']) {
      expect(resolvePollIntervalMs({ [POLL_INTERVAL_ENV_VAR]: value })).toBe(
        DEFAULT_POLL_INTERVAL_MS,
      );
    }
  });

  it('reads process.env when no environment is injected', () => {
    vi.stubEnv(POLL_INTERVAL_ENV_VAR, '750');
    expect(resolvePollIntervalMs()).toBe(750);
  });
});

describe('resolveDetailPollIntervalMs', () => {
  it('is five times the live cadence', () => {
    expect(resolveDetailPollIntervalMs(2000)).toBe(10000);
    expect(resolveDetailPollIntervalMs(500)).toBe(2500);
  });

  it('falls back to the default cadence for an unusable input', () => {
    expect(resolveDetailPollIntervalMs(0)).toBe(10000);
    expect(resolveDetailPollIntervalMs(-5)).toBe(10000);
    expect(resolveDetailPollIntervalMs(Number.NaN)).toBe(10000);
  });
});
