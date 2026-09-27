import Link from "next/link";

import { Card } from "@/components/ui/Card";
import { DailyBars } from "@/components/ui/DailyBars";
import { EquityChart } from "@/components/ui/EquityChart";
import { cn } from "@/lib/cn";
import type { ApiWindow, DailyBarPoint, EquityPoint } from "@/lib/types";

/** Aggregation windows offered by the equity selector, in display order. */
export const PROFILE_WINDOWS: readonly ApiWindow[] = ["24h", "7d", "30d", "all"];

/** Validate the `?window=` parameter of the profile page; anything else is `24h`. */
export function normaliseProfileWindow(value: string | string[] | undefined): ApiWindow {
  const candidate = Array.isArray(value) ? value[0] : value;
  return PROFILE_WINDOWS.find((entry) => entry === candidate) ?? "24h";
}

export interface ProfileEquitySectionProps {
  profileId: string;
  /** Window the payload was fetched for; it drives the selector and the caption. */
  window: ApiWindow;
  equity: EquityPoint[];
  daily: DailyBarPoint[];
  className?: string;
}

/**
 * Equity curve of one profile, with its window selector, and the daily bars.
 *
 * The selector is a list of links to `?window=...` of the same route: Next
 * patches the re-rendered server component in, so switching window is never a
 * full page reload, works without JavaScript and keeps the operator's scroll
 * position. Both charts are hand-rolled SVG from the design system and both are
 * doubled by a real table, so the numbers survive a narrow screen.
 */
export function ProfileEquitySection({
  profileId,
  window: apiWindow,
  equity,
  daily,
  className,
}: ProfileEquitySectionProps) {
  const points = equity.map((point) => ({
    timestamp: point.timestamp,
    value: point.portfolio_value,
  }));

  return (
    <>
      <Card
        className={className}
        headingLevel={2}
        title="Equity curve"
        description={`Portfolio value over ${apiWindow}`}
        actions={
          <nav aria-label="Equity window" className="flex flex-wrap gap-1">
            {PROFILE_WINDOWS.map((entry) => (
              <Link
                key={entry}
                href={`/profiles/${encodeURIComponent(profileId)}?window=${entry}`}
                aria-current={entry === apiWindow ? "true" : undefined}
                className={cn(
                  "rounded-md border border-border px-2 py-1 text-sm transition-smooth",
                  entry === apiWindow ? "bg-secondary text-foreground" : "text-muted-foreground",
                )}
              >
                {entry}
              </Link>
            ))}
          </nav>
        }
      >
        <EquityChart
          caption={`Portfolio value of ${profileId} over ${apiWindow}`}
          series={[
            {
              id: `${profileId}-portfolio`,
              label: "Portfolio value",
              points,
            },
          ]}
        />
      </Card>

      <Card
        className={className}
        headingLevel={2}
        title="Daily profit"
        description="Profit realised on every UTC day of the window"
      >
        <DailyBars bars={daily} caption={`Daily profit of ${profileId} over ${apiWindow}`} />
      </Card>
    </>
  );
}
