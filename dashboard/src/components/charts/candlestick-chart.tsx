'use client';

/**
 * Live candlestick chart of one profile.
 *
 * A real trading surface, with the constraints that come with it:
 *
 * * **one instance per mount** — the chart is created in an effect with an empty
 *   dependency array and destroyed in its cleanup, so a data refresh never
 *   rebuilds the canvas and never leaks one (pinned by the unit test);
 * * **colour is never the only signal** — a bullish body is drawn hollow (its
 *   fill is the card colour, its border the bullish token) and a bearish body
 *   filled; entry and exit markers differ by position *and* arrow direction; the
 *   average price line is solid, the stop line dashed; and every visible candle
 *   is repeated as exact numbers in the `<details>` table below the canvas;
 * * **zoom is reachable from the keyboard** — three real buttons (Zoom in, Zoom
 *   out, Reset zoom) plus `+` / `-` / `0` and `ArrowUp` / `ArrowDown` on the
 *   chart surface;
 * * **live updates never steal focus** — nothing in the update path calls
 *   `focus()`, so a poll that lands while the operator types cannot interrupt
 *   them;
 * * **nothing throws** — an empty series renders the empty state, a failed poll
 *   keeps the last data (handled by the panel), and a missing canvas or a
 *   throwing chart factory leaves the accessible table as the whole output.
 *
 * Reduced motion: when `prefers-reduced-motion: reduce` is set, the automatic
 * fit is applied synchronously — the final range appears at once instead of
 * being animated into place.
 */

import { useCallback, useEffect, useMemo, useRef } from 'react';
import type { JSX, KeyboardEvent } from 'react';

import { Maximize2, Minus, Plus } from 'lucide-react';
import { CandlestickSeries, createChart, createSeriesMarkers } from 'lightweight-charts';
import type {
  CandlestickData,
  IChartApi,
  IPriceLine,
  ISeriesApi,
  ISeriesMarkersPluginApi,
  SeriesMarker,
  Time,
} from 'lightweight-charts';

import { Button } from '@/components/ui/button';
import { DataTable, type DataTableColumn } from '@/components/ui/data-table';
import { EmptyState } from '@/components/ui/empty-state';
import {
  candleDirection,
  candleTime,
  visibleCandles,
  type CandleMarker,
  type CandlePriceLine,
} from '@/lib/candles';
import { chartTheme, chartToken } from '@/lib/chart-theme';
import { cn } from '@/lib/cn';
import { formatNumber, formatQuantity, formatTimestamp } from '@/lib/format';
import type { Candle } from '@/lib/types';

/** Default height of the canvas, in CSS pixels. */
export const CANDLE_CHART_HEIGHT = 320;

/** Width used before the container has been measured (and when it cannot be). */
const FALLBACK_WIDTH = 800;

/** How much one zoom step changes the number of visible candles. */
const ZOOM_FACTOR = 1.5;

/** Never zoom closer than this many candles: below it a chart says nothing. */
const MIN_VISIBLE_CANDLES = 10;

/**
 * `LineStyle.Solid` and `LineStyle.Dashed` of the charting library.
 *
 * Spelled as numbers on purpose: the implementation talks to the library
 * through three functions only (`createChart`, `CandlestickSeries`,
 * `createSeriesMarkers`), which is exactly the seam the unit test replaces.
 */
const LINE_STYLE_SOLID = 0;
const LINE_STYLE_DASHED = 2;

/** Props of {@link CandlestickChart}. */
export interface CandlestickChartProps {
  /** The candle window, oldest first, as `GET /api/profiles/{id}/candles` returns it. */
  candles: Candle[];
  /** Entry and exit markers ({@link CandleMarker}). */
  markers: CandleMarker[];
  /** Average price and stop lines ({@link CandlePriceLine}). */
  priceLines: CandlePriceLine[];
  /** Height of the canvas, in CSS pixels. */
  height?: number;
  /** Accessible name of the chart. */
  ariaLabel?: string;
  className?: string;
}

