/**
 * Form building blocks shared by every create screen.
 *
 * Purchase orders, sales orders, transfers, adjustments and goods receipts are
 * all the same shape: a header plus a list of product lines. Rather than write
 * that five times, they compose `LineItems` with different per-line columns.
 *
 * Two things these deliberately get right:
 *
 *   - **Server errors land on the field that caused them.** The API returns
 *     RFC 7807 problems with a per-field `errors` array; `useSubmit` maps those
 *     onto inputs so "cold-chain products need a cold-chain bin" appears under
 *     the bin selector, not in a banner the user has to interpret.
 *   - **The product picker searches.** It is fine with 12 products and still
 *     fine with the 1,200 the generator will create.
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Plus, Search, Trash2 } from "lucide-react";
import { ApiError, api, qs } from "@/lib/api";
import { cn } from "@/lib/format";
import type { Page, Product, TrackingMode } from "@/lib/types";
import { Button, Input, Select } from "./ui";

/* ------------------------------------------------------------ useSubmit */

interface SubmitOptions {
  invalidate: string[];
  onDone?: (result: unknown) => void;
  /** PATCH when editing an existing record; POST otherwise. */
  method?: "post" | "patch";
}

/**
 * Wraps a write with the error handling every form needs: a top-level message
 * for whole-request failures, and `fieldErrors` keyed by field name.
 */
export function useSubmit(
  path: string,
  { invalidate, onDone, method = "post" }: SubmitOptions,
) {
  const qc = useQueryClient();
  const [message, setMessage] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});

  const mutation = useMutation({
    mutationFn: (body: unknown) => api[method](path, body),
    onSuccess: (result) => {
      setMessage(null);
      setFieldErrors({});
      // Balances and stock are invalidated everywhere because almost every
      // document eventually moves stock, and a stale dashboard is worse than
      // an extra refetch.
      [...invalidate, "balances", "stock", "movements"].forEach((key) =>
        qc.invalidateQueries({ queryKey: [key] }),
      );
      onDone?.(result);
    },
    onError: (err) => {
      if (err instanceof ApiError) {
        setFieldErrors(err.fieldErrors);
        setMessage(err.problem.detail || "Could not save this");
      } else {
        setMessage("Could not reach the server");
      }
    },
  });

  const reset = () => {
    setMessage(null);
    setFieldErrors({});
    mutation.reset();
  };

  return { ...mutation, message, fieldErrors, reset };
}

/** Red banner for errors that belong to the whole request, not one field. */
export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="rounded-lg border border-danger/20 bg-danger-soft px-3 py-2 text-[13px] text-danger">
      {message}
    </div>
  );
}

/* -------------------------------------------------------- ProductPicker */

interface DropdownAnchor {
  left: number;
  width: number;
  top: number;
  bottom: number;
  maxHeight: number;
  flip: boolean;
}

const GAP = 4;
const MIN_DROP = 140;
const MAX_DROP = 256;

/**
 * Position the result list against its input in viewport coordinates.
 *
 * The list is rendered at the document root because both the line-item table
 * (`overflow-hidden`, for its rounded corners) and the modal body
 * (`overflow-y-auto`) would otherwise clip it — a picker on the last row would
 * show its matches cut in half. Fixed positioning sidesteps every ancestor,
 * at the cost of having to compute the coordinates by hand.
 */
function anchorFor(el: HTMLElement): DropdownAnchor {
  const r = el.getBoundingClientRect();
  const below = window.innerHeight - r.bottom - GAP;
  const above = r.top - GAP;
  // Only flip upwards when below is genuinely too cramped to be usable and
  // above is actually roomier, so the list does not jump about while typing.
  const flip = below < MIN_DROP && above > below;
  return {
    left: r.left,
    width: r.width,
    top: r.bottom + GAP,
    bottom: window.innerHeight - r.top + GAP,
    maxHeight: Math.max(MIN_DROP, Math.min(MAX_DROP, flip ? above : below)),
    flip,
  };
}

/**
 * Type-to-search product selector.
 *
 * A plain <select> would need every product in the DOM. This queries the API
 * with the same `q` filter the Products page uses, so it stays fast and shows
 * SKU and pack size — the things that disambiguate two similar drug names.
 */
