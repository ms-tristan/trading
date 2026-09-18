import { describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  OPERATOR_TOKEN_HEADER,
  apiUrl,
  errorMessage,
  fetchEquity,
  fetchHealth,
  fetchKillSwitch,
  fetchMetrics,
  fetchOrders,
  fetchPositions,
  fetchProfile,
  fetchProfiles,
  fetchTrades,
  postKillSwitch,
  requestJson,
  type RequestOptions,
} from './api';
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

// ---------------------------------------------------------------------------
// fixtures: exact payload shapes of the frozen API
// ---------------------------------------------------------------------------

const counters = {
  candles_processed: 120,
  orders_submitted: 4,
  orders_filled: 3,
  orders_rejected: 1,
  stream_reconnects: 0,
  risk_rejections: 1,
  errors: 0,
};

const profile: ProfileSnapshot = {
  profile_id: 'alpha',
  symbol: 'BTC/USDT',
  timeframe: '1h',
  strategy: 'BasicStrategy',
  mode: 'paper',
  status: 'running',
  initial_balance: 10000,
  equity: 10450.5,
  cash: 8000,
  position_value: 2450.5,
  total_return: 0.04505,
  n_trades: 12,
  open_positions: 1,
  health: {
    profile_id: 'alpha',
    status: 'running',
    last_candle_at: '2024-01-01T00:00:00+00:00',
    lag_seconds: 12.5,
    last_error: null,
    reconnect_count: 0,
    counters,
  },
  started_at: '2023-12-01T00:00:00+00:00',
  updated_at: '2024-01-01T00:00:00+00:00',
};

const health: HealthPayload = {
  status: 'ok',
  version: '0.1.0',
  uptime_seconds: 3600,
  profiles_total: 1,
  profiles_running: 1,
  kill_switch: false,
  checked_at: '2024-01-01T00:00:00+00:00',
};

const profiles: ProfilesPayload = {
  profiles: [profile],
  generated_at: '2024-01-01T00:00:00+00:00',
};

const equity: EquityPayload = {
  points: [{ timestamp: '2024-01-01T00:00:00+00:00', equity: 10000, cash: 10000, position_value: 0 }],
};

const positions: PositionsPayload = {
  positions: [
    {
      profile_id: 'alpha',
      symbol: 'BTC/USDT',
      quantity: 0.05,
      average_price: 42000,
      direction: 'long',
      opened_at: '2024-01-01T00:00:00+00:00',
      updated_at: '2024-01-01T01:00:00+00:00',
      realized_pnl: 0,
      unrealized_pnl: 12.5,
      stop_price: 40000,
    },
  ],
};

const trades: TradesPayload = {
  trades: [
    {
      entry_time: '2023-12-31T00:00:00+00:00',
      exit_time: '2023-12-31T06:00:00+00:00',
      entry_price: 41000,
      exit_price: 41500,
      size: 0.01,
      direction: 'long',
      pnl: 5,
      pnl_pct: 0.0122,
      fees: 0.5,
      exit_reason: 'take_profit',
      duration_minutes: 360,
      stop_price: 40000,
      take_profit_price: 41500,
      params_id: 'default',
    },
  ],
  count: 1,
};

const orders: OrdersPayload = {
  orders: [
    {
      client_order_id: 'order-1',
      profile_id: 'alpha',
      symbol: 'BTC/USDT',
      side: 'buy',
      type: 'market',
      quantity: 0.01,
      state: 'filled',
      mode: 'paper',
      created_at: '2024-01-01T00:00:00+00:00',
      updated_at: '2024-01-01T00:00:01+00:00',
      filled_quantity: 0.01,
      price: null,
      average_fill_price: 42000,
      broker_order_id: null,
      reject_reason: '',
    },
  ],
};

const metrics: MetricsPayload = {
  metrics: { total_return: 0.045, sharpe_ratio: 1.2, n_trades: 12 },
  benchmark: {
    variant: 'buy_and_hold',
    initial_balance: 10000,
    final_balance: 10100,
    n_periods: 120,
    timeframe: '1h',
    metrics: { total_return: 0.01 },
  },
  generated_at: '2024-01-01T00:00:00+00:00',
};

const killSwitch: KillSwitchPayload = {
  kill_switch: false,
  reason: '',
  changed_at: null,
};

// ---------------------------------------------------------------------------
// fetch doubles (no live server, no bound port)
// ---------------------------------------------------------------------------

