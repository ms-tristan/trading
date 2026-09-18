import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';
import type { HealthPayload, KillSwitchPayload, ProfileSnapshot } from '@/lib/types';

import { PlatformSummary } from './platform-summary';

const health: HealthPayload = {
  status: 'ok',
  version: '0.1.0',
  uptime_seconds: 3600,
  profiles_total: 2,
  profiles_running: 1,
  kill_switch: false,
  checked_at: '2024-01-01T00:00:00+00:00',
};

const released: KillSwitchPayload = { kill_switch: false, reason: '', changed_at: null };

const engaged: KillSwitchPayload = {
  kill_switch: true,
  reason: 'manual stop after a feed gap',
  changed_at: '2024-01-01T00:00:00+00:00',
};

function profile(profileId: string): ProfileSnapshot {
  return {
    profile_id: profileId,
    symbol: 'BTC/USDT',
    timeframe: '1h',
    strategy: 'BasicStrategy',
    mode: 'paper',
    status: 'running',
    initial_balance: 10000,
    equity: 10000,
    cash: 10000,
    position_value: 0,
    total_return: 0,
    n_trades: 0,
    open_positions: 0,
    health: {
      profile_id: profileId,
      status: 'running',
      last_candle_at: null,
      lag_seconds: null,
      last_error: null,
      reconnect_count: 0,
      counters: {
        candles_processed: 0,
        orders_submitted: 0,
        orders_filled: 0,
        orders_rejected: 0,
        stream_reconnects: 0,
        risk_rejections: 0,
        errors: 0,
      },
    },
    started_at: null,
    updated_at: null,
  };
}

const profiles = [profile('alpha'), profile('beta'), profile('gamma'), profile('delta')];

describe('PlatformSummary', () => {
  it('renders the healthy platform with version, uptime and counters', () => {
    render(<PlatformSummary health={health} killSwitch={released} profiles={profiles} />);

    const badge = screen.getByText('Healthy').closest('span[data-tone]');
    expect(badge).toHaveAttribute('data-tone', 'ok');
    expect(badge?.querySelector('svg')).not.toBeNull();

    expect(screen.getByText('0.1.0')).toBeInTheDocument();
    expect(screen.getByText('01:00:00')).toBeInTheDocument();
    expect(screen.getByText('1 / 2')).toBeInTheDocument();
    expect(screen.getByText('4 listed here')).toBeInTheDocument();

    const killSwitchBadge = screen.getByText('Released').closest('span[data-tone]');
    expect(killSwitchBadge).toHaveAttribute('data-tone', 'ok');
    expect(screen.getByText('Changed at').nextElementSibling).toHaveTextContent(EMPTY_PLACEHOLDER);

    // A released kill switch raises no degradation notice.
    expect(screen.queryByText(/kill switch engaged/i)).not.toBeInTheDocument();
  });

  it('spells out the degradation reason while the kill switch is engaged', () => {
    render(
      <PlatformSummary
        health={{ ...health, status: 'degraded', kill_switch: true }}
        killSwitch={engaged}
        profiles={profiles}
      />,
    );

    const badge = screen.getByText('Degraded').closest('span[data-tone]');
    expect(badge).toHaveAttribute('data-tone', 'warn');
    expect(badge?.querySelector('svg')).not.toBeNull();

    const notice = screen.getByText(/kill switch engaged/i).closest('p');
    expect(notice).toHaveTextContent('manual stop after a feed gap');
    expect(notice).toHaveAttribute('aria-live', 'polite');

    const killSwitchBadge = screen.getByText('Engaged').closest('span[data-tone]');
    expect(killSwitchBadge).toHaveAttribute('data-tone', 'error');
    expect(screen.getByText('Reason').nextElementSibling).toHaveTextContent(
      'manual stop after a feed gap',
    );
    expect(screen.getByText('2024-01-01 00:00:00 UTC')).toBeInTheDocument();
  });

  it('renders an em dash instead of an absent version or uptime', () => {
    render(
      <PlatformSummary
        health={{ ...health, version: '  ', uptime_seconds: null }}
        killSwitch={{ kill_switch: false, reason: '   ', changed_at: null }}
        profiles={[]}
      />,
    );

    // StatTile renders its label and its value as siblings of one tile root.
    expect(screen.getByText('Uptime').closest('div')?.parentElement).toHaveTextContent(
      EMPTY_PLACEHOLDER,
    );
    expect(screen.getByText('Version').closest('div')?.parentElement).toHaveTextContent(
      EMPTY_PLACEHOLDER,
    );
    expect(screen.getByText('0 listed here')).toBeInTheDocument();
    expect(screen.getByText('Reason').nextElementSibling).toHaveTextContent(EMPTY_PLACEHOLDER);
  });
});
