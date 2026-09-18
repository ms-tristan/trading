import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Order } from '@/lib/types';

import { OrdersTable } from './orders-table';

const FILLED_ORDER: Order = {
  client_order_id: 'ord-1',
  profile_id: 'btc-paper',
  symbol: 'BTC/USDT',
  side: 'buy',
  type: 'limit',
  quantity: 0.25,
  state: 'filled',
  mode: 'paper',
  created_at: '2024-01-01T00:00:00+00:00',
  updated_at: '2024-01-01T00:00:05+00:00',
  filled_quantity: 0.25,
  price: 20000,
  average_fill_price: 19999.5,
  broker_order_id: 'brk-1',
  reject_reason: '',
};

const REJECTED_ORDER: Order = {
  ...FILLED_ORDER,
  client_order_id: 'ord-2',
  side: 'sell',
  type: 'market',
  quantity: null,
  state: 'rejected',
  filled_quantity: null,
  average_fill_price: null,
  broker_order_id: null,
  reject_reason: 'insufficient balance',
};

const PENDING_ORDER: Order = {
  ...FILLED_ORDER,
  client_order_id: 'ord-3',
  state: 'partially_filled',
};

describe('OrdersTable', () => {
  it('renders a caption and one formatted row per order', () => {
    const { container } = render(<OrdersTable orders={[FILLED_ORDER]} />);

    expect(screen.getByRole('table', { name: 'Recent orders' })).toBeInTheDocument();
    expect(screen.getByText('2024-01-01 00:00:00 UTC')).toBeInTheDocument();
    expect(screen.getByText('Buy')).toBeInTheDocument();
    expect(screen.getByText('Limit')).toBeInTheDocument();
    // Quantity and filled quantity carry the same amount.
    expect(screen.getAllByText('0.25')).toHaveLength(2);
    expect(screen.getByText('$19,999.50')).toBeInTheDocument();
    // Side icon + state badge icon.
    expect(container.querySelectorAll('svg').length).toBeGreaterThanOrEqual(2);
  });

  it('renders the order state as a badge with its tone', () => {
    render(<OrdersTable orders={[FILLED_ORDER, REJECTED_ORDER, PENDING_ORDER]} />);

    expect(screen.getByText('Filled').closest('[data-tone="ok"]')).not.toBeNull();
    expect(screen.getByText('Rejected').closest('[data-tone="error"]')).not.toBeNull();
    expect(screen.getByText('Partially filled').closest('[data-tone="warn"]')).not.toBeNull();
  });

  it('renders the reject reason and an em dash for the absent values', () => {
    render(<OrdersTable orders={[REJECTED_ORDER]} />);

    const row = screen.getAllByRole('row')[1] as HTMLElement;
    expect(within(row).getByText('Sell')).toBeInTheDocument();
    expect(within(row).getByText('Market')).toBeInTheDocument();
    expect(within(row).getByText('insufficient balance')).toBeInTheDocument();
    // quantity, filled quantity and average fill price are all absent.
    expect(within(row).getAllByText('—')).toHaveLength(3);
  });

  it('renders an em dash when an order carries no reject reason', () => {
    render(<OrdersTable orders={[FILLED_ORDER]} />);

    const row = screen.getAllByRole('row')[1] as HTMLElement;
    expect(within(row).getByText('—')).toBeInTheDocument();
  });

  it('renders its empty message when there is no order', () => {
    render(<OrdersTable orders={[]} />);

    expect(screen.getByRole('table', { name: 'Recent orders' })).toBeInTheDocument();
    expect(screen.getByText('No order yet')).toBeInTheDocument();
  });
});
