import { act, fireEvent, render, screen } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { Candle, CandlesPayload, Position, PositionsPayload, TradesPayload } from '@/lib/types';

import { CandlestickPanel } from './candlestick-panel';

/**
 * Minimal fake of the charting library: the panel test cares about what is fed
 * to the chart (candles, markers, price lines), not about how it paints.
 */
const chart = vi.hoisted(() => {
  const record = {
    markers: [] as unknown[][],
    priceLines: [] as Array<Record<string, unknown>>,
    setData: [] as unknown[][],
  };
  function reset(): void {
    record.markers.length = 0;
    record.priceLines.length = 0;
    record.setData.length = 0;
  }
  function createChart() {
    const series = {
      setData: (data: unknown[]) => {
        record.setData.push(data);
      },
      createPriceLine: (line: Record<string, unknown>) => {
        record.priceLines.push(line);
        return line;
      },
      removePriceLine: () => undefined,
    };
    return {
      addSeries: () => series,
      applyOptions: () => undefined,
      timeScale: () => ({ fitContent: () => undefined, setVisibleLogicalRange: () => undefined }),
      remove: () => undefined,
    };
  }
  function createSeriesMarkers(_series: unknown, markers: unknown[]) {
    record.markers.push(markers);
    return { setMarkers: () => undefined };
  }
  return { record, reset, createChart, createSeriesMarkers, CandlestickSeries: { type: 'Candlestick' } };
});

vi.mock('lightweight-charts', () => ({
  createChart: chart.createChart,
  CandlestickSeries: chart.CandlestickSeries,
  createSeriesMarkers: chart.createSeriesMarkers,
}));

const CANDLES: Candle[] = [
  {
    profile_id: 'btc-paper',
    timestamp: '2024-01-01T00:00:00+00:00',
    open: 20000,
    high: 20500,
    low: 19800,
    close: 20400,
    volume: 12.5,
    closed: true,
  },
  {
    profile_id: 'btc-paper',
    timestamp: '2024-01-01T01:00:00+00:00',
    open: 20400,
    high: 20600,
    low: 20100,
    close: 20200,
    volume: 8,
    closed: true,
  },
];

const CANDLES_PAYLOAD: CandlesPayload = { candles: CANDLES, count: 2 };

const POSITIONS: PositionsPayload = {
  positions: [
    {
      profile_id: 'btc-paper',
      symbol: 'BTC/USDT',
      quantity: 1.5,
      average_price: 20000,
      direction: 'long',
      opened_at: '2024-01-01T00:00:00+00:00',
      updated_at: '2024-01-01T01:00:00+00:00',
      realized_pnl: 0,
      unrealized_pnl: 10,
      stop_price: 19000,
    },
  ],
};

const TRADES: TradesPayload = {
  trades: [
    {
      entry_time: '2024-01-01T00:00:00+00:00',
      exit_time: '2024-01-01T01:30:00+00:00',
      entry_price: 20000,
      exit_price: 20500,
      size: 0.5,
      direction: 'long',
      pnl: 250,
      pnl_pct: 0.0125,
      fees: 4.5,
      exit_reason: 'take_profit',
      duration_minutes: 90,
      stop_price: 19500,
      take_profit_price: 21000,
      params_id: 'default',
    },
  ],
  count: 1,
};

const DETAIL_INTERVAL_MS = 10_000;

type FetchHandler = (url: string) => Promise<Response>;

let handler: FetchHandler;
let fetchImpl: ReturnType<typeof vi.fn>;

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** Render the panel with the stubbed transport and the given overlays. */
function renderPanel(
  overrides: Partial<ComponentProps<typeof CandlestickPanel>> = {},
): ReturnType<typeof render> {
  return render(
    <CandlestickPanel
      profileId="btc-paper"
      positions={POSITIONS}
      trades={TRADES}
      detailIntervalMs={DETAIL_INTERVAL_MS}
      fetchImpl={fetchImpl as unknown as typeof fetch}
      {...overrides}
    />,
  );
}