/** Whether the visitor asked for reduced motion (never throws). */
function prefersReducedMotion(): boolean {
  try {
    if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') {
      return false;
    }
    return window.matchMedia('(prefers-reduced-motion: reduce)').matches === true;
  } catch {
    return false;
  }
}

/**
 * Whether this environment can host the charting library at all.
 *
 * The library needs a `<canvas>` 2D context and `window.matchMedia` (it watches
 * the device pixel ratio through it). Where either is missing — a stripped-down
 * renderer, a test environment without a canvas backend — the chart is skipped
 * entirely and the text alternative below is the whole output. This is checked
 * *before* the factory is called, because the library can also fail
 * asynchronously from one of its own animation frames, where a `try`/`catch`
 * around the call would not help.
 */
function canCreateChart(): boolean {
  if (typeof window === 'undefined' || typeof document === 'undefined') {
    return false;
  }
  if (typeof window.matchMedia !== 'function') {
    return false;
  }
  try {
    const probe = document.createElement('canvas');
    return typeof probe.getContext === 'function' && probe.getContext('2d') !== null;
  } catch {
    return false;
  }
}

/** Remove the entries a design token could not resolve, at every depth. */
function pruneUndefined<T>(value: T): T {
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return value;
  }
  const pruned: Record<string, unknown> = {};
  for (const [key, entry] of Object.entries(value as Record<string, unknown>)) {
    if (entry === undefined) {
      continue;
    }
    pruned[key] = pruneUndefined(entry);
  }
  return pruned as T;
}

/**
 * Turn a design-token reference (`var(--color-chart-bull)`) into the concrete
 * colour a canvas understands, keeping the reference when the token cannot be
 * resolved — no literal colour is ever invented here.
 */
function resolveColor(reference: string, theme: Record<string, string | undefined>): string {
  const match = /^var\(\s*(--[a-zA-Z0-9-]+)\s*\)$/.exec(reference.trim());
  if (match === null) {
    return reference;
  }
  const name = match[1] as string;
  return theme[name] ?? chartToken(name) ?? reference;
}

/** Markers in the exact shape the library's markers plugin expects. */
function toLibraryMarkers(
  markers: CandleMarker[],
  theme: Record<string, string | undefined>,
): SeriesMarker<Time>[] {
  return markers
    .filter((marker) => Number.isFinite(marker.time))
    .map((marker) => ({
      time: marker.time as Time,
      position: marker.position,
      shape: marker.shape,
      color: resolveColor(marker.color, theme),
      text: marker.text,
    }));
}

/**
 * Candles in the shape the library plots: unusable rows are dropped, and a
 * timestamp seen twice keeps its last value (which also guarantees the strictly
 * ascending order the library requires).
 */
function toChartData(candles: Candle[]): CandlestickData<Time>[] {
  const byTime = new Map<number, CandlestickData<Time>>();
  for (const candle of candles) {
    const time = candleTime(candle.timestamp);
    if (time === null) {
      continue;
    }
    const { open, high, low, close } = candle;
    if (
      !Number.isFinite(open) ||
      !Number.isFinite(high) ||
      !Number.isFinite(low) ||
      !Number.isFinite(close)
    ) {
      continue;
    }
    byTime.set(time, {
      time: time as Time,
      open: open as number,
      high: high as number,
      low: low as number,
      close: close as number,
    });
  }
  return [...byTime.entries()].sort((left, right) => left[0] - right[0]).map(([, data]) => data);
}

/** Width to give the canvas, from the measured container when there is one. */
function containerWidth(container: HTMLElement, fallback: number): number {
  const measured = container.clientWidth;
  return Number.isFinite(measured) && measured > 0 ? measured : fallback;
}

/**
 * Drop the canvas host out of the layout when no chart could be built.
 *
 * Done on the element rather than through a React state on purpose: the result
 * is identical on the server and in the browser, so hydration never disagrees
 * with the markup the server sent, and no re-render is triggered from an effect.
 */
