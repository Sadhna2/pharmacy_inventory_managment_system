/**
 * The rows an answer came back with, as a table you can actually work in.
 *
 * WHY A LIBRARY HERE AND NOWHERE ELSE
 * -----------------------------------
 * Every other table in this product shows a known shape — products have a SKU
 * and an MRP, and the column widths were decided once, by a person, against
 * real values. `DataTable` is right for those and stays where it is.
 *
 * A question's answer has no known shape. The columns are whatever the model
 * selected: two of them or eleven, a branch name beside a rupee total, or a
 * composition string four hundred characters long next to a date. Nobody can
 * pick widths in advance for a table that has not been written yet, so the
 * person reading it has to be able to. That is what @tanstack/react-table is
 * carrying — resizing, ordering and sorting as state, headless, so the styling
 * stays this codebase's own rather than a vendor's.
 *
 * WHAT IT DELIBERATELY DOES NOT DO
 * --------------------------------
 * No filtering and no pagination. The result is capped at 200 rows before it
 * ever reaches the browser, and a filter box over an answer invites somebody to
 * narrow the rows here and read the number off the screen — when the honest way
 * to narrow an answer is to ask a narrower question, which re-runs the SQL and
 * shows it. A filter would quietly produce a figure with no query behind it.
 */

import { useMemo, useRef, useState } from "react";
import {
  type ColumnDef,
  type ColumnOrderState,
  type SortingState,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
} from "@tanstack/react-table";
import {
  ArrowDown,
  ArrowUp,
  CalendarDays,
  ChevronsUpDown,
  Download,
  GripVertical,
  Hash,
  IndianRupee,
  Percent,
  RotateCcw,
  Tag,
  Type,
} from "lucide-react";

import { Button } from "@/components/ui";
import { type CellKind, formatCell, isNumericKind } from "@/components/answerFormat";
import { cn } from "@/lib/format";

export interface ResultTableProps {
  labels: string[];
  kinds: CellKind[];
  rows: unknown[][];
  /** Names the file when the table is downloaded. */
  downloadName?: string;
}

/** One result row, carried with its position so two identical rows stay apart. */
interface Row {
  index: number;
  cells: unknown[];
}

/**
 * What each column holds, as a mark beside its name.
 *
 * The column names come out of the model's SELECT, so `total` might be rupees
 * or a count of boxes and the heading alone does not say which. The formatter
 * has already decided — it is what put the ₹ in front of the values — and this
 * shows that decision at the top of the column rather than leaving the reader
 * to infer it from the first row, which is exactly the row a sort moves.
 */
const KIND_ICON = {
  money: IndianRupee,
  quantity: Hash,
  percent: Percent,
  date: CalendarDays,
  timestamp: CalendarDays,
  code: Tag,
  text: Type,
} as const satisfies Record<CellKind, unknown>;

/**
 * RFC 4180. Not `join(",")`.
 *
 * A pharmacy's data is full of the three characters that break the naive
 * version: commas in "Unit 4, MIDC Phase II", quotes in a pack description,
 * newlines in an address. The file opens in Excel, and a row that splits into
 * the wrong columns there is worse than no download, because it looks fine.
 */
