import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { BUTTON_PRESSED_CLASSES, type ButtonVariant, Button } from './button';

const VARIANTS: ButtonVariant[] = ['primary', 'secondary', 'danger', 'ghost'];

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

  it.each(VARIANTS)('exposes the pressed-state class of the %s variant', (variant) => {
    render(<Button variant={variant}>Engage</Button>);

    const button = screen.getByRole('button', { name: 'Engage' });
    expect(button).toHaveClass(BUTTON_PRESSED_CLASSES[variant]);
    // The pressed feedback is a colour change only: nothing that can reflow.
    expect(button.className).not.toMatch(/(?:^|\s)scale-/);
  });

  it('exposes the secondary pressed-state class by default', () => {
    render(<Button>Engage</Button>);

    const button = screen.getByRole('button', { name: 'Engage' });
    expect(button).toHaveClass(BUTTON_PRESSED_CLASSES.secondary);
  });

  it('renders no pressed-state class at all when disabled', () => {
    render(
      <Button variant="secondary" disabled>
        Engage
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Engage' });
    expect(button).toBeDisabled();
    expect(button).not.toHaveClass(BUTTON_PRESSED_CLASSES.secondary);
    expect(button.className).not.toContain('active:bg-');
  });

  it.each([
    ['sm', 'primary'],
    ['md', 'danger'],
  ] as const)('exposes the pressed-state class on a %s %s button', (size, variant) => {
    render(
      <Button size={size} variant={variant}>
        Engage
      </Button>,
    );

    const button = screen.getByRole('button', { name: 'Engage' });
    expect(button).toHaveClass(BUTTON_PRESSED_CLASSES[variant]);
    expect(button).toHaveClass(size === 'sm' ? 'text-xs' : 'text-sm');
  });

  it('exposes the pressed-state class on an icon-only button', () => {
    render(<Button variant="ghost" aria-label="Refresh" icon={<svg data-testid="icon" />} />);

    const button = screen.getByRole('button', { name: 'Refresh' });
    expect(button).toHaveClass(BUTTON_PRESSED_CLASSES.ghost);
  });

  it('keeps the pressed state and the focus-visible ring on the same element', () => {
    render(<Button variant="primary">Engage</Button>);

    const button = screen.getByRole('button', { name: 'Engage' });
    // Pressed feedback is a background utility, focus is a ring utility: both
    // must coexist so a keyboard press never trades one for the other.
    expect(button).toHaveClass(BUTTON_PRESSED_CLASSES.primary);
    expect(button).toHaveClass('focus-visible:ring-2');
    expect(button).toHaveClass('focus-visible:ring-ring');
    expect(button).toHaveClass('focus-visible:ring-offset-2');
  });

  it.each(VARIANTS)('stays keyboard activable with its pressed state on the %s variant', async (variant) => {
    const user = userEvent.setup();
    const onClick = vi.fn();
    render(
      <Button variant={variant} onClick={onClick}>
        Retry
      </Button>,
    );

    await user.tab();
    const button = screen.getByRole('button', { name: 'Retry' });
    expect(button).toHaveFocus();

    await user.keyboard('{Enter}');
    await user.keyboard(' ');
    expect(onClick).toHaveBeenCalledTimes(2);
    expect(button).toHaveClass(BUTTON_PRESSED_CLASSES[variant]);
  });

  it.each(VARIANTS)('keeps cursor-pointer on the %s variant', (variant) => {
    render(<Button variant={variant}>Engage</Button>);

    expect(screen.getByRole('button', { name: 'Engage' })).toHaveClass('cursor-pointer');
  });
});
