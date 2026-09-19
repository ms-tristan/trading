import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { BUTTON_PRESSED_CLASSES } from './button';
import { ErrorBanner } from './error-banner';

describe('ErrorBanner', () => {
  it('renders nothing for a null message', () => {
    const { container } = render(<ErrorBanner message={null} />);
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('announces the message politely', () => {
    render(<ErrorBanner message="network error calling /api/health" />);

    const region = screen.getByRole('status');
    expect(region).toHaveAttribute('aria-live', 'polite');
    expect(region).toHaveTextContent('network error calling /api/health');
  });

  it('renders a retry button only when a handler is given', async () => {
    const user = userEvent.setup();
    const onRetry = vi.fn();
    const { rerender } = render(<ErrorBanner message="boom" />);
    expect(screen.queryByRole('button')).not.toBeInTheDocument();

    rerender(<ErrorBanner message="boom" onRetry={onRetry} retryLabel="Try again" />);
    const button = screen.getByRole('button', { name: 'Try again' });
    await user.click(button);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it('merges a caller class name', () => {
    render(<ErrorBanner message="boom" className="mt-lg" />);
    expect(screen.getByRole('status')).toHaveClass('mt-lg');
  });

  it('renders the underlying detail in a secondary style, after the message', () => {
    render(
      <ErrorBanner
        message="The monitoring API is unreachable"
        detail="HTTP 502 · /api/profiles/btc-paper/candles"
      />,
    );

    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 502 · /api/profiles/btc-paper/candles');
    expect(detail).toHaveClass('text-muted-foreground');
    expect(detail).toHaveClass('font-mono');

    // The headline is still the first thing the operator reads.
    const region = screen.getByRole('status');
    expect(region).toHaveTextContent('The monitoring API is unreachable');
    const message = screen.getByText('The monitoring API is unreachable');
    expect(detail.previousElementSibling).toBe(message);
  });

  it('renders no detail element without a detail', () => {
    render(<ErrorBanner message="boom" />);
    expect(screen.queryByTestId('error-banner-detail')).not.toBeInTheDocument();
  });

  it('renders no detail element for an empty detail', () => {
    render(<ErrorBanner message="boom" detail="" />);
    expect(screen.queryByTestId('error-banner-detail')).not.toBeInTheDocument();
  });

  it('renders nothing at all for a null message, even with a detail', () => {
    const { container } = render(<ErrorBanner message={null} detail="HTTP 502" />);

    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.queryByTestId('error-banner-detail')).not.toBeInTheDocument();
  });

  it('gives the retry control the pressed state of its variant', () => {
    render(<ErrorBanner message="boom" detail="HTTP 502" onRetry={vi.fn()} />);

    const retry = screen.getByRole('button', { name: 'Retry' });
    expect(retry).toHaveClass(BUTTON_PRESSED_CLASSES.secondary);
  });
});
