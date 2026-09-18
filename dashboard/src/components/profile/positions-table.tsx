import { Minus, TrendingDown, TrendingUp } from 'lucide-react';
import type { ReactNode } from 'react';

import { DataTable, type DataTableColumn } from '@/components/ui/data-table';
import { cn } from '@/lib/cn';
import { formatMoney, formatQuantity, formatSignedMoney, formatTimestamp, trendOf } from '@/lib/format';
import type { Direction, Position } from '@/lib/types';

/** Props of {@link PositionsTable}. */
export interface PositionsTableProps {
  /** Open positions of one profile, exactly as the API returns them. */
  positions: Position[];
  className?: string;
}

const TREND_CLASSES = {
  up: 'text-profit',
  down: 'text-loss',
  flat: 'text-muted-foreground',
} as const;

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

/** Signed money plus a direction icon, so the sign is never colour-only. */
function PnlValue({ value }: { value: number | null }): ReactNode {
  const trend = trendOf(value);
  const Icon = trend === 'up' ? TrendingUp : trend === 'down' ? TrendingDown : Minus;
  return (
    <span className={cn('inline-flex items-center gap-xs', TREND_CLASSES[trend])}>
      <Icon aria-hidden="true" className="size-3.5 shrink-0" />
      <span>{formatSignedMoney(value)}</span>
    </span>
  );
}

/**
 * Open positions of one profile.
 *
 * Pure and props-driven: the parent (a Client Component) owns the polling, this
 * table only renders what it is given.
 */
export function PositionsTable({ positions, className }: PositionsTableProps) {
  const columns: DataTableColumn<Position>[] = [
    {
      key: 'direction',
      header: 'Direction',
      render: (row) => <DirectionLabel direction={row.direction} />,
    },
    {
      key: 'quantity',
      header: 'Quantity',
      numeric: true,
      render: (row) => formatQuantity(row.quantity),
    },
    {
      key: 'average_price',
      header: 'Average price',
      numeric: true,
      render: (row) => formatMoney(row.average_price),
    },
    {
      key: 'unrealized_pnl',
      header: 'Unrealized PnL',
      numeric: true,
      render: (row) => <PnlValue value={row.unrealized_pnl} />,
    },
    {
      key: 'realized_pnl',
      header: 'Realized PnL',
      numeric: true,
      render: (row) => <PnlValue value={row.realized_pnl} />,
    },
    {
      key: 'stop_price',
      header: 'Stop price',
      numeric: true,
      render: (row) => formatMoney(row.stop_price),
    },
    {
      key: 'opened_at',
      header: 'Opened at',
      render: (row) => formatTimestamp(row.opened_at),
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={positions}
      rowKey={(row) => `${row.profile_id}:${row.symbol}:${row.opened_at}`}
      caption="Open positions"
      emptyMessage="No open position"
      className={className}
    />
  );
}
