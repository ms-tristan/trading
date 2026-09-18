import { Minus, TrendingDown, TrendingUp } from 'lucide-react';

import { DataTable, type DataTableColumn } from '@/components/ui/data-table';
import { cn } from '@/lib/cn';
import {
  EMPTY_PLACEHOLDER,
  formatDuration,
  formatMoney,
  formatQuantity,
  formatRatioAsPercent,
  formatSignedMoney,
  formatTimestamp,
  trendOf,
} from '@/lib/format';
import type { Direction, ExitReason, TradeRecord } from '@/lib/types';

/** Props of {@link TradesTable}. */
export interface TradesTableProps {
  /** Closed round-trips, newest first as the API returns them. */
  trades: TradeRecord[];
  className?: string;
}

const TREND_CLASSES = {
  up: 'text-profit',
  down: 'text-loss',
  flat: 'text-muted-foreground',
} as const;

/** Readable label of every documented exit reason. */
const EXIT_REASON_LABELS: Record<ExitReason, string> = {
  stop_loss: 'Stop loss',
  take_profit: 'Take profit',
  signal: 'Signal',
  end_of_data: 'End of data',
  max_duration: 'Max duration',
};

/** Readable label of an exit reason, including an unexpected value. */
function exitReasonLabel(reason: ExitReason): string {
  const known: string | undefined = EXIT_REASON_LABELS[reason];
  if (known !== undefined) {
    return known;
  }
  const words = String(reason).replace(/[_-]+/g, ' ').trim();
  return words === '' ? EMPTY_PLACEHOLDER : words.charAt(0).toUpperCase() + words.slice(1);
}

/** Direction as a text label plus an icon: never as a colour alone. */
function DirectionLabel({ direction }: { direction: Direction }) {
  const isLong = direction === 'long';
  return (
    <span className="inline-flex items-center gap-xs">
      {isLong ? (
        <TrendingUp aria-hidden="true" className="size-3.5 shrink-0 text-profit" />
      ) : (
        <TrendingDown aria-hidden="true" className="size-3.5 shrink-0 text-loss" />
      )}
      <span>{isLong ? 'Long' : 'Short'}</span>
    </span>
  );
}

/**
 * Closed trades of one profile.
 *
 * Pure and props-driven: the parent (a Client Component) owns the polling.
 */
export function TradesTable({ trades, className }: TradesTableProps) {
  const columns: DataTableColumn<TradeRecord>[] = [
    {
      key: 'exit_time',
      header: 'Exit time',
      render: (row) => formatTimestamp(row.exit_time),
    },
    {
      key: 'direction',
      header: 'Direction',
      render: (row) => <DirectionLabel direction={row.direction} />,
    },
    { key: 'size', header: 'Size', numeric: true, render: (row) => formatQuantity(row.size) },
    {
      key: 'entry_price',
      header: 'Entry price',
      numeric: true,
      render: (row) => formatMoney(row.entry_price),
    },
    {
      key: 'exit_price',
      header: 'Exit price',
      numeric: true,
      render: (row) => formatMoney(row.exit_price),
    },
    {
      key: 'pnl',
      header: 'PnL',
      numeric: true,
      render: (row) => {
        const trend = trendOf(row.pnl);
        const Icon = trend === 'up' ? TrendingUp : trend === 'down' ? TrendingDown : Minus;
        return (
          <span className={cn('inline-flex items-center gap-xs', TREND_CLASSES[trend])}>
            <Icon aria-hidden="true" className="size-3.5 shrink-0" />
            <span>{formatSignedMoney(row.pnl)}</span>
          </span>
        );
      },
    },
    {
      key: 'pnl_pct',
      header: 'PnL %',
      numeric: true,
      render: (row) => (
        <span className={TREND_CLASSES[trendOf(row.pnl_pct)]}>
          {formatRatioAsPercent(row.pnl_pct, { signed: true })}
        </span>
      ),
    },
    { key: 'fees', header: 'Fees', numeric: true, render: (row) => formatMoney(row.fees) },
    {
      key: 'exit_reason',
      header: 'Exit reason',
      render: (row) => exitReasonLabel(row.exit_reason),
    },
    {
      key: 'duration_minutes',
      header: 'Duration',
      numeric: true,
      // The API counts minutes, the formatter renders seconds.
      render: (row) => formatDuration(row.duration_minutes * 60),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={trades}
      rowKey={(row) => `${row.entry_time}:${row.exit_time}`}
      caption="Closed trades"
      emptyMessage="No closed trade yet"
      className={className}
    />
  );
}
