import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from '@/lib/api';
import type {
  CandlesPayload,
  EquityPayload,
  HealthPayload,
  KillSwitchPayload,
  MetricsPayload,
  OrdersPayload,
  PositionsPayload,
  ProfileSnapshot,
  TradesPayload,
} from '@/lib/types';

import ProfileDetailPage from './page';

vi.mock('next/navigation', () => ({
  notFound: vi.fn(() => {
    throw new Error('NEXT_NOT_FOUND');
  }),
}));

vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchProfile: vi.fn(),
    fetchHealth: vi.fn(),
    fetchKillSwitch: vi.fn(),
    fetchEquity: vi.fn(),
    fetchPositions: vi.fn(),
    fetchTrades: vi.fn(),
    fetchOrders: vi.fn(),
    fetchMetrics: vi.fn(),
    fetchCandles: vi.fn(),
  };
});

import { notFound } from 'next/navigation';

import * as api from '@/lib/api';

const PROFILE: ProfileSnapshot = {
  profile_id: 'btc-paper',
  symbol: 'BTC/USDT',
  timeframe: '1h',
  strategy: 'ema_cross',
  mode: 'paper',
  status: 'running',
  initial_balance: 10000,
  equity: 10250,
  cash: 10000,
  position_value: 250,
  total_return: 0.025,
  n_trades: 7,
  open_positions: 1,
  health: {
    profile_id: 'btc-paper',
    status: 'running',
    last_candle_at: '2024-01-01T05:00:00+00:00',
    lag_seconds: 45,
    last_error: null,
    reconnect_count: 0,
    counters: {
      candles_processed: 1000,
      orders_submitted: 12,
      orders_filled: 10,
      orders_rejected: 2,
      stream_reconnects: 0,
      risk_rejections: 0,
      errors: 0,
    },
  },
  started_at: '2024-01-01T00:00:00+00:00',
  updated_at: '2024-01-01T05:00:45+00:00',
};

const HEALTH: HealthPayload = {
  status: 'ok',
  version: '0.3.0',
  uptime_seconds: 3600,
  profiles_total: 2,
  profiles_running: 1,
  kill_switch: false,
  checked_at: '2024-01-01T05:01:00+00:00',
};

const KILL_SWITCH: KillSwitchPayload = { kill_switch: false, reason: '', changed_at: null };

const EQUITY: EquityPayload = {
  points: [
    { timestamp: '2024-01-01T00:00:00+00:00', equity: 10000, cash: 10000, position_value: 0 },
    { timestamp: '2024-01-01T01:00:00+00:00', equity: 10250, cash: 10000, position_value: 250 },
  ],
};

const POSITIONS: PositionsPayload = {
  positions: [
    {
      profile_id: 'btc-paper',
      symbol: 'BTC/USDT',
      quantity: 1.5,
      average_price: 20000,
      direction: 'long',
      opened_at: '2024-01-01T00:00:00+00:00',
      updated_at: '2024-01-01T01:00:00+00:00',
      realized_pnl: 12.5,
      unrealized_pnl: -25.75,
      stop_price: 19000,
    },
  ],
};

const TRADES: TradesPayload = {
  trades: [
    {
      entry_time: '2024-01-01T00:00:00+00:00',
      exit_time: '2024-01-01T01:30:00+00:00',
      entry_price: 20000,
      exit_price: 20500,
      size: 0.5,
      direction: 'long',
      pnl: 123.456,
      pnl_pct: 0.0123,
      fees: 4.5,
      exit_reason: 'take_profit',
      duration_minutes: 90,
      stop_price: 19500,
      take_profit_price: 21000,
      params_id: 'default',
    },
  ],
  count: 1,
};

