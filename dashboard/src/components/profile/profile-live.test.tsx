import { act, fireEvent, render, screen, within } from '@testing-library/react';
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
  ProfileDetailBundle,
  ProfileLiveBundle,
  ProfileSnapshot,
  TradesPayload,
} from '@/lib/types';

import { ProfileLive } from './profile-live';

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
  benchmark: {
    variant: 'buy_and_hold',
    initial_balance: 10000,
    final_balance: 10100,
    n_periods: 24,
    timeframe: '1h',
    metrics: { total_return: 0.01 },
  },
  generated_at: '2024-01-01T05:01:00+00:00',
};

const CANDLES: CandlesPayload = {
  candles: [
    {
      profile_id: 'btc-paper',
      timestamp: '2024-01-01T00:30:00+00:00',
      open: 20000,
      high: 20500,
      low: 19800,
      close: 20400,
      volume: 12.5,
      closed: true,
    },
  ],
  count: 1,
};

const INITIAL_LIVE: ProfileLiveBundle = { profile: PROFILE, health: HEALTH, killSwitch: KILL_SWITCH };
const INITIAL_DETAIL: ProfileDetailBundle = {
  equity: EQUITY,
  positions: POSITIONS,
  trades: TRADES,
  orders: ORDERS,
  metrics: METRICS,
};

const INITIAL_CHECKED_AT = '2024-01-01T05:01:00+00:00';

function renderLive(
  overrides: Partial<{
    pollIntervalMs: number;
    detailIntervalMs: number;
    initialLive: ProfileLiveBundle;
    initialDetail: ProfileDetailBundle;
  }> = {},
): void {
  render(
    <ProfileLive
      profileId="btc-paper"
      initialLive={overrides.initialLive ?? INITIAL_LIVE}
      initialDetail={overrides.initialDetail ?? INITIAL_DETAIL}
      initialCheckedAt={INITIAL_CHECKED_AT}
      pollIntervalMs={overrides.pollIntervalMs ?? 2000}
      detailIntervalMs={overrides.detailIntervalMs ?? 10000}
    />,
  );
}

