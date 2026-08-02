/**
 * The one table component.
 *
 * Responsive strategy: a real table on >=md, and a stacked card list below it.
 * Columns opt into the mobile card via `primary` / `secondary` / `meta`, so
 * each screen decides what survives on a phone rather than shrinking a
 * 9-column grid into something unreadable.
 */

import { Fragment, type ReactNode } from "react";
import { ChevronLeft, ChevronRight, Inbox } from "lucide-react";
import { cn } from "@/lib/format";
import { Button, EmptyState, TableSkeleton } from "./ui";

export interface Column<T> {
  key: string;
  header: string;
  render: (row: T) => ReactNode;
  /** Right-align and use tabular figures — for quantities and money. */
  numeric?: boolean;
  /**
   * Card slot on mobile. Columns with no role are hidden on phones.
   *
   * `actions` gets its own full-width row under the content, because buttons
   * squeezed beside a truncating product name are the first thing to become
   * untappable on a narrow screen.
   */
  card?: "primary" | "secondary" | "meta" | "actions";
  /** Hide below this breakpoint on the desktop table. */
  hideBelow?: "sm" | "md" | "lg" | "xl";
  width?: string;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  loading?: boolean;
  onRowClick?: (row: T) => void;
  emptyTitle?: string;
  emptyDescription?: string;
  emptyAction?: ReactNode;
  page?: number;
  pages?: number;
  total?: number;
  onPageChange?: (page: number) => void;
}

const HIDE_CLASSES = {
  sm: "hidden sm:table-cell",
  md: "hidden md:table-cell",
  lg: "hidden lg:table-cell",
  xl: "hidden xl:table-cell",
} as const;

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  loading,
  onRowClick,
  emptyTitle = "Nothing here yet",
  emptyDescription,
  emptyAction,
  page,
  pages,
  total,
  onPageChange,
}: DataTableProps<T>) {
  if (loading) return <TableSkeleton />;

  if (rows.length === 0) {
    return (
      <EmptyState
        icon={Inbox}
        title={emptyTitle}
        description={emptyDescription}
        action={emptyAction}
      />
    );
  }

  const primary = columns.filter((c) => c.card === "primary");
  const secondary = columns.filter((c) => c.card === "secondary");
  const meta = columns.filter((c) => c.card === "meta");
  const actions = columns.filter((c) => c.card === "actions");

  return (
    <div>
      {/* ------------------------------------------------ desktop: real table */}
      <div className="scroll-x hidden md:block">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-line">
              {columns.map((col) => (
                <th
                  key={col.key}
                  style={col.width ? { width: col.width } : undefined}
                  className={cn(
                    "px-4 py-2.5 text-[11px] font-semibold tracking-wide text-ink-faint uppercase",
                    col.numeric ? "text-right" : "text-left",
                    col.hideBelow && HIDE_CLASSES[col.hideBelow],
                  )}
                >
                  {col.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((row) => (
              <tr
                key={rowKey(row)}
                onClick={() => onRowClick?.(row)}
                className={cn(
                  "transition-colors",
                  onRowClick && "cursor-pointer hover:bg-muted/60",
                )}
              >
                {columns.map((col) => (
                  <td
                    key={col.key}
                    className={cn(
                      "px-4 py-3 align-middle",
                      col.numeric && "text-right tnum",
                      col.hideBelow && HIDE_CLASSES[col.hideBelow],
                    )}
                  >
                    {col.render(row)}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* -------------------------------------------------- mobile: card list */}
      <ul className="divide-y divide-line md:hidden">
        {rows.map((row) => (
          <li
            key={rowKey(row)}
            onClick={() => onRowClick?.(row)}
            className={cn(
              "px-4 py-3.5",
              onRowClick && "cursor-pointer active:bg-muted",
            )}
          >
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0 flex-1 space-y-1">
                {primary.map((col) => (
                  <div
                    key={col.key}
                    className="truncate text-sm font-medium text-ink"
                  >
                    {col.render(row)}
                  </div>
                ))}
                {secondary.length > 0 && (
                  <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[13px] text-ink-soft">
                    {secondary.map((col) => (
                      <span key={col.key} className="truncate">
                        {col.render(row)}
                      </span>
                    ))}
                  </div>
                )}
              </div>
              {meta.length > 0 && (
                <div className="flex shrink-0 flex-col items-end gap-1 text-right">
                  {meta.map((col) => (
                    <div key={col.key} className="text-sm tnum">
                      {col.render(row)}
                    </div>
                  ))}
                </div>
              )}
            </div>

            {actions.length > 0 && (
              // Buttons are declared `size="sm"` for the dense desktop table;
              // grow them here so they clear a comfortable touch target.
              <div
                onClick={(e) => e.stopPropagation()}
                className={cn(
                  // Columns render straight into this row rather than through
                  // a wrapper, so a row whose contents are all permission- or
                  // status-gated away is genuinely empty and collapses.
                  "mt-2.5 flex flex-wrap items-center justify-end gap-2 empty:hidden",
                  "[&_button]:h-9 [&_button]:px-3",
                )}
              >
                {actions.map((col) => (
                  <Fragment key={col.key}>{col.render(row)}</Fragment>
                ))}
              </div>
            )}
          </li>
        ))}
      </ul>

      {/* ------------------------------------------------------- pagination */}
      {onPageChange && pages !== undefined && pages > 1 && (
        <div className="flex items-center justify-between gap-3 border-t border-line px-4 py-2.5">
          <p className="text-[13px] text-ink-soft">
            Page <span className="tnum font-medium text-ink">{page}</span> of{" "}
            <span className="tnum font-medium text-ink">{pages}</span>
            {total !== undefined && (
              <span className="hidden sm:inline">
                {" "}
                · <span className="tnum">{total}</span> records
              </span>
            )}
          </p>
          <div className="flex gap-1.5">
            <Button
              size="sm"
              onClick={() => onPageChange((page ?? 1) - 1)}
              disabled={(page ?? 1) <= 1}
              aria-label="Previous page"
            >
              <ChevronLeft className="size-4" />
            </Button>
            <Button
              size="sm"
              onClick={() => onPageChange((page ?? 1) + 1)}
              disabled={(page ?? 1) >= pages}
              aria-label="Next page"
            >
              <ChevronRight className="size-4" />
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
