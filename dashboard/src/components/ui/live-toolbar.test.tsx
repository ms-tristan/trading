import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { EMPTY_PLACEHOLDER } from '@/lib/format';

import { BUTTON_PRESSED_CLASSES } from './button';
import { LiveToolbar } from './live-toolbar';

describe('LiveToolbar', () => {
  it('shows the live state and the checked-at timestamp', () => {
    render(
      <LiveToolbar
        checkedAt="2024-01-01T00:00:00+00:00"
        isPaused={false}
        onToggle={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    expect(screen.getByText('Live updates')).toBeInTheDocument();
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-01-01 00:00:00 UTC');
  });

  it('renders the em dash placeholder when nothing has been checked yet', () => {
    render(<LiveToolbar checkedAt={null} isPaused={false} onToggle={vi.fn()} onRefresh={vi.fn()} />);

    expect(screen.getByText(/checked at/i)).toHaveTextContent(EMPTY_PLACEHOLDER);
  });

  it('marks the checked-at region as a polite live region', () => {
    render(
      <LiveToolbar
        checkedAt="2024-01-01T00:00:00Z"
        isPaused={false}
        onToggle={vi.fn()}
        onRefresh={vi.fn()}
      />,
    );

    const region = screen.getByText(/checked at/i);
    expect(region).toHaveAttribute('aria-live', 'polite');
  });

  it('exposes the pause state on the toggle button', async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    const { rerender } = render(
      <LiveToolbar checkedAt={null} isPaused={false} onToggle={onToggle} onRefresh={vi.fn()} />,
    );

    const liveButton = screen.getByRole('button', { name: 'Pause live updates' });
    expect(liveButton).toHaveAttribute('aria-pressed', 'false');
    await user.click(liveButton);
    expect(onToggle).toHaveBeenCalledTimes(1);

    rerender(<LiveToolbar checkedAt={null} isPaused onToggle={onToggle} onRefresh={vi.fn()} />);
    const pausedButton = screen.getByRole('button', { name: 'Resume live updates' });
    expect(pausedButton).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByText('Live updates paused')).toBeInTheDocument();
  });

  it('refreshes on demand', async () => {
    const user = userEvent.setup();
    const onRefresh = vi.fn();
    render(
      <LiveToolbar checkedAt={null} isPaused={false} onToggle={vi.fn()} onRefresh={onRefresh} />,
    );

    await user.click(screen.getByRole('button', { name: 'Refresh now' }));
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it('is fully keyboard operable', async () => {
    const user = userEvent.setup();
    const onToggle = vi.fn();
    render(
      <LiveToolbar checkedAt={null} isPaused={false} onToggle={onToggle} onRefresh={vi.fn()} />,
    );

    await user.tab();
    expect(screen.getByRole('button', { name: 'Pause live updates' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(onToggle).toHaveBeenCalledTimes(1);
    await user.tab();
    expect(screen.getByRole('button', { name: 'Refresh now' })).toHaveFocus();
  });

  it('shows a non-blocking error banner and hides it when the error clears', () => {
    const { rerender } = render(
      <LiveToolbar
        checkedAt="2024-01-01T00:00:00Z"
        isPaused={false}
        onToggle={vi.fn()}
        onRefresh={vi.fn()}
        error="network error calling /api/profiles"
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('network error calling /api/profiles');
    // The last known good timestamp stays on screen.
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-01-01 00:00:00 UTC');

    rerender(
      <LiveToolbar
        checkedAt="2024-01-01T00:00:00Z"
        isPaused={false}
        onToggle={vi.fn()}
        onRefresh={vi.fn()}
        error={null}
      />,
    );
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
  });

  it('accepts a custom live label', () => {
    render(
      <LiveToolbar
        checkedAt={null}
        isPaused={false}
        onToggle={vi.fn()}
        onRefresh={vi.fn()}
        liveLabel="Profile updates"
      />,
    );

    expect(screen.getByText('Profile updates')).toBeInTheDocument();
  });

  it('shows the failure detail next to the error headline', () => {
    render(
      <LiveToolbar
        checkedAt="2024-01-01T00:00:00Z"
        isPaused={false}
        onToggle={vi.fn()}
        onRefresh={vi.fn()}
        error="The monitoring API is unreachable"
        errorDetail="HTTP 502 · /api/profiles"
      />,
    );

    const region = screen.getByRole('status');
    expect(region).toHaveTextContent('The monitoring API is unreachable');
    const detail = screen.getByTestId('error-banner-detail');
    expect(detail).toHaveTextContent('HTTP 502 · /api/profiles');
    expect(detail).toHaveClass('text-muted-foreground');
    // The last known good timestamp is still on screen.
    expect(screen.getByText(/checked at/i)).toHaveTextContent('2024-01-01 00:00:00 UTC');
  });

  it('renders no detail element when only an error is given', () => {
    render(
      <LiveToolbar
        checkedAt={null}
        isPaused={false}
        onToggle={vi.fn()}
        onRefresh={vi.fn()}
        error="network error calling /api/profiles"
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('network error calling /api/profiles');
    expect(screen.queryByTestId('error-banner-detail')).not.toBeInTheDocument();
  });

  it('gives both controls the pressed state of their variant', () => {
    render(<LiveToolbar checkedAt={null} isPaused={false} onToggle={vi.fn()} onRefresh={vi.fn()} />);

    expect(screen.getByRole('button', { name: 'Pause live updates' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.secondary,
    );
    expect(screen.getByRole('button', { name: 'Refresh now' })).toHaveClass(
      BUTTON_PRESSED_CLASSES.ghost,
    );
  });
});
