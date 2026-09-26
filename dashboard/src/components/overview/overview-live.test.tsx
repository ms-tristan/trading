import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';
import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type {
  HealthPayload,
  KillSwitchPayload,
  OrphanReport,
  ProfilesPayload,
  ProfileSnapshot,
  WalletSnapshot,
} from '@/lib/types';

import { BUTTON_SIZE_CLASSES } from '@/components/ui/button';

import { OverviewLive } from './overview-live';

// ---------------------------------------------------------------------------
// fixtures: exact payload shapes of the frozen API
// ---------------------------------------------------------------------------

function makeProfile(profileId: string, overrides: Partial<ProfileSnapshot> = {}): ProfileSnapshot {
  return {
    profile_id: profileId,
    symbol: 'BTC/USDT',
    timeframe: '1h',
    strategy: 'BasicStrategy',
    mode: 'paper',
    status: 'running',
    initial_balance: 10000,
    equity: profileId === 'alpha' ? 10450.5 : 9900,
    cash: 8000,
    position_value: 2450.5,
    total_return: 0.045,
    n_trades: 12,
    open_positions: 1,
    health: {
      profile_id: profileId,
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
    ...overrides,
  };
}

const health: HealthPayload = {
  status: 'ok',
  version: '0.1.0',
  uptime_seconds: 3600,
  profiles_total: 2,
  profiles_running: 2,
  kill_switch: false,
  checked_at: '2024-01-01T00:00:00+00:00',
};

const killSwitch: KillSwitchPayload = { kill_switch: false, reason: '', changed_at: null };

/**
 * The report of the startup safety sweep: one position closed at the venue, one
 * that could not be closed and is still open. The loud state, on purpose — a
 * failure must never be softened by the success line next to it.
 */
const orphans: OrphanReport = {
  found: 2,
  orphaned: 2,
  closed_count: 1,
  failed_count: 1,
  closed: [
    { profile_id: 'test1', symbol: 'BTC/USDT', quantity: 0.05, side: 'sell', price: 42123.5 },
  ],
  failed: [
    {
      profile_id: 'ghost',
      symbol: 'ETH/USDT',
      quantity: null,
      error: 'venue rejected the closing order',
    },
  ],
  swept_at: '2024-06-01T12:00:00+00:00',
};

/** A sweep that found nothing: the banner stays away. */
const noOrphans: OrphanReport = {
  found: 2,
  orphaned: 0,
  closed_count: 0,
  failed_count: 0,
  closed: [],
  failed: [],
  swept_at: '2024-06-01T12:00:00+00:00',
};

/**
 * The shared platform wallet of the fixtures, as the PAPER ledger.
 *
 * `cash = initial_balance - deployed + realized_pnl`
 * (21,500 = 25,000 - 4,500 + 1,000) and
 * `equity = initial_balance + realized_pnl + unrealized_pnl`
 * (25,750 = 25,000 + 1,000 - 250). The three totals are coherent:
 * `total_portfolio_value = total_cash + positions_value`
 * (25,750 = 21,500 + 4,250).
 */
const wallet: WalletSnapshot = {
  name: 'usdt',
  mode: 'paper',
  initial_balance: 25000,
  cash: 21500,
  equity: 25750,
  deployed: 4500,
  realized_pnl: 1000,
  unrealized_pnl: -250,
  total_exposure: 4700,
  profiles: 2,
  source: 'local',
  updated_at: '2024-01-01T00:00:00+00:00',
  total_cash: 21500,
  positions_value: 4250,
  total_portfolio_value: 25750,
};

/** The REAL ledger: a different ledger, with clearly different totals. */
const liveWallet: WalletSnapshot = {
  name: 'binance',
  mode: 'live',
  initial_balance: 5000,
  cash: 4900,
  equity: 5120,
  deployed: 220,
  realized_pnl: 20,
  unrealized_pnl: 100,
  total_exposure: 220,
  profiles: 1,
  source: 'venue',
  updated_at: '2024-01-01T00:00:00+00:00',
  total_cash: 4900,
  positions_value: 300,
  total_portfolio_value: 5200,
};

/** The wallet after one refresh: cash and equity have moved. */
const refreshedWallet: WalletSnapshot = {
  ...wallet,
  cash: 23800.25,
  deployed: 2600,
  equity: 26951,
  realized_pnl: 1400.25,
  unrealized_pnl: 550.75,
  total_exposure: 2600,
  total_cash: 23800.25,
  positions_value: 3150.75,
  total_portfolio_value: 26951,
  updated_at: '2024-06-01T12:00:02+00:00',
};

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(typeof body === 'string' ? body : JSON.stringify(body)),
  } as unknown as Response;
}

