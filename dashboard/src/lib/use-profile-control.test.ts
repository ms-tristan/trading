import { act, renderHook, waitFor, type RenderHookResult } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError, OPERATOR_TOKEN_HEADER } from './api';
import { OPERATOR_TOKEN_STORAGE_KEY } from './operator-token';
import type { ControlPayload, ProfileSnapshot } from './types';
import {
  CONTROL_NOTICE_TIMEOUT_MS,
  NO_OPERATOR_TOKEN_MESSAGE,
  useProfileControl,
  type UseProfileControlOptions,
  type UseProfileControlResult,
} from './use-profile-control';

// ---------------------------------------------------------------------------
// fixtures and fetch doubles (no live server, no bound port)
// ---------------------------------------------------------------------------

const OPERATOR_TOKEN = 'operator-secret-token';

const profile: ProfileSnapshot = {
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

function controlPayload(
  overrides: Partial<ControlPayload> & { paused?: boolean; running?: boolean } = {},
): ControlPayload {
  const { paused = false, running = true, ...rest } = overrides;
  return {
    engine_running: true,
    read_only: false,
    mutable: true,
    profiles: [{ profile_id: 'alpha', paused, running }],
    ...rest,
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
  init: RequestInit | undefined;
}

type RouteHandler = (url: string, init?: RequestInit) => Response | Promise<Response>;

function routeFetch(handler: RouteHandler): { impl: typeof fetch; calls: RecordedCall[] } {
  const calls: RecordedCall[] = [];
  const impl = (async (url: unknown, init?: RequestInit) => {
    calls.push({ url: String(url), init });
    return handler(String(url), init);
  }) as unknown as typeof fetch;
  return { impl, calls };
}

function callsTo(calls: RecordedCall[], url: string): RecordedCall[] {
  return calls.filter((call) => call.url === url);
}

function headersOf(call: RecordedCall | undefined): Record<string, string> {
  return (call?.init?.headers ?? {}) as Record<string, string>;
}

function saveToken(token: string = OPERATOR_TOKEN): void {
  window.sessionStorage.setItem(OPERATOR_TOKEN_STORAGE_KEY, token);
}

/**
 * Mount the hook and let the microtask queue of the first poll settle.
 *
 * This variant never waits on wall-clock timers, so it is safe with Vitest's
 * fake timers; use {@link renderControl} when the test needs the settled state.
 */
async function mountControl(
  profileId = 'alpha',
  options: UseProfileControlOptions = {},
): Promise<RenderHookResult<UseProfileControlResult, unknown>> {
  let view!: RenderHookResult<UseProfileControlResult, unknown>;
  await act(async () => {
    view = renderHook(() => useProfileControl(profileId, options));
  });
  return view;
}

/**
 * Mount the hook and let the first poll settle.
 *
 * `settled: false` is used by the tests that mount a fetch which never answers.
 */
async function renderControl(
  profileId = 'alpha',
  options: UseProfileControlOptions = {},
  settled = true,
): Promise<RenderHookResult<UseProfileControlResult, unknown>> {
  const view = await mountControl(profileId, options);
  if (settled) {
    await waitFor(() => {
      expect(view.result.current.known).toBe(true);
    });
  }
  return view;
}

beforeEach(() => {
  window.sessionStorage.clear();
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// polling and derived flags
// ---------------------------------------------------------------------------

describe('control polling', () => {
  it('reads /api/control on mount and exposes the profile state', async () => {
    saveToken();
    const { impl, calls } = routeFetch(() => jsonResponse(controlPayload()));

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    expect(calls).toHaveLength(1);
    expect(calls[0]?.url).toBe('/api/control');
    expect(calls[0]?.init?.method).toBe('GET');
    expect(calls[0]?.init?.cache).toBe('no-store');

    expect(result.current.known).toBe(true);
    expect(result.current.mutable).toBe(true);
    expect(result.current.engineRunning).toBe(true);
    expect(result.current.paused).toBe(false);
    expect(result.current.canPause).toBe(true);
    expect(result.current.canResume).toBe(false);
    expect(result.current.canDelete).toBe(true);
    expect(result.current.pendingAction).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.notice).toBeNull();
  });

  it('keeps every action disabled while the state is unknown', async () => {
    saveToken();
    const { impl, calls } = routeFetch(() => jsonResponse(controlPayload()));

    const { result } = await renderControl('alpha', { fetchImpl: impl, enabled: false }, false);

    expect(calls).toHaveLength(0);
    expect(result.current.known).toBe(false);
    expect(result.current.canPause).toBe(false);
    expect(result.current.canResume).toBe(false);
    expect(result.current.canDelete).toBe(false);
  });

  it('reports a failed first poll and stays unknown', async () => {
    saveToken();
    const impl = (async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;

    const { result } = await renderControl('alpha', { fetchImpl: impl }, false);

    await waitFor(() => {
      expect(result.current.error).toContain('network error calling /api/control');
    });
    expect(result.current.known).toBe(false);
    expect(result.current.canPause).toBe(false);
    expect(result.current.canDelete).toBe(false);
  });

  it('polls again on the configured cadence', async () => {
    vi.useFakeTimers();
    saveToken();
    const { impl, calls } = routeFetch(() => jsonResponse(controlPayload()));

    const view = await mountControl('alpha', { fetchImpl: impl, refreshMs: 1000 });
    expect(view.result.current.known).toBe(true);
    expect(callsTo(calls, '/api/control')).toHaveLength(1);

    await act(async () => {
      vi.advanceTimersByTime(1000);
    });

    expect(callsTo(calls, '/api/control')).toHaveLength(2);
  });

  it('refreshes on demand and keeps the last known state when a poll fails', async () => {
    saveToken();
    let fail = false;
    const { impl } = routeFetch(() =>
      fail ? jsonResponse({ error: 'engine unavailable' }, 503) : jsonResponse(controlPayload()),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });
    expect(result.current.known).toBe(true);

    fail = true;
    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.error).toBe('engine unavailable');
    expect(result.current.known).toBe(true);
    expect(result.current.paused).toBe(false);
  });

  it('never polls nor updates state once unmounted, and aborts the request in flight', async () => {
    vi.useFakeTimers();
    saveToken();
    const signals: Array<AbortSignal | undefined> = [];
    const impl = ((_url: unknown, init?: RequestInit) => {
      signals.push(init?.signal ?? undefined);
      return new Promise<Response>(() => {
        // Never answers: the request stays in flight until it is aborted.
      });
    }) as unknown as typeof fetch;

    const view = await renderControl('alpha', { fetchImpl: impl, refreshMs: 1000 }, false);
    expect(signals).toHaveLength(1);
    expect(signals[0]?.aborted).toBe(false);

    act(() => {
      view.unmount();
    });

    expect(signals[0]?.aborted).toBe(true);

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(signals).toHaveLength(1);
  });

  it('ignores a control answer that arrives after unmount', async () => {
    saveToken();
    let answer: ((response: Response) => void) | undefined;
    const impl = (() =>
      new Promise<Response>((resolve) => {
        answer = resolve;
      })) as unknown as typeof fetch;

    const view = await renderControl('alpha', { fetchImpl: impl }, false);
    act(() => {
      view.unmount();
    });

    await act(async () => {
      answer?.(jsonResponse(controlPayload()));
      await Promise.resolve();
    });

    expect(view.result.current.known).toBe(false);
    expect(view.result.current.error).toBeNull();
  });

  it('flips to the resume affordance for a paused profile', async () => {
    saveToken();
    const { impl } = routeFetch(() => jsonResponse(controlPayload({ paused: true })));

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    expect(result.current.paused).toBe(true);
    expect(result.current.canPause).toBe(false);
    expect(result.current.canResume).toBe(true);
    expect(result.current.canDelete).toBe(true);
  });

  it('reads the control state of the requested profile only', async () => {
    saveToken();
    const payload: ControlPayload = {
      engine_running: true,
      read_only: false,
      mutable: true,
      profiles: [
        { profile_id: 'beta', paused: true, running: false },
        { profile_id: 'alpha', paused: false, running: true },
      ],
    };
    const { impl } = routeFetch(() => jsonResponse(payload));

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    expect(result.current.paused).toBe(false);
    expect(result.current.canPause).toBe(true);
  });

  it('disables every action when the server refuses mutations', async () => {
    saveToken();
    const { impl } = routeFetch(() =>
      jsonResponse(controlPayload({ read_only: true, mutable: false })),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    expect(result.current.known).toBe(true);
    expect(result.current.mutable).toBe(false);
    expect(result.current.canPause).toBe(false);
    expect(result.current.canResume).toBe(false);
    expect(result.current.canDelete).toBe(false);
  });

  it('disables every action when no operator token is stored', async () => {
    const { impl } = routeFetch(() => jsonResponse(controlPayload()));

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    expect(result.current.known).toBe(true);
    expect(result.current.mutable).toBe(true);
    expect(result.current.canPause).toBe(false);
    expect(result.current.canDelete).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// mutations
// ---------------------------------------------------------------------------

describe('profile mutations', () => {
  it('pauses the profile with the documented request and reports the new state', async () => {
    saveToken();
    const { impl, calls } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload())
        : jsonResponse({ profile, paused: true }),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });
    expect(result.current.canPause).toBe(true);

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.pause();
    });

    expect(outcome).toBe(true);
    expect(result.current.notice).toBe('Profile paused.');
    expect(result.current.error).toBeNull();
    expect(result.current.pendingAction).toBeNull();
    expect(result.current.paused).toBe(true);
    expect(result.current.canResume).toBe(true);

    const pauseCall = callsTo(calls, '/api/profiles/alpha/pause')[0];
    expect(pauseCall?.init?.method).toBe('POST');
    expect(headersOf(pauseCall)['Content-Type']).toBe('application/json');
    expect(headersOf(pauseCall)[OPERATOR_TOKEN_HEADER]).toBe(OPERATOR_TOKEN);
    expect(pauseCall?.init?.body).toBe('{}');
  });

  it('resumes a paused profile', async () => {
    saveToken();
    const { impl, calls } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload({ paused: true }))
        : jsonResponse({ profile, paused: false }),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });
    expect(result.current.canResume).toBe(true);

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.resume();
    });

    expect(outcome).toBe(true);
    expect(result.current.notice).toBe('Profile resumed.');
    expect(result.current.paused).toBe(false);
    expect(result.current.canPause).toBe(true);

    const resumeCall = callsTo(calls, '/api/profiles/alpha/resume')[0];
    expect(resumeCall?.init?.method).toBe('POST');
    expect(headersOf(resumeCall)[OPERATOR_TOKEN_HEADER]).toBe(OPERATOR_TOKEN);
  });

  it('deletes the profile, notifies the caller and refreshes the control state', async () => {
    saveToken();
    const onDeleted = vi.fn();
    const { impl, calls } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload())
        : jsonResponse({ profile_id: 'alpha', deleted: true }),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl, onDeleted });
    expect(callsTo(calls, '/api/control')).toHaveLength(1);

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.remove();
    });

    expect(outcome).toBe(true);
    expect(result.current.notice).toBe('Profile deleted.');
    expect(onDeleted).toHaveBeenCalledTimes(1);
    expect(onDeleted).toHaveBeenCalledWith('alpha');

    const deleteCall = callsTo(calls, '/api/profiles/alpha')[0];
    expect(deleteCall?.init?.method).toBe('DELETE');
    expect(headersOf(deleteCall)[OPERATOR_TOKEN_HEADER]).toBe(OPERATOR_TOKEN);
    expect(deleteCall?.init?.body).toBeUndefined();

    // The mount poll plus the refresh that follows the deletion.
    expect(callsTo(calls, '/api/control')).toHaveLength(2);
  });

  it('exposes the pending action while a mutation is in flight', async () => {
    saveToken();
    let release: (() => void) | undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    const { impl } = routeFetch((url) => {
      if (url === '/api/control') {
        return jsonResponse(controlPayload());
      }
      return gate.then(() => jsonResponse({ profile, paused: true }));
    });

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    let pending: Promise<boolean> | undefined;
    act(() => {
      pending = result.current.pause();
    });

    await waitFor(() => {
      expect(result.current.pendingAction).toBe('pause');
    });
    expect(result.current.canPause).toBe(false);
    expect(result.current.canDelete).toBe(false);

    await act(async () => {
      release?.();
      await pending;
    });

    expect(result.current.pendingAction).toBeNull();
    expect(result.current.notice).toBe('Profile paused.');
  });

  it('clears the success notice after the documented timeout', async () => {
    vi.useFakeTimers();
    saveToken();
    const { impl } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload())
        : jsonResponse({ profile, paused: true }),
    );

    const view = await mountControl('alpha', { fetchImpl: impl, refreshMs: 1000 });
    expect(view.result.current.known).toBe(true);

    await act(async () => {
      await view.result.current.pause();
    });
    expect(view.result.current.notice).toBe('Profile paused.');

    act(() => {
      vi.advanceTimersByTime(CONTROL_NOTICE_TIMEOUT_MS - 1);
    });
    expect(view.result.current.notice).toBe('Profile paused.');

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(view.result.current.notice).toBeNull();

    act(() => {
      view.unmount();
    });
    // The notice timer was cleared on unmount: advancing time is a no-op.
    act(() => {
      vi.advanceTimersByTime(CONTROL_NOTICE_TIMEOUT_MS * 2);
    });
  });

  it('surfaces the server refusal verbatim and returns false', async () => {
    saveToken();
    const { impl } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload())
        : jsonResponse({ error: 'missing or invalid operator token' }, 403),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.pause();
    });

    expect(outcome).toBe(false);
    expect(result.current.error).toBe('missing or invalid operator token');
    expect(result.current.notice).toBeNull();
    expect(result.current.pendingAction).toBeNull();
  });

  it('keeps a refused action readable across a successful poll', async () => {
    saveToken();
    const { impl } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload())
        : jsonResponse({ error: 'missing or invalid operator token' }, 403),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    await act(async () => {
      await result.current.pause();
    });
    expect(result.current.error).toBe('missing or invalid operator token');

    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.error).toBe('missing or invalid operator token');
  });

  it('aborts an in-flight mutation on unmount', async () => {
    saveToken();
    const mutationSignals: Array<AbortSignal | undefined> = [];
    const impl = ((url: unknown, init?: RequestInit) => {
      if (String(url) === '/api/control') {
        return Promise.resolve(jsonResponse(controlPayload()));
      }
      mutationSignals.push(init?.signal ?? undefined);
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          reject(new DOMException('The operation was aborted.', 'AbortError'));
        });
      });
    }) as unknown as typeof fetch;

    const view = await mountControl('alpha', { fetchImpl: impl });
    expect(view.result.current.canPause).toBe(true);

    let pending: Promise<boolean> | undefined;
    act(() => {
      pending = view.result.current.pause();
    });
    expect(mutationSignals).toHaveLength(1);
    expect(mutationSignals[0]?.aborted).toBe(false);

    act(() => {
      view.unmount();
    });

    expect(mutationSignals[0]?.aborted).toBe(true);
    await act(async () => {
      await expect(pending).resolves.toBe(false);
    });
  });

  it('surfaces a conflict verbatim when a deletion is refused', async () => {
    saveToken();
    const { impl } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload())
        : jsonResponse({ error: 'profile is still exposed: flat failed' }, 409),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.remove();
    });

    expect(outcome).toBe(false);
    expect(result.current.error).toBe('profile is still exposed: flat failed');
  });

  it('surfaces a network failure and returns false', async () => {
    saveToken();
    const impl = (async (url: unknown) => {
      if (String(url) === '/api/control') {
        return jsonResponse(controlPayload());
      }
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;

    const { result } = await renderControl('alpha', { fetchImpl: impl });

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.resume();
    });

    expect(outcome).toBe(false);
    expect(result.current.error).toContain('network error calling /api/profiles/alpha/resume');
    expect(result.current.notice).toBeNull();
  });

  it('fails without any request when no operator token is stored', async () => {
    const { impl, calls } = routeFetch(() => jsonResponse(controlPayload()));

    const { result } = await renderControl('alpha', { fetchImpl: impl, enabled: false }, false);
    expect(calls).toHaveLength(0);

    const outcomes: boolean[] = [];
    await act(async () => {
      outcomes.push(await result.current.pause());
      outcomes.push(await result.current.resume());
      outcomes.push(await result.current.remove());
    });

    expect(outcomes).toEqual([false, false, false]);
    expect(result.current.error).toBe(NO_OPERATOR_TOKEN_MESSAGE);
    expect(calls).toHaveLength(0);
  });

  it('does not send a mutation request when the token disappeared since the last poll', async () => {
    saveToken();
    const { impl, calls } = routeFetch(() => jsonResponse(controlPayload({ paused: true })));

    const { result } = await renderControl('alpha', { fetchImpl: impl });
    expect(result.current.canResume).toBe(true);

    window.sessionStorage.clear();

    let outcome: boolean | undefined;
    await act(async () => {
      outcome = await result.current.resume();
    });

    expect(outcome).toBe(false);
    expect(result.current.error).toBe(NO_OPERATOR_TOKEN_MESSAGE);
    expect(callsTo(calls, '/api/profiles/alpha/resume')).toHaveLength(0);
  });

  it('forwards the base URL of a Server Component', async () => {
    saveToken();
    const { impl, calls } = routeFetch((url) =>
      url.endsWith('/api/control') ? jsonResponse(controlPayload()) : jsonResponse({ profile, paused: true }),
    );

    const { result } = await renderControl('alpha', {
      fetchImpl: impl,
      baseUrl: 'http://trading-realtime:8080/',
    });

    await act(async () => {
      await result.current.pause();
    });

    expect(calls[0]?.url).toBe('http://trading-realtime:8080/api/control');
    expect(callsTo(calls, 'http://trading-realtime:8080/api/profiles/alpha/pause')).toHaveLength(1);
  });
});

