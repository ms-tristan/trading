"use client";

import Link from "next/link";
import type { ReactNode } from "react";

import { DataTable, type DataTableColumn } from "@/components/ui/DataTable";
import { SectionHeader } from "@/components/ui/SectionHeader";
import { Sparkline } from "@/components/ui/Sparkline";
import { StateBadge } from "@/components/ui/StateBadge";
import { cn } from "@/lib/cn";
import {
  directionClass,
  directionGlyph,
  formatRatioPercent,
  formatSignedRatioPercent,
  formatSignedUsdt,
  formatUsdt,
} from "@/lib/format";
import { isAttentionState } from "@/lib/states";
import type { ProfileView } from "@/lib/types";

import { formatModeSummary, rankProfiles, summariseProfiles, type RankedProfileRow } from "./aggregate";

export interface ModeSectionProps {
  /** DOM id referenced by the `aria-labelledby` of the section. */
  id: string;
  title: string;
  /** Profiles of the section, in the API order (portfolio value, descending). */
  profiles: ProfileView[];
  emptyMessage?: string;
  /** Short explanatory sentence rendered as visible text under the section header. */
  summary?: string;
  /** Optional block rendered between the header and the table. */
  notice?: ReactNode;
  className?: string;
}

/**
 * Columns of the ranking table.
 *
 * The ranking columns carry the contract `SortKey`; the other columns show
 * context and are deliberately not sortable.
 */
const COLUMNS: DataTableColumn<RankedProfileRow>[] = [
  {
    id: "rank",
    header: "#",
    headerClassName: "w-10",
    render: (row) => <span className="tabular-nums text-muted-foreground">{row.rank}</span>,
  },
  {
    id: "name",
    header: "Profile",
    sortKey: "name",
    sortValue: (row) => row.profile.name,
    render: (row) => (
      <Link
        href={`/profiles/${row.profile.id}`}
        className="font-medium underline-offset-2 hover:underline"
      >
        {row.profile.name}
      </Link>
    ),
  },
  {
    id: "strategy",
    header: "Strategy",
    sortKey: "strategy",
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
    render: (row) => (
      <span className="font-mono text-sm">{row.profile.pairs.join(", ")}</span>
    ),
  },
  {
    id: "state",
    header: "State",
    render: (row) => (
      <span className="flex min-w-0 flex-wrap items-center gap-1">
        <span className="rounded-sm border border-border px-1 text-sm uppercase text-muted-foreground">
          {row.profile.mode}
        </span>
        <StateBadge state={row.profile.state} reason={row.profile.state_reason} />
      </span>
    ),
  },
  {
    id: "value",
    header: "Value",
    align: "end",
    sortKey: "value",
    sortValue: (row) => row.profile.portfolio_value,
    render: (row) => formatUsdt(row.profile.portfolio_value),
  },
  {
    id: "profit",
    header: "Profit",
    align: "end",
    sortKey: "profit",
    sortValue: (row) => row.profile.profit_usdt,
    render: (row) => (
      <span className={directionClass(row.profile.profit_usdt)}>
        <span aria-hidden="true">{directionGlyph(row.profile.profit_usdt)}</span>{" "}
        {formatSignedUsdt(row.profile.profit_usdt)}
      </span>
    ),
  },
  {
    id: "profit_pct",
    header: "Profit %",
    align: "end",
    sortValue: (row) => row.profile.profit_pct,
    render: (row) => (
      <span className={directionClass(row.profile.profit_pct)}>
        {formatSignedRatioPercent(row.profile.profit_pct)}
      </span>
    ),
  },
  {
    id: "trades",
    header: "Open / closed",
    align: "end",
    render: (row) => `${row.profile.open_trades} / ${row.profile.closed_trades}`,
  },
  {
    id: "win_rate",
    header: "Win rate",
    align: "end",
    sortValue: (row) => row.profile.win_rate,
    render: (row) => formatRatioPercent(row.profile.win_rate),
  },
  {
    id: "sparkline",
    header: "Equity",
    render: (row) => (
      <Sparkline
        values={row.profile.sparkline}
        label={`${row.profile.name} portfolio value`}
        width={96}
        height={24}
      />
    ),
  },
];

/**
 * One overview section: header, aggregate line and the ranked profile table.
 *
 * The profiles keep the order the API returned (portfolio value, descending):
 * the rank column is that order, and the table only re-sorts when an operator
 * clicks a sortable header. Blocked and error rows stay in the ranking and are
 * marked with a tinted background next to their badge and reason.
 */
export function ModeSection({
  id,
  title,
  profiles,
  emptyMessage = "No profile in this section.",
  summary,
  notice,
  className,
}: ModeSectionProps) {
  const rows = rankProfiles(profiles);
  const aggregate = summariseProfiles(profiles);

  return (
    <section aria-labelledby={id} className={cn("min-w-0", className)}>
      <SectionHeader
        id={id}
        title={title}
        count={profiles.length}
        subtitle={formatModeSummary(aggregate)}
      />
      {summary !== undefined && summary.trim() !== "" ? (
        <p className="mb-2 min-w-0 text-sm text-muted-foreground">{summary}</p>
      ) : null}
      {notice}
      <DataTable
        columns={COLUMNS}
        rows={rows}
        rowKey={(row) => row.profile.id}
        caption={`${title}: profiles ranked by portfolio value`}
        emptyMessage={emptyMessage}
        bodyClassName="list-reveal"
        rowClassName={(row) => (isAttentionState(row.profile.state) ? "bg-muted/50" : undefined)}
      />
    </section>
  );
}
