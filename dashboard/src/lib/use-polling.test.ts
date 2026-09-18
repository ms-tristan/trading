import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { usePolling } from './use-polling';

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('usePolling', () => {
  it('renders the server payload without fetching on mount', () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() =>
      usePolling({ fetcher, initialData: 'first', initialCheckedAt: '2024-01-01T00:00:00Z' }),
    );

    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.data).toBe('first');
    expect(result.current.checkedAt).toBe('2024-01-01T00:00:00Z');
    expect(result.current.error).toBeNull();
    expect(result.current.isPaused).toBe(false);
  });

  it('polls on the configured cadence and updates data and checkedAt', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() =>
      usePolling({ fetcher, initialData: 'first', intervalMs: 2000 }),
    );

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1999);
    });
    expect(fetcher).not.toHaveBeenCalled();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(1);
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe('second');
    expect(result.current.checkedAt).not.toBeNull();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('clears the timer on unmount', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { unmount } = renderHook(() => usePolling({ fetcher, initialData: 'first' }));

    unmount();

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('aborts the in-flight request on unmount', async () => {
    let captured: AbortSignal | null = null;
    const fetcher = (signal: AbortSignal): Promise<string> => {
      captured = signal;
      return new Promise<string>((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          reject(new DOMException('aborted', 'AbortError'));
        });
      });
    };

    const { result, unmount } = renderHook(() => usePolling({ fetcher, initialData: 'first' }));

    act(() => {
      void result.current.refreshNow();
    });
    expect(captured).not.toBeNull();
    expect((captured as unknown as AbortSignal).aborted).toBe(false);

    unmount();
    expect((captured as unknown as AbortSignal).aborted).toBe(true);

    // The rejection of the aborted request must not set any state.
    await act(async () => {
      await Promise.resolve();
    });
  });

  it('pause stops the timer and keeps the data', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'first' }));

    act(() => {
      result.current.pause();
    });
    expect(result.current.isPaused).toBe(true);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(fetcher).not.toHaveBeenCalled();
    expect(result.current.data).toBe('first');
  });

  it('resume restarts the timer and refreshes immediately', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() =>
      usePolling({ fetcher, initialData: 'first', intervalMs: 2000 }),
    );

    act(() => {
      result.current.pause();
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(6000);
    });
    expect(fetcher).not.toHaveBeenCalled();

    await act(async () => {
      result.current.resume();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.isPaused).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe('second');

    // ... and the timer runs again.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it('toggles between pause and resume', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'first' }));

    act(() => {
      result.current.toggle();
    });
    expect(result.current.isPaused).toBe(true);

    await act(async () => {
      result.current.toggle();
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(result.current.isPaused).toBe(false);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('starts paused when disabled', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() =>
      usePolling({ fetcher, initialData: 'first', enabled: false }),
    );

    expect(result.current.isPaused).toBe(true);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10000);
    });
    expect(fetcher).not.toHaveBeenCalled();
  });

  it('keeps the last known good data and sets the error when a poll fails', async () => {
    const fetcher = async (): Promise<string> => {
      throw new Error('poll failed');
    };
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    await act(async () => {
      await result.current.refreshNow();
    });

    expect(result.current.data).toBe('good');
    expect(result.current.error).toBe('poll failed');
  });

  it('never lets a rejection escape the hook', async () => {
    const fetcher = async (): Promise<string> => {
      throw new Error('boom');
    };
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    await expect(
      act(async () => {
        await result.current.refreshNow();
      }),
    ).resolves.toBeUndefined();
  });

  it('clears the error after the next success', async () => {
    let call = 0;
    const fetcher = async (): Promise<string> => {
      call += 1;
      if (call === 1) {
        throw new Error('poll failed');
      }
      return 'fresh';
    };
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    await act(async () => {
      await result.current.refreshNow();
    });
    expect(result.current.error).toBe('poll failed');

    await act(async () => {
      await result.current.refreshNow();
    });
    expect(result.current.error).toBeNull();
    expect(result.current.data).toBe('fresh');
    expect(result.current.checkedAt).not.toBeNull();
  });

  it('refreshNow performs exactly one cycle', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() =>
      usePolling({ fetcher, initialData: 'first', intervalMs: 2000 }),
    );

    await act(async () => {
      await result.current.refreshNow();
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe('second');
  });

  it('works while paused (manual refresh stays available)', async () => {
    const fetcher = vi.fn(async () => 'second');
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'first' }));

    act(() => {
      result.current.pause();
    });
    await act(async () => {
      await result.current.refreshNow();
    });

    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe('second');
    expect(result.current.isPaused).toBe(true);
  });

  it('uses the latest fetcher without restarting the timer', async () => {
    const first = vi.fn(async () => 'first-result');
    const second = vi.fn(async () => 'second-result');
    const { result, rerender } = renderHook(
      ({ fetcher }: { fetcher: () => Promise<string> }) =>
        usePolling({ fetcher: () => fetcher(), initialData: 'initial', intervalMs: 2000 }),
      { initialProps: { fetcher: first } },
    );

    rerender({ fetcher: second });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000);
    });

    expect(first).not.toHaveBeenCalled();
    expect(second).toHaveBeenCalledTimes(1);
    expect(result.current.data).toBe('second-result');
  });
});
