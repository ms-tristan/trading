import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { OrderState, ProfileStatus } from '@/lib/types';

import { StatusBadge, orderStateTone, profileStatusTone } from './status-badge';

describe('StatusBadge', () => {
  it('renders the label together with an icon (never colour alone)', () => {
    const { container } = render(<StatusBadge label="Running" tone="ok" />);

    expect(screen.getByText('Running')).toBeInTheDocument();
    const badge = container.querySelector('[data-tone="ok"]');
    expect(badge).not.toBeNull();
    expect(badge?.querySelector('svg')).not.toBeNull();
  });

  it('exposes the tone as a data attribute for every tone', () => {
    const tones = ['ok', 'warn', 'error', 'info', 'neutral'] as const;
    const { container } = render(
      <div>
        {tones.map((tone) => (
          <StatusBadge key={tone} label={tone} tone={tone} />
        ))}
      </div>,
    );

    for (const tone of tones) {
      expect(container.querySelector(`[data-tone="${tone}"]`)).not.toBeNull();
    }
    expect(container.querySelectorAll('svg')).toHaveLength(tones.length);
  });

  it('accepts an icon override', () => {
    render(<StatusBadge label="Halted" tone="error" icon={<svg data-testid="custom" />} />);
    expect(screen.getByTestId('custom')).toBeInTheDocument();
  });
});

describe('profileStatusTone', () => {
  it('maps every lifecycle status onto a tone', () => {
    const expected: Record<ProfileStatus, string> = {
      running: 'ok',
      starting: 'info',
      degraded: 'warn',
      halted: 'error',
      error: 'error',
      stopped: 'neutral',
    };

    for (const [status, tone] of Object.entries(expected)) {
      expect(profileStatusTone(status as ProfileStatus)).toBe(tone);
    }
  });
});

describe('orderStateTone', () => {
  it('maps every order state onto a tone', () => {
    const expected: Record<OrderState, string> = {
      filled: 'ok',
      submitted: 'info',
      partially_filled: 'warn',
      rejected: 'error',
      pending: 'neutral',
      cancelled: 'neutral',
    };

    for (const [state, tone] of Object.entries(expected)) {
      expect(orderStateTone(state as OrderState)).toBe(tone);
    }
  });
});
