import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import NotFound from './not-found';

describe('NotFound', () => {
  it('renders the not-found heading and an empty state', () => {
    render(<NotFound />);

    expect(screen.getByRole('heading', { level: 1, name: 'Page not found' })).toBeInTheDocument();
    expect(screen.getByText('This page does not exist')).toBeInTheDocument();
  });

  it('offers a link back to the overview', () => {
    render(<NotFound />);

    const link = screen.getByRole('link', { name: 'Back to the overview' });
    expect(link).toHaveAttribute('href', '/');
  });
});
