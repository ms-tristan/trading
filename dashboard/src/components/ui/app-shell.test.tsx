import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { AppShell } from './app-shell';

describe('AppShell', () => {
  it('renders its children inside the page frame', () => {
    const { container } = render(
      <AppShell>
        <p>Dashboard body</p>
      </AppShell>,
    );

    expect(screen.getByText('Dashboard body')).toBeInTheDocument();
    const frame = container.firstElementChild;
    expect(frame).not.toBeNull();
    expect(frame).toHaveClass('bg-background', 'text-foreground', 'min-h-dvh');
  });

  it('merges a caller class name', () => {
    const { container } = render(<AppShell className="custom-frame">content</AppShell>);
    expect(container.firstElementChild).toHaveClass('custom-frame');
  });
});
