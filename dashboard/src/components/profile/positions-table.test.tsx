import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { Position } from '@/lib/types';

import { PositionsTable } from './positions-table';

const LONG_POSITION: Position = {
  profile_id: 'btc-paper',
  symbol: 'BTC/USDT',
  quantity: 1.5,
  average_price: 20000,
  direction: 'long',
  opened_at: '2024-01-01T00:00:00+00:00',
  updated_at: '2024-01-01T01:00:00+00:00',
  realized_pnl: 12.5,
  unrealized_pnl: -25.75,
  stop_price: 19000,
};

const SHORT_POSITION: Position = {
  profile_id: 'btc-paper',
  symbol: 'ETH/USDT',
  quantity: null,
  average_price: null,
  direction: 'short',
  opened_at: '2024-01-02T00:00:00+00:00',
  updated_at: '2024-01-02T00:00:00+00:00',
  realized_pnl: null,
  unrealized_pnl: null,
  stop_price: null,
};

describe('PositionsTable', () => {
  it('renders a caption and one formatted row per position', () => {
    render(<PositionsTable positions={[LONG_POSITION]} />);

    const table = screen.getByRole('table', { name: 'Open positions' });
    expect(table).toBeInTheDocument();
    expect(screen.getByText('Long')).toBeInTheDocument();
    expect(screen.getByText('1.5')).toBeInTheDocument();
    expect(screen.getByText('$20,000.00')).toBeInTheDocument();
    expect(screen.getByText('$19,000.00')).toBeInTheDocument();
    expect(screen.getByText('2024-01-01 00:00:00 UTC')).toBeInTheDocument();
  });

  it('renders PnL with an explicit sign and a direction icon, never colour alone', () => {
    const { container } = render(<PositionsTable positions={[LONG_POSITION]} />);

    expect(screen.getByText('+$12.50')).toBeInTheDocument();
    expect(screen.getByText('-$25.75')).toBeInTheDocument();
    // direction icon + one icon per PnL cell.
    expect(container.querySelectorAll('svg').length).toBeGreaterThanOrEqual(3);
  });

  it('renders a flat PnL without a sign and without pretending it is a gain', () => {
    render(<PositionsTable positions={[{ ...LONG_POSITION, unrealized_pnl: 0, realized_pnl: 0 }]} />);

    expect(screen.getAllByText('$0.00')).toHaveLength(2);
    expect(screen.queryByText('+$0.00')).toBeNull();
    expect(screen.queryByText('-$0.00')).toBeNull();
  });

  it('renders an em dash for every absent value', () => {
    render(<PositionsTable positions={[SHORT_POSITION]} />);

    expect(screen.getByText('Short')).toBeInTheDocument();
    expect(screen.queryByText('NaN')).toBeNull();
    expect(screen.queryByText('undefined')).toBeNull();
    expect(screen.getAllByText('—')).toHaveLength(5);
  });

  it('renders its empty message when there is no position', () => {
    render(<PositionsTable positions={[]} />);

    expect(screen.getByRole('table', { name: 'Open positions' })).toBeInTheDocument();
    expect(screen.getByText('No open position')).toBeInTheDocument();
  });
});
