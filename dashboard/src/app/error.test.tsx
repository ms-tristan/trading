import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import RouteError from './error';

describe('RouteError', () => {
  it('announces the failure in a banner and offers a reset', async () => {
    const user = userEvent.setup();
    const reset = vi.fn();

    render(<RouteError error={new Error('profile detail failed')} reset={reset} />);

    expect(screen.getByRole('heading', { level: 1, name: 'Something went wrong' })).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('profile detail failed');

    await user.click(screen.getByRole('button', { name: 'Try again' }));
    expect(reset).toHaveBeenCalledTimes(1);
  });

  it('never renders an empty or undefined message', () => {
    render(<RouteError error={new Error('')} reset={vi.fn()} />);

    const banner = screen.getByRole('status');
    expect(banner).toHaveTextContent('Unexpected error');
    expect(banner.textContent ?? '').not.toContain('undefined');
  });
});
