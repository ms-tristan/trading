import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Button } from './button';

describe('Button', () => {
  it('renders its label and defaults to type="button"', () => {
    render(<Button>Refresh</Button>);

    const button = screen.getByRole('button', { name: 'Refresh' });
    expect(button).toHaveAttribute('type', 'button');
    expect(button).toHaveClass('cursor-pointer');
  });

  it('accepts an explicit type', () => {
    render(<Button type="submit">Save</Button>);
    expect(screen.getByRole('button', { name: 'Save' })).toHaveAttribute('type', 'submit');
  });

  it('calls the click handler', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Engage</Button>);

    await user.click(screen.getByRole('button', { name: 'Engage' }));
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it('does not call the handler when disabled', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Engage
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Engage' });
    expect(button).toBeDisabled();
    await user.click(button);
    expect(onClick).not.toHaveBeenCalled();
  });

  it('applies the variant and size classes', () => {
    render(
      <Button variant="danger" size="sm">
        Stop
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Stop' });
    expect(button).toHaveClass('bg-destructive', 'text-on-destructive', 'text-xs');
  });

  it('renders a decorative icon out of the accessible name', () => {
    render(<Button icon={<svg data-testid="icon" />}>Save token</Button>);

    expect(screen.getByRole('button', { name: 'Save token' })).toBeInTheDocument();
    expect(screen.getByTestId('icon').closest('[aria-hidden="true"]')).not.toBeNull();
  });

  it('is keyboard operable', async () => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Retry</Button>);

    await user.tab();
    expect(screen.getByRole('button', { name: 'Retry' })).toHaveFocus();

    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    expect(onClick).toHaveBeenCalledTimes(2);
  });
});
