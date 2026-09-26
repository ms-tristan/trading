import { useState } from 'react';

import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { RunMode } from '@/lib/types';

import { ModeToggle } from './mode-toggle';

// ---------------------------------------------------------------------------
// fixtures
// ---------------------------------------------------------------------------

/** Two paper profiles, one live profile: the counts must follow the payload. */
const COUNTS: Record<RunMode, number> = { paper: 2, live: 1 };

/** The group, resolved through the accessible handle first, the test id second. */
function group(): HTMLElement {
  return screen.getByRole('radiogroup', { name: 'Trading mode' });
}

function option(label: string): HTMLElement {
  return within(group()).getByRole('radio', { name: new RegExp(label, 'i') });
}

function renderToggle(value: RunMode = 'paper', onChange = vi.fn()) {
  const view = render(
    <ModeToggle value={value} onChange={onChange} counts={COUNTS} />,
  );
  return { view, onChange };
}

/**
 * Render the toggle with the selection held in state.
 *
 * The keyboard path can only be pinned on a *controlled* toggle whose owner
 * really accepts the change: a static `value` would leave `aria-checked` where
 * it started, and the focus assertion of the roving tabindex would test nothing.
 */
function renderControlled(initial: RunMode = 'paper') {
  function Harness() {
    const [mode, setMode] = useState<RunMode>(initial);
    return <ModeToggle value={mode} onChange={setMode} counts={COUNTS} />;
  }
  return render(<Harness />);
}

describe('ModeToggle', () => {
  it('renders both modes in order, each labelled and carrying its profile count', () => {
    renderToggle();

    const options = within(group()).getAllByRole('radio');
    expect(options).toHaveLength(2);
    // Order is part of the contract: the simulated ledger first, the venue last.
    expect(options[0]).toHaveTextContent('Paper trading');
    expect(options[1]).toHaveTextContent('Real trading');

    // The count tells the operator what each side holds *before* switching.
    expect(option('Paper trading')).toHaveTextContent('2 profiles');
    expect(option('Real trading')).toHaveTextContent('1 profile');
  });

  it('is a labelled radiogroup and not a tablist, with a stable test hook', () => {
    renderToggle();

    // A radiogroup is "one choice out of two"; a tablist would promise a
    // tabpanel relationship this filter does not own.
    expect(group()).toHaveAttribute('data-testid', 'mode-toggle');
    expect(screen.queryByRole('tablist')).not.toBeInTheDocument();
    expect(screen.queryByRole('tab')).not.toBeInTheDocument();
    expect(group()).toHaveAccessibleName('Trading mode');
  });

  it('carries the selection in aria-checked and keeps one tab stop for the group', () => {
    const { view } = renderToggle('paper');

    expect(option('Paper trading')).toHaveAttribute('aria-checked', 'true');
    expect(option('Real trading')).toHaveAttribute('aria-checked', 'false');

    // Roving tabindex: the selected option is the only tab stop of the group.
    expect(option('Paper trading')).toHaveAttribute('tabindex', '0');
    expect(option('Real trading')).toHaveAttribute('tabindex', '-1');

    view.unmount();
    renderToggle('live');

    expect(option('Paper trading')).toHaveAttribute('aria-checked', 'false');
    expect(option('Real trading')).toHaveAttribute('aria-checked', 'true');
    expect(option('Real trading')).toHaveAttribute('tabindex', '0');
    expect(option('Paper trading')).toHaveAttribute('tabindex', '-1');
  });

  it('never lets colour alone carry the mode: an icon and the mode word are always present', () => {
    renderToggle('live');

    for (const label of ['Paper trading', 'Real trading']) {
      const target = option(label);
      // The icon vocabulary of the dashboard, per mode, plus the spelled-out word.
      expect(target.querySelector('svg')).not.toBeNull();
      expect(target).toHaveTextContent(label);
    }

    // The selected surface is a real style change, not the label colour alone.
    expect(option('Real trading').className).toMatch(/border-warn/);
    expect(option('Paper trading').className).not.toMatch(/border-info\b.*bg-info/);
  });

  it('selects a mode on click', () => {
    const { onChange } = renderToggle('paper');

    fireEvent.click(option('Real trading'));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange).toHaveBeenCalledWith('live');
  });

  it('moves the selection and the focus with the ArrowRight and ArrowLeft keys', () => {
    renderControlled('paper');

    // Right: paper -> real, and the focus lands on the newly selected option.
    fireEvent.keyDown(group(), { key: 'ArrowRight' });
    expect(option('Real trading')).toHaveAttribute('aria-checked', 'true');
    expect(option('Paper trading')).toHaveAttribute('aria-checked', 'false');
    expect(option('Real trading')).toHaveFocus();

    // Left: back to paper.
    fireEvent.keyDown(group(), { key: 'ArrowLeft' });
    expect(option('Paper trading')).toHaveAttribute('aria-checked', 'true');
    expect(option('Paper trading')).toHaveFocus();
  });

  it('moves the selection with the ArrowDown and ArrowUp keys too', () => {
    renderControlled('paper');

    fireEvent.keyDown(group(), { key: 'ArrowDown' });
    expect(option('Real trading')).toHaveAttribute('aria-checked', 'true');

    fireEvent.keyDown(group(), { key: 'ArrowUp' });
    expect(option('Paper trading')).toHaveAttribute('aria-checked', 'true');
  });

  it('wraps around at both ends of the two options', () => {
    renderControlled('live');

    fireEvent.keyDown(group(), { key: 'ArrowRight' });
    expect(option('Paper trading')).toHaveAttribute('aria-checked', 'true');
    expect(option('Paper trading')).toHaveFocus();

    fireEvent.keyDown(group(), { key: 'ArrowLeft' });
    expect(option('Real trading')).toHaveAttribute('aria-checked', 'true');
  });

  it('leaves every other key to the browser', () => {
    const { onChange } = renderToggle('paper');

    fireEvent.keyDown(group(), { key: 'Tab' });
    fireEvent.keyDown(group(), { key: 'a' });
    expect(onChange).not.toHaveBeenCalled();
  });
});
