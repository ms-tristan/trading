import { ArrowDownRight, ArrowUpRight } from 'lucide-react';

import { DataTable, type DataTableColumn } from '@/components/ui/data-table';
import { StatusBadge, orderStateTone } from '@/components/ui/status-badge';
import { EMPTY_PLACEHOLDER, formatMoney, formatQuantity, formatTimestamp } from '@/lib/format';
import type { Order, OrderSide, OrderState, OrderType } from '@/lib/types';

/** Props of {@link OrdersTable}. */
export interface OrdersTableProps {
  /** Most recent orders of one profile, newest first as the API returns them. */
  orders: Order[];
  className?: string;
}

/** Readable label of a snake_case token (`partially_filled` -> `Partially filled`). */
function humaniseToken(value: string): string {
  const words = value.replace(/[_-]+/g, ' ').trim();
  return words === '' ? EMPTY_PLACEHOLDER : words.charAt(0).toUpperCase() + words.slice(1);
}

/** Side as a text label plus an icon: never as a colour alone. */
function SideLabel({ side }: { side: OrderSide }) {
  const isBuy = side === 'buy';
  return (
    <span className="inline-flex items-center gap-xs">
      {isBuy ? (
        <ArrowUpRight aria-hidden="true" className="size-3.5 shrink-0 text-profit" />
      ) : (
        <ArrowDownRight aria-hidden="true" className="size-3.5 shrink-0 text-loss" />
      )}
      <span>{isBuy ? 'Buy' : 'Sell'}</span>
    </span>
  );
}

/** Readable label of a persisted order state. */
function orderStateLabel(state: OrderState): string {
  return humaniseToken(String(state));
}

/**
 * Recent orders of one profile.
 *
 * The state is a {@link StatusBadge}: tone, icon and text always travel
 * together. Pure and props-driven, like every table of this package.
 */
export function OrdersTable({ orders, className }: OrdersTableProps) {
  const columns: DataTableColumn<Order>[] = [
    {
      key: 'created_at',
      header: 'Created at',
      render: (row) => formatTimestamp(row.created_at),
    },
    { key: 'side', header: 'Side', render: (row) => <SideLabel side={row.side} /> },
    {
      key: 'type',
      header: 'Type',
      render: (row) => humaniseToken(String(row.type as OrderType)),
    },
    {
      key: 'quantity',
      header: 'Quantity',
      numeric: true,
      render: (row) => formatQuantity(row.quantity),
    },
    {
      key: 'state',
      header: 'State',
      render: (row) => (
        <StatusBadge label={orderStateLabel(row.state)} tone={orderStateTone(row.state)} />
      ),
    },
    {
      key: 'filled_quantity',
      header: 'Filled quantity',
      numeric: true,
      render: (row) => formatQuantity(row.filled_quantity),
    },
    {
      key: 'average_fill_price',
      header: 'Average fill price',
      numeric: true,
      render: (row) => formatMoney(row.average_fill_price),
    },
    {
      key: 'reject_reason',
      header: 'Reject reason',
      render: (row) => {
        const reason = row.reject_reason.trim();
        return reason === '' ? EMPTY_PLACEHOLDER : reason;
      },
    },
  ];

  return (
    <DataTable
      columns={columns}
      rows={orders}
      rowKey={(row) => row.client_order_id}
      caption="Recent orders"
      emptyMessage="No order yet"
      className={className}
    />
  );
}
