'use client';

import { useEffect, useRef, type KeyboardEvent, type ReactNode } from 'react';

import { Banknote, FlaskConical } from 'lucide-react';

import { cn } from '@/lib/cn';
import { formatInteger } from '@/lib/format';
import type { RunMode } from '@/lib/types';

/** Props of {@link ModeToggle}. */
export interface ModeToggleProps {
  /** Currently selected run mode. */
  value: RunMode;
  /** Called with the newly selected mode. */
  onChange: (mode: RunMode) => void;
  /** How many profiles each mode holds, rendered as a hint. */
  counts: Record<RunMode, number>;
  className?: string;
}

/** Test hook of the control (`role="radiogroup"` is the accessible handle). */
const TEST_ID = 'mode-toggle';

/** Accessible name of the group; it is the only label of the control. */
const GROUP_LABEL = 'Trading mode';

const OPTION_ICON_CLASSES = 'size-4';

/** The two modes, in rendering order: the simulated ledger first, the venue second. */
const MODE_ORDER: readonly RunMode[] = ['paper', 'live'];

/**
 * Label of each option, with the icon vocabulary of the rest of the dashboard.
 *
 * The icons are the ones {@link WalletPanel} and `ProfileCard` already use for a
 * run mode — `FlaskConical` for the simulated ledger, `Banknote` for the real
 * one. A second vocabulary (a beaker here, a coin there) would make the same
 * mode read as two different things on one page.
 *
 * `checkedClasses` is the *selected* surface of the option. The selected state
 * is deliberately carried four times over — `aria-checked`, the icon, the label
 * of the mode and this surface — so it never rests on the colour alone.
 */
const MODE_OPTIONS: Record<
  RunMode,
  { label: string; icon: ReactNode; checkedClasses: string; idleClasses: string }
> = {
  paper: {
    label: 'Paper trading',
    icon: <FlaskConical aria-hidden="true" className={OPTION_ICON_CLASSES} />,
    checkedClasses: 'border-info bg-info/15 text-info',
    idleClasses: 'hover:border-info/60 hover:text-info',
  },
  live: {
    label: 'Real trading',
    icon: <Banknote aria-hidden="true" className={OPTION_ICON_CLASSES} />,
    checkedClasses: 'border-warn bg-warn/15 text-warn',
    idleClasses: 'hover:border-warn/60 hover:text-warn',
  },
};

/**
 * Trading-mode selector of the overview.
 *
 * **A radiogroup, not a tablist.** The control selects a *filter* over one
 * single live region: it swaps no panel and owns no `aria-controls` target, so a
 * tablist would promise a tabpanel relationship that does not exist and would
 * force fake `tabpanel` semantics onto the whole page. A radiogroup is exactly
 * "one choice out of two", which is what this is.
 *
 * It is built from real `<button type="button">` elements carrying
 * `aria-checked`, so the role and the state can never drift apart the way a
 * hand-rolled div would. The two options are always both rendered, in the
 * documented order (paper, then real), each with the number of profiles it holds
 * — the operator sees what a side contains *before* switching to it.
 *
 * Keyboard: the group implements the roving pattern of a radiogroup — exactly
 * one option is in the tab order (`tabIndex={0}` on the selected one, `-1` on
 * the other) and Left/Right (and Up/Down) move the selection to the neighbouring
 * option *and* focus it, so the whole control is operable without a pointer. A
 * click selects too. `Home`/`End` are intentionally not implemented: the group
 * holds two options and the arrows already reach both, so a second pair of keys
 * would be surface without a user.
 */
export function ModeToggle({ value, onChange, counts, className }: ModeToggleProps) {
  /**
   * The mode the keyboard asked to focus, `null` when the move came from a click.
   *
   * A click already puts the focus on the button the user pressed, so only the
   * arrow keys need to move it. The request is recorded during the event and
   * consumed *after* the commit, because focusing the next option before its
   * `aria-checked` flipped would briefly announce the wrong state.
   */
  const pendingFocus = useRef<RunMode | null>(null);

  const groupRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const next = pendingFocus.current;
    if (next === null) {
      return;
    }
    pendingFocus.current = null;
    groupRef.current?.querySelector<HTMLButtonElement>(`[data-mode="${next}"]`)?.focus();
  }, [value]);

  /**
   * Move the selection to the neighbouring option, wrapping around.
   *
   * Focus follows the selection because a roving-tabindex radiogroup has to keep
   * the focused option and the checked option the same one: moving `aria-checked`
   * without moving the focus would leave the operator on a control that reports
   * itself unchecked.
   */
  function move(step: number): void {
    const current = MODE_ORDER.indexOf(value);
    const next = MODE_ORDER[(current + step + MODE_ORDER.length) % MODE_ORDER.length];
    pendingFocus.current = next;
    onChange(next);
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>): void {
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') {
      event.preventDefault();
      move(1);
      return;
    }
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') {
      event.preventDefault();
      move(-1);
    }
  }

  return (
    <div
      ref={groupRef}
      data-testid={TEST_ID}
      role="radiogroup"
      aria-label={GROUP_LABEL}
      onKeyDown={handleKeyDown}
      className={cn('inline-flex flex-wrap items-center gap-xs', className)}
    >
      {MODE_ORDER.map((mode) => {
        const option = MODE_OPTIONS[mode];
        const isChecked = mode === value;
        const count = counts[mode] ?? 0;

        return (
          <button
            key={mode}
            type="button"
            role="radio"
            aria-checked={isChecked}
            // Roving tabindex: one tab stop for the whole group, then arrows.
            tabIndex={isChecked ? 0 : -1}
            data-mode={mode}
            onClick={() => onChange(mode)}
            className={cn(
              'inline-flex cursor-pointer select-none items-center gap-sm rounded-button border px-lg py-sm text-xs font-medium',
              'motion-safe:transition-colors motion-safe:duration-200',
              'active:bg-muted-pressed',
              'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
              isChecked ? option.checkedClasses : cn('border-border text-muted-foreground', option.idleClasses),
            )}
          >
            {option.icon}
            <span className="font-mono">{option.label}</span>
            {/* The count is the hint of the option: what this side holds right
                now, so a switch is never a leap into the unknown. */}
            <span className="text-muted-foreground">
              {formatInteger(count)} {count === 1 ? 'profile' : 'profiles'}
            </span>
          </button>
        );
      })}
    </div>
  );
}
