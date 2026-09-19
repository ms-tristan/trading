import { describe, expect, it, vi } from 'vitest';

import {
  ApiError,
  OPERATOR_TOKEN_HEADER,
  apiUrl,
  createProfile,
  deleteProfile,
  errorMessage,
  fetchCandles,
  fetchCatalog,
  fetchControl,
  fetchEquity,
  fetchHealth,
  fetchKillSwitch,
  fetchMetrics,
  fetchOrders,
  fetchPositions,
  fetchProfile,
  fetchProfiles,
  fetchTrades,
  isWalletSnapshot,
  pauseProfile,
  postKillSwitch,
  requestJson,
  resumeProfile,
  type MutatingRequestOptions,
  type RequestOptions,
} from './api';
import type {
  CandlesPayload,
  CatalogPayload,
  ControlPayload,
  CreateProfileBody,
  DeletePayload,
  EquityPayload,
  HealthPayload,
  KillSwitchPayload,
  LifecyclePayload,
  MetricsPayload,
  OrdersPayload,
  PositionsPayload,
  ProfilesPayload,
  ProfileSnapshot,
  TradesPayload,
  WalletSnapshot,
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
  wallet: null,
};

/**
 * The one shared USDT wallet of the platform, as `GET /api/profiles` and
 * `GET /api/health` emit it (`source` is `'local'` for the paper ledger and
 * `'venue'` for the exchange account of live mode).
 */
const wallet: WalletSnapshot = {
  name: 'usdt',
  mode: 'paper',
  initial_balance: 25000,
  cash: 20964.75,
  equity: 25380.5,
  deployed: 4450.5,
  realized_pnl: 415.25,
  unrealized_pnl: -34.75,
  total_exposure: 4450.5,
  profiles: 2,
  source: 'local',
  updated_at: '2024-01-01T00:00:00+00:00',
};

const profiles: ProfilesPayload = {
  profiles: [profile],
  generated_at: '2024-01-01T00:00:00+00:00',
  wallet,
};

/** The keys a server that predates the attributed breakdown does not emit. */
const ATTRIBUTED_KEYS = [
  'allocation',
  'deployed',
  'realized_pnl',
  'unrealized_pnl',
  'last_block_reason',
] as const;

/** A profile of an older server: every attributed key removed. */
function legacyProfile(entry: ProfileSnapshot): Record<string, unknown> {
  const copy: Record<string, unknown> = { ...entry };
  for (const key of ATTRIBUTED_KEYS) {
    delete copy[key];
  }
  return copy;
}

/** A payload of an older server: the shared-wallet key removed. */
function withoutWalletKey<T extends { wallet?: unknown }>(payload: T): Record<string, unknown> {
  const copy: Record<string, unknown> = { ...payload };
  delete copy.wallet;
  return copy;
}

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
      postKillSwitch({ engage: true, reason: 'test' }, { fetchImpl: impl, operatorToken: 'unit-test-secret' }),
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

// ---------------------------------------------------------------------------
// the shared platform wallet (additive part of the contract)
// ---------------------------------------------------------------------------