/** Flush a polling cycle: timers, microtasks and React updates. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2024-06-01T12:00:00.000Z'));
  vi.mocked(api.fetchProfile).mockResolvedValue(PROFILE);
  vi.mocked(api.fetchHealth).mockResolvedValue(HEALTH);
  vi.mocked(api.fetchKillSwitch).mockResolvedValue(KILL_SWITCH);
  vi.mocked(api.fetchEquity).mockResolvedValue(EQUITY);
  vi.mocked(api.fetchPositions).mockResolvedValue(POSITIONS);
  vi.mocked(api.fetchTrades).mockResolvedValue(TRADES);
  vi.mocked(api.fetchOrders).mockResolvedValue(ORDERS);
  vi.mocked(api.fetchMetrics).mockResolvedValue(METRICS);
  vi.mocked(api.fetchCandles).mockResolvedValue(CANDLES);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('ProfileLive', () => {
  it('renders the server bundles on the first paint', () => {
    renderLive();

    expect(screen.getByRole('heading', { level: 1, name: 'btc-paper' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Open positions' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Closed trades' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Recent orders' })).toBeInTheDocument();
    expect(screen.getByText('Sharpe ratio')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Kill switch' })).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Equity curve of btc-paper' })).toBeInTheDocument();
    expect(screen.getAllByText('2024-01-01 05:01:00 UTC').length).toBeGreaterThan(0);
    // A passive first paint: nothing was polled before the first interval.
    expect(api.fetchProfile).not.toHaveBeenCalled();
  });

  it('polls the live loop on the fast cadence and the detail loop on the slow one', async () => {
    vi.mocked(api.fetchProfile).mockResolvedValue({ ...PROFILE, equity: 10500 });
    renderLive({ pollIntervalMs: 2000, detailIntervalMs: 10000 });

    await advance(2000);

    expect(api.fetchProfile).toHaveBeenCalledTimes(1);
    expect(api.fetchHealth).toHaveBeenCalledTimes(1);
    expect(api.fetchKillSwitch).toHaveBeenCalledTimes(1);
    expect(api.fetchProfile).toHaveBeenCalledWith('btc-paper', expect.anything());
    // Same-origin in the browser: no absolute base URL is passed.
    expect(vi.mocked(api.fetchProfile).mock.calls[0]?.[1]).not.toHaveProperty('baseUrl');
    expect(api.fetchEquity).not.toHaveBeenCalled();
    expect(screen.getAllByText('$10,500.00').length).toBeGreaterThan(0);
    // The toolbar stamps the successful poll with the current time.
    expect(screen.getAllByText('2024-06-01 12:00:02 UTC').length).toBeGreaterThan(0);

    await advance(8000);

    expect(api.fetchEquity).toHaveBeenCalledTimes(1);
    expect(api.fetchPositions).toHaveBeenCalledTimes(1);
    expect(api.fetchTrades).toHaveBeenCalledTimes(1);
    expect(api.fetchOrders).toHaveBeenCalledTimes(1);
    expect(api.fetchMetrics).toHaveBeenCalledTimes(1);
  });

  it('drives the kill switch and the header from the live loop', async () => {
    vi.mocked(api.fetchKillSwitch).mockResolvedValue({
      kill_switch: true,
      reason: 'drawdown breach',
      changed_at: '2024-06-01T11:00:00+00:00',
    });
    renderLive();

    // Header row and kill-switch badge both report the released state.
    expect(screen.getAllByText('Released')).toHaveLength(2);

    await advance(2000);

    expect(screen.getAllByText('Engaged')).toHaveLength(2);
    // The reason reaches the header row and the kill-switch panel.
    expect(screen.getAllByText('drawdown breach')).toHaveLength(2);
    expect(screen.getByText('Kill switch engaged')).toBeInTheDocument();
  });

  it('pauses both loops with one control and resumes them together', async () => {
    renderLive();

    fireEvent.click(screen.getByRole('button', { name: 'Pause live updates' }));
    expect(screen.getByRole('button', { name: 'Resume live updates' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    expect(screen.getByText('Live updates paused')).toBeInTheDocument();

    await advance(30000);
    expect(api.fetchProfile).not.toHaveBeenCalled();
    expect(api.fetchEquity).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Resume live updates' }));
    await advance(1);

    expect(api.fetchProfile).toHaveBeenCalledTimes(1);
    expect(api.fetchEquity).toHaveBeenCalledTimes(1);
    expect(screen.getByText('Live updates')).toBeInTheDocument();
  });

  it('refreshes both loops on demand without waiting for the interval', async () => {
    renderLive();

    // The candles panel carries its own "Refresh now"; the toolbar's is first.
    fireEvent.click(screen.getAllByRole('button', { name: 'Refresh now' })[0] as HTMLElement);
    await advance(1);

    expect(api.fetchProfile).toHaveBeenCalledTimes(1);
    expect(api.fetchEquity).toHaveBeenCalledTimes(1);
    expect(api.fetchMetrics).toHaveBeenCalledTimes(1);
  });

  it('polls the candles of the profile on the detail cadence', async () => {
    renderLive({ pollIntervalMs: 2000, detailIntervalMs: 10000 });

    expect(api.fetchCandles).not.toHaveBeenCalled();
    expect(screen.getByText('Candles')).toBeInTheDocument();
    expect(screen.getByText('No candle yet')).toBeInTheDocument();

    await advance(10000);

    expect(api.fetchCandles).toHaveBeenCalledTimes(1);
    expect(api.fetchCandles).toHaveBeenCalledWith(
      'btc-paper',
      500,
      expect.objectContaining({ signal: expect.anything() }),
    );
    // The payload reaches the chart's text alternative, between the equity
    // curve and the positions table.
    expect(screen.getByText('Candle data')).toBeInTheDocument();
    expect(screen.getByText('2024-01-01 00:30:00 UTC')).toBeInTheDocument();
    expect(screen.queryByText('No candle yet')).toBeNull();
  });

  it('keeps the last known good data and shows one banner when both loops fail', async () => {
    vi.mocked(api.fetchProfile).mockRejectedValue(
      new ApiError('network', 'network error calling /api/profiles/btc-paper'),
    );
    vi.mocked(api.fetchEquity).mockRejectedValue(
      new ApiError('malformed', 'unexpected response from /api/profiles/btc-paper/equity'),
    );
    renderLive();

    await advance(2000);
    expect(
      screen.getByText(/Live updates: The monitoring API is unreachable/),
    ).toBeInTheDocument();

    await advance(8000);
    // ONE banner carries both loops, each with its own operator-facing headline.
    const headline = screen.getByText(
      /Detail data: The monitoring API returned an unexpected response/,
    );
    const banner = headline.closest('[role="status"]');
    expect(banner).not.toBeNull();
    expect(banner?.textContent).toContain('Live updates: The monitoring API is unreachable');

    // The raw cause of each loop survives as the shared detail line.
    const detail = within(banner as HTMLElement).getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('network error calling /api/profiles/btc-paper');
    expect(detail).toHaveTextContent('unexpected response from /api/profiles/btc-paper/equity');

    // Nothing was cleared: the last known good bundles are still on screen.
    expect(screen.getByRole('heading', { level: 1, name: 'btc-paper' })).toBeInTheDocument();
    // Header tile and equity data table both keep the last known good equity.
    expect(screen.getAllByText('$10,250.00').length).toBeGreaterThan(0);
    expect(screen.getByRole('table', { name: 'Open positions' })).toBeInTheDocument();
    expect(screen.getByRole('table', { name: 'Closed trades' })).toBeInTheDocument();
    expect(screen.getAllByText('Long').length).toBeGreaterThan(0);
  });

  it('clears the banner once a poll succeeds again', async () => {
    vi.mocked(api.fetchHealth).mockRejectedValueOnce(new ApiError('network', 'network error'));
    renderLive();

    await advance(2000);
    expect(
      screen.getByText(/Live updates: The monitoring API is unreachable/),
    ).toBeInTheDocument();

    await advance(2000);
    expect(screen.queryByText(/Live updates: The monitoring API is unreachable/)).toBeNull();
  });

  it('stops polling when it unmounts', async () => {
    const { unmount } = render(
      <ProfileLive
        profileId="btc-paper"
        initialLive={INITIAL_LIVE}
        initialDetail={INITIAL_DETAIL}
        initialCheckedAt={INITIAL_CHECKED_AT}
        pollIntervalMs={2000}
        detailIntervalMs={10000}
      />,
    );

    unmount();
    await advance(30000);

    expect(api.fetchProfile).not.toHaveBeenCalled();
    expect(api.fetchEquity).not.toHaveBeenCalled();
  });
});
