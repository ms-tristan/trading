import type { JSX } from 'react';

import { cn } from '@/lib/cn';

/**
 * One entry of the candlestick legend.
 *
 * `variant` drives the little SVG swatch: a hollow body, a filled body, an
 * upward arrow, a downward arrow, a solid line and a dashed line. The swatch is
 * how the chart draws it — so the legend is a faithful key, not a decorative
 * list of colours.
 */
export interface CandleLegendEntry {
  /** Row label, also the text that makes colour non-essential. */
  label: string;
  /** Which glyph to draw in front of the label. */
  variant: 'hollow' | 'filled' | 'entry' | 'exit' | 'solid' | 'dashed';
  /** CSS colour reference of the design token set (never a raw hex). */
  color: string;
}

/** Every entry of the legend, in display order. */
export const CANDLE_LEGEND_ENTRIES: readonly CandleLegendEntry[] = [
  { label: 'Bullish (hollow)', variant: 'hollow', color: 'var(--color-chart-bull)' },
  { label: 'Bearish (filled)', variant: 'filled', color: 'var(--color-chart-bear)' },
  { label: 'Entry', variant: 'entry', color: 'var(--color-chart-marker-entry)' },
  { label: 'Exit', variant: 'exit', color: 'var(--color-chart-marker-exit)' },
  { label: 'Average price', variant: 'solid', color: 'var(--color-chart-average)' },
  { label: 'Stop loss', variant: 'dashed', color: 'var(--color-chart-stop)' },
];

/** Props of {@link CandlestickLegend}. */
export interface CandlestickLegendProps {
  className?: string;
}

/** Decorative swatch of one legend entry (an inline SVG, never an emoji). */
function LegendSwatch({ entry }: { entry: CandleLegendEntry }): JSX.Element {
  const body = { fill: 'none', stroke: entry.color, strokeWidth: 1.5 } as const;
  return (
    <svg aria-hidden="true" focusable="false" width="20" height="12" viewBox="0 0 20 12">
      {entry.variant === 'hollow' ? <rect x="7" y="2" width="6" height="8" {...body} /> : null}
      {entry.variant === 'filled' ? (
        <rect x="7" y="2" width="6" height="8" fill={entry.color} stroke={entry.color} strokeWidth="1.5" />
      ) : null}
      {entry.variant === 'entry' ? (
        <path d="M10 1 L15 8 L5 8 Z" fill={entry.color} />
      ) : null}
      {entry.variant === 'exit' ? (
        <path d="M10 11 L15 4 L5 4 Z" fill={entry.color} />
      ) : null}
      {entry.variant === 'solid' ? (
        <line x1="1" y1="6" x2="19" y2="6" stroke={entry.color} strokeWidth="2" strokeLinecap="round" />
      ) : null}
      {entry.variant === 'dashed' ? (
        <line
          x1="1"
          y1="6"
          x2="19"
          y2="6"
          stroke={entry.color}
          strokeWidth="2"
          strokeDasharray="4 3"
          strokeLinecap="round"
        />
      ) : null}
    </svg>
  );
}

/**
 * Legend of the candlestick chart.
 *
 * Every entry is named in text *and* drawn as its own glyph, which is what makes
 * the chart readable when colour is not available: a hollow box against a filled
 * one, an up arrow against a down one, a solid line against a dashed one.
 */
export function CandlestickLegend({ className }: CandlestickLegendProps) {
  return (
    <ul
      aria-label="Chart legend"
      className={cn('flex flex-wrap items-center gap-x-lg gap-y-sm', className)}
    >
      {CANDLE_LEGEND_ENTRIES.map((entry) => (
        <li key={entry.label} className="flex items-center gap-sm text-xs text-muted-foreground">
          <LegendSwatch entry={entry} />
          <span>{entry.label}</span>
        </li>
      ))}
    </ul>
  );
}
