import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import type { ComponentProps } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { CANDLE_RENDER_LIMIT, type CandleMarker, type CandlePriceLine } from '@/lib/candles';
import type { Candle } from '@/lib/types';

import { BUTTON_PRESSED_CLASSES } from '@/components/ui/button';

import { CandlestickChart } from './candlestick-chart';

/**
 * Hand-written fake of the charting library.
 *
 * It exposes exactly the three functions the component may use
 * (`createChart`, `CandlestickSeries`, `createSeriesMarkers`) and records every
 * call, so the test pins the lifecycle (one chart per mount, one `remove()` per
 * instance) and the exact options the component pushes into the library.
 */
const lwc = vi.hoisted(() => {
  interface FakeSeries {
    setData: (data: unknown[]) => void;
    createPriceLine: (line: Record<string, unknown>) => Record<string, unknown>;
    removePriceLine: (line: unknown) => void;
  }

  const record = {
    createChart: [] as Array<{ container: HTMLElement; options: Record<string, unknown> }>,
    addSeries: [] as Array<Record<string, unknown>>,
    setData: [] as unknown[][],
    markers: [] as unknown[][],
    setMarkers: [] as unknown[][],
    priceLines: [] as Array<Record<string, unknown>>,
    removedPriceLines: [] as unknown[],
    applyOptions: [] as Array<Record<string, unknown>>,
    visibleRanges: [] as Array<{ from: number; to: number }>,
    fitContent: 0,
    removals: [] as number[],
    throwOnCreate: false,
  };

  let nextId = 0;

  function reset(): void {
    record.createChart.length = 0;
    record.addSeries.length = 0;
    record.setData.length = 0;
    record.markers.length = 0;
    record.setMarkers.length = 0;
    record.priceLines.length = 0;
    record.removedPriceLines.length = 0;
    record.applyOptions.length = 0;
    record.visibleRanges.length = 0;
    record.fitContent = 0;
    record.removals.length = 0;
    record.throwOnCreate = false;
    nextId = 0;
  }

  function createChart(container: HTMLElement, options: Record<string, unknown>) {
    if (record.throwOnCreate) {
      throw new Error('canvas is unavailable');
    }
    const id = nextId;
    nextId += 1;
    record.createChart.push({ container, options });
    const canvas = container.ownerDocument.createElement('canvas');
    container.appendChild(canvas);

    const series: FakeSeries = {
      setData: (data: unknown[]) => {
        record.setData.push(data);
      },
      createPriceLine: (line: Record<string, unknown>) => {
        record.priceLines.push(line);
        return { id: record.priceLines.length, ...line };
      },
      removePriceLine: (line: unknown) => {
        record.removedPriceLines.push(line);
      },
    };

    return {
      addSeries: (_definition: unknown, seriesOptions: Record<string, unknown>) => {
        record.addSeries.push(seriesOptions);
        return series;
      },
      applyOptions: (next: Record<string, unknown>) => {
        record.applyOptions.push(next);
      },
      timeScale: () => ({
        fitContent: () => {
          record.fitContent += 1;
        },
        setVisibleLogicalRange: (range: { from: number; to: number }) => {
          record.visibleRanges.push(range);
        },
      }),
      remove: () => {
        record.removals.push(id);
        canvas.remove();
      },
    };
  }

  function createSeriesMarkers(_series: unknown, markers: unknown[]) {
    record.markers.push(markers);
    return {
      setMarkers: (next: unknown[]) => {
        record.setMarkers.push(next);
      },
    };
  }

  return { record, reset, createChart, createSeriesMarkers, CandlestickSeries: { type: 'Candlestick' } };
});

vi.mock('lightweight-charts', () => ({
  createChart: lwc.createChart,
  CandlestickSeries: lwc.CandlestickSeries,
  createSeriesMarkers: lwc.createSeriesMarkers,
}));

const CANVAS_ID = 'candlestick-canvas';

function candle(overrides: Partial<Candle> = {}): Candle {
  return {
    profile_id: 'btc-paper',
    timestamp: '2024-01-01T00:00:00+00:00',
    open: 20000,
    high: 20500,
    low: 19800,
    close: 20400,
    volume: 12.5,
    closed: true,
    ...overrides,
  };
}

