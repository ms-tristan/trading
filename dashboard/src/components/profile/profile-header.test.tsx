import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { HealthPayload, KillSwitchPayload, ProfileSnapshot } from '@/lib/types';

import { ProfileHeader } from './profile-header';

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

describe('ProfileHeader', () => {
  it('renders the identity of the profile with a status badge and a mode badge', () => {
    render(<ProfileHeader profile={PROFILE} health={HEALTH} killSwitch={KILL_SWITCH} />);

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

  it('renders every platform and snapshot field of the profile', () => {
    render(<ProfileHeader profile={PROFILE} health={HEALTH} killSwitch={KILL_SWITCH} />);

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

  it('renders the engine counters for the profile', () => {
    render(<ProfileHeader profile={PROFILE} health={HEALTH} killSwitch={KILL_SWITCH} />);

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

  it('shows the last error as a labelled warning row when the API reports one', () => {
    const failing: ProfileSnapshot = {
      ...PROFILE,
      status: 'degraded',
      health: { ...PROFILE.health, last_error: 'stream closed by peer' },
    };
    render(<ProfileHeader profile={failing} health={HEALTH} killSwitch={KILL_SWITCH} />);

    expect(screen.getByText('Last error')).toBeInTheDocument();
    expect(screen.getByText('stream closed by peer')).toBeInTheDocument();
    // The status badge and the lifecycle row both report the degraded status.
    expect(screen.getAllByText('Degraded')).toHaveLength(2);
  });

  it('renders an em dash for every absent value instead of NaN or undefined', () => {
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
    render(
      <ProfileHeader
        profile={empty}
        health={degradedHealth}
        killSwitch={{ kill_switch: true, reason: '', changed_at: null }}
      />,
    );

    expect(screen.getByText('Live trading')).toBeInTheDocument();
    expect(screen.getByText('Platform degraded')).toBeInTheDocument();
    expect(screen.getByText('Kill switch engaged')).toBeInTheDocument();
    expect(screen.queryByText('NaN')).toBeNull();
    expect(screen.queryByText('undefined')).toBeNull();
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(8);
  });
});
