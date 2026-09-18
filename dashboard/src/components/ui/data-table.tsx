import type { ReactNode } from 'react';

import { cn } from '@/lib/cn';

/** One column of a {@link DataTable}. */
export interface DataTableColumn<T> {
  /** Stable identity of the column. */
  key: string;
  /** Visible header text (also part of the accessible table name). */
  header: string;
  /** Right-align the column and render it with tabular figures. */
  numeric?: boolean;
  /** Cell renderer, called once per row. */
  render: (row: T) => ReactNode;
}

/** Props of {@link DataTable}. */
export interface DataTableProps<T> {
  columns: DataTableColumn<T>[];
  rows: T[];
  /** Stable React key of a row (a profile id, an order id, ...). */
  rowKey: (row: T) => string;
  /** Accessible caption of the table. */
  caption: string;
  /** Message rendered in place of the body when there is no row. */
  emptyMessage: string;
  className?: string;
}

/**
 * Scrollable data table.
 *
 * The wrapper scrolls horizontally on a narrow screen so the page itself never
 * does (375 px target). Column headers are real `<th scope="col">` cells and the
 * caption names the table for assistive technology.
 */
export function DataTable<T>({
  columns,
  rows,
  rowKey,
  caption,
  emptyMessage,
  className,
}: DataTableProps<T>) {
  return (
    <div className={cn('w-full overflow-x-auto', className)}>
      <table className="w-full border-collapse text-sm">
        <caption className="sr-only">{caption}</caption>
        <thead>
          <tr className="border-b border-border text-left">
            {columns.map((column) => (
              <th
                key={column.key}
                scope="col"
                className={cn(
                  'px-md py-sm font-mono text-xs font-semibold uppercase tracking-wide text-muted-foreground',
                  column.numeric === true && 'text-right',
                )}
              >
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td
                colSpan={columns.length}
                className="px-md py-xl text-center text-sm text-muted-foreground"
              >
                {emptyMessage}
              </td>
            </tr>
          ) : (
            rows.map((row) => (
              <tr
                key={rowKey(row)}
                className={cn(
                  'border-b border-border/60 last:border-b-0',
                  'motion-safe:transition-colors motion-safe:duration-150 hover:bg-muted/40',
                )}
              >
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={cn(
                      'px-md py-sm align-top text-foreground',
                      column.numeric === true && 'text-right font-mono tabular-nums',
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