interface StubServer {
  health: HealthPayload;
  profiles: ProfilesPayload;
  killSwitch: KillSwitchPayload;
  /** Report answered by `GET /api/orphans`. */
  orphans: OrphanReport;
  /** When `true`, `GET /api/orphans` fails while every other route answers. */
  orphansFailing: boolean;
  /** When `true`, every request fails (no response ever arrives). */
  failing: boolean;
  /** Requested paths of the page cycle, in call order. */
  calls: string[];
  fetchMock: ReturnType<typeof vi.fn>;
}

/**
 * Install a network double: no live server is ever reached (see api.test.ts).
 *
 * The cards render the per-profile action island, which polls `GET /api/control`
 * on its own cadence. That route is answered here — it must never 404 and never
 * join the failure switch, or the pages under test would show an unrelated
 * banner — but it is deliberately **not** recorded in `calls`, which tracks the
 * page cycle only.
 */
function startServer(): StubServer {
  const server: StubServer = {
    health: { ...health, wallet: { ...wallet } },
    profiles: {
      profiles: [makeProfile('alpha'), makeProfile('beta')],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
      wallets: { paper: { ...wallet }, live: { ...liveWallet } },
    },
    killSwitch: { ...killSwitch },
    orphans: { ...orphans },
    orphansFailing: false,
    failing: false,
    calls: [],
    fetchMock: vi.fn(),
  };

  server.fetchMock.mockImplementation(async (url: unknown): Promise<Response> => {
    const target = String(url);
    if (target.endsWith('/api/control')) {
      return jsonResponse({
        engine_running: true,
        read_only: false,
        mutable: true,
        profiles: [
          { profile_id: 'alpha', paused: false, running: true },
          { profile_id: 'beta', paused: false, running: true },
        ],
      });
    }
    server.calls.push(target);
    if (server.failing) {
      throw new TypeError('Failed to fetch');
    }
    if (target.endsWith('/api/health')) {
      return jsonResponse(server.health);
    }
    if (target.endsWith('/api/profiles')) {
      return jsonResponse(server.profiles);
    }
    if (target.endsWith('/api/kill-switch')) {
      return jsonResponse(server.killSwitch);
    }
    if (target.endsWith('/api/orphans')) {
      // Only this route can be made to fail on its own: a dashboard that
      // cannot read it must never lose the profile list over it.
      if (server.orphansFailing) {
        throw new TypeError('Failed to fetch');
      }
      return jsonResponse(server.orphans);
    }
    return jsonResponse({ error: 'not found' }, 404);
  });

  vi.stubGlobal('fetch', server.fetchMock);
  return server;
}

function renderLive(server: StubServer, initialOrphans: OrphanReport | null = null) {
  return render(
    <OverviewLive
      initialProfiles={server.profiles}
      initialHealth={server.health}
      initialKillSwitch={server.killSwitch}
      initialOrphans={initialOrphans}
      initialCheckedAt="2024-01-01T00:00:00+00:00"
      pollIntervalMs={2000}
    />,
  );
}

/** Advance the fake clock and let the polling cycle settle. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** Let the pending promises of a click handler settle. */
async function settle(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
  });
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2024-06-01T12:00:00Z'));
  // The action islands of the cards only offer their controls to a saved token.
  window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, 'overview-operator-token');
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

