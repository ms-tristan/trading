import { act, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { OPERATOR_TOKEN_STORAGE_KEY } from '@/lib/operator-token';
import type { ProfileSnapshot } from '@/lib/types';
import { CONTROL_NOTICE_TIMEOUT_MS } from '@/lib/use-profile-control';

import { ProfileActions } from './profile-actions';

/*
 * The island navigates with the App Router. The double is installed before the
 * component module is imported, so nothing in these tests ever reaches a router
 * (and no test ever needs a running Next.js server).
 */
const nav = vi.hoisted(() => ({ refresh: vi.fn(), push: vi.fn(), pathname: '/' }));

vi.mock('next/navigation', () => ({
  useRouter: () => ({ refresh: nav.refresh, push: nav.push }),
  usePathname: () => nav.pathname,
}));

// ---------------------------------------------------------------------------
// fixtures: exact payload shapes of the frozen API
// ---------------------------------------------------------------------------

const TOKEN = 'operator-secret-token';
const CONTROL_PATH = '/api/control';
const PAUSE_PATH = '/api/profiles/alpha/pause';
const RESUME_PATH = '/api/profiles/alpha/resume';
const DELETE_PATH = '/api/profiles/alpha';
const POLL_INTERVAL_MS = 2000;

function profileSnapshot(): ProfileSnapshot {
  return {
    profile_id: 'alpha',
    symbol: 'BTC/USDT',
    timeframe: '1h',
    strategy: 'basic',
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
}

function jsonResponse(body: unknown, status = 200): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
  } as unknown as Response;
}

interface RecordedCall {
  url: string;
  init?: RequestInit;
}

/** Response of a mutation request; `undefined` falls back to the happy path. */
type MutationHandler = (
  url: string,
  init?: RequestInit,
) => Response | Promise<Response> | undefined;

interface StubServer {
  /** Stubbed global `fetch`: no test ever reaches the network. */
  fetchMock: ReturnType<typeof vi.fn>;
  /** Every request, in call order. */
  calls: RecordedCall[];
  /** Pause state served by `GET /api/control`, and flipped by pause/resume. */
  paused: boolean;
  /** Whether the server accepts mutations (`mutable` of `/api/control`). */
  mutable: boolean;
  /** Per-test override of the mutation routes. */
  mutation: MutationHandler;
}

/** Install the network double of the whole file (see `api.test.ts`). */
function startServer(options: { paused?: boolean; mutable?: boolean } = {}): StubServer {
  const server: StubServer = {
    fetchMock: vi.fn(),
    calls: [],
    paused: options.paused ?? false,
    mutable: options.mutable ?? true,
    mutation: () => undefined,
  };

  server.fetchMock.mockImplementation(
    async (url: unknown, init?: RequestInit): Promise<Response> => {
      const target = String(url);
      const method = init?.method ?? 'GET';
      server.calls.push({ url: target, init });

      if (method === 'GET' && target === CONTROL_PATH) {
        return jsonResponse({
          engine_running: true,
          read_only: !server.mutable,
          mutable: server.mutable,
          profiles: [{ profile_id: 'alpha', paused: server.paused, running: true }],
        });
      }

      const override = await server.mutation(target, init);
      if (override !== undefined) {
        return override;
      }

      if (method === 'POST' && target === PAUSE_PATH) {
        server.paused = true;
        return jsonResponse({ profile: profileSnapshot(), paused: true });
      }
      if (method === 'POST' && target === RESUME_PATH) {
        server.paused = false;
        return jsonResponse({ profile: profileSnapshot(), paused: false });
      }
      if (method === 'DELETE' && target === DELETE_PATH) {
        return jsonResponse({ profile_id: 'alpha', deleted: true });
      }
      return jsonResponse({ error: `no stub for ${method} ${target}` }, 404);
    },
  );

  vi.stubGlobal('fetch', server.fetchMock);
  return server;
}

/** Every request that is not a poll. */
function mutationCalls(server: StubServer): RecordedCall[] {
  return server.calls.filter((call) => (call.init?.method ?? 'GET') !== 'GET');
}

function callsTo(server: StubServer, url: string): RecordedCall[] {
  return server.calls.filter((call) => call.url === url);
}

function headersOf(call: RecordedCall | undefined): Record<string, string> {
  return (call?.init?.headers ?? {}) as Record<string, string>;
}