const CANDLES: Candle[] = [
  candle({
    timestamp: '2024-01-01T00:00:00+00:00',
    open: 20000,
    high: 20500,
    low: 19800,
    close: 20400,
    volume: 12.5,
  }),
  candle({
    timestamp: '2024-01-01T01:00:00+00:00',
    open: 20400,
    high: 20600,
    low: 20100,
    close: 20200,
    volume: 8,
  }),
  candle({
    timestamp: '2024-01-01T02:00:00+00:00',
    open: 20200,
    high: 20900,
    low: 20150,
    close: 20800,
    volume: 3.25,
  }),
];

/** 40 candles, enough for the zoom steps to have room to move. */
const MANY_CANDLES: Candle[] = Array.from({ length: 40 }, (_, index) =>
  candle({
    timestamp: new Date(Date.UTC(2024, 0, 1, index)).toISOString(),
    open: 20000 + index,
    high: 20100 + index,
    low: 19900 + index,
    close: 20050 + index,
  }),
);

const MARKERS: CandleMarker[] = [
  {
    time: 1704067200,
    position: 'belowBar',
    color: 'var(--color-chart-marker-entry)',
    shape: 'arrowUp',
    text: 'Long 20,000.00',
  },
  {
    time: 1704070800,
    position: 'aboveBar',
    color: 'var(--color-chart-marker-exit)',
    shape: 'arrowDown',
    text: 'take_profit 20,500.00',
  },
];

const PRICE_LINES: CandlePriceLine[] = [
  { price: 20000, color: 'var(--color-chart-average)', title: 'Average', lineStyle: 'solid' },
  { price: 19000, color: 'var(--color-chart-stop)', title: 'Stop', lineStyle: 'dashed' },
];

/** A `MediaQueryList` good enough for the component (it only reads `matches`). */
function mediaQueryList(matches: boolean): MediaQueryList {
  return {
    matches,
    media: '',
    onchange: null,
    addListener: () => undefined,
    removeListener: () => undefined,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    dispatchEvent: () => false,
  } as unknown as MediaQueryList;
}

/** Give the component the capabilities its chart factory needs. */
function stubChartEnvironment({ reducedMotion = false } = {}): void {
  vi.stubGlobal('matchMedia', (query: string) => mediaQueryList(reducedMotion && query.includes('reduce')));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    {} as unknown as CanvasRenderingContext2D,
  );
}

function renderChart(props: Partial<ComponentProps<typeof CandlestickChart>> = {}) {
  return render(
    <CandlestickChart candles={CANDLES} markers={MARKERS} priceLines={PRICE_LINES} {...props} />,
  );
}

