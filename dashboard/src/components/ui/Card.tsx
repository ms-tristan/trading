import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface CardProps {
  /** Optional card title; when omitted the card is a plain surface. */
  title?: string;
  description?: string;
  /** Controls shown on the right of the title row. */
  actions?: ReactNode;
  children?: ReactNode;
  className?: string;
  /** Heading level of the title, so the page outline stays valid. */
  headingLevel?: 2 | 3;
}

/**
 * Surface of the dashboard: `--card` background, `--border` outline, 12px of
 * padding. Every panel of every page is a `Card`.
 */
export function Card({
  title,
  description,
  actions,
  children,
  className,
  headingLevel = 3,
}: CardProps) {
  const Heading = headingLevel === 2 ? "h2" : "h3";
  const hasHeader = Boolean(title) || Boolean(actions) || Boolean(description);

  return (
    <section
      className={cn(
        "min-w-0 rounded-lg border border-border bg-card p-3 text-card-foreground",
        className,
      )}
    >
      {hasHeader ? (
        <header className="mb-2 flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            {title ? <Heading className="text-base font-semibold">{title}</Heading> : null}
            {description ? (
              <p className="text-sm text-muted-foreground">{description}</p>
            ) : null}
          </div>
          {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
        </header>
      ) : null}
      {children}
    </section>
  );
}
