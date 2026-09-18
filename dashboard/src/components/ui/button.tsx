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

const SIZE_CLASSES: Record<ButtonSize, string> = {
  sm: 'gap-xs px-lg py-sm text-xs',
  md: 'gap-sm px-xl py-md text-sm',
};

/**
 * Action button of the dashboard.
 *
 * Keyboard operable by construction, always with a visible focus ring and a
 * `cursor-pointer`. `type` defaults to `button` so a button inside a form never
 * submits it by accident.
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
        VARIANT_CLASSES[variant],
        SIZE_CLASSES[size],
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
