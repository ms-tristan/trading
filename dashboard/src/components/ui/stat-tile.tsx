import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';
import { EMPTY_PLACEHOLDER } from '@/lib/format';

/** Direction a stat tile can report. */
export type StatTrend = 'up' | 'down' | 'flat';

/** Props of {@link StatTile}. */
export interface StatTileProps {
  /** What the value measures. */
  label: string;
  /** Already formatted value (use `format*` helpers; absent values show an em dash). */
  value: string;
  /** Secondary line, for example the period the value covers. */
  hint?: string;
  /** Direction of the value: colour, icon and text label are always paired. */
  trend?: StatTrend;
  /** Decorative icon (`lucide-react`). */
  icon?: ReactNode;
}

const TREND_CLASSES: Record<StatTrend, string> = {
  up: 'text-profit',
  down: 'text-loss',
  flat: 'text-muted-foreground',
};

const TREND_LABELS: Record<StatTrend, string> = {
  up: 'Up',
  down: 'Down',
  flat: 'Flat',
};

const TREND_GLYPHS: Record<StatTrend, string> = {
  up: '▲',
  down: '▼',
  flat: '■',
};

/**
 * One dense key/value tile of the dashboard.
 *
 * The trend is never conveyed by colour alone: the tile renders a glyph, the
 * matching text label and the colour together.
 */
export function StatTile({ label, value, hint, trend, icon }: StatTileProps) {
  return (
    <div className="rounded-card border border-border bg-card p-lg shadow-sm">
      <div className="flex items-start justify-between gap-sm">
        <span className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        {icon !== undefined ? (
          <span aria-hidden="true" className="inline-flex shrink-0 text-muted-foreground">
            {icon}
          </span>
        ) : null}
      </div>
      <p className="mt-sm font-mono text-xl tabular-nums text-foreground">
        {value.trim() === '' ? EMPTY_PLACEHOLDER : value}
      </p>
      {trend !== undefined || hint !== undefined ? (
        <p className="mt-xs flex flex-wrap items-center gap-xs text-xs">
          {trend !== undefined ? (
            <span className={cn('inline-flex items-center gap-xs', TREND_CLASSES[trend])}>
              <span aria-hidden="true" className="font-mono">
                {TREND_GLYPHS[trend]}
              </span>
              <span>{TREND_LABELS[trend]}</span>
            </span>
          ) : null}
          {hint !== undefined ? <span className="text-muted-foreground">{hint}</span> : null}
        </p>
      ) : null}
    </div>
  );
}