function hideCanvasHost(container: HTMLElement): void {
  container.classList.add('hidden');
}

/**
 * Interactive candlestick chart of one profile.
 *
 * The canvas is decorative: the chart carries a `role="img"` name, and the
 * `<details>` table below it repeats every rendered candle as text, so the
 * figure is fully readable without seeing it. The zoom controls are ordinary
 * buttons, hence keyboard operable by construction.
 */
export function CandlestickChart({
  candles,
  markers,
  priceLines,
  height = CANDLE_CHART_HEIGHT,
  ariaLabel = 'Candlestick chart',
  className,
}: CandlestickChartProps): JSX.Element {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Candlestick', Time> | null>(null);
  const markersPluginRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const priceLinesRef = useRef<IPriceLine[]>([]);
  const fitFrameRef = useRef<number | null>(null);
  const candleCountRef = useRef(0);
  const windowRef = useRef<number | null>(null);
  const autoFitRef = useRef(true);

  const heightValue = Number.isFinite(height) && height > 0 ? height : CANDLE_CHART_HEIGHT;
  const rendered = useMemo(() => visibleCandles(candles), [candles]);

  /**
   * Fit the visible range to the whole window, on the automatic path.
   *
   * Motion allowed: deferred by one animation frame, which is what gives the
   * range its transition. Reduced motion: applied at once, so the final state is
   * on screen immediately.
   */
  const scheduleFit = useCallback((): void => {
    const chart = chartRef.current;
    if (chart === null || !autoFitRef.current) {
      return;
    }
    const pending = fitFrameRef.current;
    if (pending !== null && typeof cancelAnimationFrame === 'function') {
      cancelAnimationFrame(pending);
      fitFrameRef.current = null;
    }
    if (prefersReducedMotion() || typeof requestAnimationFrame !== 'function') {
      chart.timeScale().fitContent();
      return;
    }
    fitFrameRef.current = requestAnimationFrame(() => {
      fitFrameRef.current = null;
      chart.timeScale().fitContent();
    });
  }, []);

  // Creation: exactly once per mount, whatever the data does afterwards.
  useEffect(() => {
    const container = containerRef.current;
    if (container === null) {
      return;
    }
    if (!canCreateChart()) {
      hideCanvasHost(container);
      return;
    }

    let chart: IChartApi | null = null;
    try {
      const theme = chartTheme();
      chart = createChart(
        container,
        pruneUndefined({
          width: containerWidth(container, FALLBACK_WIDTH),
          height: heightValue,
          layout: {
            background: { color: theme['--color-chart-bull-hollow'] },
            textColor: theme['--color-chart-axis'],
            // The library's attribution logo stays: it is part of its licence.
            attributionLogo: true,
          },
          grid: {
            vertLines: { color: theme['--color-chart-grid'] },
            horzLines: { color: theme['--color-chart-grid'] },
          },
          crosshair: {
            vertLine: { color: theme['--color-chart-crosshair'] },
            horzLine: { color: theme['--color-chart-crosshair'] },
          },
          rightPriceScale: { borderColor: theme['--color-chart-grid'] },
          timeScale: { borderColor: theme['--color-chart-grid'], timeVisible: true },
        }),
      );
      const series = chart.addSeries(
        CandlestickSeries,
        pruneUndefined({
          // Bullish: hollow body (the card colour shows through) with a bullish
          // border. Bearish: filled body. The two stay distinguishable in
          // greyscale, which colour alone could never guarantee.
          upColor: theme['--color-chart-bull-hollow'],
          downColor: theme['--color-chart-bear'],
          borderUpColor: theme['--color-chart-bull'],
          borderDownColor: theme['--color-chart-bear'],
          wickUpColor: theme['--color-chart-wick-bull'],
          wickDownColor: theme['--color-chart-wick-bear'],
        }),
      );
      const plugin = createSeriesMarkers(series, toLibraryMarkers(markers, theme));
      chartRef.current = chart;
      seriesRef.current = series;
      markersPluginRef.current = plugin;
    } catch {
      // No canvas, no chart factory: the table below is the whole figure.
      try {
        chart?.remove();
      } catch {
        // A chart that failed to build cannot be removed either.
      }
      chartRef.current = null;
      seriesRef.current = null;
      markersPluginRef.current = null;
      hideCanvasHost(container);
      return;
    }

    return () => {
      const pending = fitFrameRef.current;
      if (pending !== null && typeof cancelAnimationFrame === 'function') {
        cancelAnimationFrame(pending);
      }
      fitFrameRef.current = null;
      chartRef.current = null;
      seriesRef.current = null;
      markersPluginRef.current = null;
      priceLinesRef.current = [];
      try {
        chart?.remove();
      } catch {
        // Nothing left to release.
      }
    };
    // Mount-only by contract: the data effect below pushes every update into
    // the instance created here.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Data: one effect for the series, the markers and the price lines.
  useEffect(() => {
    const series = seriesRef.current;
    if (series === null) {
      return;
    }
    const theme = chartTheme();
    try {
      series.setData(toChartData(rendered));
      markersPluginRef.current?.setMarkers(toLibraryMarkers(markers, theme));

      // Price lines are recreated from scratch: the previous ones belong to a
      // position that may have been closed since the last poll.
      for (const line of priceLinesRef.current) {
        series.removePriceLine(line);
      }
      priceLinesRef.current = priceLines
        .filter((line) => Number.isFinite(line.price))
        .map((line) =>
          series.createPriceLine(
            pruneUndefined({
              price: line.price,
              color: resolveColor(line.color, theme),
              lineWidth: 1,
              lineStyle: line.lineStyle === 'dashed' ? LINE_STYLE_DASHED : LINE_STYLE_SOLID,
              axisLabelVisible: true,
              title: line.title,
            }),
          ),
        );

      candleCountRef.current = rendered.length;
      scheduleFit();
    } catch {
      // A rejected update keeps the previous series on screen.
    }
  }, [markers, priceLines, rendered, scheduleFit]);

  // The canvas follows its container; the observer is released on unmount.
  useEffect(() => {
    const container = containerRef.current;
    const chart = chartRef.current;
    if (container === null || chart === null || typeof ResizeObserver === 'undefined') {
      return;
    }
    let width = containerWidth(container, FALLBACK_WIDTH);
    const observer = new ResizeObserver(() => {
      const measured = containerWidth(container, width);
      if (measured === width) {
        return;
      }
      width = measured;
      try {
        chart.applyOptions({ width, height: heightValue });
      } catch {
        // A resize that cannot be applied leaves the previous canvas size.
      }
    });
    observer.observe(container);
    return () => {
      observer.disconnect();
    };
  }, [heightValue]);

  const zoom = useCallback((factor: number): void => {
    const chart = chartRef.current;
    const total = candleCountRef.current;
    if (chart === null || total === 0) {
      return;
    }
    const current = windowRef.current ?? total;
    const next =
      factor < 1
        ? Math.max(MIN_VISIBLE_CANDLES, Math.floor(current * factor))
        : Math.min(total, Math.ceil(current * factor));
    windowRef.current = next;
    autoFitRef.current = false;
    chart.timeScale().setVisibleLogicalRange({
      from: Math.max(0, total - next),
      to: total - 1,
    });
  }, []);

  const zoomIn = useCallback((): void => {
    zoom(1 / ZOOM_FACTOR);
  }, [zoom]);

  const zoomOut = useCallback((): void => {
    zoom(ZOOM_FACTOR);
  }, [zoom]);

  const resetZoom = useCallback((): void => {
    const chart = chartRef.current;
    if (chart === null) {
      return;
    }
    windowRef.current = null;
    autoFitRef.current = true;
    chart.timeScale().fitContent();
  }, []);

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>): void => {
      switch (event.key) {
        case '+':
        case '=':
        case 'ArrowUp':
          event.preventDefault();
          zoomIn();
          break;
        case '-':
        case '_':
        case 'ArrowDown':
          event.preventDefault();
          zoomOut();
          break;
        case '0':
          event.preventDefault();
          resetZoom();
          break;
        default:
          break;
      }
    },
    [resetZoom, zoomIn, zoomOut],
  );

  const columns: DataTableColumn<Candle>[] = useMemo(
    () => [
      {
        key: 'timestamp',
        header: 'Timestamp',
        render: (row) => formatTimestamp(row.timestamp),
      },
      {
        key: 'body',
        header: 'Body',
        render: (row) =>
          candleDirection(row) === 'bear' ? 'Bearish (filled)' : 'Bullish (hollow)',
      },
      { key: 'open', header: 'Open', numeric: true, render: (row) => formatNumber(row.open) },
      { key: 'high', header: 'High', numeric: true, render: (row) => formatNumber(row.high) },
      { key: 'low', header: 'Low', numeric: true, render: (row) => formatNumber(row.low) },
      { key: 'close', header: 'Close', numeric: true, render: (row) => formatNumber(row.close) },
      {
        key: 'volume',
        header: 'Volume',
        numeric: true,
        render: (row) => formatQuantity(row.volume),
      },
    ],
    [],
  );

  return (
    <div className={cn('flex flex-col gap-md', className)}>
      <div className="flex flex-wrap items-center justify-between gap-sm">
        <p className="font-mono text-xs text-muted-foreground">
          {formatQuantity(rendered.length)} candles · hollow body = bullish, filled body = bearish
        </p>
        <div role="group" aria-label="Chart zoom" className="flex items-center gap-xs">
          <Button
            size="sm"
            variant="ghost"
            aria-label="Zoom in"
            onClick={zoomIn}
            icon={<Plus className="size-3.5" />}
          />
          <Button
            size="sm"
            variant="ghost"
            aria-label="Zoom out"
            onClick={zoomOut}
            icon={<Minus className="size-3.5" />}
          />
          <Button
            size="sm"
            variant="ghost"
            aria-label="Reset zoom"
            onClick={resetZoom}
            icon={<Maximize2 className="size-3.5" />}
          />
        </div>
      </div>

      {/*
        The keyboard shortcuts are handled on the wrapper, so they work whether
        the chart surface itself or one of the zoom buttons has focus. The
        controls stay OUTSIDE the `role="img"` element: children of an image role
        are presentational, and a control a screen reader cannot reach is not a
        keyboard-accessible control.
      */}
      <div onKeyDown={handleKeyDown} className="w-full">
        <div
          role="img"
          aria-label={ariaLabel}
          tabIndex={0}
          className={cn(
            'relative w-full rounded-card',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        >
          {/* The canvas is decorative: the wrapper names the figure and the
              table below repeats every value, so no `aria-hidden` is needed —
              and the library's attribution link must stay reachable. */}
          <div
            ref={containerRef}
            data-testid="candlestick-canvas"
            style={{ height: `${heightValue}px` }}
            className={cn('w-full', rendered.length === 0 && 'hidden')}
          />
        </div>
      </div>

      {rendered.length === 0 ? (
        <EmptyState
          title="No candle yet"
          description="The chart appears as soon as this profile has processed at least one candle."
        />
      ) : null}

      <details className="rounded-card border border-border bg-card/50 px-lg py-md">
        <summary
          className={cn(
            'cursor-pointer font-mono text-xs font-medium text-muted-foreground',
            // A real click target: it acknowledges the press like every other
            // control of the dashboard, without moving or resizing anything.
            'active:bg-muted-pressed active:text-foreground',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        >
          Candle data
        </summary>
        <div className="mt-md">
          <DataTable
            columns={columns}
            rows={rendered}
            rowKey={(row) => row.timestamp}
            caption="Candlestick data"
            emptyMessage="No candle recorded"
          />
        </div>
      </details>
    </div>
  );
}
