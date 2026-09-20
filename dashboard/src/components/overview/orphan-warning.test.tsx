import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { formatNumber, formatQuantity } from '@/lib/format';
import type { OrphanClosure, OrphanFailure, OrphanReport } from '@/lib/types';

import { OrphanWarning } from './orphan-warning';

// ---------------------------------------------------------------------------
// fixtures: the exact `OrphanReport` shape of the frozen API contract
// ---------------------------------------------------------------------------

const closedOne: OrphanClosure = {
  profile_id: 'test1',
  symbol: 'BTC/USDT',
  quantity: 0.05,
  side: 'sell',
  price: 42123.5,
};

const closedTwo: OrphanClosure = {
  profile_id: 'ghost',
  symbol: 'ETH/USDT',
  quantity: 2,
  side: 'buy',
  price: 2310.25,
};

const failedOne: OrphanFailure = {
  profile_id: 'stray',
  symbol: 'SOL/USDT',
  quantity: 12.5,
  error: 'venue rejected the closing order',
};

/** A sweep that closed every orphan it found. */
const allClosed: OrphanReport = {
  found: 2,
  orphaned: 2,
  closed_count: 2,
  failed_count: 0,
  closed: [closedOne, closedTwo],
  failed: [],
  swept_at: '2024-06-01T12:00:00+00:00',
};

/** A sweep that left one position open: the loud half of the report. */
const withFailure: OrphanReport = {
  found: 2,
  orphaned: 2,
  closed_count: 1,
  failed_count: 1,
  closed: [closedOne],
  failed: [failedOne],
  swept_at: '2024-06-01T12:00:00+00:00',
};

/** A sweep that could not close anything at all. */
const allFailed: OrphanReport = {
  found: 1,
  orphaned: 1,
  closed_count: 0,
  failed_count: 1,
  closed: [],
  failed: [failedOne],
  swept_at: '2024-06-01T12:00:00+00:00',
};

/** The report of a platform that was never swept: no warning to render. */
const neverSwept: OrphanReport = {
  found: 0,
  orphaned: 0,
  closed_count: 0,
  failed_count: 0,
  closed: [],
  failed: [],
  swept_at: null,
};

describe('OrphanWarning', () => {
  it('renders nothing at all when the platform was never swept', () => {
    const { container } = render(<OrphanWarning report={null} />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('renders nothing when the report carries no orphan at all', () => {
    // A sweep that ran and found nothing still has a report: it is not a
    // warning, so the banner stays away and the operator is not cried wolf at.
    const { container } = render(<OrphanWarning report={neverSwept} />);

    expect(container).toBeEmptyDOMElement();
  });

  it('states every closed position of a clean sweep, with profile, symbol, quantity, side and price', () => {
    render(<OrphanWarning report={allClosed} />);

    const banner = screen.getByRole('status');
    expect(banner).toHaveTextContent('Orphaned positions were closed at the venue');
    expect(banner).not.toHaveTextContent('could not all be closed');

    // Every field of every closure is in the rendered text, never colour alone.
    const detail = screen.getByTestId('error-banner-detail');
    for (const closure of allClosed.closed) {
      expect(detail).toHaveTextContent(closure.profile_id);
      expect(detail).toHaveTextContent(closure.symbol);
      expect(detail).toHaveTextContent(formatQuantity(closure.quantity));
      expect(detail).toHaveTextContent(formatNumber(closure.price));
      expect(detail).toHaveTextContent(`(${closure.side})`);
    }
    expect(detail).toHaveTextContent('2 position(s) closed');
    expect(detail).toHaveTextContent('test1 BTC/USDT 0.05 @ 42,123.50 (sell)');
    expect(detail).toHaveTextContent('ghost ETH/USDT 2 @ 2,310.25 (buy)');
  });

  it('takes the failure headline and states every unclosable position', () => {
    render(<OrphanWarning report={withFailure} />);

    const banner = screen.getByRole('status');
    // The failure headline wins, and the success headline is never printed.
    expect(banner).toHaveTextContent('Orphaned positions could not all be closed');
    expect(banner).not.toHaveTextContent('Orphaned positions were closed at the venue');

    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('1 position(s) COULD NOT be closed');
    expect(detail).toHaveTextContent(failedOne.profile_id);
    expect(detail).toHaveTextContent(failedOne.symbol);
    expect(detail).toHaveTextContent(formatQuantity(failedOne.quantity));
    expect(detail).toHaveTextContent(failedOne.error);
  });

  it('reports a total failure as loudly as a partial one', () => {
    render(<OrphanWarning report={allFailed} />);

    const banner = screen.getByRole('status');
    expect(banner).toHaveTextContent('Orphaned positions could not all be closed');
    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('1 position(s) COULD NOT be closed');
    expect(detail).toHaveTextContent(failedOne.error);
  });

  it('never hides the failure behind the success line when both are present', () => {
    render(<OrphanWarning report={withFailure} />);

    const detail = screen.getByTestId('error-banner-detail');
    const text = detail.textContent ?? '';

    // Both halves are on screen: the successes are reported, not swallowed...
    expect(text).toContain('1 position(s) closed');
    expect(text).toContain('test1 BTC/USDT');
    // ...but the failure comes FIRST and is never softened.
    expect(text.indexOf('COULD NOT be closed')).toBeGreaterThanOrEqual(0);
    expect(text.indexOf('COULD NOT be closed')).toBeLessThan(text.indexOf('position(s) closed'));
    expect(screen.getByRole('status')).toHaveTextContent(
      'Orphaned positions could not all be closed',
    );
  });

  it('offers no way to dismiss an unclosable position', () => {
    render(<OrphanWarning report={withFailure} />);

    // No dismiss/close control anywhere: an operator must not be able to hide a
    // position that is still open at the venue.
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /close/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /hide/i })).not.toBeInTheDocument();
  });

  it('renders the em dash placeholder for a closure the venue reported no number for', () => {
    const partial: OrphanReport = {
      ...allClosed,
      found: 1,
      orphaned: 1,
      closed_count: 1,
      closed: [{ ...closedOne, quantity: null, price: null }],
      failed: [],
    };

    render(<OrphanWarning report={partial} />);

    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('test1 BTC/USDT — @ — (sell)');
    expect(detail).not.toHaveTextContent('NaN');
    expect(detail).not.toHaveTextContent('null');
  });

  it('announces the warning politely, without interrupting the screen reader', () => {
    render(<OrphanWarning report={withFailure} />);

    const banner = screen.getByRole('status');
    expect(banner).toHaveAttribute('aria-live', 'polite');
    // The copy is explicit: colour is never the only carrier of the meaning.
    expect(banner).toHaveTextContent('could not all be closed');
  });
});
