import { describe, expect, it } from 'vitest';

import { DEFAULT_API_ORIGIN, apiOrigin } from '../../next.config';

/**
 * `NodeJS.ProcessEnv` requires `NODE_ENV` in the current type definitions; the
 * helper keeps the test cases focused on `API_ORIGIN` alone.
 */
function env(values: Record<string, string | undefined> = {}): NodeJS.ProcessEnv {
  return values as NodeJS.ProcessEnv;
}

describe('apiOrigin', () => {
  it('exports the documented default origin', () => {
    expect(DEFAULT_API_ORIGIN).toBe('http://127.0.0.1:8080');
  });

  it('falls back to the default when API_ORIGIN is absent', () => {
    expect(apiOrigin(env())).toBe(DEFAULT_API_ORIGIN);
    expect(apiOrigin(env({ API_ORIGIN: undefined }))).toBe(DEFAULT_API_ORIGIN);
  });

  it('reads the configured origin (the docker compose service name)', () => {
    expect(apiOrigin(env({ API_ORIGIN: 'http://trading-realtime:8080' }))).toBe(
      'http://trading-realtime:8080',
    );
  });

  it('trims whitespace and removes trailing slashes', () => {
    expect(apiOrigin(env({ API_ORIGIN: ' http://127.0.0.1:9000/ ' }))).toBe(
      'http://127.0.0.1:9000',
    );
    expect(apiOrigin(env({ API_ORIGIN: 'http://127.0.0.1:9000///' }))).toBe(
      'http://127.0.0.1:9000',
    );
  });

  it('falls back when the value is blank', () => {
    expect(apiOrigin(env({ API_ORIGIN: '' }))).toBe(DEFAULT_API_ORIGIN);
    expect(apiOrigin(env({ API_ORIGIN: '   ' }))).toBe(DEFAULT_API_ORIGIN);
    expect(apiOrigin(env({ API_ORIGIN: '/' }))).toBe(DEFAULT_API_ORIGIN);
  });

  it('accepts a full process.env shape and defaults to it', () => {
    expect(apiOrigin({ ...process.env, API_ORIGIN: 'http://localhost:9999' })).toBe(
      'http://localhost:9999',
    );
    expect(typeof apiOrigin()).toBe('string');
  });
});
