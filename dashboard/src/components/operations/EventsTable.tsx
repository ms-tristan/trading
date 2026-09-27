"use client";

import Link from "next/link";

import { Card } from "@/components/ui/Card";
import { DataTable, type DataTableColumn } from "@/components/ui/DataTable";
import { cn } from "@/lib/cn";
import { formatTimestamp } from "@/lib/format";
import type { EventItem } from "@/lib/types";

/** How one event level is written out. */
interface LevelMeta {
  label: string;
  /** Plain text marker, never an emoji; doubles the colour for every reader. */
  glyph: string;
  className: string;
}

const LEVEL_META: Record<EventItem["level"], LevelMeta> = {
  info: { label: "Info", glyph: "i", className: "border-border" },
  warning: { label: "Warning", glyph: "!", className: "border-status-queued" },
  error: { label: "Error", glyph: "\u2715", className: "border-destructive" },
};

export interface EventLevelBadgeProps {
  level: EventItem["level"];
}

/**
 * Level of one event.
 *
 * The level is carried by three signals at once - its wording, a text marker and
 * the border colour - so it stays readable without colour vision and in a
 * greyscale screenshot. An unknown level of a newer API renders as itself.
 */
export function EventLevelBadge({ level }: EventLevelBadgeProps) {
  const meta: LevelMeta = LEVEL_META[level] ?? {
    label: String(level),
    glyph: "?",
    className: "border-border",
  };

  return (
    <span
      data-level={level}
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-sm",
        meta.className,
      )}
    >
      <span aria-hidden="true">{meta.glyph}</span>
      <span>{meta.label}</span>
    </span>
  );
}

const COLUMNS: DataTableColumn<EventItem>[] = [
  {
    id: "timestamp",
    header: "Time",
    sortValue: (row) => row.timestamp,
    render: (row) => <span className="tabular-nums">{formatTimestamp(row.timestamp)}</span>,
  },
  {
    id: "level",
    header: "Level",
    sortValue: (row) => row.level,
    render: (row) => <EventLevelBadge level={row.level} />,
  },
  {
    id: "kind",
    header: "Kind",
    sortValue: (row) => row.kind,
    render: (row) => <span className="font-mono text-sm">{row.kind}</span>,
  },
  {
    id: "profile",
    header: "Profile",
    sortValue: (row) => row.profile_id ?? "",
    render: (row) =>
      row.profile_id === null ? (
        <span className="text-muted-foreground">platform</span>
      ) : (
        <Link
          href={`/profiles/${encodeURIComponent(row.profile_id)}`}
          className="underline-offset-2 hover:underline"
        >
          {row.profile_id}
        </Link>
      ),
  },
  {
    id: "message",
    header: "Message",
    render: (row) => <span className="break-words">{row.message}</span>,
  },
];

export interface EventsTableProps {
  /** Newest first, as `GET /api/events?limit=` publishes them. */
  events: EventItem[];
  className?: string;
}

/**
 * The engine journal: the 50 newest rows, newest first.
 *
 * Time, level, kind, profile and message - the five fields an incident is
 * reconstructed from. A failed profile is a link to its page, a platform-wide
 * event says `platform`, and a warning or an error tints its whole row next to
 * the badge that already spells the level out.
 */
export function EventsTable({ events, className }: EventsTableProps) {
  return (
    <Card
      className={className}
      headingLevel={2}
      title="Recent events"
      description={`${events.length} newest rows of the engine journal`}
    >
      <DataTable
        columns={COLUMNS}
        rows={events}
        rowKey={(row) => row.id}
        caption="Recent engine events, newest first"
        emptyMessage="The engine journal is empty."
        rowClassName={(row) => {
          if (row.level === "error") {
            return "bg-destructive/10";
          }
          if (row.level === "warning") {
            return "bg-muted/50";
          }
          return undefined;
        }}
        minWidthClassName="min-w-[880px]"
      />
    </Card>
  );
}