const ORDERS: OrdersPayload = {
  orders: [
    {
      client_order_id: 'ord-1',
      profile_id: 'btc-paper',
      symbol: 'BTC/USDT',
      side: 'buy',
      type: 'limit',
      quantity: 0.25,
      state: 'filled',
      mode: 'paper',
      created_at: '2024-01-01T00:00:00+00:00',
      updated_at: '2024-01-01T00:00:05+00:00',
      filled_quantity: 0.25,
      price: 20000,
      average_fill_price: 19999.5,
      broker_order_id: 'brk-1',
      reject_reason: '',
    },
  ],
};

const METRICS: MetricsPayload = {
  metrics: { total_return: 0.025, sharpe_ratio: 1.25, win_rate: null },
  benchmark: null,
  generated_at: '2024-01-01T05:01:00+00:00',
};

const CANDLES: CandlesPayload = {
  candles: [
    {
      profile_id: 'btc-paper',
      timestamp: '2024-01-01T00:00:00+00:00',
      open: 20000,
      high: 20100,
      low: 19900,
      close: 20050,
      volume: 12.5,
      closed: true,
    },
    {
      profile_id: 'btc-paper',
      timestamp: '2024-01-01T01:00:00+00:00',
      open: 20050,
      high: 20200,
      low: 20000,
      close: 20150,
      volume: 9.75,
      closed: true,
    },
  ],
  count: 2,
};

/** Resolve every mocked read with a valid payload. */
function mockHappyPath(): void {
  vi.mocked(api.fetchProfile).mockResolvedValue(PROFILE);
  vi.mocked(api.fetchHealth).mockResolvedValue(HEALTH);
  vi.mocked(api.fetchKillSwitch).mockResolvedValue(KILL_SWITCH);
  vi.mocked(api.fetchEquity).mockResolvedValue(EQUITY);
  vi.mocked(api.fetchPositions).mockResolvedValue(POSITIONS);
  vi.mocked(api.fetchTrades).mockResolvedValue(TRADES);
  vi.mocked(api.fetchOrders).mockResolvedValue(ORDERS);
  vi.mocked(api.fetchMetrics).mockResolvedValue(METRICS);
  vi.mocked(api.fetchCandles).mockResolvedValue(CANDLES);
}

/** Render the Server Component exactly as Next renders it. */
async function renderPage(id = 'btc-paper'): Promise<void> {
  const element = await ProfileDetailPage({ params: Promise.resolve({ id }) });
  render(element);
}

