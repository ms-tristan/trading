import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

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
});
