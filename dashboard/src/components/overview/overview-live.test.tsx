import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type { HealthPayload, KillSwitchPayload, ProfilesPayload, ProfileSnapshot } from '@/lib/types';

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
    health: { ...health },
    profiles: {
      profiles: [makeProfile('alpha'), makeProfile('beta')],
      generated_at: '2024-01-01T00:00:00+00:00',
    },
    killSwitch: { ...killSwitch },
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
    return jsonResponse({ error: 'not found' }, 404);
  });

  vi.stubGlobal('fetch', server.fetchMock);
  return server;
}

function renderLive(server: StubServer) {
  return render(
    <OverviewLive
      initialProfiles={server.profiles}
      initialHealth={server.health}
      initialKillSwitch={server.killSwitch}
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
    expect(server.calls).toEqual(['/api/health', '/api/profiles', '/api/kill-switch']);

    await advance(2000);
    expect(server.calls).toHaveLength(6);
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

    const banner = screen.getByRole('status');
    expect(banner).toHaveTextContent('network error calling /api/health');
    // The last known good payload and its stamp stay on screen.
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();
    expect(screen.getAllByRole('article')).toHaveLength(2);
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-06-01 12:00:02 UTC');
  });

  it('stops polling when paused and refreshes again on resume', async () => {
    const server = startServer();
    renderLive(server);
    await advance(2000);
    expect(server.calls).toHaveLength(3);

    fireEvent.click(screen.getByRole('button', { name: 'Pause live updates' }));
    expect(screen.getByText('Live updates paused')).toBeInTheDocument();

    await advance(6000);
    expect(server.calls).toHaveLength(3);

    fireEvent.click(screen.getByRole('button', { name: 'Resume live updates' }));
    await settle();
    expect(server.calls).toHaveLength(6);

    await advance(2000);
    expect(server.calls).toHaveLength(9);
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

  it('renders the documented empty states', async () => {
    const server = startServer();
    server.profiles = { profiles: [], generated_at: null };
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
});