beforeEach(() => {
  mockHappyPath();
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('ProfileDetailPage', () => {
  it('fetches every payload server-side against the monitoring API origin', async () => {
    await renderPage();

    expect(api.fetchProfile).toHaveBeenCalledWith('btc-paper', { baseUrl: 'http://127.0.0.1:8080' });
    expect(api.fetchEquity).toHaveBeenCalledWith('btc-paper', { baseUrl: 'http://127.0.0.1:8080' });
    // The candle window belongs to the first paint: without it the chart would
    // show its empty state until the ten-second detail loop fired.
    expect(api.fetchCandles).toHaveBeenCalledWith('btc-paper', expect.any(Number), {
      baseUrl: 'http://127.0.0.1:8080',
    });
    expect(screen.queryByText(/no candle yet/i)).not.toBeInTheDocument();
    expect(api.fetchHealth).toHaveBeenCalledWith({ baseUrl: 'http://127.0.0.1:8080' });
    expect(api.fetchKillSwitch).toHaveBeenCalledWith({ baseUrl: 'http://127.0.0.1:8080' });
  });

  it('renders the identity, the equity curve, the tables and the metrics panel', async () => {
    await renderPage();

    expect(screen.getByRole('heading', { level: 1, name: 'btc-paper' })).toBeInTheDocument();
    expect(screen.getByText('BTC/USDT · 1h · ema_cross')).toBeInTheDocument();
    expect(screen.getByText('Paper trading')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Equity curve of btc-paper' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Open positions' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Closed trades' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Recent orders' })).toBeInTheDocument();
    expect(screen.getByText('Sharpe ratio')).toBeInTheDocument();
    expect(screen.getByText('No benchmark configured')).toBeInTheDocument();
  });

  it('renders the kill-switch panel with the server-rendered state', async () => {
    await renderPage();

    expect(screen.getByRole('heading', { name: 'Kill switch' })).toBeInTheDocument();
    expect(screen.getAllByText('Released').length).toBeGreaterThan(0);
    expect(screen.getByRole('button', { name: /engage kill switch/i })).toBeInTheDocument();
  });

  it('maps the documented 404 to notFound()', async () => {
    vi.mocked(api.fetchProfile).mockRejectedValue(
      new ApiError('http', 'unknown profile', { status: 404, path: '/api/profiles/nope' }),
    );

    await expect(
      ProfileDetailPage({ params: Promise.resolve({ id: 'nope' }) }),
    ).rejects.toThrow('NEXT_NOT_FOUND');

    expect(vi.mocked(notFound)).toHaveBeenCalledTimes(1);
    // A missing profile is not an outage: no other payload is requested.
    expect(api.fetchEquity).not.toHaveBeenCalled();
    expect(api.fetchHealth).not.toHaveBeenCalled();
  });

  it('renders the banner and starts no polling when the API is unreachable', async () => {
    vi.mocked(api.fetchProfile).mockRejectedValue(
      new ApiError('network', 'network error calling /api/profiles/btc-paper'),
    );

    await renderPage();

    expect(screen.getByText('The monitoring API is unreachable')).toBeInTheDocument();
    // The raw transport message is not hidden: it is the detail line.
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      'network error calling /api/profiles/btc-paper',
    );
    expect(screen.getByText('Profile unavailable')).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryByRole('img')).toBeNull();
    expect(vi.mocked(notFound)).not.toHaveBeenCalled();
    expect(api.fetchEquity).not.toHaveBeenCalled();
    expect(api.fetchHealth).not.toHaveBeenCalled();
  });

  it('renders the banner instead of crashing when a detail payload fails', async () => {
    vi.mocked(api.fetchEquity).mockRejectedValue(
      new ApiError('malformed', 'unexpected response from /api/profiles/btc-paper/equity', {
        path: '/api/profiles/btc-paper/equity',
      }),
    );

    await renderPage();

    expect(
      screen.getByText('The monitoring API returned an unexpected response'),
    ).toBeInTheDocument();
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      'unexpected response from /api/profiles/btc-paper/equity',
    );
    expect(screen.getByText('Profile unavailable')).toBeInTheDocument();
  });

  it('keeps a non-404 HTTP failure out of notFound()', async () => {
    vi.mocked(api.fetchProfile).mockRejectedValue(
      new ApiError('http', 'HTTP 500', { status: 500, path: '/api/profiles/btc-paper' }),
    );

    await renderPage();

    expect(vi.mocked(notFound)).not.toHaveBeenCalled();
    // The status is a server-side error, so it reads as an answered error...
    expect(screen.getByText('The monitoring API answered an error')).toBeInTheDocument();
    // ...and the raw status plus the requested path survive as the detail.
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      'HTTP 500 · /api/profiles/btc-paper',
    );
    expect(screen.getByText('Profile unavailable')).toBeInTheDocument();
  });

  it('reads a proxy 502 as an outage and never as a missing profile', async () => {
    vi.mocked(api.fetchProfile).mockRejectedValue(
      new ApiError('http', 'HTTP 502', { status: 502, path: '/api/profiles/btc-paper' }),
    );

    await renderPage();

    // Only the documented 404 maps to notFound(): a gateway failure is an outage.
    expect(vi.mocked(notFound)).not.toHaveBeenCalled();
    expect(screen.getByText('The monitoring API is unreachable')).toBeInTheDocument();
    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 502');
    expect(detail).toHaveTextContent('/api/profiles/btc-paper');
    expect(screen.queryByText('HTTP 502')).not.toBeInTheDocument();
    expect(screen.getByText('Profile unavailable')).toBeInTheDocument();
    expect(api.fetchEquity).not.toHaveBeenCalled();
  });
});