interface RecordedCall {
  url: string;
  init: RequestInit | undefined;
}

function jsonResponse(body: unknown, status = 200): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
  } as unknown as Response;
}

function recordingFetch(response: Response): { impl: typeof fetch; calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const impl = (async (url: unknown, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return response;
  }) as unknown as typeof fetch;
  return { impl, calls };
}

function failingFetch(error: unknown): typeof fetch {
  return (async () => {
    throw error;
  }) as unknown as typeof fetch;
}

const readCases: Array<{
  name: string;
  call: (options: RequestOptions) => Promise<unknown>;
  path: string;
  payload: unknown;
}> = [
  { name: 'fetchHealth', call: (o) => fetchHealth(o), path: '/api/health', payload: health },
  { name: 'fetchProfiles', call: (o) => fetchProfiles(o), path: '/api/profiles', payload: profiles },
  {
    name: 'fetchProfile',
    call: (o) => fetchProfile('alpha', o),
    path: '/api/profiles/alpha',
    payload: profile,
  },
  {
    name: 'fetchEquity',
    call: (o) => fetchEquity('alpha', o),
    path: '/api/profiles/alpha/equity',
    payload: equity,
  },
  {
    name: 'fetchTrades',
    call: (o) => fetchTrades('alpha', o),
    path: '/api/profiles/alpha/trades',
    payload: trades,
  },
  {
    name: 'fetchOrders',
    call: (o) => fetchOrders('alpha', o),
    path: '/api/profiles/alpha/orders',
    payload: orders,
  },
  {
    name: 'fetchPositions',
    call: (o) => fetchPositions('alpha', o),
    path: '/api/profiles/alpha/positions',
    payload: positions,
  },
  {
    name: 'fetchMetrics',
    call: (o) => fetchMetrics('alpha', o),
    path: '/api/profiles/alpha/metrics',
    payload: metrics,
  },
  {
    name: 'fetchKillSwitch',
    call: (o) => fetchKillSwitch(o),
    path: '/api/kill-switch',
    payload: killSwitch,
  },
];

// ---------------------------------------------------------------------------
// reading the API
// ---------------------------------------------------------------------------

describe('read routes', () => {
  it.each(readCases)('$name requests $path and returns the payload', async ({ call, path, payload }) => {
    const { impl, calls } = recordingFetch(jsonResponse(payload));

    await expect(call({ fetchImpl: impl })).resolves.toEqual(payload);

    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe(path);
    expect(calls[0]?.init?.cache).toBe('no-store');
    expect(calls[0]?.init?.method).toBe('GET');
  });

  it.each(readCases)('$name rejects a payload that does not match the shape', async ({ call, path }) => {
    const { impl } = recordingFetch(jsonResponse({ unexpected: true }));

    await expect(call({ fetchImpl: impl })).rejects.toMatchObject({
      name: 'ApiError',
      kind: 'malformed',
      path,
    });
  });

  it('percent-encodes the profile id', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(profile));
    await fetchProfile('a b/c', { fetchImpl: impl });
    expect(calls[0]?.url).toBe('/api/profiles/a%20b%2Fc');
  });

  it('prefixes the request with the base URL of a Server Component', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(health));
    await fetchHealth({ baseUrl: 'http://trading-realtime:8080/', fetchImpl: impl });
    expect(calls[0]?.url).toBe('http://trading-realtime:8080/api/health');
  });

  it('forwards the caller signal', async () => {
    const controller = new AbortController();
    const { impl, calls } = recordingFetch(jsonResponse(health));
    await fetchHealth({ fetchImpl: impl, signal: controller.signal });
    expect(calls[0]?.init?.signal).toBe(controller.signal);
  });
});

// ---------------------------------------------------------------------------
// failures
// ---------------------------------------------------------------------------

