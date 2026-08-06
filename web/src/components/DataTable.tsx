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
import { Button, EmptyState, ErrorState, TableSkeleton } from "./ui";

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
  /**
   * Hug the content instead of taking a share of the spare width.
   *
   * A `w-full` table with no widths hands every column a slice of whatever is
   * left over, so a status badge and a row menu each got as much room as a
   * document reference and the table grew a corridor of dead space in the
   * middle — most visibly between the last text column and Status.
   *
   * `1%` rather than `0`: it is the smallest width a browser will honour while
   * still expanding the cell to fit its content, which is exactly the rule
   * wanted here. Numeric columns get this by default — a figure never needs
   * slack — and anything else that should hug asks for it.
   */
  shrink?: boolean;
}

/**
 * Does this column hug its content rather than share the spare width?
 *
 * Never under `even`, and the exception that looked obvious is worth recording
 * so nobody re-adds it. Giving the row-menu column `width: 1%` there does not
 * hug: `table-fixed` takes a declared width literally, so the cell became
 * twelve pixels, its button overflowed, and the table grew past its container
 * — a horizontal scrollbar on every operations screen and the menu itself
 * pushed off the right edge. `1%` only means "as small as the content allows"
 * under automatic layout, which is exactly what `even` turns off.
 */
function hugs<T>(col: Column<T>, even: boolean): boolean {
  return even ? false : (col.shrink ?? Boolean(col.numeric));
}

/** An explicit width wins; otherwise a hugging column asks for the minimum. */
function colWidth<T>(
  col: Column<T>,
  even: boolean,
): { width: string } | undefined {
  // A width still wins under `even`. `table-fixed` splits what is left over
  // equally between the columns that did not ask, so one column saying it
  // needs more does not stop the others being a regular grid.
  if (col.width) return { width: col.width };
  return !even && hugs(col, even) ? { width: "1%" } : undefined;
}

interface DataTableProps<T> {
  columns: Column<T>[];
  rows: T[];
  rowKey: (row: T) => string | number;
  /**
   * True until the query has produced *something* — data or an error.
   *
   * Pass React Query's `isPending`, not `isLoading`. They differ exactly where
   * it matters: between a failed attempt and its retry, `isLoading` is false
   * because no request is in flight, while `error` is still null because the
   * query has not given up. Both falsy means this component falls through to
   * its empty state and tells the user there is nothing to see — during a
   * retry, and indefinitely if the retry is paused because the tab is in the
   * background. `isPending` stays true across the whole gap.
   */
  loading?: boolean;
  /**
   * One width, divided equally between the columns.
   *
   * Spare width has to go somewhere, and every way of deciding where put it
   * somewhere that looked wrong: shared in proportion to content it opened a
   * corridor before Status; named as percentages it scaled with the viewport,
   * so a 10-character reference sat in a cell four times its own width; given
   * wholly to the trailing actions column it packed everything hard left.
   *
   * So this stops choosing. `table-fixed` with no widths at all gives every
   * column exactly the same share, which is the one arrangement that cannot
   * favour a column — a plain, regular grid, the same at every window width.
   * Cells that outgrow their share wrap, which is why the crowded tables (nine
   * columns, long warehouse names) are left on the automatic layout instead.
   */
  even?: boolean;
  /**
   * Whatever the query failed with, if it did.
   *
   * Without this the table falls straight through to its empty state, and a
   * screen that could not reach the server says "No purchase orders" — which
   * is a statement about the business, not about the network. Every list here
   * passes it; the prop is optional only so a table fed from local state does
   * not have to invent one.
   */
  error?: unknown;
  onRetry?: () => void;
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
  even = false,
  error,
  onRetry,
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

  // Before the empty state, always: an error and an empty result are the two
  // things this component must never conflate.
  if (error) return <ErrorState error={error} onRetry={onRetry} />;

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
        <table
          className={cn(
            "w-full border-collapse text-sm",
            even && "table-fixed",
          )}
        >
          <thead>
            <tr className="border-b border-line">
              {columns.map((col) => (
                <th
                  key={col.key}
                  style={colWidth(col, even)}
                  className={cn(
                    "px-4 py-2.5 text-[11px] font-semibold tracking-wide text-ink-faint uppercase",
                    col.numeric ? "text-right" : "text-left",
                    hugs(col, even) && "whitespace-nowrap",
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
                      hugs(col, even) && "whitespace-nowrap",
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
