import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';

import { StatTile } from './stat-tile';

describe('StatTile', () => {
  it('renders a label and a value', () => {
    render(<StatTile label="Equity" value="$10,450.50" />);

    expect(screen.getByText('Equity')).toBeInTheDocument();
    expect(screen.getByText('$10,450.50')).toBeInTheDocument();
  });

  it('renders the em dash placeholder for an absent value', () => {
    render(<StatTile label="Position value" value={EMPTY_PLACEHOLDER} />);

    expect(screen.getByText(EMPTY_PLACEHOLDER)).toBeInTheDocument();
  });

  it('replaces a blank value with the em dash', () => {
    render(<StatTile label="Cash" value="   " />);
    expect(screen.getByText(EMPTY_PLACEHOLDER)).toBeInTheDocument();
  });

  it('pairs the trend colour with a glyph and a text label', () => {
    render(<StatTile label="Total return" value="+4.51%" trend="up" />);

    expect(screen.getByText('Up')).toBeInTheDocument();
    expect(screen.getByText('▲')).toHaveAttribute('aria-hidden', 'true');
  });

  it('labels a down and a flat trend as well', () => {
    const { rerender } = render(<StatTile label="Drawdown" value="-2.10%" trend="down" />);
    expect(screen.getByText('Down')).toBeInTheDocument();

    rerender(<StatTile label="Drawdown" value="0.00%" trend="flat" />);
    expect(screen.getByText('Flat')).toBeInTheDocument();
  });

  it('renders a hint and an icon when provided', () => {
    const { container } = render(
      <StatTile
        label="Uptime"
        value="01:00:00"
        hint="since 00:00 UTC"
        icon={<svg data-testid="tile-icon" />}
      />,
    );

    expect(screen.getByText('since 00:00 UTC')).toBeInTheDocument();
    expect(screen.getByTestId('tile-icon')).toBeInTheDocument();
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull();
  });

  it('omits the trend line when no trend and no hint are given', () => {
    const { container } = render(<StatTile label="Trades" value="12" />);
    expect(container.querySelectorAll('p')).toHaveLength(1);
  });
});