describe('HTTP failures', () => {
  it('exposes the status and the server error message verbatim', async () => {
    const { impl } = recordingFetch(
      jsonResponse({ error: "unknown profile: 'ghost'" }, 404),
    );

    const failure = await fetchProfile('ghost', { fetchImpl: impl }).catch((error: unknown) => error);

    expect(failure).toBeInstanceOf(ApiError);
    const apiError = failure as ApiError;
    expect(apiError.kind).toBe('http');
    expect(apiError.status).toBe(404);
    expect(apiError.path).toBe('/api/profiles/ghost');
    expect(apiError.message).toBe("unknown profile: 'ghost'");
  });

  it('exposes the read-only refusal of the mutating route verbatim (403)', async () => {
    const { impl } = recordingFetch(
      jsonResponse({ error: 'mutations are disabled on this server' }, 403),
    );

    await expect(
      postKillSwitch({ engage: true, reason: 'test' }, { fetchImpl: impl, operatorToken: 'tok' }),
    ).rejects.toMatchObject({
      kind: 'http',
      status: 403,
      message: 'mutations are disabled on this server',
    });
  });

  it('falls back to the status line when the error body is not JSON', async () => {
    const { impl } = recordingFetch(jsonResponse('<html>boom</html>', 500));

    await expect(fetchHealth({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'http',
      status: 500,
      message: 'HTTP 500',
    });
  });

  it('falls back to the status line for a JSON body without an error message', async () => {
    const { impl } = recordingFetch(jsonResponse({ detail: 'nope' }, 500));

    await expect(fetchHealth({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'http',
      status: 500,
      message: 'HTTP 500',
    });
  });
});

describe('malformed responses', () => {
  it('rejects a 2xx body that is not valid JSON', async () => {
    const { impl } = recordingFetch(jsonResponse('<html>not json</html>'));

    await expect(fetchHealth({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
      path: '/api/health',
    });
  });

  it('rejects an empty 2xx body', async () => {
    const { impl } = recordingFetch(jsonResponse(''));

    await expect(fetchHealth({ fetchImpl: impl })).rejects.toMatchObject({ kind: 'malformed' });
  });

  it('rejects a 2xx body with a missing field', async () => {
    const { impl } = recordingFetch(jsonResponse({ ...health, version: undefined }));

    await expect(fetchHealth({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
      message: expect.stringContaining('does not match the expected shape'),
    });
  });

  it('rejects a profiles payload whose profiles entry is not a snapshot', async () => {
    const { impl } = recordingFetch(jsonResponse({ profiles: [{ profile_id: 'alpha' }] }));

    await expect(fetchProfiles({ fetchImpl: impl })).rejects.toMatchObject({ kind: 'malformed' });
  });
});

describe('network failures', () => {
  it('normalises an unreachable server', async () => {
    const failing = failingFetch(new TypeError('fetch failed'));

    await expect(fetchHealth({ fetchImpl: failing })).rejects.toMatchObject({
      kind: 'network',
      status: null,
      message: expect.stringContaining('network error calling /api/health'),
    });
  });

  it('normalises an aborted request', async () => {
    const controller = new AbortController();
    controller.abort();
    const impl = (async (_url: unknown, init?: RequestInit) => {
      if (init?.signal?.aborted === true) {
        throw new DOMException('The operation was aborted.', 'AbortError');
      }
      return jsonResponse(health);
    }) as unknown as typeof fetch;

    await expect(
      fetchHealth({ fetchImpl: impl, signal: controller.signal }),
    ).rejects.toMatchObject({ kind: 'network' });
  });

  it('normalises a body that fails while being read', async () => {
    const impl = (async () =>
      ({
        ok: true,
        status: 200,
        text: () => Promise.reject(new TypeError('terminated')),
      }) as unknown as Response) as unknown as typeof fetch;

    await expect(fetchHealth({ fetchImpl: impl })).rejects.toMatchObject({ kind: 'network' });
  });

  it('reports a missing fetch implementation instead of throwing', async () => {
    vi.stubGlobal('fetch', undefined);
    try {
      await expect(fetchHealth()).rejects.toMatchObject({ kind: 'network' });
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

// ---------------------------------------------------------------------------
// the mutating route
// ---------------------------------------------------------------------------

describe('postKillSwitch', () => {
  const engaged: KillSwitchPayload = {
    kill_switch: true,
    reason: 'manual stop',
    changed_at: '2024-01-01T00:00:00+00:00',
  };

  it('sends the documented headers and body, and returns the new state', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(engaged));

    const result = await postKillSwitch(
      { engage: true, reason: 'manual stop' },
      { fetchImpl: impl, operatorToken: 'secret-token' },
    );

    expect(result).toEqual(engaged);
    expect(calls[0]?.url).toBe('/api/kill-switch');
    expect(calls[0]?.init?.method).toBe('POST');
    expect(calls[0]?.init?.cache).toBe('no-store');

    const headers = calls[0]?.init?.headers as Record<string, string>;
    expect(headers['Content-Type']).toBe('application/json');
    expect(headers[OPERATOR_TOKEN_HEADER]).toBe('secret-token');
    expect(calls[0]?.init?.body).toBe(JSON.stringify({ engage: true, reason: 'manual stop' }));
  });

  it('sends the release command', async () => {
    const released: KillSwitchPayload = { kill_switch: false, reason: '', changed_at: null };
    const { impl, calls } = recordingFetch(jsonResponse(released));

    const result = await postKillSwitch(
      { engage: false, reason: '' },
      { fetchImpl: impl, operatorToken: 'secret-token' },
    );

    expect(result).toEqual(released);
    expect(calls[0]?.init?.body).toBe(JSON.stringify({ engage: false, reason: '' }));
  });

  it('never leaks the operator token into a message', async () => {
    const token = 'super-secret-token';
    const failing = failingFetch(new Error(`connection refused for token ${token}`));

    const failure = await postKillSwitch(
      { engage: true, reason: 'test' },
      { fetchImpl: failing, operatorToken: token },
    ).catch((error: unknown) => error);

    const message = errorMessage(failure);
    expect(message).not.toContain(token);
    expect(message).toContain('***');
  });

  it('never leaks the token from an HTTP error body', async () => {
    const token = 'super-secret-token';
    const { impl } = recordingFetch(jsonResponse({ error: `invalid token ${token}` }, 403));

    const failure = await postKillSwitch(
      { engage: true, reason: 'test' },
      { fetchImpl: impl, operatorToken: token },
    ).catch((error: unknown) => error);

    expect(errorMessage(failure)).not.toContain(token);
  });

  it('rejects a malformed success payload', async () => {
    const { impl } = recordingFetch(jsonResponse({ kill_switch: 'yes' }));

    await expect(
      postKillSwitch({ engage: true, reason: 'test' }, { fetchImpl: impl, operatorToken: 'tok' }),
    ).rejects.toMatchObject({ kind: 'malformed', path: '/api/kill-switch' });
  });
});

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

describe('apiUrl', () => {
  it('keeps the path relative for the browser (same origin)', () => {
    expect(apiUrl('/api/health')).toBe('/api/health');
    expect(apiUrl('api/health')).toBe('/api/health');
    expect(apiUrl('/api/health', '')).toBe('/api/health');
    expect(apiUrl('/api/health', '   ')).toBe('/api/health');
  });

  it('joins an absolute origin without doubling the slash', () => {
    expect(apiUrl('/api/health', 'http://127.0.0.1:8080')).toBe('http://127.0.0.1:8080/api/health');
    expect(apiUrl('/api/health', 'http://127.0.0.1:8080/')).toBe('http://127.0.0.1:8080/api/health');
    expect(apiUrl('/api/health', 'http://127.0.0.1:8080///')).toBe(
      'http://127.0.0.1:8080/api/health',
    );
  });
});

describe('errorMessage', () => {
  it('returns the message of an Error', () => {
    expect(errorMessage(new Error('boom'))).toBe('boom');
    expect(errorMessage(new ApiError('network', 'offline'))).toBe('offline');
  });

  it('returns a non-empty string for anything else', () => {
    expect(errorMessage('plain string')).toBe('plain string');
    expect(errorMessage(null)).toBe('Unexpected error');
    expect(errorMessage(undefined)).toBe('Unexpected error');
    expect(errorMessage(42)).toBe('Unexpected error');
    expect(errorMessage({})).toBe('Unexpected error');
    expect(errorMessage(new Error(''))).toBe('Unexpected error');
    expect(errorMessage('   ')).toBe('Unexpected error');
  });

  it('never renders undefined or NaN', () => {
    const rendered = [errorMessage(undefined), errorMessage(Number.NaN), errorMessage(null)].join(
      ' ',
    );
    expect(rendered).not.toContain('undefined');
    expect(rendered).not.toContain('NaN');
  });
});

describe('requestJson', () => {
  it('returns the decoded body of any route', async () => {
    const { impl, calls } = recordingFetch(jsonResponse({ ok: true }));

    await expect(requestJson<{ ok: boolean }>('/api/custom', { fetchImpl: impl })).resolves.toEqual(
      { ok: true },
    );
    expect(calls[0]?.url).toBe('/api/custom');
  });
});
