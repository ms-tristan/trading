"use client";

import { useMemo, useState, type ReactNode } from "react";

import { cn } from "@/lib/cn";
import type { SortKey } from "@/lib/types";

/** ARIA sort direction of one column. */
export type SortDirection = "ascending" | "descending";

export interface DataTableColumn<Row> {
  id: string;
  header: string;
  /** Present when the column is sortable; returns the value to order by. */
  sortValue?: (row: Row) => number | string;
  /** Ranking key of the contract this column implements, when it has one. */
  sortKey?: SortKey;
  align?: "start" | "end";
  headerClassName?: string;
  cellClassName?: string;
  render: (row: Row) => ReactNode;
}

export interface DataTableProps<Row> {
  columns: DataTableColumn<Row>[];
  rows: Row[];
  rowKey: (row: Row) => string;
  /** Accessible name of the table (rendered as a screen-reader-only caption). */
  caption: string;
  emptyMessage?: string;
  rowClassName?: (row: Row, index: number) => string | undefined;
  /** Minimum width of the table; the wrapper scrolls, the page never does. */
  minWidthClassName?: string;
  /** Class names of the `<tbody>`, e.g. the staggered list reveal. */
  bodyClassName?: string;
  className?: string;
}

type SortState = { id: string; direction: SortDirection } | null;

function compareValues(left: number | string, right: number | string): number {
  if (typeof left === "number" && typeof right === "number") {
    return (Number.isFinite(left) ? left : 0) - (Number.isFinite(right) ? right : 0);
  }
  return String(left).localeCompare(String(right));
}

/**
 * Generic sortable table.
 *
 * * every sortable header is a real `<button>` inside the `<th>`, which carries
 *   `aria-sort` (`none` until the operator clicks it, then `ascending`,
 *   `descending`, then back to `none`);
 * * the rows are rendered in the order they are given until a click happens, so
 *   the API ranking of the overview survives untouched;
 * * a wide table scrolls inside its own container: the page never scrolls
 *   horizontally;
 * * rows are 36px tall (`h-9`), the house row height.
 */
export function DataTable<Row>({
  columns,
  rows,
  rowKey,
  caption,
  emptyMessage = "Nothing to show.",
  rowClassName,
  minWidthClassName = "min-w-[880px]",
  bodyClassName,
  className,
}: DataTableProps<Row>) {
  const [sort, setSort] = useState<SortState>(null);

  const orderedRows = useMemo(() => {
    if (sort === null) {
      return rows;
    }
    const column = columns.find((candidate) => candidate.id === sort.id);
    if (!column || !column.sortValue) {
      return rows;
    }
    const getSortValue = column.sortValue;
    const factor = sort.direction === "ascending" ? 1 : -1;
    return rows
      .map((row, index) => ({ row, index }))
      .sort((left, right) => {
        const comparison = compareValues(getSortValue(left.row), getSortValue(right.row));
        return comparison !== 0 ? comparison * factor : left.index - right.index;
      })
      .map((entry) => entry.row);
  }, [columns, rows, sort]);

  function toggleSort(columnId: string) {
    setSort((current) => {
      if (current === null || current.id !== columnId) {
        return { id: columnId, direction: "ascending" };
      }
      if (current.direction === "ascending") {
        return { id: columnId, direction: "descending" };
      }
      return null;
    });
  }

  return (
    <div className={cn("w-full min-w-0 overflow-x-auto", className)}>
      <table className={cn("w-full border-collapse text-base", minWidthClassName)}>
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border text-muted-foreground">
            {columns.map((column) => {
              const sortable = typeof column.sortValue === "function";
              const activeDirection =
                sort !== null && sort.id === column.id ? sort.direction : "none";
              return (
                <th
                  key={column.id}
                  scope="col"
                  aria-sort={sortable ? activeDirection : undefined}
                  className={cn(
                    "px-2 py-2 text-sm font-medium",
                    column.align === "end" ? "text-right" : "text-left",
                    column.headerClassName,
                  )}
                >
                  {sortable ? (
                    <button
                      type="button"
                      onClick={() => toggleSort(column.id)}
                      aria-label={`Sort by ${column.header}`}
                      className="inline-flex items-center gap-1 rounded-sm transition-smooth hover:text-foreground"
                    >
                      <span>{column.header}</span>
                      <span aria-hidden="true">
                        {activeDirection === "ascending"
                          ? "\u25B2"
                          : activeDirection === "descending"
                            ? "\u25BC"
                            : "\u2195"}
                      </span>
                    </button>
                  ) : (
                    column.header
                  )}
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className={bodyClassName}>
          {orderedRows.length === 0 ? (
            <tr className="h-9">
              <td colSpan={columns.length} className="px-2 py-2 text-sm text-muted-foreground">
                {emptyMessage}
              </td>
            </tr>
          ) : (
            orderedRows.map((row, index) => (
              <tr
                key={rowKey(row)}
                className={cn(
                  "h-9 border-b border-border/60 align-middle transition-smooth",
                  rowClassName?.(row, index),
                )}
              >
                {columns.map((column) => (
                  <td
                    key={column.id}
                    className={cn(
                      "px-2 py-1",
                      column.align === "end" ? "text-right tabular-nums" : "text-left",
                      column.cellClassName,
                    )}
                  >
                    {column.render(row)}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </table>
    </div>
  );
}