export function ProductPicker({
  value,
  onChange,
  placeholder = "Search a product…",
  invalid,
}: {
  /** Only the name and SKU are displayed, so a line's narrow product fits. */
  value: LineProduct | null;
  onChange: (product: Product | null) => void;
  placeholder?: string;
  invalid?: boolean;
}) {
  const [term, setTerm] = useState("");
  const [open, setOpen] = useState(false);
  const [anchor, setAnchor] = useState<DropdownAnchor | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const list = useRef<HTMLUListElement>(null);

  // The list is portalled to <body>, so it is outside `box` in the DOM even
  // though it belongs to this picker — click-away has to spare it explicitly.
  useEffect(() => {
    const away = (e: MouseEvent) => {
      const target = e.target as Node;
      if (box.current?.contains(target) || list.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, []);

  const place = useCallback(() => {
    if (!box.current) return;
    setAnchor(anchorFor(box.current));
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    place();
    // Capture phase so the modal's own scroll container is heard, not just
    // the window — otherwise the list detaches from its input on scroll.
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, place]);

  const { data, isFetching } = useQuery({
    queryKey: ["products", "picker", term],
    queryFn: () =>
      // Retired products are offered nowhere a new document is being written.
      // They stay readable on the documents that already name them — retiring
      // is how the catalogue stops something being ordered again, not how it
      // erases what was ordered before.
      api.get<Page<Product>>(
        `/api/v1/products${qs({ q: term, size: 20, is_active: true })}`,
      ),
    enabled: open,
  });

  if (value) {
    return (
      <button
        type="button"
        onClick={() => {
          onChange(null);
          setTerm("");
          setOpen(true);
        }}
        className={cn(
          "flex h-9.5 w-full items-center justify-between gap-2 rounded-lg border px-3 text-left",
          "border-line bg-surface hover:bg-muted",
        )}
      >
        <span className="min-w-0 truncate text-sm text-ink">{value.name}</span>
        <span className="shrink-0 font-mono text-[11px] text-ink-faint">
          {value.sku}
        </span>
      </button>
    );
  }

  return (
    <div ref={box} className="relative">
      <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-faint" />
      <Input
        value={term}
        placeholder={placeholder}
        onFocus={() => setOpen(true)}
        onChange={(e) => {
          setTerm(e.target.value);
          setOpen(true);
        }}
        className={cn("pl-9", invalid && "border-danger")}
      />
      {open && anchor && createPortal(
        <ul
          ref={list}
          style={{
            position: "fixed",
            left: anchor.left,
            width: anchor.width,
            maxHeight: anchor.maxHeight,
            ...(anchor.flip
              ? { bottom: anchor.bottom }
              : { top: anchor.top }),
          }}
          className="z-50 overflow-y-auto rounded-lg border border-line bg-surface py-1 shadow-lg"
        >
          {isFetching && !data ? (
            <li className="px-3 py-2 text-[13px] text-ink-faint">Searching…</li>
          ) : !data?.items.length ? (
            <li className="px-3 py-2 text-[13px] text-ink-faint">
              Nothing matches “{term}”
            </li>
          ) : (
            data.items.map((p) => (
              <li key={p.id}>
                <button
                  type="button"
                  onClick={() => {
                    onChange(p);
                    setOpen(false);
                  }}
                  className="flex w-full items-baseline justify-between gap-3 px-3 py-1.5 text-left hover:bg-muted"
                >
                  <span className="min-w-0 truncate text-[13px] text-ink">
                    {p.name}
                  </span>
                  <span className="shrink-0 font-mono text-[11px] text-ink-faint">
                    {p.sku}
                  </span>
                </button>
              </li>
            ))
          )}
        </ul>,
        document.body,
      )}
    </div>
  );
}

/* ------------------------------------------------------------ LineItems */

/**
 * The part of a product a line actually needs.
 *
 * A full `Product` is assignable to this, so the picker keeps handing over
 * whole records. It exists so a line can also be built from something that is
 * *not* a full product — a purchase-order line, say, which knows the id, name,
 * SKU and tracking mode and nothing else. Widening those four fields into a
 * fabricated `Product` would mean inventing two dozen values that no caller
 * reads and that would be wrong if one ever did.
 */
export interface LineProduct {
  id: number;
  sku: string;
  name: string;
  tracking_mode: TrackingMode;
}

export interface Line {
  key: number;
  product: LineProduct | null;
  /** Free-form per-document fields — quantity, price, batch, expiry. */
  values: Record<string, string>;
}

export interface LineOption {
  value: string;
  label: string;
}

export interface LineColumn {
  name: string;
  header: string;
  placeholder?: string;
  type?: "text" | "number" | "date" | "select";
  /**
   * Track width as a CSS length — `"6rem"`, not a utility class.
   *
   * It has to be a length because the header and the input row are laid out
   * from one generated `grid-template-columns`, and a class name cannot be
   * summed into the row's minimum width. Same convention as DataTable.
   */
  width?: string;
  /** Hide unless the chosen product needs it — e.g. batch on a tracked drug. */
  showFor?: (product: LineProduct | null) => boolean;
  /**
   * Choices for a `select` column, resolved per row.
   *
   * A function rather than a fixed list because the useful cases depend on the
   * row: which batches of *this* product are on the shelf, which branches are
   * valid for *this* receipt. Returning an empty list disables the control.
   */
  options?: (product: LineProduct | null) => LineOption[];
  /** Shown as the empty choice on a `select`. */
  emptyLabel?: string;
}

let nextKey = 1;
export const emptyLine = (): Line => ({ key: nextKey++, product: null, values: {} });

/**
 * The repeating product-line editor.
 *
 * Renders as a table on desktop and stacked cards on mobile, mirroring
 * DataTable so the app reads the same way at every width.
 */
export function LineItems({
  lines,
  onChange,
  columns,
  fieldErrors = {},
  validate,
  note,
  rowAction,
  lockProduct,
}: {
  lines: Line[];
  onChange: (lines: Line[]) => void;
  columns: LineColumn[];
  fieldErrors?: Record<string, string>;
  /**
   * Check a row as it is typed, before anything is sent.
   *
   * The server is still the authority — it holds the row locks and it is the
   * only thing that can be right about stock at the moment of dispatch. This
   * is here so an impossible line is refused while the person is still looking
   * at the field they got wrong, instead of two screens later.
   */
  validate?: (line: Line, index: number) => string | null;
  /**
   * Quiet context under a row — what the order asked for, what has already
   * arrived. Shown only when the row is not in error, because a correction to
   * make always outranks background information.
   */
  note?: (line: Line, index: number) => ReactNode;
  /**
   * An offer under a row, for when its problem has a fix this form can make.
   *
   * Shown alongside the error rather than instead of it — the error says what
   * is wrong and the action is one way out, not a replacement for knowing.
   */
  rowAction?: (line: Line, index: number) => ReactNode;
  /**
   * Render the product as text instead of a picker.
   *
   * Used where the product was not chosen here and swapping it would break a
   * link to something else — a receipt line that came from a purchase order
   * belongs to that order's line, and re-pointing it would silently orphan it.
   */
  lockProduct?: (line: Line, index: number) => boolean;
}) {
  const update = (key: number, patch: Partial<Line>) =>
    onChange(lines.map((l) => (l.key === key ? { ...l, ...patch } : l)));

  const setValue = (key: number, name: string, v: string) =>
    onChange(
      lines.map((l) =>
        l.key === key ? { ...l, values: { ...l.values, [name]: v } } : l,
      ),
    );

  /**
   * One grid template, shared by the header and every input row.
   *
   * These used to be two flex rows that happened to list the same widths. They
   * were free to disagree — the header cells could shrink while the input cells
   * could not — so as soon as the columns outgrew the dialog the labels slid
   * left of the boxes they name and the last column was cut off. Deriving both
   * from a single template makes that failure unrepresentable: the only free
   * track is the product name, and it resolves identically in both because both
   * sit in the same width.
   */
  const DEFAULT_WIDTH = "7rem";
  const PRODUCT_WIDTH = "13rem";
  const ACTION_WIDTH = "2.25rem";
  const GAP = 0.5;

  const { template, minWidth } = useMemo(() => {
    const widths = columns.map((c) => c.width ?? DEFAULT_WIDTH);
    return {
      template: [`minmax(${PRODUCT_WIDTH},1fr)`, ...widths, ACTION_WIDTH].join(" "),
      // Below this the row scrolls sideways rather than crushing its columns.
      // The row's own horizontal padding is in the sum because `border-box`
      // counts it inside min-width.
      minWidth: `calc(${[PRODUCT_WIDTH, ...widths, ACTION_WIDTH].join(" + ")} + ${
        (columns.length + 1) * GAP + 1.5
      }rem)`,
    };
  }, [columns]);

  const lineError = useMemo(() => {
    // The API reports line problems as `lines.0.quantity`; pull the index out
    // so the message can sit on the row that caused it.
    const byIndex: Record<number, string> = {};
    Object.entries(fieldErrors).forEach(([field, msg]) => {
      const m = /^lines\.(\d+)/.exec(field);
      if (m) byIndex[Number(m[1])] = msg;
    });
    return byIndex;
  }, [fieldErrors]);

  return (
    <div className="space-y-2">
      {/* The dropdown of the product picker is portalled to <body> and fixed,
          so it is not clipped by this scroll container and re-anchors on
          scroll — see ProductPicker. */}
      <div
        className="rounded-lg border border-line max-md:overflow-hidden md:overflow-x-auto"
        style={
          {
            "--line-cols": template,
            "--line-min": minWidth,
          } as CSSProperties
        }
      >
        <div className="hidden bg-muted/60 px-3 py-2 md:grid md:min-w-[var(--line-min)] md:grid-cols-[var(--line-cols)] md:gap-2">
          <span className="text-[11px] font-semibold tracking-wide text-ink-faint uppercase">
            Product
          </span>
          {columns.map((c) => (
            <span
              key={c.name}
              className="truncate text-[11px] font-semibold tracking-wide text-ink-faint uppercase"
            >
              {c.header}
            </span>
          ))}
          <span />
        </div>

        <div className="divide-y divide-line">
          {lines.map((line, index) => {
            // A live check wins over a server message: if the user has already
            // corrected the field, the old server error is stale.
            const problem = validate?.(line, index) ?? lineError[index] ?? null;
            return (
            <div key={line.key} className="p-3 md:min-w-[var(--line-min)]">
              <div className="flex flex-col gap-2 md:grid md:grid-cols-[var(--line-cols)] md:items-start md:gap-2">
                <div className="min-w-0">
                  {lockProduct?.(line, index) && line.product ? (
                    <div className="flex h-9.5 items-center justify-between gap-2 rounded-lg border border-line bg-muted/40 px-3">
                      <span className="min-w-0 truncate text-sm text-ink">
                        {line.product.name}
                      </span>
                      <span className="shrink-0 font-mono text-[11px] text-ink-faint">
                        {line.product.sku}
                      </span>
                    </div>
                  ) : (
                    <ProductPicker
                      value={line.product}
                      onChange={(product) => update(line.key, { product })}
                      invalid={Boolean(problem)}
                    />
                  )}
                </div>

                {columns.map((c) => {
                  const hidden = c.showFor && !c.showFor(line.product);
                  return (
                    <div key={c.name} className="min-w-0">
                      {hidden ? (
                        <div className="hidden h-9.5 items-center justify-center rounded-lg border border-dashed border-line md:flex">
                          <span className="text-[11px] text-ink-faint">n/a</span>
                        </div>
                      ) : (
                        <>
                          <span className="mb-1 block text-[11px] text-ink-faint md:hidden">
                            {c.header}
                          </span>
                          {c.type === "select" ? (
                            (() => {
                              const options = c.options?.(line.product) ?? [];
                              return (
                                <Select
                                  value={line.values[c.name] ?? ""}
                                  disabled={options.length === 0}
                                  onChange={(e) =>
                                    setValue(line.key, c.name, e.target.value)
                                  }
                                >
                                  <option value="">
                                    {options.length === 0
                                      ? "—"
                                      : (c.emptyLabel ?? "Any")}
                                  </option>
                                  {options.map((o) => (
                                    <option key={o.value} value={o.value}>
                                      {o.label}
                                    </option>
                                  ))}
                                </Select>
                              );
                            })()
                          ) : (
                            <Input
                              type={c.type ?? "text"}
                              inputMode={c.type === "number" ? "decimal" : undefined}
                              placeholder={c.placeholder}
                              value={line.values[c.name] ?? ""}
                              aria-invalid={problem ? true : undefined}
                              className={cn(
                                problem && "border-danger focus:border-danger",
                              )}
                              onChange={(e) => setValue(line.key, c.name, e.target.value)}
                            />
                          )}
                        </>
                      )}
                    </div>
                  );
                })}

                <Button
                  variant="ghost"
                  size="sm"
                  aria-label="Remove line"
                  disabled={lines.length === 1}
                  onClick={() => onChange(lines.filter((l) => l.key !== line.key))}
                  className="shrink-0 self-end px-2 md:self-start"
                >
                  <Trash2 className="size-4" />
                </Button>
              </div>

              {problem ? (
                <p className="mt-1.5 text-xs text-danger">{problem}</p>
              ) : (
                note?.(line, index) && (
                  <p className="mt-1.5 text-xs text-ink-faint">
                    {note(line, index)}
                  </p>
                )
              )}
              {rowAction?.(line, index)}
            </div>
            );
          })}
        </div>
      </div>

      <Button size="sm" onClick={() => onChange([...lines, emptyLine()])}>
        <Plus className="size-3.5" /> Add line
      </Button>
    </div>
  );
}

/** Two-column grid for the header fields above the lines. */
export function FormGrid({ children }: { children: ReactNode }) {
  return <div className="grid gap-4 sm:grid-cols-2">{children}</div>;
}