describe('OverviewLive', () => {
  it('renders the server payload without a request, then polls every 2 seconds', async () => {
    const server = startServer();
    renderLive(server);

    // The Server Component already fetched: mounting must not duplicate the
    // page cycle (the action islands poll their own read-only control route).
    expect(server.calls).toEqual([]);
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();

    await advance(1999);
    expect(server.calls).toEqual([]);

    await advance(1);
    expect(server.calls).toEqual([
      '/api/health',
      '/api/profiles',
      '/api/kill-switch',
      '/api/orphans',
    ]);

    await advance(2000);
    expect(server.calls).toHaveLength(8);
  });

  it('renders the action island of every card, fed by the control route', async () => {
    const server = startServer();
    renderLive(server);

    expect(screen.getAllByRole('button', { name: 'Delete profile' })).toHaveLength(2);
    expect(screen.getAllByRole('button', { name: 'Pause' })).toHaveLength(2);
    expect(server.fetchMock).toHaveBeenCalledWith('/api/control', expect.anything());
    // Nothing of the page cycle was duplicated by those control polls.
    expect(server.calls).toEqual([]);
  });

  it('refreshes the cards in place and stamps the checked-at time', async () => {
    const server = startServer();
    renderLive(server);

    const card = screen.getByRole('article', { name: 'alpha' });
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-01-01 00:00:00 UTC');

    server.profiles = {
      profiles: [makeProfile('alpha', { equity: 11111 }), makeProfile('beta')],
      generated_at: '2024-06-01T12:00:02+00:00',
    };
    await advance(2000);

    expect(screen.getByText('$11,111.00')).toBeInTheDocument();
    // Same DOM node: a profile card is never unmounted on an update (no flicker).
    expect(screen.getByRole('article', { name: 'alpha' })).toBe(card);
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-06-01 12:00:02 UTC');
  });

  it('keeps the last known good data and announces a failed cycle', async () => {
    const server = startServer();
    renderLive(server);
    await advance(2000);
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-06-01 12:00:02 UTC');

    server.failing = true;
    await advance(2000);

    // Scoped on purpose: the page carries more than one `role="status"` now
    // that the sweep warning is a notice of its own; this test is about the
    // polling failure of the toolbar.
    const banner = within(screen.getByRole('region', { name: 'Profiles — paper trading' })).getByRole('status');
    // The operator reads the mapped headline, never the bare transport message.
    expect(within(banner).getByText('The monitoring API is unreachable')).toBeInTheDocument();
    // The raw cause stays visible, verbatim, as the detail line.
    expect(within(banner).getByTestId('error-banner-detail')).toHaveTextContent(
      'network error calling /api/health',
    );
    // The last known good payload and its stamp stay on screen.
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-06-01 12:00:02 UTC');
  });

  it('stops polling when paused and refreshes again on resume', async () => {
    const server = startServer();
    renderLive(server);
    await advance(2000);
    expect(server.calls).toHaveLength(4);

    fireEvent.click(screen.getByRole('button', { name: 'Pause live updates' }));
    expect(screen.getByText('Live updates paused')).toBeInTheDocument();

    await advance(6000);
    expect(server.calls).toHaveLength(4);

    fireEvent.click(screen.getByRole('button', { name: 'Resume live updates' }));
    await settle();
    expect(server.calls).toHaveLength(8);

    await advance(2000);
    expect(server.calls).toHaveLength(12);
  });

  it('exposes the checked-at stamp and the paused state as a polite live region', async () => {
    const server = startServer();
    renderLive(server);

    const region = screen.getByText(/checked at/i);
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(screen.getByText('Live updates')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Pause live updates' }));
    expect(screen.getByText('Live updates paused')).toBeInTheDocument();
  });

  it('offers exactly one New profile action, inside the live toolbar of the Profiles section', () => {
    const server = startServer();
    renderLive(server);

    // Exactly one: the header no longer carries a second, orphaned link.
    const links = screen.getAllByRole('link', { name: 'New profile' });
    expect(links).toHaveLength(1);
    expect(links[0]).toHaveAttribute('href', '/profiles/new');

    // Placement: the same control cluster as 'Pause live updates' / 'Refresh now',
    // inside the region labelled by the 'Profiles' heading.
    const refresh = screen.getByRole('button', { name: 'Refresh now' });
    expect(refresh.parentElement).toContainElement(links[0]);
    expect(screen.getByRole('region', { name: 'Profiles — paper trading' })).toContainElement(links[0]);

    // The affordances of the action are unchanged by the move.
    expect(links[0]).toHaveClass(
      'cursor-pointer',
      'hover:text-accent',
      'active:bg-muted-pressed',
      'active:border-accent',
      'active:text-accent',
      'motion-safe:transition-colors',
      'focus-visible:ring-2',
    );

    // Peer parity with the two live controls it sits between: the action borrows
    // the same `size="sm"` box metrics, so the three controls of the cluster
    // share one height instead of the misaligned 26 / 26 / 30px measured in a
    // browser when the action restated the padding with `text-sm`.
    for (const metric of BUTTON_SIZE_CLASSES.sm.split(' ')) {
      expect(links[0]).toHaveClass(metric);
      expect(refresh).toHaveClass(metric);
    }
  });

  it('renders the documented empty states', async () => {
    const server = startServer();
    server.profiles = { profiles: [], generated_at: null, wallet: { ...wallet } };
    server.health = { ...health, profiles_total: 0, profiles_running: 0 };
    const view = renderLive(server);

    expect(screen.getByText('No profile configured')).toBeInTheDocument();
    expect(screen.queryByRole('article')).not.toBeInTheDocument();

    view.unmount();
    server.health = { ...health, profiles_total: 2, profiles_running: 0 };
    renderLive(server);

    expect(screen.getByText('No profile reported yet')).toBeInTheDocument();
    expect(screen.getByText(/Configured profiles: 2/)).toBeInTheDocument();
  });

  it('renders the shared wallet of the server payload without a request', () => {
    const server = startServer();
    renderLive(server);

    // One panel, seeded from the server payload: mounting issues no request.
    expect(server.calls).toEqual([]);
    const panel = screen.getByRole('region', { name: 'Platform wallet — paper trading' });
    expect(within(panel).getAllByText('Platform wallet — paper trading')).toHaveLength(1);

    // The three totals of the paper ledger, straight out of the payload.
    const tiles = screen.getByTestId('wallet-tiles');
    expect(within(tiles).getByText('$21,500.00')).toBeInTheDocument();
    expect(within(tiles).getByText('$4,250.00')).toBeInTheDocument();
    expect(within(tiles).getByText('$25,750.00')).toBeInTheDocument();

    expect(screen.getByText('Local ledger')).toBeInTheDocument();
    // And it is the *only* wallet panel of the page.
    expect(
      screen.getAllByRole('region', { name: 'Platform wallet — paper trading' }),
    ).toHaveLength(1);
  });

  it('refreshes the shared wallet with the polling cycle', async () => {
    const server = startServer();
    renderLive(server);

    const tiles = screen.getByTestId('wallet-tiles');
    expect(within(tiles).getByText('$21,500.00')).toBeInTheDocument();
    expect(within(tiles).queryByText('$23,800.25')).not.toBeInTheDocument();

    server.profiles = {
      profiles: [makeProfile('alpha'), makeProfile('beta')],
      generated_at: '2024-06-01T12:00:02+00:00',
      wallet: { ...refreshedWallet },
      wallets: { paper: { ...refreshedWallet }, live: { ...liveWallet } },
    };
    server.health = { ...server.health, wallet: { ...refreshedWallet } };
    await advance(2000);

    expect(server.calls).toEqual([
      '/api/health',
      '/api/profiles',
      '/api/kill-switch',
      '/api/orphans',
    ]);
    expect(within(tiles).getByText('$23,800.25')).toBeInTheDocument();
    expect(within(tiles).getByText('$26,951.00')).toBeInTheDocument();
    // The panel is updated in place, never remounted.
    expect(screen.getByTestId('wallet-tiles')).toBe(tiles);
  });

  it('keeps the last known good wallet when a cycle fails', async () => {
    const server = startServer();
    renderLive(server);
    await advance(2000);
    expect(within(screen.getByTestId('wallet-tiles')).getByText('$21,500.00')).toBeInTheDocument();

    server.failing = true;
    await advance(2000);

    // The failure is announced by the toolbar, and the wallet of the last good
    // payload stays on screen instead of collapsing to em dashes.
    expect(
      within(screen.getByRole('region', { name: 'Profiles — paper trading' })).getByRole('status'),
    ).toHaveTextContent('The monitoring API is unreachable');
    expect(within(screen.getByTestId('wallet-tiles')).getByText('$21,500.00')).toBeInTheDocument();
    expect(screen.getByText('Local ledger')).toBeInTheDocument();
  });

  it('renders the em dash state of a payload without a wallet key', async () => {
    const server = startServer();
    // An older monitoring server: the payload carries neither `wallet` nor the
    // mode-keyed `wallets` object.
    server.profiles = {
      profiles: [makeProfile('alpha'), makeProfile('beta')],
      generated_at: '2024-01-01T00:00:00+00:00',
    };
    renderLive(server);

    // The note is scoped to the selected mode: the platform has not been proven
    // to lack a wallet, only this mode's ledger row is missing.
    expect(screen.getByText('No paper trading ledger reported')).toBeInTheDocument();
    expect(within(screen.getByTestId('wallet-tiles')).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(
      3,
    );
    // The cards of the profiles keep rendering, and nothing crashed.
    expect(screen.getAllByRole('article')).toHaveLength(2);

    await advance(2000);
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.getByText('No paper trading ledger reported')).toBeInTheDocument();
  });

  it('surfaces a kill switch engaged while the page is open', async () => {
    const server = startServer();
    renderLive(server);
    expect(screen.queryByText(/Kill switch engaged/i)).not.toBeInTheDocument();

    server.killSwitch = {
      kill_switch: true,
      reason: 'manual stop',
      changed_at: '2024-06-01T11:00:00+00:00',
    };
    await advance(2000);

    const notice = screen.getByText(/Kill switch engaged/i).closest('p');
    expect(notice).toHaveTextContent('manual stop');
    expect(notice).toHaveTextContent('2024-06-01 11:00:00 UTC');
    expect(notice).toHaveAttribute('aria-live', 'polite');
  });

  // -------------------------------------------------------------------------
  // the startup safety sweep warning
  // -------------------------------------------------------------------------

  it('renders no orphan warning when the platform was never swept', () => {
    const server = startServer();
    renderLive(server, null);

    expect(screen.queryByText(/Orphaned positions/i)).not.toBeInTheDocument();
    // The rest of the overview is untouched by the absence of a report.
    expect(screen.getAllByRole('article')).toHaveLength(2);
  });

  it('renders the orphan warning of the server-rendered report without a request', () => {
    const server = startServer();
    renderLive(server, orphans);

    // The first paint already carries the report: mounting issues no request.
    expect(server.calls).toEqual([]);

    const banner = screen.getByText(/Orphaned positions could not all be closed/i);
    const notice = banner.closest('[role="status"]');
    expect(notice).toHaveTextContent('test1 BTC/USDT');
    expect(notice).toHaveTextContent('venue rejected the closing order');

    // Placement: the warning is rendered above the profiles section, so a
    // safety notice is never pushed below the fold by the list it is about.
    const section = screen.getByRole('region', { name: 'Profiles — paper trading' });
    expect(notice).not.toBeNull();
    expect(
      (notice as HTMLElement).compareDocumentPosition(section) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it('surfaces an orphan report found by a polling cycle', async () => {
    const server = startServer();
    server.orphans = { ...noOrphans };
    const view = renderLive(server, noOrphans);
    expect(screen.queryByText(/Orphaned positions/i)).not.toBeInTheDocument();

    server.orphans = { ...orphans };
    await advance(2000);

    expect(screen.getByText(/Orphaned positions could not all be closed/i)).toBeInTheDocument();

    // And a later clean sweep takes the warning away again.
    server.orphans = { ...noOrphans };
    await advance(2000);
    expect(screen.queryByText(/Orphaned positions/i)).not.toBeInTheDocument();

    view.unmount();
  });

  it('keeps the profile cards when the orphan route alone cannot be read', async () => {
    const server = startServer();
    const view = renderLive(server, orphans);

    server.orphansFailing = true;
    await advance(2000);

    // The failing call of the cycle is normalised to `null`: the cards, the
    // wallet and the toolbar all keep the last known good bundle.
    expect(server.calls).toContain('/api/orphans');
    expect(screen.queryByText(/checked at/i)).toHaveTextContent('2024-06-01 12:00:02 UTC');
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.getByRole('link', { name: 'alpha' })).toBeInTheDocument();
    expect(within(screen.getByTestId('wallet-tiles')).getByText('$21,500.00')).toBeInTheDocument();

    // No warning survives a report the dashboard could not read, and the
    // operator is not told the monitoring API is unreachable either.
    expect(screen.queryByText(/Orphaned positions/i)).not.toBeInTheDocument();
    expect(screen.queryByText('The monitoring API is unreachable')).not.toBeInTheDocument();

    view.unmount();
  });

  // -------------------------------------------------------------------------
  // the Paper / Real toggle: the whole page follows it, with no request
  // -------------------------------------------------------------------------

  it('defaults to paper and names the mode in the profiles heading and the toggle', () => {
    const server = startServer();
    renderLive(server);

    // The default is paper, and both the list and the panel say so in words.
    expect(screen.getByRole('radio', { name: /Paper trading/i })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(
      screen.getByRole('heading', { level: 2, name: 'Profiles — paper trading' }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 2, name: 'Platform wallet — paper trading' }),
    ).toBeInTheDocument();
  });

  it('shows what each mode holds before it is switched to', () => {
    const server = startServer();
    server.profiles = {
      profiles: [makeProfile('alpha'), makeProfile('live-1', { mode: 'live' })],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
      wallets: { paper: { ...wallet }, live: { ...liveWallet } },
    };
    renderLive(server);

    const toggle = screen.getByTestId('mode-toggle');
    expect(within(toggle).getByRole('radio', { name: /Paper trading/i })).toHaveTextContent(
      '1 profile',
    );
    expect(within(toggle).getByRole('radio', { name: /Real trading/i })).toHaveTextContent(
      '1 profile',
    );
  });

  it('switches the list AND the ledger to the real mode WITHOUT issuing a request', async () => {
    const server = startServer();
    // Two profiles, one per mode: the two lists must never share an entry.
    server.profiles = {
      profiles: [
        makeProfile('alpha'),
        makeProfile('live-1', { mode: 'live', equity: 7777 }),
      ],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
      wallets: { paper: { ...wallet }, live: { ...liveWallet } },
    };
    renderLive(server);

    // The paper view holds the paper profile and the paper ledger.
    expect(screen.getByRole('link', { name: 'alpha' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'live-1' })).not.toBeInTheDocument();
    expect(within(screen.getByTestId('wallet-tiles')).getByText('$25,750.00')).toBeInTheDocument();

    const callsBefore = [...server.calls];

    fireEvent.click(screen.getByRole('radio', { name: /Real trading/i }));
    await settle();

    // THE point of the switch: no request was issued for it at all.
    expect(server.calls).toEqual(callsBefore);
    expect(server.calls).toEqual([]);

    // The list follows the toggle: only the live profile is rendered now.
    expect(screen.getByRole('link', { name: 'live-1' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'alpha' })).not.toBeInTheDocument();
    expect(
      screen.getByRole('heading', { level: 2, name: 'Profiles — real trading' }),
    ).toBeInTheDocument();

    // And so does the ledger: the REAL totals, never the paper ones.
    const tiles = screen.getByTestId('wallet-tiles');
    expect(within(tiles).getByText('$4,900.00')).toBeInTheDocument();
    expect(within(tiles).getByText('$5,200.00')).toBeInTheDocument();
    expect(within(tiles).queryByText('$25,750.00')).not.toBeInTheDocument();
    expect(screen.getByText('Venue account')).toBeInTheDocument();
    expect(screen.getByText('Real mode')).toBeInTheDocument();
  });

  it('restores the paper list and totals when the toggle goes back', async () => {
    const server = startServer();
    server.profiles = {
      profiles: [makeProfile('alpha'), makeProfile('live-1', { mode: 'live' })],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
      wallets: { paper: { ...wallet }, live: { ...liveWallet } },
    };
    renderLive(server);

    fireEvent.click(screen.getByRole('radio', { name: /Real trading/i }));
    await settle();
    expect(screen.getByRole('link', { name: 'live-1' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('radio', { name: /Paper trading/i }));
    await settle();

    expect(screen.getByRole('link', { name: 'alpha' })).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'live-1' })).not.toBeInTheDocument();
    expect(within(screen.getByTestId('wallet-tiles')).getByText('$25,750.00')).toBeInTheDocument();
    expect(screen.getByText('Local ledger')).toBeInTheDocument();
    // Still nothing was requested: both directions read the same payload.
    expect(server.calls).toEqual([]);
  });

  it('never mixes two modes in one list', () => {
    const server = startServer();
    server.profiles = {
      profiles: [
        makeProfile('alpha'),
        makeProfile('beta'),
        makeProfile('live-1', { mode: 'live' }),
        makeProfile('live-2', { mode: 'live' }),
      ],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
      wallets: { paper: { ...wallet }, live: { ...liveWallet } },
    };
    renderLive(server);

    const paperCards = screen.getAllByRole('article');
    expect(paperCards).toHaveLength(2);
    // Scoping by the accessible name of each card: the two live profiles are
    // simply not there, so the two lists can never share an entry.
    expect(screen.getByRole('article', { name: 'alpha' })).toBeInTheDocument();
    expect(screen.getByRole('article', { name: 'beta' })).toBeInTheDocument();
    expect(screen.queryByRole('article', { name: 'live-1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('article', { name: 'live-2' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'live-1' })).not.toBeInTheDocument();
    expect(screen.queryByRole('link', { name: 'live-2' })).not.toBeInTheDocument();
  });

  it('distinguishes "no profile for this mode" from "no profile configured"', async () => {
    const server = startServer();
    // The platform holds profiles, but only on the real side.
    server.profiles = {
      profiles: [makeProfile('alpha', { mode: 'live' }), makeProfile('beta', { mode: 'live' })],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
      wallets: { paper: null, live: { ...liveWallet } },
    };
    renderLive(server);

    // The paper side: profiles exist elsewhere, so the copy must NOT claim the
    // platform is unconfigured.
    expect(screen.getByText('No profile for this mode')).toBeInTheDocument();
    expect(screen.getByText(/none of them in paper trading/i)).toBeInTheDocument();
    expect(screen.queryByText('No profile configured')).not.toBeInTheDocument();

    // An empty paper ledger is a ledger that was never traded: em dash totals,
    // and the note says so for the mode rather than for the whole platform.
    expect(within(screen.getByTestId('wallet-tiles')).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(
      3,
    );
    expect(screen.getByText('No paper trading ledger reported')).toBeInTheDocument();

    // Switching to the side that DOES hold profiles restores a real list.
    fireEvent.click(screen.getByRole('radio', { name: /Real trading/i }));
    await settle();
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.queryByText('No profile for this mode')).not.toBeInTheDocument();
  });

  it('still reads "no profile configured" when the platform holds none at all', () => {
    const server = startServer();
    server.profiles = { profiles: [], generated_at: null, wallet: { ...wallet } };
    server.health = { ...health, profiles_total: 0, profiles_running: 0 };
    renderLive(server);

    expect(screen.getByText('No profile configured')).toBeInTheDocument();
    expect(screen.queryByText('No profile for this mode')).not.toBeInTheDocument();
  });

  it('falls back to the paper ledger when an older server emits no `wallets` key', () => {
    const server = startServer();
    // An older server: `wallets` is absent entirely, `wallet` is the paper ledger.
    server.profiles = {
      profiles: [makeProfile('alpha'), makeProfile('beta')],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
    };
    renderLive(server);

    expect(within(screen.getByTestId('wallet-tiles')).getByText('$25,750.00')).toBeInTheDocument();

    // The fallback is for paper ONLY: `wallet` is the paper ledger by contract,
    // so the real side must never show paper money under a real heading.
    fireEvent.click(screen.getByRole('radio', { name: /Real trading/i }));
    expect(screen.getByText('No real trading ledger reported')).toBeInTheDocument();
    expect(within(screen.getByTestId('wallet-tiles')).getAllByText(EMPTY_PLACEHOLDER)).toHaveLength(
      3,
    );
  });

  it('keeps the mode across polling cycles and follows a refreshed payload', async () => {
    const server = startServer();
    server.profiles = {
      profiles: [makeProfile('alpha'), makeProfile('live-1', { mode: 'live' })],
      generated_at: '2024-01-01T00:00:00+00:00',
      wallet: { ...wallet },
      wallets: { paper: { ...wallet }, live: { ...liveWallet } },
    };
    renderLive(server);

    fireEvent.click(screen.getByRole('radio', { name: /Real trading/i }));
    await settle();
    expect(screen.getByRole('link', { name: 'live-1' })).toBeInTheDocument();

    // A cycle refreshes the payload: the selection is page state, so it stays.
    server.profiles = {
      profiles: [
        makeProfile('alpha', { equity: 11111 }),
        makeProfile('live-1', { mode: 'live', equity: 22222 }),
      ],
      generated_at: '2024-06-01T12:00:02+00:00',
      wallet: { ...wallet },
      wallets: { paper: { ...wallet }, live: { ...liveWallet, total_portfolio_value: 6000 } },
    };
    await advance(2000);

    expect(screen.getByRole('radio', { name: /Real trading/i })).toHaveAttribute(
      'aria-checked',
      'true',
    );
    expect(screen.getByRole('link', { name: 'live-1' })).toBeInTheDocument();
    expect(within(screen.getByTestId('wallet-tiles')).getByText('$6,000.00')).toBeInTheDocument();
  });
});