function saveToken(token: string = TOKEN): void {
  window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, token);
}

/**
 * Let every pending promise of a fetch cycle settle while the fake clock stays
 * frozen (the polling timers must not fire by accident).
 */
async function settle(): Promise<void> {
  await act(async () => {
    for (let tick = 0; tick < 10; tick += 1) {
      await Promise.resolve();
    }
  });
}

/** Move the fake clock forward and let the polling cycle it triggers settle. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

function renderActions(): HTMLElement {
  const { container } = render(<ProfileActions profileId="alpha" />);
  const island = container.firstElementChild;
  if (!(island instanceof HTMLElement)) {
    throw new Error('the actions island rendered no root element');
  }
  return island;
}

/** Mount the island and wait for its first control poll. */
async function mountActions(): Promise<HTMLElement> {
  const island = renderActions();
  await settle();
  return island;
}

function button(name: string): HTMLElement {
  return screen.getByRole('button', { name });
}

let user: ReturnType<typeof userEvent.setup>;

beforeEach(() => {
  /*
   * `shouldAdvanceTime` is required next to the fake clock: React Testing
   * Library drains its microtask queue through a real `setTimeout(…, 0)` while
   * it wraps a user event, so a completely frozen clock would deadlock the
   * interaction. The clock still advances only by itself — a polling cycle
   * never fires unless a test asks for it.
   */
  vi.useFakeTimers({ shouldAdvanceTime: true });
  user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime, delay: null });
  window.sessionStorage.clear();
  nav.refresh.mockClear();
  nav.push.mockClear();
  nav.pathname = '/';
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.sessionStorage.clear();
});

// ---------------------------------------------------------------------------
// operator token
// ---------------------------------------------------------------------------

describe('operator token', () => {
  it('disables every action, asks for a token and sends no request without one', async () => {
    const server = startServer();
    await mountActions();

    expect(
      screen.getByText('Save an operator token to pause, resume or delete a profile.'),
    ).toBeInTheDocument();

    expect(button('Pause')).toBeDisabled();
    expect(button('Resume')).toBeDisabled();
    expect(button('Delete profile')).toBeDisabled();
    expect(button('Pause')).toHaveAttribute('aria-busy', 'false');

    await user.click(button('Pause'));
    await user.click(button('Delete profile'));
    await settle();

    // Nothing destructive happened: no dialog, no mutation request at all.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mutationCalls(server)).toHaveLength(0);
  });

  it('never renders the operator token it uses', async () => {
    saveToken();
    startServer();
    await mountActions();

    await user.click(button('Pause'));
    await settle();

    expect(document.body.textContent ?? '').not.toContain(TOKEN);
    expect(document.body.innerHTML).not.toContain(TOKEN);
  });
});

// ---------------------------------------------------------------------------
// pause and resume
// ---------------------------------------------------------------------------

