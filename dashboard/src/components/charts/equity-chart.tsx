'use client';

/**
 * Equity curve of one profile: equity, cash and position value over time.
 *
 * Design decisions that the rest of the dashboard relies on:
 *
 * * **inline SVG, no charting dependency** — a fixed `viewBox` (800 x 320 by
 *   default) with `h-auto w-full`, so the curve is responsive without a resize
 *   observer and deterministic under jsdom;
 * * **the curve has a text alternative** — the focused point is announced in a
 *   polite live region, and a `<details>` block repeats every point as a table,
 *   so the chart is never the only way to read the data;
 * * **keyboard and pointer share one crosshair** — the chart is focusable and
 *   ArrowLeft/ArrowRight/Home/End move the same index a pointer hover selects;
 * * **meaning never rests on colour** — the legend labels each series in text and
 *   draws it with its own line style.
 */

import { useCallback, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';

import { EmptyState } from '@/components/ui/empty-state';
import { DataTable, type DataTableColumn } from '@/components/ui/data-table';
import { buildSeries, chartValues, formatAxisValue, nearestIndex, niceScale } from '@/lib/chart';
import { cn } from '@/lib/cn';
import { formatMoney, formatTimestamp } from '@/lib/format';
import type { EquityPoint } from '@/lib/types';

import { CHART_SERIES, EquityChartLegend } from './equity-chart-legend';

/** Width of the chart viewBox, in user units. */
export const EQUITY_CHART_VIEW_WIDTH = 800;

/** Default height of the chart viewBox, in user units. */
export const EQUITY_CHART_VIEW_HEIGHT = 320;

/** Inner margin of the plot area, in user units. */
export const EQUITY_CHART_PADDING = 56;

/** Requested number of grid intervals on the y axis. */
const TICK_COUNT = 4;

/** Default accessible name of the chart. */
const DEFAULT_ARIA_LABEL = 'Equity curve over time';

/** Hint shown while no point is focused. */
const IDLE_ANNOUNCEMENT = 'No point selected. Use the arrow keys to inspect the equity curve.';

/** Props of {@link EquityChart}. */
export interface EquityChartProps {
  /** The curve, oldest first, exactly as `GET /api/profiles/{id}/equity` returns it. */
  points: EquityPoint[];
  /** Height of the viewBox, in user units. */
  height?: number;
  /** Accessible name of the chart. */
  ariaLabel?: string;
  className?: string;
}

/** Announcement of one point: every series is named and formatted. */
function announcePoint(point: EquityPoint): string {
  const timestamp = formatTimestamp(point.timestamp);
  const equity = formatMoney(point.equity);
  const cash = formatMoney(point.cash);
  const positionValue = formatMoney(point.position_value);
  return `${timestamp} - equity ${equity}, cash ${cash}, position value ${positionValue}`;
}

/**
 * Interactive equity curve.
 *
 * An empty or single-point payload renders {@link EmptyState} instead of an
 * empty frame: one point cannot draw a line, and an axis around nothing is
 * noise.
 */
export function EquityChart({
  points,
  height = EQUITY_CHART_VIEW_HEIGHT,
  ariaLabel = DEFAULT_ARIA_LABEL,
  className,
}: EquityChartProps) {
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);

  const viewBoxHeight = Number.isFinite(height) && height > 0 ? height : EQUITY_CHART_VIEW_HEIGHT;

  const scale = useMemo(() => niceScale(chartValues(points), TICK_COUNT), [points]);

  const series = useMemo(
    () =>
      CHART_SERIES.map((descriptor) => ({
        descriptor,
        geometry: buildSeries(
          points,
          descriptor.key,
          EQUITY_CHART_VIEW_WIDTH,
          viewBoxHeight,
          EQUITY_CHART_PADDING,
        ),
      })),
    [points, viewBoxHeight],
  );

  const yFor = useCallback(
    (value: number): number => {
      const span = scale.max - scale.min;
      const ratio = span <= 0 ? 0.5 : (value - scale.min) / span;
      return EQUITY_CHART_PADDING + (1 - ratio) * (viewBoxHeight - EQUITY_CHART_PADDING * 2);
    },
    [scale, viewBoxHeight],
  );

  const handleKeyDown = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      // The chart itself is only rendered for two points or more.
      const last = Math.max(points.length - 1, 0);
      const current = cursor === null ? last : Math.min(Math.max(cursor, 0), last);
      switch (event.key) {
        case 'ArrowLeft':
          event.preventDefault();
          setCursor(Math.max(0, current - 1));
          break;
        case 'ArrowRight':
          event.preventDefault();
          setCursor(Math.min(last, current + 1));
          break;
        case 'Home':
          event.preventDefault();
          setCursor(0);
          break;
        case 'End':
          event.preventDefault();
          setCursor(last);
          break;
        default:
          break;
      }
    },
    [cursor, points.length],
  );

  const handlePointerMove = useCallback(
    (event: PointerEvent<HTMLDivElement>) => {
      const svg = svgRef.current;
      const baseCoords = series[0].geometry.coords;
      if (svg === null) {
        return;
      }
      const rect = svg.getBoundingClientRect();
      if (!Number.isFinite(rect.width) || rect.width <= 0) {
        return;
      }
      const x = ((event.clientX - rect.left) / rect.width) * EQUITY_CHART_VIEW_WIDTH;
      // `coords` carries one entry per point, so the index is a point index.
      setCursor(nearestIndex(baseCoords, x));
    },
    [series],
  );

  if (points.length < 2) {
    return (
      <EmptyState
        title="No equity point yet"
        description="The curve appears as soon as the profile has recorded at least two equity points."
        className={className}
      />
    );
  }

  const focused = cursor !== null && cursor >= 0 && cursor < points.length ? points[cursor] : null;
  const focusedX = cursor !== null ? series[0]?.geometry.coords[cursor]?.x : undefined;
  const announcement = focused === null ? IDLE_ANNOUNCEMENT : announcePoint(focused);

  const columns: DataTableColumn<EquityPoint>[] = [
    { key: 'timestamp', header: 'Time', render: (row) => formatTimestamp(row.timestamp) },
    {
      key: 'equity',
      header: 'Equity',
      numeric: true,
      render: (row) => formatMoney(row.equity),
    },
    { key: 'cash', header: 'Cash', numeric: true, render: (row) => formatMoney(row.cash) },
    {
      key: 'position_value',
      header: 'Position value',
      numeric: true,
      render: (row) => formatMoney(row.position_value),
    },
  ];

  const first = points[0];
  const last = points[points.length - 1];
  return (
    <div className={cn('flex flex-col gap-md', className)}>
      <div
        role="img"
        aria-label={ariaLabel}
        tabIndex={0}
        onKeyDown={handleKeyDown}
        onPointerMove={handlePointerMove}
        className={cn(
          'relative w-full cursor-crosshair rounded-card',
          'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        )}
      >
        <svg
          ref={svgRef}
          aria-hidden="true"
          focusable="false"
          viewBox={`0 0 ${EQUITY_CHART_VIEW_WIDTH} ${viewBoxHeight}`}
          className="block h-auto w-full"
        >
          <g>
            {scale.ticks.map((tick) => (
              <line
                key={`grid-${tick}`}
                x1={EQUITY_CHART_PADDING}
                x2={EQUITY_CHART_VIEW_WIDTH - EQUITY_CHART_PADDING}
                y1={yFor(tick)}
                y2={yFor(tick)}
                stroke="var(--color-chart-grid)"
                strokeWidth="1"
                vectorEffect="non-scaling-stroke"
              />
            ))}
          </g>

          {focusedX !== undefined && Number.isFinite(focusedX) ? (
            <g>
              <line
                x1={focusedX}
                x2={focusedX}
                y1={EQUITY_CHART_PADDING}
                y2={viewBoxHeight - EQUITY_CHART_PADDING}
                stroke="var(--color-chart-axis)"
                strokeWidth="1"
                strokeDasharray="3 3"
                vectorEffect="non-scaling-stroke"
              />
              {series.map(({ descriptor, geometry }) => {
                const y = cursor === null ? undefined : geometry.coords[cursor]?.y;
                if (y === undefined || !Number.isFinite(y)) {
                  return null;
                }
                return (
                  <circle
                    key={`marker-${descriptor.key}`}
                    cx={focusedX}
                    cy={y}
                    r="3.5"
                    fill={descriptor.color}
                    stroke="var(--color-background)"
                    strokeWidth="1.5"
                    vectorEffect="non-scaling-stroke"
                  />
                );
              })}
            </g>
          ) : null}

          {series.map(({ descriptor, geometry }) => (
            <path
              key={descriptor.key}
              d={geometry.path}
              fill="none"
              stroke={descriptor.color}
              strokeWidth="2"
              strokeDasharray={descriptor.dash}
              strokeLinecap="round"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
              className="motion-safe:transition-opacity motion-safe:duration-200"
            />
          ))}
        </svg>

        {/* Axis values are overlay text, so they stay crisp and readable at every
            width instead of being scaled by the viewBox. */}
        <div aria-hidden="true" className="pointer-events-none absolute inset-0">
          {scale.ticks.map((tick) => (
            <span
              key={`tick-${tick}`}
              style={{
                top: `${(yFor(tick) / viewBoxHeight) * 100}%`,
                width: `${(EQUITY_CHART_PADDING / EQUITY_CHART_VIEW_WIDTH) * 100}%`,
              }}
              className="absolute left-0 -translate-y-1/2 pr-sm text-right font-mono text-[10px] leading-none tabular-nums text-muted-foreground sm:text-xs"
            >
              {formatAxisValue(tick)}
            </span>
          ))}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-x-lg gap-y-xs font-mono text-[10px] tabular-nums text-muted-foreground sm:text-xs">
        <span>{formatTimestamp(first?.timestamp)}</span>
        <span>{formatTimestamp(last?.timestamp)}</span>
      </div>

      <EquityChartLegend />

      <p role="status" aria-live="polite" className="font-mono text-xs text-muted-foreground">
        {announcement}
      </p>

      <details className="rounded-card border border-border bg-card/50 px-lg py-md">
        <summary
          className={cn(
            'cursor-pointer font-mono text-xs font-medium text-muted-foreground',
            // A real click target, like the candle-data disclosure: it
            // acknowledges the press without moving or resizing anything.
            'active:bg-muted-pressed active:text-foreground',
            'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
          )}
        >
          Data table
        </summary>
        <div className="mt-md">
          <DataTable
            columns={columns}
            rows={points}
            rowKey={(row) => row.timestamp}
            caption="Equity curve data"
            emptyMessage="No equity point yet"
          />
        </div>
      </details>
    </div>
  );
}
