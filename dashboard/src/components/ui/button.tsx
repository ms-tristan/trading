'use client';

import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { cn } from '@/lib/cn';

/** Visual weight of a {@link Button}. */
export type ButtonVariant = 'primary' | 'secondary' | 'danger' | 'ghost';

/** Height of a {@link Button}. */
export type ButtonSize = 'sm' | 'md';

/** Props of {@link Button}. */
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  /** Decorative icon rendered before the label (`lucide-react`, never an emoji). */
  icon?: ReactNode;
}

const VARIANT_CLASSES: Record<ButtonVariant, string> = {
  primary: 'bg-accent text-on-accent hover:bg-accent/90',
  secondary: 'border border-border bg-secondary text-on-secondary hover:bg-secondary/80',
  danger: 'bg-destructive text-on-destructive hover:bg-destructive/90',
  ghost: 'border border-border bg-transparent text-foreground hover:bg-muted',
};

/**
 * Box metrics of every button size.
 *
 * Exported so a control that must sit in the same cluster as a button - the
 * "New profile" anchor of the Profiles toolbar is one - can borrow the exact
 * metrics instead of restating them. Both live controls of that toolbar are
 * `size="sm"`, hence a measured height of 26px; a peer that restated the
 * padding with `text-sm` measured 30px, and the cluster read as misaligned.
 */
export const BUTTON_SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'gap-xs px-lg py-sm text-xs',
  md: 'gap-sm px-xl py-md text-sm',
};

/**
 * Pressed-state classes of every button variant.
 *
 * A press is a background-colour change only, so the control never reflows, and
 * the `enabled:` modifier is a second, CSS-level guard on top of the runtime
 * check: a disabled button can never light up under the pointer.
 */
export const BUTTON_PRESSED_CLASSES: Record<ButtonVariant, string> = {
  primary: 'enabled:active:bg-accent-pressed',
  secondary: 'enabled:active:bg-secondary-pressed',
  danger: 'enabled:active:bg-destructive-pressed',
  ghost: 'enabled:active:bg-muted-pressed',
};

/**
 * Action button of the dashboard.
 *
 * Keyboard operable by construction, always with a visible focus ring and a
 * `cursor-pointer`. A press darkens or lightens the surface (never a scale or a
 * size change, so nothing reflows) and a disabled button shows no press at all.
 * `type` defaults to `button` so a button inside a form never submits it by
 * accident.
 */
export function Button({
  variant = 'secondary',
  size = 'md',
  icon,
  className,
  type = 'button',
  children,
  ...rest
}: ButtonProps) {
  const isDisabled = rest.disabled === true;

  return (
    <button
      {...rest}
      type={type}
      className={cn(
        'inline-flex items-center justify-center rounded-button font-medium',
        'cursor-pointer select-none',
        'motion-safe:transition-colors motion-safe:duration-200',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background',
        'disabled:cursor-not-allowed disabled:opacity-50',
        isDisabled ? null : BUTTON_PRESSED_CLASSES[variant],
        VARIANT_CLASSES[variant],
        BUTTON_SIZE_CLASSES[size],
        className,
      )}
    >
      {icon !== undefined ? (
        <span aria-hidden="true" className="inline-flex shrink-0 items-center">
          {icon}
        </span>
      ) : null}
      {children}
    </button>
  );
}
