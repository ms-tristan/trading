import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { CHART_SERIES } from './equity-chart-legend';
import { EquityChart } from './equity-chart';
import type { EquityPoint } from '@/lib/types';

const POINTS: EquityPoint[] = [
  {
    timestamp: '2024-01-01T00:00:00+00:00',
    equity: 10000,
    cash: 10000,
    position_value: 0,
  },
  {
    timestamp: '2024-01-01T01:00:00+00:00',
    equity: 10100,
    cash: 10050,
    position_value: 50,
  },
  {
    timestamp: '2024-01-01T02:00:00+00:00',
    equity: 9900,
    cash: 9950,
    position_value: -50,
  },
];

/** Give the SVG a measurable box: jsdom reports a zero-sized rect otherwise. */
function stubSvgRect(container: HTMLElement): SVGSVGElement {
  const svg = container.querySelector('svg');
  if (svg === null) {
    throw new Error('the chart did not render an svg element');
  }
  svg.getBoundingClientRect = () =>
    ({
      x: 0,
      y: 0,
      top: 0,
      left: 0,
      right: 800,
      bottom: 320,
      width: 800,
      height: 320,
      toJSON: () => ({}),
    }) as DOMRect;
  return svg;
}

describe('EquityChart', () => {
  it('draws one path per series with its own token colour and line style', () => {
    const { container } = render(<EquityChart points={POINTS} />);

    const paths = Array.from(container.querySelectorAll('path'));
    expect(paths).toHaveLength(CHART_SERIES.length);
    for (const series of CHART_SERIES) {
      const path = paths.find((node) => node.getAttribute('stroke') === series.color);
      expect(path).toBeDefined();
      expect(path?.getAttribute('fill')).toBe('none');
      expect(path?.getAttribute('d')?.startsWith('M')).toBe(true);
      expect(path?.getAttribute('stroke-dasharray')).toBe(series.dash ?? null);
    }
  });

  it('labels every series in the legend and in the axis', () => {
    render(<EquityChart points={POINTS} />);

    const legend = screen.getByRole('list', { name: 'Chart series' });
    for (const series of CHART_SERIES) {
      expect(within(legend).getByText(series.label)).toBeInTheDocument();
    }
    expect(within(legend).getAllByRole('listitem')).toHaveLength(CHART_SERIES.length);
    expect(screen.getAllByText('10,000').length).toBeGreaterThan(0);
  });

  it('exposes the chart with an accessible name and the first and last timestamp', () => {
    render(<EquityChart points={POINTS} ariaLabel="Equity curve of btc-paper" />);

    const chart = screen.getByRole('img', { name: 'Equity curve of btc-paper' });
    expect(chart).toHaveAttribute('tabindex', '0');
    expect(screen.getAllByText('2024-01-01 00:00:00 UTC').length).toBeGreaterThan(0);
    expect(screen.getAllByText('2024-01-01 02:00:00 UTC').length).toBeGreaterThan(0);
  });

  it('moves the crosshair with the arrow keys, Home and End and announces the point', () => {
    render(<EquityChart points={POINTS} />);

    const chart = screen.getByRole('img', { name: 'Equity curve over time' });
    const status = screen.getByRole('status');
    expect(status).toHaveTextContent('No point selected');

    chart.focus();
    expect(chart).toHaveFocus();

    // No crosshair yet: ArrowLeft starts at the newest point and steps back.
    fireEvent.keyDown(chart, { key: 'ArrowLeft' });
    expect(status).toHaveTextContent(
      '2024-01-01 01:00:00 UTC - equity $10,100.00, cash $10,050.00, position value $50.00',
    );

    fireEvent.keyDown(chart, { key: 'ArrowLeft' });
    expect(status).toHaveTextContent(
      '2024-01-01 00:00:00 UTC - equity $10,000.00, cash $10,000.00, position value $0.00',
    );

    // Clamped at the left end.
    fireEvent.keyDown(chart, { key: 'ArrowLeft' });
    expect(status).toHaveTextContent('2024-01-01 00:00:00 UTC');

    fireEvent.keyDown(chart, { key: 'End' });
    expect(status).toHaveTextContent(
      '2024-01-01 02:00:00 UTC - equity $9,900.00, cash $9,950.00, position value -$50.00',
    );

    fireEvent.keyDown(chart, { key: 'ArrowRight' });
    expect(status).toHaveTextContent('2024-01-01 02:00:00 UTC');

    fireEvent.keyDown(chart, { key: 'Home' });
    expect(status).toHaveTextContent('2024-01-01 00:00:00 UTC');
  });

  it('moves the same crosshair on pointer hover', () => {
    const { container } = render(<EquityChart points={POINTS} />);
    const chart = screen.getByRole('img', { name: 'Equity curve over time' });
    stubSvgRect(container);

    // x = 400 out of 800 lands exactly on the middle point.
    fireEvent.pointerMove(chart, { clientX: 400 });

    expect(screen.getByRole('status')).toHaveTextContent(
      '2024-01-01 01:00:00 UTC - equity $10,100.00',
    );
    expect(container.querySelectorAll('circle')).toHaveLength(CHART_SERIES.length);
  });

  it('ignores a pointer hover it cannot map to the plot', () => {
    const { container } = render(<EquityChart points={POINTS} />);
    const chart = screen.getByRole('img', { name: 'Equity curve over time' });
    const svg = stubSvgRect(container);
    svg.getBoundingClientRect = () =>
      ({ x: 0, y: 0, top: 0, left: 0, right: 0, bottom: 0, width: 0, height: 0, toJSON: () => ({}) }) as DOMRect;

    fireEvent.pointerMove(chart, { clientX: 400 });

    expect(screen.getByRole('status')).toHaveTextContent('No point selected');
  });

  it('repeats the series as a text alternative behind a data-table disclosure', () => {
    render(<EquityChart points={POINTS} />);

    const summary = screen.getByText('Data table');
    const details = summary.closest('details');
    expect(details).not.toBeNull();
    const table = within(details as HTMLElement).getByRole('table', {
      name: 'Equity curve data',
      hidden: true,
    });
    expect(within(table).getByText('Position value')).toBeInTheDocument();
    expect(within(table).getByText('$10,100.00')).toBeInTheDocument();
  });

  it('renders the empty state instead of an empty frame', () => {
    render(<EquityChart points={[]} />);

    expect(screen.getByText('No equity point yet')).toBeInTheDocument();
    expect(screen.queryByRole('img')).toBeNull();
  });

  it('renders the empty state for a single point too', () => {
    render(<EquityChart points={[POINTS[0] as EquityPoint]} />);

    expect(screen.getByText('No equity point yet')).toBeInTheDocument();
  });

  it('honours the height prop in the viewBox', () => {
    const { container } = render(<EquityChart points={POINTS} height={200} />);

    expect(container.querySelector('svg')?.getAttribute('viewBox')).toBe('0 0 800 200');
  });
});
