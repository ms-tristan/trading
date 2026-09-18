import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { EmptyState } from './empty-state';

describe('EmptyState', () => {
  it('renders a title and a description', () => {
    render(<EmptyState title="No open positions" description="This profile is flat." />);

    expect(screen.getByText('No open positions')).toBeInTheDocument();
    expect(screen.getByText('This profile is flat.')).toBeInTheDocument();
  });

  it('renders a decorative default icon', () => {
    const { container } = render(<EmptyState title="No trades" />);

    const icon = container.querySelector('svg');
    expect(icon).not.toBeNull();
    expect(icon?.closest('[aria-hidden="true"]')).not.toBeNull();
  });

  it('accepts an icon override and omits the description', () => {
    render(<EmptyState title="No orders" icon={<svg data-testid="custom-icon" />} />);

    expect(screen.getByTestId('custom-icon')).toBeInTheDocument();
    expect(screen.queryByText(/flat/i)).not.toBeInTheDocument();
  });
});
