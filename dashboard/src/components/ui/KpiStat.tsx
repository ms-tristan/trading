import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/** Direction of a KPI: rising, falling or flat. */
export type KpiDirection = "up" | "down" | "flat";

export interface KpiStatProps {
  label: string;
  /** Already formatted value, e.g. `formatSignedUsdt(profit)`. */
  value: string;
  hint?: ReactNode;
  direction?: KpiDirection;
  className?: string;
}

const DIRECTION_CLASS: Record<KpiDirection, string> = {
  up: "text-profit",
  down: "text-loss",
  flat: "text-foreground",
};

const DIRECTION_GLYPH: Record<KpiDirection, string> = {
  up: "\u25B2",
  down: "\u25BC",
  flat: "",
};

const DIRECTION_LABEL: Record<KpiDirection, string> = {
  up: "up",
  down: "down",
  flat: "flat",
};

/**
 * One compact metric of a KPI row: label, tabular value, optional hint.
 *
 * The direction is never carried by colour alone: a glyph (or, for `flat`, the
 * wording of the hint) doubles it for every reader.
 */
export function KpiStat({ label, value, hint, direction = "flat", className }: KpiStatProps) {
  const glyph = DIRECTION_GLYPH[direction];

  return (
    <div className={cn("min-w-0 rounded-md border border-border/60 bg-muted/40 p-2", className)}>
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className={cn("text-base font-semibold tabular-nums", DIRECTION_CLASS[direction])}>
        {glyph === "" ? null : (
          <span aria-hidden="true" className="mr-1">
            {glyph}
          </span>
        )}
        {value}
        <span className="sr-only"> ({DIRECTION_LABEL[direction]})</span>
      </p>
      {hint ? <p className="mt-0.5 text-sm text-muted-foreground">{hint}</p> : null}
    </div>
  );
}
