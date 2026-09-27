import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

export interface SectionHeaderProps {
  title: string;
  /** `id` referenced by the `aria-labelledby` of the section. */
  id?: string;
  /** Aggregate line of the section, rendered under the title. */
  subtitle?: ReactNode;
  /** Number of rows the section lists. */
  count?: number;
  actions?: ReactNode;
  className?: string;
}

/** Header of a page section: title, row count, aggregate line and actions. */
export function SectionHeader({
  title,
  id,
  subtitle,
  count,
  actions,
  className,
}: SectionHeaderProps) {
  return (
    <header className={cn("mb-2 flex flex-wrap items-end justify-between gap-2", className)}>
      <div className="min-w-0">
        <h2 id={id} className="text-lg font-semibold">
          {title}
          {typeof count === "number" ? (
            <span className="ml-2 text-sm font-normal text-muted-foreground tabular-nums">
              {count} {count === 1 ? "profile" : "profiles"}
            </span>
          ) : null}
        </h2>
        {subtitle ? <p className="text-sm text-muted-foreground">{subtitle}</p> : null}
      </div>
      {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
    </header>
  );
}
