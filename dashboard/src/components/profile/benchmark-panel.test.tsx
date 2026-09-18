import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { BenchmarkPayload } from '@/lib/types';

import { BenchmarkPanel } from './benchmark-panel';

const BENCHMARK: BenchmarkPayload = {
  variant: 'buy_and_hold',
  initial_balance: 10000,
  final_balance: 11234.5,
  n_periods: 720,
  timeframe: '1h',
  metrics: { total_return: 0.1234, win_rate: null },
};

describe('BenchmarkPanel', () => {
  it('renders an empty state when no benchmark is configured', () => {
    render(<BenchmarkPanel benchmark={null} />);

    expect(screen.getByText('No benchmark configured')).toBeInTheDocument();
  });

  it('renders the benchmark summary with formatted numbers', () => {
    const { container } = render(<BenchmarkPanel benchmark={BENCHMARK} />);

    expect(screen.getByText('buy_and_hold')).toBeInTheDocument();
    expect(screen.getByText('1h')).toBeInTheDocument();
    expect(screen.getByText('720')).toBeInTheDocument();
    expect(screen.getByText('$10,000.00')).toBeInTheDocument();
    expect(screen.getByText('$11,234.50')).toBeInTheDocument();
    expect(container.querySelectorAll('dt').length).toBeGreaterThanOrEqual(5);
  });

  it('formats every benchmark metric with the format of its name and never hides one', () => {
    const { container } = render(<BenchmarkPanel benchmark={BENCHMARK} />);

    expect(screen.getByText('12.34%')).toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
    expect(container.querySelectorAll('dt')).toHaveLength(7);
  });

  it('renders a benchmark without metrics without inventing a value', () => {
    render(<BenchmarkPanel benchmark={{ ...BENCHMARK, metrics: {} }} />);

    expect(screen.getByText('The benchmark carries no metric.')).toBeInTheDocument();
  });
});