/** Flush a polling cycle: timers, microtasks and React updates. */
async function advance(ms: number): Promise<void> {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

/** The URL of the nth request the panel issued. */
function requestedUrl(index = 0): string {
  return String(fetchImpl.mock.calls[index]?.[0]);
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2024-06-01T12:00:00.000Z'));
  chart.reset();
  vi.stubGlobal(
    'matchMedia',
    vi.fn(() => ({
      matches: false,
      media: '',
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    })),
  );
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    {} as unknown as CanvasRenderingContext2D,
  );
  handler = async () => jsonResponse(CANDLES_PAYLOAD);
  fetchImpl = vi.fn(async (url: string) => handler(String(url)));
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('CandlestickPanel', () => {
  it('polls the candles route on the detail cadence, not before', async () => {
    renderPanel();

    expect(fetchImpl).not.toHaveBeenCalled();

    await advance(DETAIL_INTERVAL_MS - 1);
    expect(fetchImpl).not.toHaveBeenCalled();

    await advance(1);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(requestedUrl()).toBe('/api/profiles/btc-paper/candles?limit=500');

    await advance(DETAIL_INTERVAL_MS);
    expect(fetchImpl).toHaveBeenCalledTimes(2);
  });

  it('calls the API against the base URL it was given', async () => {
    renderPanel({ baseUrl: 'http://127.0.0.1:8080' });

    await advance(DETAIL_INTERVAL_MS);

    expect(requestedUrl()).toBe('http://127.0.0.1:8080/api/profiles/btc-paper/candles?limit=500');
  });

  it('renders the candles with the trades as markers and the position as price lines', async () => {
    renderPanel();

    expect(screen.getByText('No candle yet')).toBeInTheDocument();

    await advance(DETAIL_INTERVAL_MS);

    // The series reaches the chart, oldest first.
    expect(chart.record.setData.at(-1)).toHaveLength(2);

    // One entry and one exit marker per closed trade.
    expect(chart.record.markers.at(-1)).toEqual([
      {
        time: 1704067200,
        position: 'belowBar',
        shape: 'arrowUp',
        color: 'var(--color-chart-marker-entry)',
        text: 'Long 20,000.00',
      },
      {
        time: 1704072600,
        position: 'aboveBar',
        shape: 'arrowDown',
        color: 'var(--color-chart-marker-exit)',
        text: 'take_profit 20,500.00',
      },
    ]);

    // A solid average line and a dashed stop line. They are already drawn at
    // mount (positions are known before the first candle poll); the two last
    // entries are the ones recreated from the refreshed payload.
    const priceLines = chart.record.priceLines.slice(-2);
    expect(priceLines).toHaveLength(2);
    expect(priceLines[0]).toMatchObject({
      price: 20000,
      title: 'Average',
      lineStyle: 0,
      axisLabelVisible: true,
    });
    expect(priceLines[1]).toMatchObject({
      price: 19000,
      title: 'Stop',
      lineStyle: 2,
      axisLabelVisible: true,
    });

    // The legend and the text alternative are part of the panel.
    expect(screen.getByRole('list', { name: 'Chart legend' })).toBeInTheDocument();
    expect(screen.getByText('Candle data')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Candlestick chart of btc-paper' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Candles' })).toBeInTheDocument();
    expect(screen.getByText('BTC/USDT · persisted candles')).toBeInTheDocument();
  });

  it('keeps the last candles and names the failure when the server answers an error', async () => {
    renderPanel();

    await advance(DETAIL_INTERVAL_MS);
    expect(chart.record.setData.at(-1)).toHaveLength(2);

    handler = async () => jsonResponse({ error: 'unknown profile' }, 404);
    await advance(DETAIL_INTERVAL_MS);

    expect(screen.getByText('unknown profile')).toBeInTheDocument();
    // The last known good series is still on screen.
    expect(chart.record.setData.at(-1)).toHaveLength(2);
    expect(screen.queryByText('No candle yet')).toBeNull();
  });

  it('reports a server error without the documented JSON body', async () => {
    renderPanel();
    handler = async () => new Response('boom', { status: 500 });

    await advance(DETAIL_INTERVAL_MS);

    expect(screen.getByText('HTTP 500')).toBeInTheDocument();
  });

  it('reports a network failure without throwing', async () => {
    renderPanel();
    handler = async () => {
      throw new TypeError('Failed to fetch');
    };

    await advance(DETAIL_INTERVAL_MS);

    expect(
      screen.getByText('network error calling /api/profiles/btc-paper/candles: Failed to fetch'),
    ).toBeInTheDocument();
    expect(screen.getByText('No candle yet')).toBeInTheDocument();
  });

  it('renders the empty state for an empty payload without throwing', async () => {
    renderPanel();
    handler = async () => jsonResponse({ candles: [], count: 0 });

    await advance(DETAIL_INTERVAL_MS);

    expect(chart.record.setData.at(-1)).toEqual([]);
    expect(screen.getByText('No candle yet')).toBeInTheDocument();
    expect(screen.queryByText(/network error/)).toBeNull();
  });

  it('surfaces a malformed payload as an error and keeps rendering', async () => {
    renderPanel();
    handler = async () => jsonResponse({ candles: 'nope' });

    await advance(DETAIL_INTERVAL_MS);

    expect(
      screen.getByText(
        'unexpected response from /api/profiles/btc-paper/candles: payload does not match the expected shape',
      ),
    ).toBeInTheDocument();
  });

  it('renders without overlays when the profile holds no position and no trade', async () => {
    renderPanel({ positions: { positions: [] }, trades: { trades: [], count: 0 } });

    expect(screen.getByText('Persisted candles of this profile')).toBeInTheDocument();

    await advance(DETAIL_INTERVAL_MS);

    expect(chart.record.markers.at(-1)).toEqual([]);
    expect(chart.record.priceLines).toEqual([]);
  });

  it('falls back to a neutral description when no position names the instrument', async () => {
    renderPanel({
      positions: {
        positions: [{ ...(POSITIONS.positions[0] as Position), symbol: '   ', stop_price: null }],
      },
    });

    expect(screen.getByText('Persisted candles of this profile')).toBeInTheDocument();

    await advance(DETAIL_INTERVAL_MS);

    // A position without a stop still contributes its average price line.
    expect(chart.record.priceLines.filter((line) => line.title === 'Stop')).toEqual([]);
    expect(chart.record.priceLines.at(-1)).toMatchObject({ title: 'Average', lineStyle: 0 });
  });

  it('refreshes on demand without waiting for the detail interval', async () => {
    renderPanel();

    expect(fetchImpl).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Refresh now' }));
    await advance(1);

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(requestedUrl()).toBe('/api/profiles/btc-paper/candles?limit=500');
  });

  it('stops polling and aborts the in-flight request on unmount', async () => {
    const { unmount } = renderPanel();

    await advance(DETAIL_INTERVAL_MS);
    const signal = fetchImpl.mock.calls[0]?.[1]?.signal as AbortSignal | undefined;
    expect(signal?.aborted).toBe(false);

    unmount();

    expect(signal?.aborted).toBe(true);
    await advance(DETAIL_INTERVAL_MS * 3);
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
