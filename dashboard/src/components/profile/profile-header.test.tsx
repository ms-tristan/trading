import { act, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type { ControlPayload, HealthPayload, KillSwitchPayload, ProfileSnapshot } from '@/lib/types';

import { ProfileHeader, type ProfileHeaderProps } from './profile-header';

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
  total_return: 0.1234,
  n_trades: 7,
  open_positions: 1,
  health: {
    profile_id: 'btc-paper',
    status: 'running',
    last_candle_at: '2024-01-01T05:00:00+00:00',
    lag_seconds: 45,
    last_error: null,
    reconnect_count: 5,
    counters: {
      candles_processed: 1000,
      orders_submitted: 21,
      orders_filled: 18,
      orders_rejected: 2,
      stream_reconnects: 3,
      risk_rejections: 4,
      errors: 0,
    },
  },
  started_at: '2024-01-01T00:00:00+00:00',
  updated_at: '2024-01-01T05:00:45+00:00',
};

const HEALTH: HealthPayload = {
  status: 'ok',
  version: '0.3.0',
  uptime_seconds: 90061,
  profiles_total: 9,
  profiles_running: 8,
  kill_switch: false,
  checked_at: '2024-01-01T05:01:00+00:00',
};

const KILL_SWITCH: KillSwitchPayload = {
  kill_switch: false,
  reason: '',
  changed_at: null,
};

const HEADER_PROPS: ProfileHeaderProps = {
  profile: PROFILE,
  health: HEALTH,
  killSwitch: KILL_SWITCH,
};

const OPERATOR_TOKEN = 'header-operator-token';

/** `GET /api/control` answer of the header tests: only this profile, running. */
function controlPayload(paused = false): ControlPayload {
  return {
    engine_running: true,
    read_only: false,
    mutable: true,
    profiles: [{ profile_id: PROFILE.profile_id, paused, running: true }],
  };
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response;
}

let fetchMock: ReturnType<typeof vi.fn>;

/** Let the first control poll of the actions island settle. */
async function settle(): Promise<void> {
  await act(async () => {
    for (let tick = 0; tick < 8; tick += 1) {
      await Promise.resolve();
    }
  });
}

/** Render the header and wait for the control poll of its actions island. */
async function renderHeader(overrides: Partial<ProfileHeaderProps> = {}) {
  const view = render(<ProfileHeader {...HEADER_PROPS} {...overrides} />);
  await settle();
  return view;
}