describe('the shared platform wallet', () => {
  it('accepts a wallet snapshot and refuses a half-known one', () => {
    expect(isWalletSnapshot(wallet)).toBe(true);

    // A snapshot missing a documented key, or carrying an undocumented source
    // or mode, is not a wallet: it is refused instead of half-rendered.
    expect(isWalletSnapshot({ ...wallet, cash: undefined })).toBe(false);
    expect(isWalletSnapshot({ ...wallet, source: 'exchange' })).toBe(false);
    expect(isWalletSnapshot({ ...wallet, mode: 'backtest' })).toBe(false);
    expect(isWalletSnapshot({ ...wallet, profiles: '2' })).toBe(false);
    expect(isWalletSnapshot({ ...wallet, updated_at: 0 })).toBe(false);
    expect(isWalletSnapshot(null)).toBe(false);
    expect(isWalletSnapshot([])).toBe(false);
    expect(isWalletSnapshot('usdt')).toBe(false);
  });

  it('returns the shared wallet of the profiles payload verbatim', async () => {
    const { impl } = recordingFetch(jsonResponse(profiles));

    const result = await fetchProfiles({ fetchImpl: impl });

    expect(result.wallet).toEqual(wallet);
    expect(result).toEqual(profiles);
  });

  it('defaults the wallet to null for a payload of an older server', async () => {
    const { impl } = recordingFetch(jsonResponse(withoutWalletKey(profiles)));

    const result = await fetchProfiles({ fetchImpl: impl });

    // Absent is not `undefined` for a consumer: the read route makes the
    // documented "no wallet" state explicit.
    expect(result.wallet).toBeNull();
    expect(result.generated_at).toBe(profiles.generated_at);
    expect(result.profiles).toHaveLength(1);

    const healthImpl = recordingFetch(jsonResponse(withoutWalletKey(health))).impl;
    const healthResult = await fetchHealth({ fetchImpl: healthImpl });

    expect(healthResult.wallet).toBeNull();
    expect(healthResult.version).toBe(health.version);
  });

  it('accepts an explicit null wallet', async () => {
    const { impl } = recordingFetch(jsonResponse({ ...profiles, wallet: null }));

    await expect(fetchProfiles({ fetchImpl: impl })).resolves.toMatchObject({ wallet: null });
  });

  it('still accepts a profile that predates the attributed breakdown', async () => {
    const legacy = {
      profiles: [legacyProfile(profile)],
      generated_at: '2024-01-01T00:00:00+00:00',
    };
    const { impl } = recordingFetch(jsonResponse(legacy));

    const result = await fetchProfiles({ fetchImpl: impl });

    expect(result.profiles).toHaveLength(1);
    expect(result.profiles[0]?.profile_id).toBe('alpha');
    expect(result.profiles[0]?.equity).toBe(profile.equity);
    expect(result.profiles[0]?.allocation).toBeUndefined();
    expect(result.profiles[0]?.last_block_reason).toBeUndefined();
    expect(result.wallet).toBeNull();
  });

  it('refuses an invalid attributed value instead of rendering it', async () => {
    const broken = {
      profiles: [{ ...profile, allocation: 'ten thousand' }],
      generated_at: null,
    };
    const { impl } = recordingFetch(jsonResponse(broken));

    await expect(fetchProfiles({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
      path: '/api/profiles',
    });
  });

  it('refuses a wallet value that is not a wallet snapshot', async () => {
    const { impl } = recordingFetch(jsonResponse({ ...profiles, wallet: { name: 'usdt' } }));

    await expect(fetchProfiles({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
      path: '/api/profiles',
    });

    const healthImpl = recordingFetch(
      jsonResponse({ ...health, wallet: { ...wallet, cash: 'lots' } }),
    ).impl;

    await expect(fetchHealth({ fetchImpl: healthImpl })).rejects.toMatchObject({
      kind: 'malformed',
      path: '/api/health',
    });
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
      postKillSwitch({ engage: true, reason: 'test' }, { fetchImpl: impl, operatorToken: 'unit-test-secret' }),
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

// ---------------------------------------------------------------------------
// candles, catalog and profile lifecycle (additive part of the contract)
// ---------------------------------------------------------------------------

const candle = {
  profile_id: 'alpha',
  timestamp: '2024-01-01T00:00:00+00:00',
  open: 42000,
  high: 42500,
  low: 41800,
  close: 42400,
  volume: 12.5,
  closed: true,
};

const candles: CandlesPayload = { candles: [candle], count: 1 };

const catalog: CatalogPayload = {
  symbols: [
    { symbol: 'BTC/USDT', base: 'BTC', quote: 'USDT' },
    { symbol: 'ETH/USDT', base: 'ETH', quote: 'USDT' },
  ],
  strategies: ['basic'],
  timeframes: ['1m', '5m', '1h'],
  modes: ['paper', 'live'],
};

const control: ControlPayload = {
  engine_running: true,
  read_only: false,
  mutable: true,
  profiles: [{ profile_id: 'alpha', paused: false, running: true }],
};

const lifecycle: LifecyclePayload = { profile, paused: true };

const deleted: DeletePayload = { profile_id: 'alpha', deleted: true };

const createBody: CreateProfileBody = {
  profile_id: 'beta',
  symbol: 'BTC/USDT',
  timeframe: '1h',
  strategy: 'basic',
  mode: 'paper',
};

const createdBody = JSON.stringify({
  profile_id: 'beta',
  symbol: 'BTC/USDT',
  timeframe: '1h',
  strategy: 'basic',
  mode: 'paper',
});

describe('fetchCandles', () => {
  it('requests the bounded candle window and returns it', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(candles));

    await expect(fetchCandles('alpha', 500, { fetchImpl: impl })).resolves.toEqual(candles);

    expect(calls[0]?.url).toBe('/api/profiles/alpha/candles?limit=500');
    expect(calls[0]?.init?.method).toBe('GET');
    expect(calls[0]?.init?.cache).toBe('no-store');
  });

  it('percent-encodes the profile id and keeps the limit in the query', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(candles));

    await fetchCandles('a b/c', 250, { fetchImpl: impl });

    expect(calls[0]?.url).toBe('/api/profiles/a%20b%2Fc/candles?limit=250');
  });

  it('reports the failing path without the query string', async () => {
    const { impl } = recordingFetch(jsonResponse({ error: "unknown profile: 'ghost'" }, 404));

    await expect(fetchCandles('ghost', 500, { fetchImpl: impl })).rejects.toMatchObject({
      kind: 'http',
      status: 404,
      path: '/api/profiles/ghost/candles',
      message: "unknown profile: 'ghost'",
    });
  });

  it('rejects a payload whose count is not a number', async () => {
    const { impl } = recordingFetch(jsonResponse({ candles: [candle], count: '1' }));

    await expect(fetchCandles('alpha', 500, { fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
      path: '/api/profiles/alpha/candles',
    });
  });

  it('rejects a candle with a renamed field', async () => {
    const { impl } = recordingFetch(
      jsonResponse({ candles: [{ ...candle, close: undefined, last: 42400 }], count: 1 }),
    );

    await expect(fetchCandles('alpha', 500, { fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
    });
  });

  it('rejects a candle missing the closed flag', async () => {
    const { impl } = recordingFetch(jsonResponse({ candles: [{ ...candle, closed: undefined }] }));

    await expect(fetchCandles('alpha', 500, { fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
    });
  });

  it('accepts a candle whose prices were never recorded (null)', async () => {
    const empty = {
      profile_id: 'alpha',
      timestamp: '2024-01-01T00:00:00+00:00',
      open: null,
      high: null,
      low: null,
      close: null,
      volume: null,
      closed: false,
    };
    const { impl } = recordingFetch(jsonResponse({ candles: [empty], count: 1 }));

    await expect(fetchCandles('alpha', 10, { fetchImpl: impl })).resolves.toEqual({
      candles: [empty],
      count: 1,
    });
  });

  it('normalises an unreachable server', async () => {
    await expect(
      fetchCandles('alpha', 500, { fetchImpl: failingFetch(new TypeError('fetch failed')) }),
    ).rejects.toMatchObject({
      kind: 'network',
      status: null,
      message: expect.stringContaining('network error calling /api/profiles/alpha/candles'),
    });
  });
});

describe('fetchCatalog', () => {
  it('requests the catalog and returns every picker list', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(catalog));

    await expect(fetchCatalog({ fetchImpl: impl })).resolves.toEqual(catalog);

    expect(calls[0]?.url).toBe('/api/catalog');
    expect(calls[0]?.init?.method).toBe('GET');
    expect(calls[0]?.init?.cache).toBe('no-store');
  });

  it('rejects a symbol without its quote currency', async () => {
    const { impl } = recordingFetch(
      jsonResponse({ ...catalog, symbols: [{ symbol: 'BTC/USDT', base: 'BTC' }] }),
    );

    await expect(fetchCatalog({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
      path: '/api/catalog',
    });
  });

  it('rejects an unknown run mode', async () => {
    const { impl } = recordingFetch(jsonResponse({ ...catalog, modes: ['paper', 'sandbox'] }));

    await expect(fetchCatalog({ fetchImpl: impl })).rejects.toMatchObject({ kind: 'malformed' });
  });

  it('rejects a strategies list that is not a list of strings', async () => {
    const { impl } = recordingFetch(jsonResponse({ ...catalog, strategies: [42] }));

    await expect(fetchCatalog({ fetchImpl: impl })).rejects.toMatchObject({ kind: 'malformed' });
  });
});

describe('fetchControl', () => {
  it('requests the control state and returns it', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(control));

    await expect(fetchControl({ fetchImpl: impl })).resolves.toEqual(control);

    expect(calls[0]?.url).toBe('/api/control');
    expect(calls[0]?.init?.method).toBe('GET');
    expect(calls[0]?.init?.cache).toBe('no-store');
  });

  it('rejects a payload missing the mutability flag', async () => {
    const { impl } = recordingFetch(jsonResponse({ ...control, mutable: undefined }));

    await expect(fetchControl({ fetchImpl: impl })).rejects.toMatchObject({
      kind: 'malformed',
      path: '/api/control',
    });
  });

  it('rejects a profile entry that is not an object', async () => {
    const { impl } = recordingFetch(jsonResponse({ ...control, profiles: ['alpha'] }));

    await expect(fetchControl({ fetchImpl: impl })).rejects.toMatchObject({ kind: 'malformed' });
  });

  it('normalises an unreachable server', async () => {
    await expect(
      fetchControl({ fetchImpl: failingFetch(new TypeError('fetch failed')) }),
    ).rejects.toMatchObject({
      kind: 'network',
      message: expect.stringContaining('network error calling /api/control'),
    });
  });
});

describe('profile lifecycle routes', () => {
  interface MutationCase {
    name: string;
    call: (options: MutatingRequestOptions) => Promise<unknown>;
    /** The same call on a profile id that must be percent-encoded. */
    callEncoded: (options: MutatingRequestOptions) => Promise<unknown>;
    url: string;
    method: string;
    body: string | undefined;
    payload: unknown;
    /** A 2xx body that must be refused as `'malformed'`. */
    malformed: unknown;
    /** The response of {@link callEncoded}. */
    encodedPayload: unknown;
  }

  const mutationCases: MutationCase[] = [
    {
      name: 'pauseProfile',
      call: (options) => pauseProfile('alpha', options),
      callEncoded: (options) => pauseProfile('a b/c', options),
      url: '/api/profiles/alpha/pause',
      method: 'POST',
      body: '{}',
      payload: lifecycle,
      malformed: { profile: 'nope', paused: true },
      encodedPayload: lifecycle,
    },
    {
      name: 'resumeProfile',
      call: (options) => resumeProfile('alpha', options),
      callEncoded: (options) => resumeProfile('a b/c', options),
      url: '/api/profiles/alpha/resume',
      method: 'POST',
      body: '{}',
      payload: { profile, paused: false },
      malformed: { profile, paused: 'yes' },
      encodedPayload: lifecycle,
    },
    {
      name: 'deleteProfile',
      call: (options) => deleteProfile('alpha', options),
      callEncoded: (options) => deleteProfile('a b/c', options),
      url: '/api/profiles/alpha',
      method: 'DELETE',
      body: undefined,
      payload: deleted,
      malformed: { profile_id: 'alpha', deleted: false },
      encodedPayload: { profile_id: 'a b/c', deleted: true },
    },
    {
      name: 'createProfile',
      call: (options) => createProfile(createBody, options),
      callEncoded: (options) => createProfile({ ...createBody, profile_id: 'a b/c' }, options),
      url: '/api/profiles',
      method: 'POST',
      body: createdBody,
      payload: { profile },
      malformed: { profile: { profile_id: 'beta' } },
      encodedPayload: lifecycle,
    },
  ];

  it.each(mutationCases)(
    '$name sends the token and the JSON headers, and returns the new state',
    async ({ call, url, method, body, payload }) => {
      const { impl, calls } = recordingFetch(jsonResponse(payload));

      await expect(call({ fetchImpl: impl, operatorToken: 'secret-token' })).resolves.toEqual(payload);

      expect(calls[0]?.url).toBe(url);
      expect(calls[0]?.init?.method).toBe(method);
      expect(calls[0]?.init?.cache).toBe('no-store');
      expect(calls[0]?.init?.body).toBe(body);

      const headers = calls[0]?.init?.headers as Record<string, string>;
      expect(headers['Content-Type']).toBe('application/json');
      expect(headers[OPERATOR_TOKEN_HEADER]).toBe('secret-token');
    },
  );

  it.each(mutationCases)(
    '$name carries a profile id that needs escaping',
    async ({ callEncoded, encodedPayload, url }) => {
      const { impl, calls } = recordingFetch(jsonResponse(encodedPayload));

      await callEncoded({ fetchImpl: impl, operatorToken: 'unit-test-secret' });

      if (url === '/api/profiles') {
        // A creation carries the identifier in its body, never in the path.
        expect(calls[0]?.url).toBe('/api/profiles');
        expect(calls[0]?.init?.body).toContain('"profile_id":"a b/c"');
        return;
      }
      expect(calls[0]?.url).toContain('a%20b%2Fc');
    },
  );

  it.each(mutationCases)('$name surfaces the 403 refusal verbatim', async ({ call }) => {
    const { impl } = recordingFetch(
      jsonResponse({ error: 'missing or invalid operator token' }, 403),
    );

    await expect(call({ fetchImpl: impl, operatorToken: 'unit-test-secret' })).rejects.toMatchObject({
      kind: 'http',
      status: 403,
      message: 'missing or invalid operator token',
    });
  });

  it.each(mutationCases)('$name surfaces a 409 conflict verbatim', async ({ call }) => {
    const { impl } = recordingFetch(
      jsonResponse({ error: "profile already exists: 'alpha'" }, 409),
    );

    await expect(call({ fetchImpl: impl, operatorToken: 'unit-test-secret' })).rejects.toMatchObject({
      kind: 'http',
      status: 409,
      message: "profile already exists: 'alpha'",
    });
  });

  it.each(mutationCases)(
    '$name rejects a 2xx payload that does not match the expected shape',
    async ({ call, malformed }) => {
      const { impl } = recordingFetch(jsonResponse(malformed));

      await expect(call({ fetchImpl: impl, operatorToken: 'unit-test-secret' })).rejects.toMatchObject({
        kind: 'malformed',
      });
    },
  );

  it('rejects a delete acknowledgement that does not confirm the deletion', async () => {
    const { impl } = recordingFetch(jsonResponse({ profile_id: 'alpha', deleted: false }));

    await expect(deleteProfile('alpha', { fetchImpl: impl, operatorToken: 'unit-test-secret' })).rejects.toMatchObject({
      kind: 'malformed',
    });
  });

  it.each(mutationCases)('$name never leaks the operator token', async ({ call }) => {
    const token = 'super-secret-token';
    const failing = failingFetch(new Error(`connection refused for token ${token}`));

    const failure = await call({ fetchImpl: failing, operatorToken: token }).catch(
      (error: unknown) => error,
    );

    const message = errorMessage(failure);
    expect(message).not.toContain(token);
    expect(message).toContain('***');
  });

  it.each(mutationCases)('$name never leaks the token from an HTTP error body', async ({ call }) => {
    const token = 'super-secret-token';
    const { impl } = recordingFetch(jsonResponse({ error: `invalid token ${token}` }, 403));

    const failure = await call({ fetchImpl: impl, operatorToken: token }).catch(
      (error: unknown) => error,
    );

    expect(errorMessage(failure)).not.toContain(token);
  });

  it.each(mutationCases)('$name falls back to the status line for a non-JSON body', async ({ call }) => {
    const { impl } = recordingFetch(jsonResponse('<html>boom</html>', 500));

    await expect(call({ fetchImpl: impl, operatorToken: 'unit-test-secret' })).rejects.toMatchObject({
      kind: 'http',
      status: 500,
      message: 'HTTP 500',
    });
  });

  it('omits the optional fields of a creation the caller left out', async () => {
    const { impl, calls } = recordingFetch(jsonResponse({ profile }));

    await createProfile(createBody, { fetchImpl: impl, operatorToken: 'unit-test-secret' });

    expect(calls[0]?.init?.body).toBe(createdBody);
  });

  it('sends the optional balance and strategy parameters when provided', async () => {
    const { impl, calls } = recordingFetch(jsonResponse({ profile }));

    await createProfile(
      {
        ...createBody,
        initial_balance: 25000,
        params: { fast_period: 10, use_stops: true, label: 'scalp' },
      },
      { fetchImpl: impl, operatorToken: 'unit-test-secret' },
    );

    expect(calls[0]?.init?.body).toBe(
      JSON.stringify({
        profile_id: 'beta',
        symbol: 'BTC/USDT',
        timeframe: '1h',
        strategy: 'basic',
        mode: 'paper',
        initial_balance: 25000,
        params: { fast_period: 10, use_stops: true, label: 'scalp' },
      }),
    );
  });

  it('sends no body on delete', async () => {
    const { impl, calls } = recordingFetch(jsonResponse(deleted));

    await deleteProfile('alpha', { fetchImpl: impl, operatorToken: 'unit-test-secret' });

    expect(calls[0]?.init?.method).toBe('DELETE');
    expect(calls[0]?.init?.body).toBeUndefined();
  });
});