// ---------------------------------------------------------------------------
// failure passthrough (the input of the operator-facing mapping)
// ---------------------------------------------------------------------------

describe('failure passthrough', () => {
  it('exposes the thrown value of a failed mutation and clears it on the next success', async () => {
    saveToken();
    let refuse = true;
    const { impl } = routeFetch((url) =>
      url === '/api/control'
        ? jsonResponse(controlPayload())
        : refuse
          ? jsonResponse({ error: 'read-only mode: mutations are disabled' }, 403)
          : jsonResponse({ profile, paused: true }),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });
    expect(result.current.failure).toBeNull();

    await act(async () => {
      await result.current.pause();
    });

    // `error` keeps its message, `failure` keeps the whole failure: a view maps
    // the second one and never prints the first one raw.
    expect(result.current.error).toBe('read-only mode: mutations are disabled');
    expect(result.current.failure).toBeInstanceOf(ApiError);
    const refused = result.current.failure as ApiError;
    expect(refused.kind).toBe('http');
    expect(refused.status).toBe(403);
    expect(refused.path).toBe('/api/profiles/alpha/pause');

    refuse = false;
    await act(async () => {
      await result.current.pause();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.failure).toBeNull();
  });

  it('exposes the thrown value of a failed poll and clears it once a poll succeeds', async () => {
    saveToken();
    let failing = false;
    const { impl } = routeFetch(() =>
      failing ? jsonResponse({ error: 'bad gateway' }, 502) : jsonResponse(controlPayload()),
    );

    const { result } = await renderControl('alpha', { fetchImpl: impl });
    expect(result.current.failure).toBeNull();

    failing = true;
    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.error).toBe('bad gateway');
    expect(result.current.failure).toBeInstanceOf(ApiError);
    expect((result.current.failure as ApiError).status).toBe(502);
    // The last known state stays on screen: only the message changed.
    expect(result.current.known).toBe(true);

    failing = false;
    await act(async () => {
      await result.current.refresh();
    });

    expect(result.current.error).toBeNull();
    expect(result.current.failure).toBeNull();
    expect(result.current.known).toBe(true);
  });

  it('never rejects: a failed poll and a failed mutation both resolve', async () => {
    saveToken();
    const impl = (async () => {
      throw new TypeError('fetch failed');
    }) as unknown as typeof fetch;

    const view = await renderControl('alpha', { fetchImpl: impl }, false);

    await act(async () => {
      await expect(view.result.current.refresh()).resolves.toBeUndefined();
      await expect(view.result.current.pause()).resolves.toBe(false);
      await expect(view.result.current.resume()).resolves.toBe(false);
      await expect(view.result.current.remove()).resolves.toBe(false);
    });

    expect(view.result.current.error).toContain('network error calling');
    expect(view.result.current.failure).toBeInstanceOf(ApiError);
  });
});