beforeEach(() => {
  lwc.reset();
  stubChartEnvironment();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('CandlestickChart lifecycle', () => {
  it('creates the chart exactly once and removes it exactly once on unmount', () => {
    const { unmount, container } = renderChart();

    expect(lwc.record.createChart).toHaveLength(1);
    const canvasHost = container.querySelector(`[data-testid="${CANVAS_ID}"]`);
    expect(canvasHost).not.toBeNull();
    expect(canvasHost?.querySelectorAll('canvas')).toHaveLength(1);

    unmount();

    expect(lwc.record.removals).toEqual([0]);
    expect(canvasHost?.querySelectorAll('canvas')).toHaveLength(0);
  });

  it('never rebuilds the chart when the data changes', () => {
    const { rerender, container } = renderChart();

    rerender(
      <CandlestickChart
        candles={[...CANDLES, candle({ timestamp: '2024-01-01T03:00:00+00:00' })]}
        markers={MARKERS}
        priceLines={PRICE_LINES}
      />,
    );
    rerender(<CandlestickChart candles={MANY_CANDLES} markers={[]} priceLines={[]} />);

    expect(lwc.record.createChart).toHaveLength(1);
    expect(lwc.record.addSeries).toHaveLength(1);
    expect(container.querySelectorAll(`[data-testid="${CANVAS_ID}"] canvas`)).toHaveLength(1);
  });

  it('removes one chart per instance when one of two is unmounted', () => {
    const { container } = render(
      <div>
        <CandlestickChart candles={CANDLES} markers={MARKERS} priceLines={PRICE_LINES} />
        <CandlestickChart
          candles={MANY_CANDLES}
          markers={[]}
          priceLines={[]}
          ariaLabel="Second chart"
        />
      </div>,
    );

    expect(lwc.record.createChart).toHaveLength(2);
    expect(lwc.record.removals).toEqual([]);

    cleanup();
    expect(lwc.record.removals).toHaveLength(2);
    expect(new Set(lwc.record.removals).size).toBe(2);
    expect(container.querySelectorAll('canvas')).toHaveLength(0);
  });

  it('never calls focus() on the chart while data updates arrive', () => {
    const focus = vi.spyOn(HTMLElement.prototype, 'focus');
    const { rerender } = renderChart();

    rerender(<CandlestickChart candles={MANY_CANDLES} markers={MARKERS} priceLines={PRICE_LINES} />);

    expect(focus).not.toHaveBeenCalled();
  });
});

describe('CandlestickChart series', () => {
  it('sends at most the render limit, oldest first, with time in UNIX seconds', () => {
    const many = Array.from({ length: CANDLE_RENDER_LIMIT + 120 }, (_, index) =>
      candle({
        timestamp: new Date(Date.UTC(2024, 0, 1) + index * 3_600_000).toISOString(),
        close: 20000 + index,
      }),
    );

    renderChart({ candles: many });

    const sent = lwc.record.setData.at(-1) as Array<Record<string, number>>;
    expect(sent).toHaveLength(CANDLE_RENDER_LIMIT);
    expect(sent[0]?.time).toBe(Math.floor(Date.UTC(2024, 0, 1) / 1000) + 120 * 3600);
    expect(sent.at(-1)?.time).toBe(Math.floor(Date.UTC(2024, 0, 1) / 1000) + 619 * 3600);
    expect(sent.every((entry) => Number.isInteger(entry.time))).toBe(true);
    expect(sent[0]).toEqual({
      time: Math.floor(Date.UTC(2024, 0, 1) / 1000) + 120 * 3600,
      open: 20000,
      high: 20500,
      low: 19800,
      close: 20120,
    });
  });

  it('draws bullish bodies hollow and bearish bodies filled', () => {
    renderChart();

    const options = lwc.record.addSeries[0];
    expect(options?.upColor).toBeUndefined();
    expect(options?.borderUpColor).toBeUndefined();
    expect(options?.downColor).toBeUndefined();
  });

  it('uses the token values when the document defines them', () => {
    vi.stubGlobal(
      'getComputedStyle',
      vi.fn(() => ({
        getPropertyValue: (name: string) =>
          ({
            '--color-chart-bull': '#22c55e',
            '--color-chart-bear': '#ef4444',
            '--color-chart-bull-hollow': '#0e1223',
            '--color-chart-grid': '#1e293b',
            '--color-chart-axis': '#94a3b8',
          })[name] ?? '',
      })),
    );

    renderChart();

    const options = lwc.record.addSeries[0];
    // Hollow bullish body: the card colour inside, the bullish token on the
    // border. Filled bearish body.
    expect(options?.upColor).toBe('#0e1223');
    expect(options?.borderUpColor).toBe('#22c55e');
    expect(options?.downColor).toBe('#ef4444');
    expect(options?.borderDownColor).toBe('#ef4444');
    expect(lwc.record.createChart[0]?.options.layout).toMatchObject({
      background: { color: '#0e1223' },
      textColor: '#94a3b8',
      attributionLogo: true,
    });
  });

  it('drops rows whose time or price cannot be plotted', () => {
    renderChart({
      candles: [
        candle({ timestamp: 'not a date' }),
        candle({ close: null }),
        candle({ timestamp: '2024-01-01T05:00:00+00:00', close: 21000 }),
      ],
    });

    const sent = lwc.record.setData.at(-1) as Array<Record<string, number>>;
    expect(sent).toHaveLength(1);
    expect(sent[0]?.close).toBe(21000);
  });

  it('plots the series in ascending time order whatever the payload order', () => {
    renderChart({ candles: [...CANDLES].reverse() });

    const sent = lwc.record.setData.at(-1) as Array<Record<string, number>>;
    expect(sent.map((entry) => entry.time)).toEqual([1704067200, 1704070800, 1704074400]);
  });

  it('feeds the markers plugin one marker per trade, with the token colours', () => {
    renderChart();

    expect(lwc.record.markers[0]).toEqual([
      {
        time: 1704067200,
        position: 'belowBar',
        shape: 'arrowUp',
        color: 'var(--color-chart-marker-entry)',
        text: 'Long 20,000.00',
      },
      {
        time: 1704070800,
        position: 'aboveBar',
        shape: 'arrowDown',
        color: 'var(--color-chart-marker-exit)',
        text: 'take_profit 20,500.00',
      },
    ]);

    const { rerender } = renderChart();
    rerender(
      <CandlestickChart candles={CANDLES} markers={[MARKERS[0] as CandleMarker]} priceLines={PRICE_LINES} />,
    );

    expect(lwc.record.setMarkers.at(-1)).toHaveLength(1);
  });

  it('draws a solid average line and a dashed stop line with a visible axis label', () => {
    renderChart();

    expect(lwc.record.priceLines).toEqual([
      {
        price: 20000,
        color: 'var(--color-chart-average)',
        lineWidth: 1,
        lineStyle: 0,
        axisLabelVisible: true,
        title: 'Average',
      },
      {
        price: 19000,
        color: 'var(--color-chart-stop)',
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: 'Stop',
      },
    ]);
  });

  it('replaces the previous price lines when the position changes', () => {
    const { rerender } = renderChart();

    expect(lwc.record.removedPriceLines).toEqual([]);

    rerender(<CandlestickChart candles={CANDLES} markers={MARKERS} priceLines={[PRICE_LINES[0] as CandlePriceLine]} />);

    expect(lwc.record.removedPriceLines).toHaveLength(2);
    expect(lwc.record.priceLines).toHaveLength(3);
    expect(lwc.record.priceLines.at(-1)?.title).toBe('Average');
  });
});

describe('CandlestickChart zoom', () => {
  it('exposes three keyboard-reachable zoom buttons', () => {
    renderChart({ candles: MANY_CANDLES });

    const zoomIn = screen.getByRole('button', { name: 'Zoom in' });
    const zoomOut = screen.getByRole('button', { name: 'Zoom out' });
    const reset = screen.getByRole('button', { name: 'Reset zoom' });

    expect(zoomIn).toHaveAttribute('type', 'button');
    expect(zoomOut).toHaveClass('cursor-pointer');
    expect(reset).toHaveClass('cursor-pointer');

    fireEvent.click(zoomIn);
    expect(lwc.record.visibleRanges.at(-1)).toEqual({ from: 40 - 26, to: 39 });

    fireEvent.click(zoomOut);
    expect(lwc.record.visibleRanges.at(-1)).toEqual({ from: 1, to: 39 });

    fireEvent.click(reset);
    expect(lwc.record.fitContent).toBeGreaterThan(0);
  });

  it('answers +, -, 0 and the arrow keys on the chart surface', () => {
    renderChart({ candles: MANY_CANDLES });
    const chart = screen.getByRole('img', { name: 'Candlestick chart' });
    expect(chart).toHaveAttribute('tabindex', '0');

    fireEvent.keyDown(chart, { key: '+' });
    expect(lwc.record.visibleRanges).toHaveLength(1);
    fireEvent.keyDown(chart, { key: '-' });
    expect(lwc.record.visibleRanges).toHaveLength(2);
    fireEvent.keyDown(chart, { key: 'ArrowUp' });
    expect(lwc.record.visibleRanges).toHaveLength(3);
    fireEvent.keyDown(chart, { key: 'ArrowDown' });
    expect(lwc.record.visibleRanges).toHaveLength(4);

    fireEvent.keyDown(chart, { key: '0' });
    expect(lwc.record.fitContent).toBeGreaterThan(0);

    // An unrelated key is left to the browser: no zoom, no scroll hijack.
    fireEvent.keyDown(chart, { key: 'a' });
    expect(lwc.record.visibleRanges).toHaveLength(4);
    expect(lwc.record.fitContent).toBe(1);
  });

  it('does not zoom without candles', () => {
    renderChart({ candles: [] });

    fireEvent.click(screen.getByRole('button', { name: 'Zoom in' }));
    fireEvent.click(screen.getByRole('button', { name: 'Zoom out' }));
    fireEvent.click(screen.getByRole('button', { name: 'Reset zoom' }));

    // Nothing to zoom into: no range is pushed. Resetting is harmless and only
    // re-fits an already fitted (empty) chart.
    expect(lwc.record.visibleRanges).toEqual([]);
    expect(lwc.record.fitContent).toBe(1);
  });
});

describe('CandlestickChart accessibility', () => {
  it('repeats every visible candle as exact text in a data table', () => {
    renderChart({ ariaLabel: 'Candlestick chart of btc-paper' });

    expect(screen.getByRole('img', { name: 'Candlestick chart of btc-paper' })).toBeInTheDocument();

    const details = screen.getByText('Candle data').closest('details');
    expect(details).not.toBeNull();
    const table = within(details as HTMLElement).getByRole('table', {
      name: 'Candlestick data',
      hidden: true,
    });

    for (const header of ['Timestamp', 'Body', 'Open', 'High', 'Low', 'Close', 'Volume']) {
      expect(within(table).getByText(header)).toBeInTheDocument();
    }
    expect(within(table).getByText('2024-01-01 00:00:00 UTC')).toBeInTheDocument();
    expect(within(table).getAllByText('20,000.00').length).toBeGreaterThan(0);
    expect(within(table).getAllByText('Bullish (hollow)').length).toBeGreaterThan(0);
    expect(within(table).getAllByText('Bearish (filled)').length).toBeGreaterThan(0);
    expect(within(table).getAllByText('12.5').length).toBe(1);
    expect(within(table).getByText('3.25')).toBeInTheDocument();
  });

  it('renders the empty state without throwing when there is no candle', () => {
    const { container } = renderChart({ candles: [] });

    expect(screen.getByText('No candle yet')).toBeInTheDocument();
    expect(lwc.record.setData.at(-1)).toEqual([]);
    expect(container.querySelector(`[data-testid="${CANVAS_ID}"]`)?.className).toContain('hidden');
  });

  it('renders only the table when the chart factory throws', () => {
    lwc.record.throwOnCreate = true;

    const { container } = renderChart();

    expect(lwc.record.createChart).toHaveLength(0);
    expect(container.querySelector(`[data-testid="${CANVAS_ID}"]`)?.className).toContain('hidden');
    expect(
      screen.getByRole('table', { name: 'Candlestick data', hidden: true }),
    ).toBeInTheDocument();
  });

  it('skips the chart entirely when the environment has no canvas', () => {
    vi.restoreAllMocks();
    vi.stubGlobal('matchMedia', () => mediaQueryList(false));
    vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(null);

    const { container } = renderChart();

    expect(lwc.record.createChart).toHaveLength(0);
    expect(container.querySelector(`[data-testid="${CANVAS_ID}"]`)?.className).toContain('hidden');
    expect(screen.getByRole('table', { name: 'Candlestick data', hidden: true })).toBeInTheDocument();
  });

  it('applies the automatic fit immediately under reduced motion', () => {
    cleanup();
    lwc.reset();
    stubChartEnvironment({ reducedMotion: true });

    renderChart();

    expect(lwc.record.fitContent).toBeGreaterThan(0);
  });
});

describe('CandlestickChart resize', () => {
  it('follows its container and disconnects the observer on unmount', () => {
    const observers: FakeResizeObserver[] = [];

    class FakeResizeObserver {
      readonly observed: Element[] = [];
      disconnects = 0;
      private readonly callback: () => void;

      constructor(callback: () => void) {
        this.callback = callback;
        observers.push(this);
      }

      observe(target: Element): void {
        this.observed.push(target);
      }

      unobserve(): void {
        // Not used by the component.
      }

      disconnect(): void {
        this.disconnects += 1;
      }

      trigger(): void {
        this.callback();
      }
    }

    vi.stubGlobal('ResizeObserver', FakeResizeObserver);

    const { container, unmount } = renderChart();
    const canvasHost = container.querySelector(`[data-testid="${CANVAS_ID}"]`) as HTMLElement;
    expect(observers).toHaveLength(1);
    expect(observers[0]?.observed).toEqual([canvasHost]);

    Object.defineProperty(canvasHost, 'clientWidth', { value: 640, configurable: true });
    observers[0]?.trigger();

    expect(lwc.record.applyOptions.at(-1)).toEqual({ width: 640, height: 320 });

    unmount();

    expect(observers[0]?.disconnects).toBe(1);
  });
});

describe('CandlestickChart press feedback', () => {
  it('acknowledges a press on the Candle data summary', () => {
    renderChart();

    const summary = screen.getByText('Candle data');
    expect(summary.tagName).toBe('SUMMARY');
    expect(summary).toHaveClass('active:bg-muted-pressed');
    expect(summary).toHaveClass('active:text-foreground');
    // Still an obvious click target with its keyboard focus ring intact.
    expect(summary).toHaveClass('cursor-pointer');
    expect(summary).toHaveClass('focus-visible:ring-2');
  });

  it('acknowledges a press on the three zoom controls', () => {
    renderChart({ candles: MANY_CANDLES });

    for (const name of ['Zoom in', 'Zoom out', 'Reset zoom']) {
      const control = screen.getByRole('button', { name });
      expect(control).toHaveClass('cursor-pointer');
      // The chart's own controls are the shared ghost Button: they inherit its
      // press feedback instead of redefining one.
      expect(control).toHaveClass(BUTTON_PRESSED_CLASSES.ghost);
    }
  });
});
