import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ApiError } from './api';
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

  it('exposes the thrown value as the failure, next to the unchanged message', async () => {
    const thrown = new ApiError('http', 'HTTP 502', { status: 502, path: '/api/health' });
    const fetcher = async (): Promise<string> => {
      throw thrown;
    };
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    expect(result.current.failure).toBeNull();

    await act(async () => {
      await result.current.refreshNow();
    });

    expect(result.current.failure).toBe(thrown);
    // `error` keeps its exact previous meaning: the message of the failure.
    expect(result.current.error).toBe('HTTP 502');
    // ... and the last known good payload is still on screen.
    expect(result.current.data).toBe('good');
  });

  it('clears the failure together with the error after the next success', async () => {
    const thrown = new Error('poll failed');
    let call = 0;
    const fetcher = async (): Promise<string> => {
      call += 1;
      if (call === 1) {
        throw thrown;
      }
      return 'fresh';
    };
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    await act(async () => {
      await result.current.refreshNow();
    });
    expect(result.current.failure).toBe(thrown);

    await act(async () => {
      await result.current.refreshNow();
    });
    expect(result.current.failure).toBeNull();
    expect(result.current.error).toBeNull();
    expect(result.current.data).toBe('fresh');
  });

  it('resolves refreshNow and keeps the failure when the fetcher rejects', async () => {
    const thrown = new TypeError('Failed to fetch');
    const fetcher = async (): Promise<string> => {
      throw thrown;
    };
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    await expect(
      act(async () => {
        await result.current.refreshNow();
      }),
    ).resolves.toBeUndefined();

    expect(result.current.failure).toBe(thrown);
  });

  it('does not record a failure for an aborted request', async () => {
    const fetcher = (signal: AbortSignal): Promise<string> =>
      new Promise<string>((_resolve, reject) => {
        signal.addEventListener('abort', () => {
          reject(new DOMException('aborted', 'AbortError'));
        });
      });
    const { result } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    act(() => {
      void result.current.refreshNow();
      result.current.pause();
    });

    await act(async () => {
      await Promise.resolve();
    });

    expect(result.current.failure).toBeNull();
    expect(result.current.error).toBeNull();
  });

  it('ignores a late success that lands after unmount', async () => {
    let release: (value: string) => void = () => {};
    const fetcher = (): Promise<string> =>
      new Promise<string>((resolve) => {
        release = resolve;
      });
    const { result, unmount } = renderHook(() => usePolling({ fetcher, initialData: 'good' }));

    let cycle: Promise<void> = Promise.resolve();
    act(() => {
      cycle = result.current.refreshNow();
    });

    unmount();
    release('late');

    await act(async () => {
      await cycle;
    });

    // Nothing was recorded: the hook is gone and never threw.
    expect(result.current.data).toBe('good');
    expect(result.current.error).toBeNull();
    expect(result.current.failure).toBeNull();
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
