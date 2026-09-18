import { cn } from '@/lib/cn';
import type { SeriesKey } from '@/lib/chart';

/**
 * One drawable series of the equity chart.
 *
 * The colour is a design token reference (never a raw hex), and the dash pattern
 * is part of the contract: the three series must stay distinguishable without
 * colour, so each one also carries a distinct line style.
 */
export interface ChartSeriesDescriptor {
  /** Key of the series inside an equity point. */
  key: SeriesKey;
  /** Legend label. */
  label: string;
  /** CSS custom property holding the series colour. */
  color: string;
  /** SVG `stroke-dasharray` value; solid when omitted. */
  dash?: string;
}

/** The three series of the equity chart, in draw order and legend order. */
export const CHART_SERIES: readonly ChartSeriesDescriptor[] = [
  { key: 'equity', label: 'Equity', color: 'var(--color-chart-equity)' },
  { key: 'cash', label: 'Cash', color: 'var(--color-chart-cash)', dash: '6 3' },
  {
    key: 'position_value',
    label: 'Position value',
    color: 'var(--color-chart-position)',
    dash: '2 3',
  },
];

/** Props of {@link EquityChartLegend}. */
export interface EquityChartLegendProps {
  className?: string;
}

/**
 * Legend of the equity chart.
 *
 * Every series is named in text **and** drawn with its own line style (solid,
 * dashed, dotted), so meaning never rests on colour alone. The swatches are
 * decorative inline SVG, not emoji.
 */
export function EquityChartLegend({ className }: EquityChartLegendProps) {
  return (
    <ul
      aria-label="Chart series"
      className={cn('flex flex-wrap items-center gap-x-lg gap-y-sm', className)}
    >
      {CHART_SERIES.map((series) => (
        <li key={series.key} className="flex items-center gap-sm text-xs text-muted-foreground">
          <svg aria-hidden="true" focusable="false" width="24" height="8" viewBox="0 0 24 8">
            <line
              x1="0"
              y1="4"
              x2="24"
              y2="4"
              stroke={series.color}
              strokeWidth="2"
              strokeDasharray={series.dash}
              strokeLinecap="round"
            />
          </svg>
          <span>{series.label}</span>
        </li>
      ))}
    </ul>
  );
}