function csvCell(value: string): string {
  return /[",\n\r]/.test(value) ? `"${value.replace(/"/g, '""')}"` : value;
}

/**
 * The values as they appear on screen, not the raw ones.
 *
 * Somebody downloading this is taking away what they just read. A rupee column
 * that says ₹1,240.50 on screen and 1240.5000000 in the file is the same fact
 * twice in two notations, and the person now has to work out which to trust.
 */
function toCsv(labels: string[], kinds: CellKind[], rows: unknown[][]): string {
  const head = labels.map(csvCell).join(",");
  const body = rows.map((cells) =>
    cells.map((cell, i) => csvCell(formatCell(cell, kinds[i]))).join(","),
  );
  return [head, ...body].join("\r\n");
}

export function ResultTable({ labels, kinds, rows, downloadName = "answer" }: ResultTableProps) {
  // Position as the id, never the name: a join returns `name` twice and two
  // columns sharing an id makes ordering and resizing move the wrong one.
  const ids = useMemo(() => labels.map((_, i) => String(i)), [labels]);

  const [sorting, setSorting] = useState<SortingState>([]);
  const [order, setOrder] = useState<ColumnOrderState>(ids);
  const dragged = useRef<string | null>(null);

  const columns = useMemo<ColumnDef<Row>[]>(
    () =>
      labels.map((label, index) => ({
        id: String(index),
        header: label,
        accessorFn: (row: Row) => row.cells[index],
        enableSorting: true,
        size: isNumericKind(kinds[index]) ? 140 : 200,
        minSize: 80,
        sortingFn: (a, b) => {
          const kind = kinds[index];
          const x = a.original.cells[index];
          const y = b.original.cells[index];
          if (x == null) return y == null ? 0 : 1;
          if (y == null) return -1;
          // Numbers compared as numbers. Sorting a rupee column as text puts
          // ₹9 above ₹10, which reads as the table being wrong.
          if (isNumericKind(kind)) return Number(x) - Number(y);
          return String(x).localeCompare(String(y), undefined, { numeric: true });
        },
      })),
    [labels, kinds],
  );

  const data = useMemo<Row[]>(() => rows.map((cells, index) => ({ index, cells })), [rows]);

  const table = useReactTable({
    data,
    columns,
    state: { sorting, columnOrder: order },
    onSortingChange: setSorting,
    onColumnOrderChange: setOrder,
    columnResizeMode: "onChange",
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  });

  const download = () => {
    const csv = toCsv(labels, kinds, rows);
    // A BOM, so Excel on Windows reads it as UTF-8 rather than Latin-1 — the
    // difference between "Ahmedabad" and mojibake in a rupee sign.
    const blob = new Blob([`﻿${csv}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${downloadName}.csv`;
    link.click();
    URL.revokeObjectURL(url);
  };

  const reset = () => {
    setOrder(ids);
    setSorting([]);
    table.resetColumnSizing();
  };

  const rearranged = order.join() !== ids.join() || sorting.length > 0;

  /** Drop `id` where `over` currently sits, and shuffle the rest along. */
  const move = (id: string, over: string) => {
    setOrder((current) => {
      const next = [...current];
      const from = next.indexOf(id);
      const to = next.indexOf(over);
      if (from < 0 || to < 0 || from === to) return current;
      next.splice(to, 0, next.splice(from, 1)[0]);
      return next;
    });
  };

  return (
    <div className="min-w-0">
      <div className="flex flex-wrap items-center justify-between gap-2 px-1 pb-2">
        <p className="text-[12px] text-ink-faint">
          <span className="tnum font-medium text-ink-soft">
            {rows.length} {rows.length === 1 ? "row" : "rows"}
          </span>
          {" · "}
          Drag a heading to move it, drag its edge to resize, click to sort.
        </p>
        <div className="flex items-center gap-2">
          {rearranged && (
            <Button size="sm" onClick={reset}>
              <RotateCcw className="size-3.5" />
              Reset
            </Button>
          )}
          <Button size="sm" onClick={download}>
            <Download className="size-3.5" />
            CSV
          </Button>
        </div>
      </div>

      {/*
        Capped and scrolled inside itself. A 200-row answer pushed the next
        question two screens down the page, so the box you type in was never
        where you left it. The table keeps its own scrollbar and the page
        stays roughly one screen per answer.
      */}
      <div className="max-h-[26rem] overflow-auto rounded-lg border border-line">
        <table
          className="text-[13px]"
          style={{ width: table.getCenterTotalSize(), minWidth: "100%" }}
        >
          <thead className="sticky top-0 z-10 bg-muted text-[11px] tracking-wide text-ink-faint uppercase">
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id}>
                {group.headers.map((header) => {
                  const kind = kinds[Number(header.column.id)];
                  const numeric = isNumericKind(kind);
                  const sorted = header.column.getIsSorted();
                  const KindIcon = KIND_ICON[kind] ?? Type;
                  return (
                    <th
                      key={header.id}
                      scope="col"
                      style={{ width: header.getSize() }}
                      draggable
                      onDragStart={() => (dragged.current = header.column.id)}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={() => {
                        if (dragged.current) move(dragged.current, header.column.id);
                        dragged.current = null;
                      }}
                      className="group relative border-b border-line px-3 py-2 text-left font-medium select-none"
                    >
                      <button
                        type="button"
                        onClick={header.column.getToggleSortingHandler()}
                        className={cn(
                          "flex w-full items-center gap-1 text-left hover:text-ink",
                          numeric && "justify-end",
                        )}
                      >
                        <GripVertical className="size-3 shrink-0 cursor-grab text-ink-faint opacity-0 group-hover:opacity-100" />
                        <KindIcon className="size-3 shrink-0 text-ink-faint/70" />
                        <span className="truncate" title={String(header.column.columnDef.header)}>
                          {flexRender(header.column.columnDef.header, header.getContext())}
                        </span>
                        {sorted === "asc" ? (
                          <ArrowUp className="size-3 shrink-0 text-brand" />
                        ) : sorted === "desc" ? (
                          <ArrowDown className="size-3 shrink-0 text-brand" />
                        ) : (
                          <ChevronsUpDown className="size-3 shrink-0 opacity-0 group-hover:opacity-60" />
                        )}
                      </button>

                      {/* The resize handle. `touch-none` so dragging it on a
                          tablet resizes the column instead of scrolling the
                          table underneath it. */}
                      <span
                        onMouseDown={header.getResizeHandler()}
                        onTouchStart={header.getResizeHandler()}
                        className={cn(
                          "absolute top-0 right-0 h-full w-1.5 cursor-col-resize touch-none select-none",
                          "bg-transparent hover:bg-brand/40",
                          header.column.getIsResizing() && "bg-brand",
                        )}
                      />
                    </th>
                  );
                })}
              </tr>
            ))}
          </thead>

          <tbody className="divide-y divide-line">
            {table.getRowModel().rows.map((row, position) => (
              // Striped by rendered position, not by `row.original.index` —
              // after a sort the original indices are shuffled, and stripes
              // that follow them come out in clumps of two and three, which
              // reads as the table having lost rows.
              <tr
                key={row.original.index}
                className={cn(
                  "transition-colors hover:bg-brand-soft/50",
                  position % 2 === 1 && "bg-muted/30",
                )}
              >
                {row.getVisibleCells().map((cell) => {
                  const index = Number(cell.column.id);
                  const numeric = isNumericKind(kinds[index]);
                  const text = formatCell(cell.getValue(), kinds[index]);
                  return (
                    <td
                      key={cell.id}
                      style={{ width: cell.column.getSize() }}
                      className={cn("px-3 py-1.5", numeric && "tnum text-right")}
                    >
                      {/* Truncated with the whole value on hover: a composition
                          string is four hundred characters and would set the
                          row height for every other column. */}
                      <span className="block truncate" title={text}>
                        {text}
                      </span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

export default ResultTable;
