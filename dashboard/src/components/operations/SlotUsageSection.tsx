"use client";

import Link from "next/link";

import { Card } from "@/components/ui/Card";
import { DataTable, type DataTableColumn } from "@/components/ui/DataTable";
import { SectionHeader } from "@/components/ui/SectionHeader";
import { StateBadge } from "@/components/ui/StateBadge";
import { cn } from "@/lib/cn";
import { formatUsdt } from "@/lib/format";
import { isAttentionState } from "@/lib/states";
import type { ProfileView } from "@/lib/types";

/** One row of the slot ranking. */
export interface SlotUsageRow {
  profile: ProfileView;
  /**
   * View model of the wire `slot`: the engine slot the profile holds, `null` when
   * it only waits for one.
   */
  slot: number | null;
}

/**
 * Rank the fleet by engine slot.
 *
 * Running profiles come first, ordered by the slot they hold; the profiles that
 * only wait for a worker follow in the order the API ranked them, each with the
 * reason the engine published. A running profile the API has not given a slot
 * number yet stays in the running group, at the end of it.
 *
 * The slot and the port the table renders come from the mapped profile: the
 * engine slot from the wire `slot`, the port from the wire `worker_port`, so a
 * running profile renders `#N` next to its port and the em dash only marks a
 * profile that holds no port.
 */
export function slotUsageRows(profiles: ProfileView[]): SlotUsageRow[] {
  const running = profiles.filter((profile) => profile.state === "running");
  const queued = profiles.filter((profile) => profile.state === "queued");

  const scheduled = running
    .map((profile, index) => ({ profile, index }))
    .sort((left, right) => {
      const leftSlot = left.profile.engine_slot;
      const rightSlot = right.profile.engine_slot;
      if (leftSlot === null && rightSlot === null) {
        return left.index - right.index;
      }
      if (leftSlot === null) {
        return 1;
      }
      if (rightSlot === null) {
        return -1;
      }
      return leftSlot - rightSlot;
    })
    .map((entry) => ({ profile: entry.profile, slot: entry.profile.engine_slot }));

  return [...scheduled, ...queued.map((profile) => ({ profile, slot: null }))];
}

const COLUMNS: DataTableColumn<SlotUsageRow>[] = [
  {
    id: "slot",
    header: "Slot",
    headerClassName: "w-16",
    sortValue: (row) => row.slot ?? Number.NaN,
    render: (row) =>
      row.slot === null ? (
        <span className="text-sm text-muted-foreground">queued</span>
      ) : (
        <span className="tabular-nums">#{row.slot}</span>
      ),
  },
  {
    id: "name",
    header: "Profile",
    sortValue: (row) => row.profile.name,
    render: (row) => (
      <Link
        href={`/profiles/${encodeURIComponent(row.profile.id)}`}
        className="font-medium underline-offset-2 hover:underline"
      >
        {row.profile.name}
      </Link>
    ),
  },
  {
    id: "strategy",
    header: "Strategy",
    sortValue: (row) => row.profile.strategy_title,
    render: (row) => <span className="text-muted-foreground">{row.profile.strategy_title}</span>,
  },
  {
    id: "timeframe",
    header: "Timeframe",
    render: (row) => <span className="tabular-nums">{row.profile.timeframe}</span>,
  },
  {
    id: "pair",
    header: "Pair",
    render: (row) => <span className="font-mono text-sm">{row.profile.pairs.join(", ")}</span>,
  },
  {
    id: "value",
    header: "Value",
    align: "end",
    sortValue: (row) => row.profile.portfolio_value,
    render: (row) => formatUsdt(row.profile.portfolio_value),
  },
  {
    id: "port",
    header: "API port",
    align: "end",
    render: (row) =>
      row.profile.api_port === null ? (
        <span className="text-muted-foreground">{"\u2014"}</span>
      ) : (
        <span className="tabular-nums">{row.profile.api_port}</span>
      ),
  },
  {
    id: "state",
    header: "State and reason",
    render: (row) => <StateBadge state={row.profile.state} reason={row.profile.state_reason} />,
  },
];

export interface SlotUsageSectionProps {
  /** The ranked profile list of `GET /api/profiles`. */
  profiles: ProfileView[];
  className?: string;
}

/**
 * Which profiles hold an engine slot and which ones wait for one, with the
 * reason the engine published for every waiting profile.
 *
 * Profiles that are stopped, blocked or in error are deliberately absent: they
 * are not part of the slot race, and the engine-state card above counts them.
 */
export function SlotUsageSection({ profiles, className }: SlotUsageSectionProps) {
  const rows = slotUsageRows(profiles);
  const running = rows.filter((row) => row.slot !== null || row.profile.state === "running").length;
  const queued = rows.length - running;

  return (
    <section aria-labelledby="slot-usage-heading" className={cn("min-w-0", className)}>
      <SectionHeader
        id="slot-usage-heading"
        title="Engine slots"
        subtitle={`${running} running, ${queued} queued - the engine promotes by priority, then by id`}
      />
      <Card>
        <DataTable
          columns={COLUMNS}
          rows={rows}
          rowKey={(row) => row.profile.id}
          caption="Engine slot usage, ranked by slot"
          emptyMessage="No profile holds or waits for an engine slot."
          rowClassName={(row) =>
            isAttentionState(row.profile.state) ? "bg-muted/50" : undefined
          }
          minWidthClassName="min-w-[960px]"
        />
      </Card>
    </section>
  );
}
