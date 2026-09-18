import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { MetricsPanel, orderMetricNames } from './metrics-panel';

/** Mixed payload: known names, a `null` value and a name the panel does not know. */
const METRICS: Record<string, number | null> = {
  unknown_metric: 42,
  win_rate: null,
  total_return: 0.1234,
  sharpe_ratio: 1.5,
};

describe('orderMetricNames', () => {
  it('follows the frozen metric order and appends unknown names alphabetically', () => {
    expect(orderMetricNames(Object.keys(METRICS))).toEqual([
      'total_return',
      'win_rate',
      'sharpe_ratio',
      'unknown_metric',
    ]);
  });

  it('keeps every key it is given', () => {
    const names = orderMetricNames(['zeta', 'alpha', 'n_trades', 'avg_win']);

    expect(names).toEqual(['avg_win', 'n_trades', 'alpha', 'zeta']);
  });
});

describe('MetricsPanel', () => {
  it('renders every key of the payload with a humanised label and a formatted value', () => {
    const { container } = render(<MetricsPanel metrics={METRICS} />);

    const terms = Array.from(container.querySelectorAll('dt'));
    expect(terms).toHaveLength(4);
    expect(terms.map((term) => term.textContent)).toEqual([
      'Total return',
      'Win rate',
      'Sharpe ratio',
      'Unknown metric',
    ]);

    expect(screen.getByText('12.34%')).toBeInTheDocument();
    expect(screen.getByText('1.50')).toBeInTheDocument();
    expect(screen.getByText('42.00')).toBeInTheDocument();
  });

  it('renders an em dash for a null metric instead of a zero', () => {
    render(<MetricsPanel metrics={METRICS} />);

    const row = screen.getByText('Win rate').closest('div');
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText('—')).toBeInTheDocument();
  });

  it('renders every key even when the payload is large', () => {
    const many: Record<string, number | null> = {};
    for (let index = 0; index < 25; index += 1) {
      many[`metric_${index}`] = index;
    }

    render(<MetricsPanel metrics={many} />);

    expect(document.querySelectorAll('dt')).toHaveLength(25);
  });

  it('renders an empty state when the payload carries no metric', () => {
    render(<MetricsPanel metrics={{}} />);

    expect(screen.getByText('No metric yet')).toBeInTheDocument();
  });
});
