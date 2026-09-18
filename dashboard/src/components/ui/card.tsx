import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';

/** Props of {@link Card}. */
export interface CardProps {
  /** Card heading (rendered as an `<h2>`). */
  title?: ReactNode;
  /** Short explanation under the heading. */
  description?: ReactNode;
  /** Controls rendered on the right of the heading. */
  actions?: ReactNode;
  className?: string;
  children?: ReactNode;
}

/** Bordered surface holding one section of the dashboard. */
export function Card({ title, description, actions, className, children }: CardProps) {
  const hasHeader = title !== undefined || description !== undefined || actions !== undefined;
  return (
    <section
      className={cn(
        'rounded-card border border-border bg-card p-xl text-card-foreground shadow-md',
        className,
      )}
    >
      {hasHeader ? (
        <header className="mb-lg flex flex-wrap items-start justify-between gap-md">
          <div className="min-w-0">
            {title !== undefined ? (
              <h2 className="font-mono text-base font-semibold text-card-foreground">{title}</h2>
            ) : null}
            {description !== undefined ? (
              <p className="mt-xs text-sm text-muted-foreground">{description}</p>
            ) : null}
          </div>
          {actions !== undefined ? (
            <div className="flex flex-wrap items-center gap-sm">{actions}</div>
          ) : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}
