'use client';

/**
 * Pause / resume / delete control surface of one profile.
 *
 * The hook is the single source of truth for the lifecycle state of a profile:
 * it polls `GET /api/control` (the read-only route that also answers in
 * `realtime serve` mode) and derives every affordance from that answer plus the
 * operator token stored in `sessionStorage`:
 *
 * * `known` stays `false` until the first successful answer, and **every** `can*`
 *   flag is `false` while the state is unknown or the server refuses mutations —
 *   the dashboard never offers an action it cannot perform;
 * * a paused profile can be resumed, a running one paused, and delete requires a
 *   known, mutable profile plus a stored token;
 * * each mutation re-reads the operator token from `sessionStorage` (it may have
 *   been saved in another tab panel), sets `pendingAction` while the request is
 *   in flight and reports the outcome through `notice`/`error`;
 * * `notice` is transient ({@link CONTROL_NOTICE_TIMEOUT_MS}) and `error` is the
 *   server's own message, never a guess; `failure` keeps the value that was
 *   thrown, so the caller can render the mapped operator-facing copy;
 * * no rejection ever escapes the hook, and unmounting aborts the in-flight poll
 *   and mutation requests and clears their timers.
 */

import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react';

import { deleteProfile, errorMessage, fetchControl, pauseProfile, resumeProfile } from './api';
import { DEFAULT_POLL_INTERVAL_MS } from './config';
import { readOperatorToken } from './operator-token';
import type { ControlPayload, LifecyclePayload } from './types';

/** How long a success notice stays on screen, in milliseconds. */
export const CONTROL_NOTICE_TIMEOUT_MS = 4000;

/** Error shown when a mutation is attempted without a stored operator token. */
export const NO_OPERATOR_TOKEN_MESSAGE = 'No operator token saved in this tab.';

/** Notice shown after a successful pause. */
export const PROFILE_PAUSED_NOTICE = 'Profile paused.';

/** Notice shown after a successful resume. */
export const PROFILE_RESUMED_NOTICE = 'Profile resumed.';

/** Notice shown after a successful delete. */
export const PROFILE_DELETED_NOTICE = 'Profile deleted.';

/** Mutation currently in flight, if any. */
export type ProfileControlAction = 'pause' | 'resume' | 'delete';

/** Control state of the profile, as the last successful poll reported it. */
interface ControlState {
  /** Whether the first answer has been received. */
  known: boolean;
  /** Whether the profile stops opening new positions. */
  paused: boolean;
  /** Whether the server accepts mutations (`mutable` of `/api/control`). */
  mutable: boolean;
  /** Whether the engine currently runs. */
  engineRunning: boolean;
}

/** Options of {@link useProfileControl}. */
export interface UseProfileControlOptions {
  /** Absolute origin used by Server Components; empty means same origin. */
  baseUrl?: string;
  /** Fetch implementation seam for tests. Defaults to the global `fetch`. */
  fetchImpl?: typeof fetch;
  /** Control polling cadence in milliseconds (defaults to the live cadence). */
  refreshMs?: number;
  /** When `false`, no control request is ever sent (the state stays unknown). */
  enabled?: boolean;
  /** Called after a successful delete, before the control state is refreshed. */
  onDeleted?: (profileId: string) => void;
}

/** State and controls returned by {@link useProfileControl}. */
export interface UseProfileControlResult {
  /** Whether the profile currently stops opening new positions. */
  paused: boolean;
  /** Whether the server accepts mutations. */
  mutable: boolean;
  /** Whether the engine currently runs. */
  engineRunning: boolean;
  /** Whether the control state has been received at least once. */
  known: boolean;
  /** Mutation in flight, or `null`. Used to disable the buttons. */
  pendingAction: ProfileControlAction | null;
  /** Message of the last failure, or `null`. */
  error: string | null;
  /**
   * The value the last failure threw — a poll failure or a mutation failure —
   * or `null`.
   *
   * `error` keeps its message, and this keeps the original failure, so a view
   * can hand it to `failureReport` and show the operator-facing headline plus
   * the raw underlying detail instead of printing a bare status.
   */
  failure: unknown | null;
  /** Transient success message, or `null`. */
  notice: string | null;
  /** Whether pausing is currently possible. */
  canPause: boolean;
  /** Whether resuming is currently possible. */
  canResume: boolean;
  /** Whether deleting is currently possible. */
  canDelete: boolean;
  /** Pause the profile. Returns `true` on success. Never rejects. */
  pause(): Promise<boolean>;
  /** Resume the profile. Returns `true` on success. Never rejects. */
  resume(): Promise<boolean>;
  /** Delete the profile (the server flattens it first). Never rejects. */
  remove(): Promise<boolean>;
  /** Run one control poll now. Never rejects. */
  refresh(): Promise<void>;
}