describe('pause and resume', () => {
  it('offers exactly one of pause and resume, following /api/control', async () => {
    saveToken();
    const server = startServer();
    const island = await mountActions();

    expect(island).toHaveAttribute('data-paused', 'false');
    expect(button('Pause')).toBeEnabled();
    expect(button('Resume')).toBeDisabled();
    expect(screen.queryByText('Paused')).not.toBeInTheDocument();

    server.paused = true;
    await advance(POLL_INTERVAL_MS);

    expect(island).toHaveAttribute('data-paused', 'true');
    expect(button('Pause')).toBeDisabled();
    expect(button('Resume')).toBeEnabled();
    expect(screen.getByText('Paused').closest('span[data-tone]')).toHaveAttribute(
      'data-tone',
      'warn',
    );
    expect(
      screen.getByText('Not opening new positions; the open position stays managed.'),
    ).toBeInTheDocument();
  });

  it('pauses with the operator token, locks the surface while pending and confirms briefly', async () => {
    saveToken();
    const server = startServer();
    let releasePause: () => void = () => {};
    server.mutation = (target, init) => {
      if (init?.method === 'POST' && target === PAUSE_PATH) {
        return new Promise<Response>((resolve) => {
          releasePause = () => {
            resolve(jsonResponse({ profile: profileSnapshot(), paused: true }));
          };
        });
      }
      return undefined;
    };

    await mountActions();
    const pause = button('Pause');
    await user.click(pause);

    // In flight: one request, the whole surface locked, no double submit.
    const pauseCalls = callsTo(server, PAUSE_PATH);
    expect(pauseCalls).toHaveLength(1);
    expect(pauseCalls[0]?.init?.method).toBe('POST');
    expect(headersOf(pauseCalls[0])['X-Operator-Token']).toBe(TOKEN);

    expect(pause).toBeDisabled();
    expect(pause).toHaveAttribute('aria-busy', 'true');
    expect(button('Resume')).toBeDisabled();
    expect(button('Delete profile')).toBeDisabled();

    await user.click(pause);
    expect(callsTo(server, PAUSE_PATH)).toHaveLength(1);

    releasePause();
    await settle();

    expect(screen.getByText('Profile paused.')).toHaveAttribute('role', 'status');
    expect(button('Resume')).toBeEnabled();
    expect(button('Pause')).toBeDisabled();

    // The next poll agrees, and the notice is transient.
    server.paused = true;
    await advance(POLL_INTERVAL_MS);
    expect(button('Resume')).toBeEnabled();

    await advance(CONTROL_NOTICE_TIMEOUT_MS);
    expect(screen.queryByText('Profile paused.')).not.toBeInTheDocument();
  });

  it('resumes a paused profile and hands the control back to pause', async () => {
    saveToken();
    startServer({ paused: true });
    await mountActions();

    expect(button('Resume')).toBeEnabled();
    await user.click(button('Resume'));
    await settle();

    expect(screen.getByText('Profile resumed.')).toBeInTheDocument();
    expect(button('Pause')).toBeEnabled();
    expect(button('Resume')).toBeDisabled();
    expect(screen.queryByText('Paused')).not.toBeInTheDocument();
  });

  it('renders the server refusal verbatim and re-enables the actions', async () => {
    saveToken();
    const server = startServer();
    server.mutation = (target, init) =>
      init?.method === 'POST' && target === PAUSE_PATH
        ? jsonResponse({ error: 'read-only mode: mutations are disabled' }, 403)
        : undefined;

    await mountActions();
    await user.click(button('Pause'));
    await settle();

    // The headline is the mapped copy; the server refusal stays verbatim below.
    expect(screen.getByText('The monitoring API refused the request')).toBeInTheDocument();
    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 403');
    expect(detail).toHaveTextContent('read-only mode: mutations are disabled');
    expect(button('Pause')).toBeEnabled();
    expect(button('Delete profile')).toBeEnabled();
  });

  it('reports a network failure explicitly and stays usable', async () => {
    saveToken();
    const server = startServer({ paused: true });
    server.mutation = (target, init) => {
      if (init?.method === 'POST' && target === RESUME_PATH) {
        throw new TypeError('Failed to fetch');
      }
      return undefined;
    };

    await mountActions();
    await user.click(button('Resume'));
    await settle();

    expect(screen.getByText('The monitoring API is unreachable')).toBeInTheDocument();
    expect(screen.getByTestId('error-banner-detail')).toHaveTextContent(
      `network error calling ${RESUME_PATH}: Failed to fetch`,
    );
    expect(button('Resume')).toBeEnabled();
    expect(nav.refresh).not.toHaveBeenCalled();
  });

  it('reads a proxy 502 on a lifecycle action as the monitoring API being unreachable', async () => {
    saveToken();
    const server = startServer();
    server.mutation = (target, init) =>
      init?.method === 'POST' && target === PAUSE_PATH
        ? jsonResponse({ error: 'Bad Gateway' }, 502)
        : undefined;

    await mountActions();
    await user.click(button('Pause'));
    await settle();

    // The raw status is the detail of the failure, never its headline.
    expect(screen.getByText('The monitoring API is unreachable')).toBeInTheDocument();
    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 502');
    expect(detail).toHaveTextContent('Bad Gateway');
    expect(screen.queryByText('HTTP 502')).not.toBeInTheDocument();

    // The action stays usable: the operator can simply press it again.
    expect(button('Pause')).toBeEnabled();
    expect(button('Delete profile')).toBeEnabled();
  });
});

// ---------------------------------------------------------------------------
// delete
// ---------------------------------------------------------------------------

