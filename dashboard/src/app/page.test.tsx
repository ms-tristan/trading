import { render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// The page is an async Server Component: awaiting it runs the real fetch seam,
// so only the three network calls are mocked. `errorMessage` stays real.
vi.mock('@/lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...actual,
    fetchHealth: vi.fn(),
    fetchProfiles: vi.fn(),
    fetchKillSwitch: vi.fn(),
    fetchOrphans: vi.fn(),
  };
});

import { ApiError, fetchHealth, fetchKillSwitch, fetchOrphans, fetchProfiles } from '@/lib/api';
import { EMPTY_PLACEHOLDER } from '@/lib/format';
import type {
  HealthPayload,
  KillSwitchPayload,
  OrphanReport,
  ProfilesPayload,
  ProfileSnapshot,
  WalletSnapshot,
} from '@/lib/types';

import OverviewPage from './page';

const API_ORIGIN = 'http://monitor.test:8080';

const health: HealthPayload = {
  status: 'ok',
  version: '0.1.0',
  uptime_seconds: 3600,
  profiles_total: 1,
  profiles_running: 1,
  kill_switch: false,
  checked_at: '2024-01-01T00:00:00+00:00',
};

const killSwitch: KillSwitchPayload = { kill_switch: false, reason: '', changed_at: null };

/** The report of the startup safety sweep, as `GET /api/orphans` emits it. */
const orphans: OrphanReport = {
  found: 1,
  orphaned: 1,
  closed_count: 0,
  failed_count: 1,
  closed: [],
  failed: [
    {
      profile_id: 'ghost',
      symbol: 'ETH/USDT',
      quantity: 2,
      error: 'venue rejected the closing order',
    },
  ],
  swept_at: '2024-01-01T00:00:00+00:00',
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
  total_return: 0.045,
  n_trades: 12,
  open_positions: 1,
  health: {
    profile_id: 'alpha',
    status: 'running',
    last_candle_at: '2024-01-01T00:00:00+00:00',
    lag_seconds: 12.5,
    last_error: null,
    reconnect_count: 0,
    counters: {
      candles_processed: 120,
      orders_submitted: 4,
      orders_filled: 3,
      orders_rejected: 1,
      stream_reconnects: 0,
      risk_rejections: 1,
      errors: 0,
    },
  },
  started_at: '2023-12-01T00:00:00+00:00',
  updated_at: '2024-01-01T00:00:00+00:00',
};

const profiles: ProfilesPayload = {
  profiles: [profile],
  generated_at: '2024-01-01T00:00:00+00:00',
  wallet: {
    name: 'usdt',
    mode: 'paper',
    initial_balance: 25000,
    cash: 21500,
    equity: 25750,
    deployed: 4500,
    realized_pnl: 1000,
    unrealized_pnl: -250,
    total_exposure: 4700,
    profiles: 1,
    source: 'local',
    updated_at: '2024-01-01T00:00:00+00:00',
  } satisfies WalletSnapshot,
};

beforeEach(() => {
  process.env.API_ORIGIN = API_ORIGIN;
  vi.mocked(fetchHealth).mockResolvedValue(health);
  vi.mocked(fetchProfiles).mockResolvedValue(profiles);
  vi.mocked(fetchKillSwitch).mockResolvedValue(killSwitch);
  vi.mocked(fetchOrphans).mockResolvedValue(orphans);
  // No live server may ever be reached from a test.
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => {
      throw new TypeError('no live server in tests');
    }),
  );
});

