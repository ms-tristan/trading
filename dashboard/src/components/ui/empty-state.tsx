import type { ReactNode } from 'react';

import { Inbox } from 'lucide-react';

import { cn } from '@/lib/cn';

/** Props of {@link EmptyState}. */
export interface EmptyStateProps {
  /** What is missing. */
  title: string;
  /** Optional explanation of why it is missing and what to do next. */
  description?: string;
  /** Decorative icon (`lucide-react`); a neutral inbox by default. */
  icon?: ReactNode;
  className?: string;
}

/** Neutral placeholder shown when a collection is legitimately empty. */
export function EmptyState({ title, description, icon, className }: EmptyStateProps) {
  return (
    <div
      className={cn(
        'flex flex-col items-center gap-sm rounded-card border border-dashed border-border bg-card/50 px-xl py-2xl text-center',
        className,
      )}
    >
      <span aria-hidden="true" className="inline-flex text-muted-foreground">
        {icon ?? <Inbox className="size-5" />}
      </span>
      <p className="font-mono text-sm font-semibold text-foreground">{title}</p>
      {description !== undefined ? (
        <p className="max-w-prose text-xs text-muted-foreground">{description}</p>
      ) : null}
    </div>
  );
}