describe('delete', () => {
  it('opens the confirmation on the first click and sends DELETE only on confirm', async () => {
    saveToken();
    const server = startServer();
    await mountActions();

    const trigger = button('Delete profile');
    await user.click(trigger);

    // The first click is never destructive.
    expect(mutationCalls(server)).toHaveLength(0);
    const dialog = screen.getByRole('dialog');
    expect(dialog).toHaveAccessibleName('Delete profile alpha?');
    expect(dialog).toHaveTextContent('Every open order will be cancelled');

    await user.click(within(dialog).getByRole('button', { name: 'Delete profile' }));
    await settle();

    const deleteCalls = server.calls.filter((call) => call.init?.method === 'DELETE');
    expect(deleteCalls).toHaveLength(1);
    expect(deleteCalls[0]?.url).toBe(DELETE_PATH);
    expect(headersOf(deleteCalls[0])['X-Operator-Token']).toBe(TOKEN);

    expect(nav.refresh).toHaveBeenCalledTimes(1);
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('closes the confirmation without any request when the operator cancels', async () => {
    saveToken();
    const server = startServer();
    await mountActions();

    const trigger = button('Delete profile');
    await user.click(trigger);
    expect(screen.getByRole('dialog')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(mutationCalls(server)).toHaveLength(0);
    expect(nav.refresh).not.toHaveBeenCalled();
    // The profile keeps its whole action surface, and the trigger is back.
    expect(trigger).toBeEnabled();
    expect(trigger).toHaveFocus();
  });

  it('locks the confirmation while the delete is in flight', async () => {
    saveToken();
    const server = startServer();
    let releaseDelete: () => void = () => {};
    server.mutation = (target, init) => {
      if (init?.method === 'DELETE' && target === DELETE_PATH) {
        return new Promise<Response>((resolve) => {
          releaseDelete = () => {
            resolve(jsonResponse({ profile_id: 'alpha', deleted: true }));
          };
        });
      }
      return undefined;
    };

    await mountActions();
    const trigger = button('Delete profile');
    await user.click(trigger);
    const dialog = screen.getByRole('dialog');
    const confirm = within(dialog).getByRole('button', { name: 'Delete profile' });

    await user.click(confirm);

    expect(dialog).toHaveAttribute('aria-busy', 'true');
    expect(confirm).toBeDisabled();
    expect(confirm).toHaveAttribute('aria-busy', 'true');
    expect(trigger).toBeDisabled();

    releaseDelete();
    await settle();

    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(nav.refresh).toHaveBeenCalledTimes(1);
  });

  it('leaves the deleted detail route for the overview', async () => {
    saveToken();
    nav.pathname = '/profiles/alpha';
    startServer();
    await mountActions();

    await user.click(button('Delete profile'));
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete profile' }),
    );
    await settle();

    expect(nav.refresh).toHaveBeenCalledTimes(1);
    expect(nav.push).toHaveBeenCalledWith('/');
  });

  it('keeps the dialog open, the error readable and the profile in place when the delete fails', async () => {
    saveToken();
    const server = startServer();
    server.mutation = (target, init) =>
      init?.method === 'DELETE' && target === DELETE_PATH
        ? jsonResponse({ error: 'cannot flatten the open position' }, 409)
        : undefined;

    await mountActions();
    const trigger = button('Delete profile');
    await user.click(trigger);
    await user.click(
      within(screen.getByRole('dialog')).getByRole('button', { name: 'Delete profile' }),
    );
    await settle();

    const dialog = screen.getByRole('dialog');
    expect(dialog).toBeInTheDocument();
    // The failure is rendered once, inside the dialog that caused it: the
    // island banner stays silent while the dialog is open.
    expect(screen.getAllByText('The monitoring API answered an error')).toHaveLength(1);
    expect(within(dialog).getByText('The monitoring API answered an error')).toBeInTheDocument();
    // The server text is not lost: it is the raw detail of the failure.
    const detail = within(dialog).getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 409');
    expect(detail).toHaveTextContent('cannot flatten the open position');
    expect(within(dialog).getByRole('button', { name: 'Delete profile' })).toBeEnabled();
    expect(nav.refresh).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
    // The profile is still there, with its whole action surface.
    expect(button('Pause')).toBeEnabled();
    expect(trigger).toBeEnabled();
  });
});