afterEach(() => {
  delete process.env.API_ORIGIN;
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe('OverviewPage', () => {
  it('server-renders the essential profile fields together with the platform summary', async () => {
    render(await OverviewPage());

    // The heading stays in the accessibility tree but is no longer painted: the
    // app shell already names the surface, and the visible "Overview" title plus
    // its subtitle were removed as pure duplication.
    expect(screen.getByRole('heading', { level: 1, name: 'Overview' })).toHaveClass('sr-only');
    expect(
      screen.queryByText('Every trading profile at a glance, refreshed by HTTP polling.'),
    ).not.toBeInTheDocument();

    // Profile identity and its link to the detail route.
    expect(screen.getByRole('link', { name: 'alpha' })).toHaveAttribute('href', '/profiles/alpha');
    expect(screen.getByText('BTC/USDT · 1h')).toBeInTheDocument();

    // The overview cards are essential only: the annex fields of a profile
    // (strategy, counters, per-profile money breakdown) live on the detail route
    // and are never rendered on the homepage anymore.
    expect(screen.queryByText('BasicStrategy')).not.toBeInTheDocument();
    expect(screen.queryByText('Strategy')).not.toBeInTheDocument();

    // Numbers, signed return and counters.
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();
    expect(screen.getByText('+4.50%')).toBeInTheDocument();
    // Every direction on the page carries the same explicit text label: the
    // profile return and the wallet P&L of the shared ledger both read "Up".
    expect(screen.getAllByText('Up').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('Down')).toBeInTheDocument();
    // The per-profile trade counter left the homepage with the annex fields: no
    // card renders the bare "12" anymore.
    expect(screen.queryByText('12')).not.toBeInTheDocument();

    // Mode and status are rendered as text, never as colour alone.
    expect(screen.getByText('Paper')).toBeInTheDocument();
    expect(screen.getByText('Running')).toBeInTheDocument();

    // Platform summary and the kill-switch control.
    expect(screen.getByText('0.1.0')).toBeInTheDocument();
    expect(screen.getByText('01:00:00')).toBeInTheDocument();
    expect(screen.getByText('1 / 1')).toBeInTheDocument();
    expect(screen.getByRole('heading', { level: 2, name: 'Kill switch' })).toBeInTheDocument();

    // The live region starts on the server-rendered stamp.
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-01-01 00:00:00 UTC');
  });

  it('server-renders the shared platform wallet of the profiles payload', async () => {
    render(await OverviewPage());

    // Exactly one panel: the shared ledger is rendered once, from the wallet of
    // the server-rendered payload — the browser issues no request for it.
    const panels = screen.getAllByRole('region', { name: 'Platform wallet' });
    expect(panels).toHaveLength(1);

    const tiles = screen.getByTestId('wallet-tiles');
    expect(within(tiles).getByText('$21,500.00')).toBeInTheDocument();
    expect(within(tiles).getByText('$25,750.00')).toBeInTheDocument();
    expect(within(tiles).getByText('+$1,000.00')).toBeInTheDocument();
    expect(within(tiles).getByText('-$250.00')).toBeInTheDocument();

    // The panel says what the wallet is, and where the cash lives.
    expect(
      screen.getByText(/one shared usdt wallet funds every order of every profile/i),
    ).toBeInTheDocument();
    expect(screen.getByText('Local ledger')).toBeInTheDocument();
    expect(screen.getByText('Paper mode')).toBeInTheDocument();

    // The profile figures stay on screen: they are the attributed share.
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'alpha' })).toBeInTheDocument();
    expect(fetchHealth).toHaveBeenCalledTimes(1);
  });

  it('renders the em dash state when the payload carries no wallet key', async () => {
    // An older monitoring server: `profiles.wallet` is absent, not null.
    vi.mocked(fetchProfiles).mockResolvedValue({
      profiles: [profile],
      generated_at: '2024-01-01T00:00:00+00:00',
    });

    render(await OverviewPage());

    // The panel is not blank and the page does not crash: it shows the em dash
    // state of every wallet value, with a note saying why.
    expect(screen.getByRole('region', { name: 'Platform wallet' })).toBeInTheDocument();
    expect(screen.getByText('No shared wallet reported')).toBeInTheDocument();
    expect(within(screen.getByTestId('wallet-tiles')).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(
      8,
    );
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();
    expect(screen.queryByText('NaN')).not.toBeInTheDocument();
  });

  it('fetches the first paint from API_ORIGIN only once, before any browser request', async () => {
    render(await OverviewPage());

    expect(fetchHealth).toHaveBeenCalledTimes(1);
    expect(fetchProfiles).toHaveBeenCalledTimes(1);
    expect(fetchKillSwitch).toHaveBeenCalledTimes(1);
    expect(fetchProfiles).toHaveBeenCalledWith({ baseUrl: API_ORIGIN });
    expect(fetchHealth).toHaveBeenCalledWith({ baseUrl: API_ORIGIN });
    expect(fetchKillSwitch).toHaveBeenCalledWith({ baseUrl: API_ORIGIN });
    expect(fetchOrphans).toHaveBeenCalledTimes(1);
    expect(fetchOrphans).toHaveBeenCalledWith({ baseUrl: API_ORIGIN });
  });

  it('renders the empty state when the platform has no profile', async () => {
    vi.mocked(fetchHealth).mockResolvedValue({ ...health, profiles_total: 0, profiles_running: 0 });
    vi.mocked(fetchProfiles).mockResolvedValue({ profiles: [], generated_at: null });

    render(await OverviewPage());

    expect(screen.getByText('No profile configured')).toBeInTheDocument();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();
  });

  it('renders the unreachable state without throwing and starts no polling', async () => {
    vi.mocked(fetchProfiles).mockRejectedValue(
      new ApiError('network', 'network error calling /api/profiles: fetch failed', {
        path: '/api/profiles',
      }),
    );

    render(await OverviewPage());

    const banner = screen.getByRole('status');
    // The headline is the mapped one: the transport message is never the
    // explanation the operator reads first...
    expect(within(banner).getByText('The monitoring API is unreachable')).toBeInTheDocument();
    // ...it stays visible, verbatim, as the detail line under it.
    expect(within(banner).getByTestId('error-banner-detail')).toHaveTextContent(
      'network error calling /api/profiles: fetch failed',
    );

    // The empty state keeps its documented title (the banner repeats it).
    expect(screen.getAllByText('The monitoring API is unreachable')).toHaveLength(2);
    expect(screen.getByText(/No payload was returned by/)).toBeInTheDocument();
    expect(screen.getByText(/API_ORIGIN/)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(API_ORIGIN))).toBeInTheDocument();

    // No live region, no toolbar: nothing polls an API that is down. The shared
    // wallet panel is absent too — there is no payload to render it from.
    expect(screen.queryByText(/checked at/i)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /live updates/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 2, name: 'Profiles' })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Platform wallet' })).not.toBeInTheDocument();

    expect(fetchHealth).toHaveBeenCalledTimes(1);
    expect(fetchProfiles).toHaveBeenCalledTimes(1);
    expect(fetchKillSwitch).toHaveBeenCalledTimes(1);
  });

  it('reads a proxy 502 as the monitoring API being unreachable, never as a bare status', async () => {
    vi.mocked(fetchProfiles).mockRejectedValue(
      new ApiError('http', 'HTTP 502', { status: 502, path: '/api/profiles' }),
    );

    render(await OverviewPage());

    const banner = screen.getByRole('status');
    expect(within(banner).getByText('The monitoring API is unreachable')).toBeInTheDocument();
    // The raw status is never hidden — it is the detail, not the headline.
    const detail = within(banner).getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 502');
    expect(detail).toHaveTextContent('/api/profiles');
    expect(screen.queryByText('HTTP 502')).not.toBeInTheDocument();

    // The page is an ordinary render, not an error screen: no polling starts.
    expect(screen.getByText(/No payload was returned by/)).toBeInTheDocument();
    expect(screen.queryByText(/checked at/i)).not.toBeInTheDocument();
    expect(fetchHealth).toHaveBeenCalledTimes(1);
  });

  it('server-renders the orphan sweep warning of the fetched report', async () => {
    render(await OverviewPage());

    // The report fetched by the Server Component reaches the live region and is
    // part of the first paint: the browser issues no request for it.
    const notice = screen.getByText('Orphaned positions could not all be closed').closest(
      '[role="status"]',
    );
    expect(notice).not.toBeNull();
    expect(notice).toHaveTextContent('ghost ETH/USDT');
    expect(notice).toHaveTextContent('venue rejected the closing order');
    expect(fetchOrphans).toHaveBeenCalledTimes(1);
  });

  it('still renders the normal page when the orphan route alone fails', async () => {
    vi.mocked(fetchOrphans).mockRejectedValue(
      new ApiError('http', 'HTTP 404', { status: 404, path: '/api/orphans' }),
    );

    render(await OverviewPage());

    // The failing orphan call is normalised to `null`: the page is NOT sent to
    // the unreachable state and every other payload still renders.
    expect(screen.queryByText('The monitoring API is unreachable')).not.toBeInTheDocument();
    expect(screen.queryByText(/No payload was returned by/)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'alpha' })).toBeInTheDocument();
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();
    expect(screen.getByRole('region', { name: 'Platform wallet' })).toBeInTheDocument();
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-01-01 00:00:00 UTC');

    // And there is simply no warning banner to show.
    expect(screen.queryByText(/Orphaned positions/i)).not.toBeInTheDocument();
    expect(fetchOrphans).toHaveBeenCalledWith({ baseUrl: API_ORIGIN });
  });

  it('renders no orphan warning when the sweep report carries nothing to warn about', async () => {
    vi.mocked(fetchOrphans).mockResolvedValue({
      found: 1,
      orphaned: 0,
      closed_count: 0,
      failed_count: 0,
      closed: [],
      failed: [],
      swept_at: '2024-01-01T00:00:00+00:00',
    });

    render(await OverviewPage());

    expect(screen.queryByText(/Orphaned positions/i)).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'alpha' })).toBeInTheDocument();
  });
});
