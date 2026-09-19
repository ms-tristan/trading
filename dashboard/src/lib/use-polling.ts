'use client';

/**
 * Polling hook of the live views.
 *
 * The dashboard is an HTTP-polling surface (`monitoring.refresh_seconds`, 2 s by
 * default): every live region keeps its own timer through this hook. Rules:
 *
 * * the timer is a plain `setInterval` — the Server Component already rendered
 *   the first payload, so mounting never fires a duplicate request;
 * * a success replaces the data and stamps `checkedAt`, and clears any error;
 * * **any** failure sets `error` and keeps the last known good data on screen;
 * * `pause()` stops the timer (and the in-flight request) and keeps the data,
 *   `resume()` restarts it and refreshes immediately, `refreshNow()` performs
 *   exactly one cycle;
 * * unmounting clears the timer and aborts the in-flight request.
 *
 * The fetcher must never be able to reject out of this hook: every rejection is
 * caught here and converted into the `error` string.
 */

import { useCallback, useEffect, useRef, useState } from 'react';

import { DEFAULT_POLL_INTERVAL_MS } from './config';
import { errorMessage } from './api';

/** Options of {@link usePolling}. */
export interface UsePollingOptions<T> {
  /** Performs one request; receives the signal that must cancel it. */
  fetcher: (signal: AbortSignal) => Promise<T>;
  /** Payload rendered by the Server Component (the first paint). */
  initialData: T;
  /** Timestamp of `initialData`, when the server provides one. */
  initialCheckedAt?: string | null;
  /** Cadence in milliseconds (defaults to the documented 2 s). */
  intervalMs?: number;
  /** When `false`, the view starts paused and waits for `resume()`. */
  enabled?: boolean;
}

/** State and controls returned by {@link usePolling}. */
export interface UsePollingResult<T> {
  /** Last known good payload. */
  data: T;
  /** ISO-8601 stamp of the last successful poll, or the server's stamp. */
  checkedAt: string | null;
  /** Message of the last failure, or `null`. */
  error: string | null;
  /**
   * The value the fetcher rejected with, or `null`. `error` is its message;
   * this keeps the original failure so a view can hand it to
   * `failureReport` and show the operator-facing headline and detail.
   */
  failure: unknown | null;
  /** Whether live updates are currently paused. */
  isPaused: boolean;
  /** Stop the timer (keeps the data on screen). */
  pause(): void;
  /** Restart the timer and refresh immediately. */
  resume(): void;
  /** Pause when live, resume when paused. */
  toggle(): void;
  /** Run one polling cycle now. Never rejects. */
  refreshNow(): Promise<void>;
}

/**
 * Poll `fetcher` every `intervalMs` and expose the last known good payload.
 */
export function usePolling<T>({
  fetcher,
  initialData,
  initialCheckedAt = null,
  intervalMs = DEFAULT_POLL_INTERVAL_MS,
  enabled = true,
}: UsePollingOptions<T>): UsePollingResult<T> {
  const [data, setData] = useState<T>(initialData);
  const [checkedAt, setCheckedAt] = useState<string | null>(initialCheckedAt);
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<unknown | null>(null);
  const [isPaused, setIsPaused] = useState<boolean>(!enabled);

  const fetcherRef = useRef(fetcher);
  const pausedRef = useRef(!enabled);
  const disposedRef = useRef(false);
  const controllerRef = useRef<AbortController | null>(null);

  // The fetcher is usually an inline arrow: keeping it in a ref means a new
  // identity never restarts the timer.
  useEffect(() => {
    fetcherRef.current = fetcher;
  }, [fetcher]);

  const runCycle = useCallback(async (): Promise<void> => {
    // A slow poll must never race the next one.
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;

    try {
      const next = await fetcherRef.current(controller.signal);
      if (disposedRef.current || controller.signal.aborted) {
        return;
      }
      setData(next);
      setCheckedAt(new Date().toISOString());
      setError(null);
      setFailure(null);
    } catch (thrown) {
      if (disposedRef.current || controller.signal.aborted) {
        return;
      }
      setError(errorMessage(thrown));
      setFailure(thrown);
    }
  }, []);

  const pause = useCallback((): void => {
    pausedRef.current = true;
    controllerRef.current?.abort();
    setIsPaused(true);
  }, []);

  const resume = useCallback((): void => {
    pausedRef.current = false;
    setIsPaused(false);
    void runCycle();
  }, [runCycle]);

  const toggle = useCallback((): void => {
    if (pausedRef.current) {
      resume();
      return;
    }
    pause();
  }, [pause, resume]);

  const refreshNow = useCallback(async (): Promise<void> => {
    await runCycle();
  }, [runCycle]);

  useEffect(() => {
    disposedRef.current = false;
    if (pausedRef.current) {
      return () => {
        disposedRef.current = true;
        controllerRef.current?.abort();
      };
    }
    const timer = setInterval(() => {
      void runCycle();
    }, intervalMs);
    return () => {
      disposedRef.current = true;
      clearInterval(timer);
      controllerRef.current?.abort();
    };
  }, [isPaused, intervalMs, runCycle]);

  return { data, checkedAt, error, failure, isPaused, pause, resume, toggle, refreshNow };
}