beforeEach(() => {
  window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, OPERATOR_TOKEN);
  fetchMock = vi.fn(async (url: unknown): Promise<Response> => {
    if (String(url) === '/api/control') {
      return jsonResponse(controlPayload());
    }
    return jsonResponse({ error: 'not found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  window.sessionStorage.clear();
});

describe('ProfileHeader', () => {
  it('renders the identity of the profile with a status badge and a mode badge', async () => {
    await renderHeader();

    expect(screen.getByRole('heading', { level: 1, name: 'btc-paper' })).toBeInTheDocument();
    expect(screen.getByText('BTC/USDT · 1h · ema_cross')).toBeInTheDocument();
    // Identity fields also appear in the lifecycle list.
    expect(screen.getAllByText('BTC/USDT')).toHaveLength(1);
    expect(screen.getAllByText('1h')).toHaveLength(1);
    expect(screen.getAllByText('ema_cross')).toHaveLength(1);
    expect(screen.getAllByText('Running')).toHaveLength(2);
    expect(screen.getByText('Paper trading')).toBeInTheDocument();
    expect(screen.getByText('Platform healthy')).toBeInTheDocument();
    expect(screen.getByText('Kill switch released')).toBeInTheDocument();
  });

  it('renders every platform and snapshot field of the profile', async () => {
    await renderHeader();

    expect(screen.getByText('$10,250.00')).toBeInTheDocument();
    expect(screen.getByText('$250.00')).toBeInTheDocument();
    // Cash and initial balance carry the same amount.
    expect(screen.getAllByText('$10,000.00')).toHaveLength(2);
    expect(screen.getByText('+12.34%')).toBeInTheDocument();
    expect(screen.getByText('Up')).toBeInTheDocument();
    expect(screen.getByText('Over 7 trades')).toBeInTheDocument();
    expect(screen.getByText('0.3.0')).toBeInTheDocument();
    expect(screen.getByText('1d 01:01:01')).toBeInTheDocument();
    expect(screen.getByText('8 / 9')).toBeInTheDocument();
    expect(screen.getByText('2024-01-01 05:01:00 UTC')).toBeInTheDocument();
    // The lag appears in the tile and in the lifecycle list.
    expect(screen.getAllByText('45s')).toHaveLength(2);
  });

  it('renders the engine counters for the profile', async () => {
    await renderHeader();

    const counters = screen.getByRole('region', { name: 'Engine counters' });
    expect(within(counters).getByText('Candles processed')).toBeInTheDocument();
    expect(within(counters).getByText('1,000')).toBeInTheDocument();
    expect(within(counters).getByText('Orders submitted')).toBeInTheDocument();
    expect(within(counters).getByText('21')).toBeInTheDocument();
    expect(within(counters).getByText('Orders filled')).toBeInTheDocument();
    expect(within(counters).getByText('18')).toBeInTheDocument();
    expect(within(counters).getByText('Orders rejected')).toBeInTheDocument();
    expect(within(counters).getByText('Stream reconnects')).toBeInTheDocument();
    expect(within(counters).getByText('Risk rejections')).toBeInTheDocument();
    expect(within(counters).getByText('Errors')).toBeInTheDocument();
    expect(within(counters).getByText('0')).toBeInTheDocument();
  });

  it('shows the last error as a labelled warning row when the API reports one', async () => {
    const failing: ProfileSnapshot = {
      ...PROFILE,
      status: 'degraded',
      health: { ...PROFILE.health, last_error: 'stream closed by peer' },
    };
    await renderHeader({ profile: failing });

    expect(screen.getByText('Last error')).toBeInTheDocument();
    expect(screen.getByText('stream closed by peer')).toBeInTheDocument();
    // The status badge and the lifecycle row both report the degraded status.
    expect(screen.getAllByText('Degraded')).toHaveLength(2);
  });

  it('renders an em dash for every absent value instead of NaN or undefined', async () => {
    const empty: ProfileSnapshot = {
      profile_id: 'eth-live',
      symbol: 'ETH/USDT',
      timeframe: '5m',
      strategy: 'breakout',
      mode: 'live',
      status: 'starting',
      initial_balance: null,
      equity: null,
      cash: null,
      position_value: null,
      total_return: null,
      n_trades: 0,
      open_positions: 0,
      health: {
        ...PROFILE.health,
        last_candle_at: null,
        lag_seconds: null,
        reconnect_count: 0,
      },
      started_at: null,
      updated_at: null,
    };
    const degradedHealth: HealthPayload = {
      ...HEALTH,
      status: 'degraded',
      uptime_seconds: null,
      checked_at: null,
    };
    await renderHeader({
      profile: empty,
      health: degradedHealth,
      killSwitch: { kill_switch: true, reason: '', changed_at: null },
    });

    expect(screen.getByText('Live trading')).toBeInTheDocument();
    expect(screen.getByText('Platform degraded')).toBeInTheDocument();
    expect(screen.getByText('Kill switch engaged')).toBeInTheDocument();
    expect(screen.queryByText('NaN')).toBeNull();
    expect(screen.queryByText('undefined')).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(8);
  });

  it('renders the actions island of the profile inside the header card', async () => {
    await renderHeader();

    const header = screen.getByRole('region', { name: `Profile ${PROFILE.profile_id}` });
    expect(within(header).getByRole('button', { name: 'Pause' })).toBeEnabled();
    expect(within(header).getByRole('button', { name: 'Resume' })).toBeDisabled();
    expect(within(header).getByRole('button', { name: 'Delete profile' })).toBeEnabled();
    expect(
      within(header).getByRole('button', { name: 'Pause' }).closest('[data-paused]'),
    ).toHaveAttribute('data-paused', 'false');
    expect(fetchMock).toHaveBeenCalledWith('/api/control', expect.anything());
  });

  it('shows the profile as paused when /api/control reports it paused', async () => {
    fetchMock.mockImplementation(async (url: unknown): Promise<Response> => {
      if (String(url) === '/api/control') {
        return jsonResponse(controlPayload(true));
      }
      return jsonResponse({ error: 'not found' }, 404);
    });

    await renderHeader();

    expect(screen.getByText('Paused').closest('span[data-tone]')).toHaveAttribute(
      'data-tone',
      'warn',
    );
    expect(
      screen.getByText('Not opening new positions; the open position stays managed.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Resume' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Pause' })).toBeDisabled();
  });
});
