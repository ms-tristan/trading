import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { TradeRecord } from '@/lib/types';

import { TradesTable } from './trades-table';

const WINNING_TRADE: TradeRecord = {
  entry_time: '2024-01-01T00:00:00+00:00',
  exit_time: '2024-01-01T01:30:00+00:00',
  entry_price: 20000,
  exit_price: 20500,
  size: 0.5,
  direction: 'long',
  pnl: 123.456,
  pnl_pct: 0.0123,
  fees: 4.5,
  exit_reason: 'take_profit',
  duration_minutes: 90,
  stop_price: 19500,
  take_profit_price: 21000,
  params_id: 'default',
};

const LOSING_TRADE: TradeRecord = {
  ...WINNING_TRADE,
  entry_time: '2024-01-02T00:00:00+00:00',
  exit_time: '2024-01-02T00:10:00+00:00',
  direction: 'short',
  pnl: -50,
  pnl_pct: -0.05,
  exit_reason: 'stop_loss',
  duration_minutes: 10,
};

describe('TradesTable', () => {
  it('renders a caption and one formatted row per trade', () => {
    render(<TradesTable trades={[WINNING_TRADE]} />);

    expect(screen.getByRole('table', { name: 'Closed trades' })).toBeInTheDocument();
    expect(screen.getByText('2024-01-01 01:30:00 UTC')).toBeInTheDocument();
    expect(screen.getByText('Long')).toBeInTheDocument();
    expect(screen.getByText('0.5')).toBeInTheDocument();
    expect(screen.getByText('$20,000.00')).toBeInTheDocument();
    expect(screen.getByText('$20,500.00')).toBeInTheDocument();
    expect(screen.getByText('$4.50')).toBeInTheDocument();
    expect(screen.getByText('Take profit')).toBeInTheDocument();
    // The API counts minutes; the column renders a duration.
    expect(screen.getByText('01:30:00')).toBeInTheDocument();
  });

  it('renders PnL and PnL % with an explicit sign', () => {
    render(<TradesTable trades={[WINNING_TRADE, LOSING_TRADE]} />);

    expect(screen.getByText('+$123.46')).toBeInTheDocument();
    expect(screen.getByText('+1.23%')).toBeInTheDocument();
    expect(screen.getByText('-$50.00')).toBeInTheDocument();
    expect(screen.getByText('-5.00%')).toBeInTheDocument();
    expect(screen.getByText('Stop loss')).toBeInTheDocument();
  });

  it('keeps the newest first order the API returns', () => {
    render(<TradesTable trades={[LOSING_TRADE, WINNING_TRADE]} />);

    const rows = screen.getAllByRole('row').slice(1);
    expect(rows[0]).toHaveTextContent('2024-01-02 00:10:00 UTC');
    expect(rows[1]).toHaveTextContent('2024-01-01 01:30:00 UTC');
  });

  it('renders its empty message when there is no trade', () => {
    render(<TradesTable trades={[]} />);

    expect(screen.getByRole('table', { name: 'Closed trades' })).toBeInTheDocument();
    expect(screen.getByText('No closed trade yet')).toBeInTheDocument();
  });
});
