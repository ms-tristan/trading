import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { DataTable, type DataTableColumn } from './data-table';

interface Row {
  id: string;
  symbol: string;
  pnl: number;
}

const rows: Row[] = [
  { id: 'a', symbol: 'BTC/USDT', pnl: 12.5 },
  { id: 'b', symbol: 'ETH/USDT', pnl: -3.25 },
];

const columns: DataTableColumn<Row>[] = [
  { key: 'symbol', header: 'Symbol', render: (row) => row.symbol },
  {
    key: 'pnl',
    header: 'PnL',
    numeric: true,
    render: (row) => `${row.pnl < 0 ? '-$' : '$'}${Math.abs(row.pnl).toFixed(2)}`,
  },
];

describe('DataTable', () => {
  it('names the table with its caption and renders the headers', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption="Closed trades"
        emptyMessage="No trades yet"
      />,
    );

    expect(screen.getByRole('table', { name: 'Closed trades' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'Symbol' })).toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'PnL' })).toBeInTheDocument();
  });

  it('renders one row per record through the column renderers', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption="Closed trades"
        emptyMessage="No trades yet"
      />,
    );

    expect(screen.getAllByRole('row')).toHaveLength(3); // header + 2 rows
    expect(screen.getByText('BTC/USDT')).toBeInTheDocument();
    expect(screen.getByText('-$3.25')).toBeInTheDocument();
  });

  it('marks a numeric column and right-aligns its cells', () => {
    render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption="Closed trades"
        emptyMessage="No trades yet"
      />,
    );

    expect(screen.getByRole('columnheader', { name: 'PnL' })).toHaveClass('text-right');
    expect(screen.getByRole('cell', { name: '$12.50' })).toHaveClass('text-right', 'font-mono');
  });

  it('renders the empty message instead of a body', () => {
    render(
      <DataTable
        columns={columns}
        rows={[]}
        rowKey={(row) => row.id}
        caption="Closed trades"
        emptyMessage="No trades yet"
      />,
    );

    expect(screen.getByText('No trades yet')).toBeInTheDocument();
    expect(screen.getAllByRole('row')).toHaveLength(2); // header + the empty row
  });

  it('scrolls horizontally instead of pushing the page wide', () => {
    const { container } = render(
      <DataTable
        columns={columns}
        rows={rows}
        rowKey={(row) => row.id}
        caption="Closed trades"
        emptyMessage="No trades yet"
        className="custom-table"
      />,
    );

    const wrapper = container.firstElementChild;
    expect(wrapper).toHaveClass('overflow-x-auto', 'custom-table');
  });
});