const UNKNOWN_CONTROL: ControlState = {
  known: false,
  paused: false,
  mutable: false,
  engineRunning: false,
};

/*
 * `sessionStorage` is a browser-only external store, read through
 * `useSyncExternalStore` (the same pattern as the kill-switch panel): the server
 * snapshot is always `false`, so the flags derived from the token never cause a
 * hydration mismatch and never need a `setState` inside an effect. Every poll
 * and every mutation re-reads the store, so a token saved in another panel is
 * picked up on the next render.
 */
const tokenListeners = new Set<() => void>();

function subscribeToken(listener: () => void): () => void {
  tokenListeners.add(listener);
  return () => {
    tokenListeners.delete(listener);
  };
}

function readTokenSnapshot(): boolean {
  return readOperatorToken() !== null;
}

function readTokenServerSnapshot(): boolean {
  return false;
}

function notifyTokenChange(): void {
  for (const listener of tokenListeners) {
    listener();
  }
}

/**
 * Track and drive the lifecycle of `profileId`.
 *
 * Every state read of the profile (paused, mutable, engine running) comes from
 * this hook: the components never re-implement the control logic, they render
 * the flags and call the actions.
 */
export function useProfileControl(
  profileId: string,
  options: UseProfileControlOptions = {},
): UseProfileControlResult {
  const refreshMs =
    typeof options.refreshMs === 'number' &&
    Number.isFinite(options.refreshMs) &&
    options.refreshMs > 0
      ? options.refreshMs
      : DEFAULT_POLL_INTERVAL_MS;
  const enabled = options.enabled ?? true;

  const [control, setControl] = useState<ControlState>(UNKNOWN_CONTROL);
  const hasToken = useSyncExternalStore(
    subscribeToken,
    readTokenSnapshot,
    readTokenServerSnapshot,
  );
  const [pendingAction, setPendingAction] = useState<ProfileControlAction | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const optionsRef = useRef(options);
  const disposedRef = useRef(false);
  const pollControllerRef = useRef<AbortController | null>(null);
  const mutationControllerRef = useRef<AbortController | null>(null);
  const noticeTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  /** Whether the message on screen comes from a mutation (it outlives a poll). */
  const mutationErrorRef = useRef(false);

  // The options object is usually an inline literal: keeping it in a ref means a
  // new identity never restarts the timer nor invalidates the actions.
  useEffect(() => {
    optionsRef.current = options;
  });

  const clearNoticeTimer = useCallback((): void => {
    if (noticeTimerRef.current !== null) {
      clearTimeout(noticeTimerRef.current);
      noticeTimerRef.current = null;
    }
  }, []);

  const showNotice = useCallback(
    (message: string): void => {
      clearNoticeTimer();
      setNotice(message);
      noticeTimerRef.current = setTimeout(() => {
        noticeTimerRef.current = null;
        setNotice(null);
      }, CONTROL_NOTICE_TIMEOUT_MS);
    },
    [clearNoticeTimer],
  );

  /** Adopt a control payload: the profile entry wins, the flags come from it. */
  const applyControl = useCallback(
    (payload: ControlPayload): void => {
      const entry = payload.profiles.find((profile) => profile.profile_id === profileId);
      setControl({
        known: true,
        paused: entry?.paused === true,
        mutable: payload.mutable === true,
        engineRunning: payload.engine_running === true,
      });
      if (!mutationErrorRef.current) {
        setError(null);
        // The last failure is gone with the message that reported it: a view
        // that renders the mapped report must not keep showing a stale outage.
        setFailure(null);
      }
    },
    [profileId],
  );

  const runCycle = useCallback(async (): Promise<void> => {
    // A slow poll must never race the next one.
    pollControllerRef.current?.abort();
    const controller = new AbortController();
    pollControllerRef.current = controller;

    try {
      const payload = await fetchControl({
        baseUrl: optionsRef.current.baseUrl,
        fetchImpl: optionsRef.current.fetchImpl,
        signal: controller.signal,
      });
      if (disposedRef.current || controller.signal.aborted) {
        return;
      }
      applyControl(payload);
    } catch (thrown) {
      if (disposedRef.current || controller.signal.aborted) {
        return;
      }
      // The last known state stays on screen; only the message changes.
      mutationErrorRef.current = false;
      setError(errorMessage(thrown));
      setFailure(thrown);
    }
  }, [applyControl]);

  const refresh = useCallback(async (): Promise<void> => {
    await runCycle();
  }, [runCycle]);

  useEffect(() => {
    disposedRef.current = false;

    if (!enabled) {
      return () => {
        disposedRef.current = true;
        pollControllerRef.current?.abort();
      };
    }

    void runCycle();
    const timer = setInterval(() => {
      void runCycle();
    }, refreshMs);

    return () => {
      disposedRef.current = true;
      clearInterval(timer);
      pollControllerRef.current?.abort();
    };
  }, [enabled, refreshMs, runCycle]);

  useEffect(
    () => () => {
      clearNoticeTimer();
      mutationControllerRef.current?.abort();
    },
    [clearNoticeTimer],
  );

  /**
   * Run one authenticated mutation.
   *
   * The token is re-read right before the request: without one the call fails
   * immediately with an explicit message and **no** network traffic.
   */
  const runMutation = useCallback(
    async (
      action: ProfileControlAction,
      request: (operatorToken: string, signal: AbortSignal) => Promise<void>,
      successNotice: string,
    ): Promise<boolean> => {
      const operatorToken = readOperatorToken();
      // The token may have been saved (or cleared) since the last render: the
      // store change re-renders the consumers when it actually moved.
      notifyTokenChange();
      if (operatorToken === null) {
        // A local refusal, not a server answer: the poll must not erase it.
        // Nothing was thrown here, so `failure` is left alone — `error` carries
        // the message, and no view maps the token hint through `failureReport`.
        mutationErrorRef.current = true;
        setError(NO_OPERATOR_TOKEN_MESSAGE);
        return false;
      }

      mutationControllerRef.current?.abort();
      const controller = new AbortController();
      mutationControllerRef.current = controller;

      clearNoticeTimer();
      setNotice(null);
      mutationErrorRef.current = false;
      setError(null);
      setFailure(null);
      setPendingAction(action);

      try {
        await request(operatorToken, controller.signal);
        if (disposedRef.current || controller.signal.aborted) {
          return false;
        }
        showNotice(successNotice);
        return true;
      } catch (thrown) {
        if (disposedRef.current || controller.signal.aborted) {
          return false;
        }
        // A refused action must stay readable, so the next successful poll
        // keeps showing it until another action replaces it.
        mutationErrorRef.current = true;
        setError(errorMessage(thrown));
        setFailure(thrown);
        return false;
      } finally {
        if (!disposedRef.current) {
          setPendingAction(null);
        }
      }
    },
    [clearNoticeTimer, showNotice],
  );

  /** Adopt the profile state a lifecycle route just answered. */
  const applyLifecycle = useCallback((payload: LifecyclePayload): void => {
    setControl((previous) => ({
      known: true,
      paused: payload.paused === true,
      mutable: previous.mutable,
      engineRunning: previous.engineRunning,
    }));
  }, []);

  const pause = useCallback(
    (): Promise<boolean> =>
      runMutation(
        'pause',
        async (operatorToken, signal) => {
          const payload = await pauseProfile(profileId, {
            baseUrl: optionsRef.current.baseUrl,
            fetchImpl: optionsRef.current.fetchImpl,
            signal,
            operatorToken,
          });
          if (!disposedRef.current) {
            applyLifecycle(payload);
          }
        },
        PROFILE_PAUSED_NOTICE,
      ),
    [applyLifecycle, profileId, runMutation],
  );

  const resume = useCallback(
    (): Promise<boolean> =>
      runMutation(
        'resume',
        async (operatorToken, signal) => {
          const payload = await resumeProfile(profileId, {
            baseUrl: optionsRef.current.baseUrl,
            fetchImpl: optionsRef.current.fetchImpl,
            signal,
            operatorToken,
          });
          if (!disposedRef.current) {
            applyLifecycle(payload);
          }
        },
        PROFILE_RESUMED_NOTICE,
      ),
    [applyLifecycle, profileId, runMutation],
  );

  const remove = useCallback(
    (): Promise<boolean> =>
      runMutation(
        'delete',
        async (operatorToken, signal) => {
          await deleteProfile(profileId, {
            baseUrl: optionsRef.current.baseUrl,
            fetchImpl: optionsRef.current.fetchImpl,
            signal,
            operatorToken,
          });
          if (disposedRef.current) {
            return;
          }
          optionsRef.current.onDeleted?.(profileId);
          // The profile left the platform: the control payload must be re-read.
          await runCycle();
        },
        PROFILE_DELETED_NOTICE,
      ),
    [profileId, runCycle, runMutation],
  );

  return useMemo<UseProfileControlResult>(() => {
    const actionable = control.known && control.mutable && hasToken && pendingAction === null;
    return {
      paused: control.paused,
      mutable: control.mutable,
      engineRunning: control.engineRunning,
      known: control.known,
      pendingAction,
      error,
      failure,
      notice,
      canPause: actionable && !control.paused,
      canResume: actionable && control.paused,
      canDelete: actionable,
      pause,
      resume,
      remove,
      refresh,
    };
  }, [control, error, failure, hasToken, notice, pause, pendingAction, refresh, remove, resume]);
}
