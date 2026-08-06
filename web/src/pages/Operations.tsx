import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Ban,
  Check,
  PackageCheck,
  Plus,
  MoreHorizontal,
  Printer,
  ScanLine,
  Send,
  ShieldCheck,
  Truck,
} from "lucide-react";
import { api, ApiError, qs } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { clock, cn, date, money, num, qty } from "@/lib/format";
import type {
  Adjustment,
  Allocation,
  Page,
  PurchaseOrder,
  SalesOrder,
  Transfer,
} from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { Badge, Button, Card, Modal, StatusBadge } from "@/components/ui";
import { ConfirmDialog } from "@/components/confirm";
import { PurchaseOrderForm } from "@/forms/PurchaseOrderForm";
import { GoodsReceiptForm } from "@/forms/GoodsReceiptForm";
import { SalesOrderForm } from "@/forms/SalesOrderForm";
import { TransferForm } from "@/forms/TransferForm";
import { AdjustmentForm } from "@/forms/AdjustmentForm";

/** Surfaces API problem details inline instead of a silent failure. */
function useAction(invalidate: string[]) {
  const qc = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const mutation = useMutation({
    mutationFn: ({ path, body }: { path: string; body?: unknown }) =>
      api.post(path, body),
    onSuccess: () => {
      setError(null);
      invalidate.forEach((key) => qc.invalidateQueries({ queryKey: [key] }));
    },
    onError: (err) => setError(problem(err)),
  });

  return {
    ...mutation,
    error,
    clearError: () => setError(null),
    /**
     * Show a failure that happened outside `mutate`.
     *
     * Some actions return a body the page has to keep — allocation hands back
     * the batches it picked — so they call the API directly rather than
     * through the mutation, and then have nowhere to put a refusal. Without
     * this the only way to reach the banner was to re-send the request purely
     * to make it fail again, which is not a thing to do to an endpoint that
     * reserves stock.
     */
    report: (err: unknown) => setError(problem(err)),
    /** Refresh the same lists a successful `mutate` would. */
    settled: () =>
      invalidate.forEach((key) => qc.invalidateQueries({ queryKey: [key] })),
  };
}

/** The server's own words if it gave any, a fallback if it did not. */
function problem(err: unknown): string {
  return err instanceof ApiError ? err.problem.detail : "Action failed";
}

function ErrorBanner({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div className="mx-4 mt-3 rounded-lg border border-danger/20 bg-danger-soft px-3 py-2 text-[13px] text-danger">
      {message}
    </div>
  );
}

/**
 * Who raised a document, and who signed it off.
 *
 * Purchase orders and adjustments both refuse to let one person do both, and
 * the approver is certifying someone else's work. Until now the API returned
 * `created_by: 2` and no screen showed even that, so the second signature was
 * being given without knowing whose work it covered. The names were always in
 * the database — this is the missing half of a control that already existed.
 */
function RaisedBy({
  createdBy,
  approvedBy,
}: {
  createdBy?: string | null;
  approvedBy?: string | null;
}) {
  if (!createdBy && !approvedBy) {
    return <span className="text-[13px] text-ink-faint">—</span>;
  }
  return (
    <div className="min-w-0 leading-tight">
      <p className="truncate text-[13px] text-ink-soft">{createdBy ?? "—"}</p>
      {approvedBy && (
        <p className="truncate text-[11px] text-ink-faint">
          {/* Named rather than a tick, because "approved" without a name is
              the same gap in a different shape. */}
          approved by {approvedBy}
        </p>
      )}
    </div>
  );
}

/** The same column in the three tables that have a maker and a checker. */
function raisedByColumn<T extends {
  created_by_name?: string | null;
  approved_by_name?: string | null;
}>(): Column<T> {
  return {
    key: "raised_by",
    header: "Raised by",
    hideBelow: "md",
    card: "secondary",
    // Two short names, and Purchasing does not pack its columns — left to
    // share the spare width there it opened a gap between itself and Status
    // wide enough that the two stopped reading as one row.
    shrink: true,
    render: (row) => (
      <RaisedBy
        createdBy={row.created_by_name}
        approvedBy={row.approved_by_name}
      />
    ),
  };
}

/**
 * A date with the clock time under it.
 *
 * Two lines rather than one string. Every operations list is ordered newest
 * first, and on a busy day that is a run of rows all reading "05 Aug 2026" —
 * the order was right and looked arbitrary, and there was no way to tell which
 * of two orders came first. Stacked, the date stays the thing you scan and the
 * time is there when you need it, without widening the column.
 *
 * `business` is the date the document bears — an order date can be back-dated
 * and is what the document is *about*. `stamp` is when the row was written.
 * When they differ the business date leads, because that is the one printed on
 * the paperwork.
 */
function Stamp({
  business,
  stamp,
}: {
  business?: string | null;
  stamp?: string | null;
}) {
  return (
    <div className="leading-tight whitespace-nowrap">
      <p className="text-[13px] text-ink-soft">{date(business ?? stamp)}</p>
      {stamp && <p className="text-[11px] text-ink-faint tnum">{clock(stamp)}</p>}
    </div>
  );
}

/** The date column shared by every operations list. */
function stampColumn<T>(
  header: string,
  pick: (row: T) => { business?: string | null; stamp?: string | null },
): Column<T> {
  return {
    key: "date",
    header,
    hideBelow: "md",
    // A fixed share rather than `shrink`. Under `even` the tables use
    // `table-fixed`, where shrink is ignored and every column takes an equal
    // slice — and an equal slice of a table this narrow clipped "05 Aug 2026"
    // to "05 Aug 2…". This is the one width a date needs and no more, which
    // also hands the surplus back to the columns holding names.
    width: "13%",
    render: (row) => <Stamp {...pick(row)} />,
  };
}

/**
 * One button per row, opening the actions for that document.
 *
 * Approve and Cancel sitting side by side in a table gave every row a
 * different silhouette — two buttons, one button, or none — which read as
 * arbitrary even though it is driven entirely by status. A single control
 * with a consistent shape is calmer, and the sheet it opens has room to say
 * *why* an action is unavailable instead of silently omitting it.
 */
function RowMenuButton({ label, onOpen }: { label: string; onOpen: () => void }) {
  return (
    <Button
      size="sm"
      variant="ghost"
      aria-label={label}
      onClick={(e) => {
        e.stopPropagation(); // the row itself is clickable
        onOpen();
      }}
    >
      <MoreHorizontal className="size-4" />
    </Button>
  );
}

/** A labelled fact inside the actions sheet. */
function SheetFact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-1.5">
      <span className="text-[12px] text-ink-faint">{label}</span>
      <span className="text-right text-[13px] text-ink">{value}</span>
    </div>
  );
}

/**
 * Why an approval is not on offer.
 *
 * Separation of duties is a rule people meet rather than read, so the moment
 * it bites is the moment to explain it. Omitting the button silently teaches
 * nothing and looks like a bug.
 */
function SelfApprovalNote() {
  return (
    <p className="rounded-lg border border-warn/20 bg-warn-soft px-3 py-2 text-[12px] text-ink-soft">
      You raised this, so you cannot also approve it. A second person has to
      sign it off.
    </p>
  );
}

/* =========================================================== purchase orders */

export function PurchaseOrders() {
  const { can, user } = useAuth();
  const [page, setPage] = useState(1);
  const [formOpen, setFormOpen] = useState(false);
  const [grnOpen, setGrnOpen] = useState(false);
  const [cancelling, setCancelling] = useState<PurchaseOrder | null>(null);
  const [acting, setActing] = useState<PurchaseOrder | null>(null);
  const action = useAction(["purchase-orders", "balances", "stock"]);

  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["purchase-orders", page],
    queryFn: () =>
      api.get<Page<PurchaseOrder>>(`/api/v1/purchase-orders${qs({ page, size: 25 })}`),
  });

  const columns: Column<PurchaseOrder>[] = [
    {
      key: "po",
      header: "Order",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-mono text-[13px] font-medium text-ink">
            {row.po_number}
          </p>
          <p className="truncate text-[11px] text-ink-faint">{row.supplier_name}</p>
        </div>
      ),
    },
    {
      key: "warehouse",
      header: "Deliver to",
      hideBelow: "lg",
      card: "secondary",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{row.warehouse_name}</span>
      ),
    },
    stampColumn<PurchaseOrder>("Ordered", (row) => ({
      business: row.order_date,
      stamp: row.created_at,
    })),
    {
      key: "gst",
      header: "GST",
      hideBelow: "xl",
      render: (row) => (
        <Badge tone={row.is_interstate ? "info" : "neutral"}>
          {row.is_interstate ? "IGST" : "CGST + SGST"}
        </Badge>
      ),
    },
    {
      key: "received",
      header: "Received",
      hideBelow: "md",
      render: (row) => {
        // "Partially received" on its own says nothing useful — the question is
        // always how much is still coming. The numbers were already in this
        // response, just never rendered.
        const ordered = (row.lines ?? []).reduce(
          (total, l) => total + Number(l.qty_ordered),
          0,
        );
        const received = (row.lines ?? []).reduce(
          (total, l) => total + Number(l.qty_received),
          0,
        );
        if (!ordered) return <span className="text-[13px] text-ink-faint">—</span>;
        const done = received >= ordered;
        return (
          <span
            className={cn(
              "text-[13px] tabular-nums",
              done ? "text-ink-soft" : "text-ink",
            )}
          >
            {qty(received)} <span className="text-ink-faint">of</span>{" "}
            {qty(ordered)}
          </span>
        );
      },
    },
    raisedByColumn<PurchaseOrder>(),
    {
      key: "status",
      header: "Status",
      card: "secondary",
      // A badge is as wide as its word, and this table shares its width out.
      shrink: true,
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "total",
      header: "Total",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="font-medium">{money(row.grand_total)}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      numeric: true,
      card: "actions",
      render: (row) => (
        <RowMenuButton
          label={`Actions for ${row.po_number}`}
          onOpen={() => setActing(row)}
        />
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Purchasing"
        description="Orders to distributors. Approval is separated from creation."
        actions={
          <>
            {can("grn.create") && (
              <Button onClick={() => setGrnOpen(true)}>
                <PackageCheck className="size-4" /> Receive goods
              </Button>
            )}
            {can("po.create") && (
              <Button variant="primary" onClick={() => setFormOpen(true)}>
                <Plus className="size-4" /> New order
              </Button>
            )}
          </>
        }
      />
      {/*
        The one place in the product where a language model does the work, said
        out loud in the section that owns it.

        It lived as a line of grey text inside the receive-goods dialog, which
        meant nobody met the feature until they had already decided to use it.
        The alternative — a page of its own explaining "our AI" — was worse: a
        tour that sits away from the work is a brochure, and the claim only
        carries weight standing next to the form it fills in. So it is stated
        here, one click from the thing it does.

        Gated on po.create rather than grn.create: scanning raises the order
        now, so this advertises the New order dialog and would be a button
        that got refused for anyone who cannot raise one.
      */}
      {can("po.create") && (
        <Card className="mb-3 border-brand/25 bg-brand-soft/40 p-4">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
            <div className="flex min-w-0 items-start gap-3">
              <ScanLine className="mt-0.5 size-5 shrink-0 text-brand" />
              <div className="min-w-0 space-y-1.5">
                <div className="flex flex-wrap items-center gap-2">
                  <h2 className="text-sm font-medium text-ink">
                    Read a distributor&rsquo;s invoice
                  </h2>
                  <Badge tone="brand">AI — vision + language</Badge>
                </div>
                <p className="text-[13px] leading-relaxed text-ink-soft">
                  Photograph the invoice and the products, quantities and rates
                  fill in the order — including trade names no rule can reach,
                  like OMEZ-20 for omeprazole. The file is kept against the
                  order and can be downloaded again when the goods arrive.
                </p>
                {/*
                  The part that matters more than the feature: what stops it
                  being believed on trust. An invoice is over-determined, so a
                  misreading is caught by arithmetic rather than by somebody
                  noticing.
                */}
                <p className="flex gap-1.5 text-xs leading-relaxed text-ink-soft">
                  <ShieldCheck className="mt-0.5 size-3.5 shrink-0 text-ok" />
                  <span>
                    The model never produces an answer, only something that can
                    be checked — every reading is tested against the
                    invoice&rsquo;s own arithmetic and its GSTIN checksum, and
                    nothing reaches the ledger until a person accepts it.
                  </span>
                </p>
              </div>
            </div>
            <Button
              variant="primary"
              className="shrink-0"
              onClick={() => setFormOpen(true)}
            >
              <ScanLine className="size-4" /> Scan an invoice
            </Button>
          </div>
        </Card>
      )}

      <Card>
        <ErrorBanner message={action.error} />
        <DataTable
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isPending}
          error={error}
          onRetry={refetch}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No purchase orders"
          emptyDescription="Orders raised against distributors appear here."
        />
      </Card>

      {formOpen && <PurchaseOrderForm open onClose={() => setFormOpen(false)} />}
      {grnOpen && <GoodsReceiptForm open onClose={() => setGrnOpen(false)} />}

      <Modal
        open={acting !== null}
        onClose={() => setActing(null)}
        title={acting?.po_number ?? "Order"}
        description={acting?.supplier_name ?? undefined}
      >
        {acting && (
          <div className="space-y-3">
            <div className="divide-y divide-line rounded-lg border border-line px-3">
              <SheetFact label="Status" value={<StatusBadge status={acting.status} />} />
              <SheetFact
                label="Ordered"
                value={`${date(acting.order_date)} · ${clock(acting.created_at)}`}
              />
              <SheetFact label="Raised by" value={acting.created_by_name ?? "—"} />
              {acting.approved_by_name && (
                <SheetFact label="Approved by" value={acting.approved_by_name} />
              )}
              <SheetFact label="Deliver to" value={acting.warehouse_name ?? "—"} />
              <SheetFact label="Total" value={money(acting.grand_total)} />
            </div>

            {/* Both of the server's refusals, stated rather than implied. */}
            {can("po.approve") &&
              (acting.status === "DRAFT" ||
                acting.status === "PENDING_APPROVAL") &&
              !!user &&
              acting.created_by === user.id && <SelfApprovalNote />}

            <div className="flex flex-col gap-2">
              {can("po.approve") &&
                (acting.status === "DRAFT" ||
                  acting.status === "PENDING_APPROVAL") &&
                (!user || acting.created_by !== user.id) && (
                  <Button
                    variant="primary"
                    loading={action.isPending}
                    onClick={() => {
                      action.mutate({
                        path: `/api/v1/purchase-orders/${acting.id}/approve`,
                      });
                      setActing(null);
                    }}
                  >
                    <Check className="size-4" /> Approve order
                  </Button>
                )}
              {/* The server refuses once goods have landed; mirror that so the
                  button never appears where it cannot work. */}
              {can("po.create") &&
                acting.status !== "RECEIVED" &&
                acting.status !== "CANCELLED" && (
                  <Button
                    onClick={() => {
                      // Hand straight to the existing confirm step — cancelling
                      // is destructive enough to deserve its own sentence.
                      const row = acting;
                      setActing(null);
                      setCancelling(row);
                    }}
                  >
                    <Ban className="size-4" /> Cancel order
                  </Button>
                )}
              {/* A received or cancelled order is finished; say so rather than
                  showing an empty sheet. */}
              {(acting.status === "RECEIVED" ||
                acting.status === "CANCELLED") && (
                <p className="text-center text-[13px] text-ink-faint">
                  Nothing further to do on this order.
                </p>
              )}
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={cancelling !== null}
        onClose={() => setCancelling(null)}
        danger
        title={`Cancel ${cancelling?.po_number ?? "order"}?`}
        description="The order stays in the list marked Cancelled. Nothing is deleted."
        confirmLabel="Cancel order"
        path={`/api/v1/purchase-orders/${cancelling?.id}/cancel`}
        invalidate={["purchase-orders"]}
      >
        {cancelling && (
          <div className="rounded-lg border border-line bg-muted/40 px-3 py-2.5 text-[13px]">
            <p className="text-ink-soft">
              {cancelling.supplier_name} · {money(cancelling.grand_total)}
            </p>
            <p className="mt-1 text-ink-soft">
              Raise a fresh order if the details need to change — an order already
              sent to a distributor is never edited in place.
            </p>
          </div>
        )}
      </ConfirmDialog>
    </>
  );
}

/* ============================================================== sales orders */

/**
 * Products whose allocation spans batches with different printed MRPs.
 *
 * FEFO can satisfy one order line from two batches, and MRP is a ceiling per
 * pack — so the older batch may legally be worth less than the newer one. The
 * order carries a single price per line, which means someone has to look. The
 * system flags it rather than silently picking one of the two prices.
 */
function splitPrices(allocations: Allocation[] | null): string[] {
  if (!allocations) return [];
  const seen = new Map<number, Set<string>>();
  for (const row of allocations) {
    if (!row.mrp) continue;
    const prices = seen.get(row.product_id) ?? new Set<string>();
    prices.add(String(Number(row.mrp)));
    seen.set(row.product_id, prices);
  }
  return allocations
    .filter(
      (row, index) =>
        (seen.get(row.product_id)?.size ?? 0) > 1 &&
        allocations.findIndex((other) => other.product_id === row.product_id) ===
          index,
    )
    .map((row) => row.product_name ?? row.sku ?? `Product ${row.product_id}`);
}

export function SalesOrders() {
  const { can } = useAuth();
  const [page, setPage] = useState(1);
  const [formOpen, setFormOpen] = useState(false);
  const [allocations, setAllocations] = useState<Allocation[] | null>(null);
  const [cancelling, setCancelling] = useState<SalesOrder | null>(null);
  const [acting, setActing] = useState<SalesOrder | null>(null);
  // Carries the order it belongs to, not just the words. As a bare string it
  // outlived the modal that raised it: refusing to print a cancelled order and
  // then opening a completed one showed the completed order the cancelled
  // one's refusal, naming a document the reader was no longer looking at.
  const [printError, setPrintError] = useState<{
    orderId: number;
    message: string;
  } | null>(null);
  const action = useAction(["sales-orders", "balances", "stock"]);

  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["sales-orders", page],
    queryFn: () =>
      api.get<Page<SalesOrder>>(`/api/v1/sales-orders${qs({ page, size: 25 })}`),
  });

  /**
   * Reserve batches against an order, and show which ones.
   *
   * Called directly rather than through `action.mutate` because the response
   * body is the point — the operator needs to see the batches FEFO picked.
   * That means the two things `mutate` would have done have to be done by
   * hand, and both were missing:
   *
   * `settled()`, because allocation moves the order from DRAFT to ALLOCATED
   * and reserves stock. Without it the row kept saying Draft for a document
   * that had already moved, and the only way to see the truth was to reload
   * the page.
   *
   * `report()`, because the old catch block re-sent the same request through
   * `mutate` just to get its error into the banner. On a refusal that is
   * merely wasteful; on a call that half-succeeded it is a second attempt to
   * reserve stock, issued by the error handler.
   */
  async function allocate(id: number) {
    try {
      const result = await api.post<Allocation[]>(
        `/api/v1/sales-orders/${id}/allocate`,
      );
      setAllocations(result);
      action.reset();
    } catch (err) {
      action.report(err);
    } finally {
      action.settled();
    }
  }

  const columns: Column<SalesOrder>[] = [
    {
      key: "so",
      header: "Order",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-mono text-[13px] font-medium text-ink">
            {row.so_number}
          </p>
          <p className="truncate text-[11px] text-ink-faint">{row.customer_name}</p>
        </div>
      ),
    },
    {
      key: "from",
      header: "Ship from",
      hideBelow: "lg",
      card: "secondary",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{row.warehouse_name}</span>
      ),
    },
    stampColumn<SalesOrder>("Ordered", (row) => ({
      business: row.order_date,
      stamp: row.created_at,
    })),
    {
      key: "status",
      header: "Status",
      card: "secondary",
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "total",
      header: "Total",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="font-medium">{money(row.grand_total)}</span>
      ),
    },
    {
      key: "actions",
      header: "",
      numeric: true,
      card: "actions",
      render: (row) => (
        <RowMenuButton
          label={`Actions for ${row.so_number}`}
          onOpen={() => setActing(row)}
        />
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Sales"
        description="Institutional orders. Allocation picks batches automatically by earliest expiry."
        actions={
          can("so.create") && (
            <Button variant="primary" onClick={() => setFormOpen(true)}>
              <Plus className="size-4" /> New order
            </Button>
          )
        }
      />
      <Card>
        <ErrorBanner message={action.error} />
        <DataTable
        even
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isPending}
          error={error}
          onRetry={refetch}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No sales orders"
          emptyDescription="Orders from hospitals and clinics appear here."
        />
      </Card>

      {formOpen && <SalesOrderForm open onClose={() => setFormOpen(false)} />}

      <Modal
        open={acting !== null}
        onClose={() => setActing(null)}
        title={acting?.so_number ?? "Order"}
        description={acting?.customer_name ?? undefined}
      >
        {acting && (
          <div className="space-y-3">
            <div className="divide-y divide-line rounded-lg border border-line px-3">
              <SheetFact label="Status" value={<StatusBadge status={acting.status} />} />
              <SheetFact label="Ship from" value={acting.warehouse_name ?? "—"} />
              <SheetFact
                label="Ordered"
                value={`${date(acting.order_date)} · ${clock(acting.created_at)}`}
              />
              <SheetFact label="Total" value={money(acting.grand_total)} />
            </div>

            <div className="flex flex-col gap-2">
              {can("so.fulfil") && acting.status === "DRAFT" && (
                <Button
                  variant="primary"
                  onClick={() => {
                    const id = acting.id;
                    setActing(null);
                    allocate(id);
                  }}
                >
                  <PackageCheck className="size-4" /> Allocate batches
                </Button>
              )}
              {can("so.fulfil") && acting.status === "ALLOCATED" && (
                <Button
                  variant="primary"
                  loading={action.isPending}
                  onClick={() => {
                    action.mutate({
                      path: `/api/v1/sales-orders/${acting.id}/ship`,
                    });
                    setActing(null);
                  }}
                >
                  <Send className="size-4" /> Ship
                </Button>
              )}
              {/* SHIPPED or COMPLETED, which is exactly what the route's
                  INVOICEABLE allows — the button used to appear on everything
                  except a DRAFT, so a cancelled or merely allocated order
                  offered a print that the server then refused. An action that
                  cannot succeed should not be on the screen.

                  It is also the right rule and not just the matching one: a
                  tax invoice is raised against a supply that happened. A
                  cancelled order is not a supply, and printing one would be
                  claiming a sale that never took place.

                  A read rather than a workflow step, so it stays available for
                  good once the goods have gone — a reprint is usually asked
                  for long afterwards. No permission check because the list
                  itself needs `so.view`, which is all the endpoint asks for. */}
              {(acting.status === "SHIPPED" ||
                acting.status === "COMPLETED") && (
                <Button
                  onClick={() => {
                    // The window is opened synchronously, inside the click, and
                    // filled in afterwards. Both halves matter: opening it
                    // after the `await` would be swallowed by the popup
                    // blocker, and pointing it straight at the API path would
                    // render a 401, because a top-level navigation carries no
                    // Authorization header.
                    const printed = window.open("", "_blank");
                    api
                      .getText(`/api/v1/sales-orders/${acting.id}/invoice`)
                      .then((html) => {
                        if (!printed) return;
                        printed.document.write(html);
                        printed.document.close();
                        // Open the print dialogue rather than leaving a tab of
                        // HTML for the reader to find the menu item in. "Save
                        // as PDF" is a destination in that dialogue on every
                        // desktop browser, so this is also how the invoice
                        // gets downloaded as a PDF — there is no server-side
                        // renderer to add, and a 2 GB box is not the place to
                        // put one. The tab stays open behind it, so cancelling
                        // the dialogue still leaves the invoice on screen.
                        //
                        // The document is inline: no webfonts, no images, no
                        // stylesheet to fetch, so there is nothing still
                        // loading by the time write() returns.
                        printed.focus();
                        printed.print();
                      })
                      .catch((err) => {
                        printed?.close();
                        setPrintError({
                          orderId: acting.id,
                          message:
                            err instanceof ApiError
                              ? err.problem.detail
                              : "The invoice could not be produced.",
                        });
                      });
                  }}
                >
                  <Printer className="size-4" /> Print or save as PDF
                </Button>
              )}
              {/* A blocked popup is silent, and so is a failed fetch — without
                  this the button would look like it did nothing at all, which
                  is the failure it was just fixed for. */}
              {printError?.orderId === acting.id && (
                <p className="text-center text-[12px] text-danger">
                  {printError.message}
                </p>
              )}
              {/* Cancelling releases any batches already held, so it stays
                  available right up until the goods leave the building. */}
              {can("so.create") &&
                acting.status !== "COMPLETED" &&
                acting.status !== "CANCELLED" &&
                acting.status !== "SHIPPED" && (
                  <Button
                    onClick={() => {
                      const row = acting;
                      setActing(null);
                      setCancelling(row);
                    }}
                  >
                    <Ban className="size-4" /> Cancel order
                  </Button>
                )}
              {(acting.status === "COMPLETED" ||
                acting.status === "CANCELLED" ||
                acting.status === "SHIPPED") && (
                <p className="text-center text-[13px] text-ink-faint">
                  Nothing further to do on this order.
                </p>
              )}
            </div>
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={cancelling !== null}
        onClose={() => setCancelling(null)}
        danger
        title={`Cancel ${cancelling?.so_number ?? "order"}?`}
        description="The order stays in the list marked Cancelled. Nothing is deleted."
        confirmLabel="Cancel order"
        path={`/api/v1/sales-orders/${cancelling?.id}/cancel`}
        invalidate={["sales-orders"]}
      >
        {cancelling && (
          <div className="rounded-lg border border-line bg-muted/40 px-3 py-2.5 text-[13px]">
            <p className="text-ink-soft">
              {cancelling.customer_name} · {money(cancelling.grand_total)}
            </p>
            <p className="mt-1 text-ink-soft">
              Any batches already held for this order are released back to
              available stock.
            </p>
          </div>
        )}
      </ConfirmDialog>

      {/* FEFO result — tells the picker exactly which batch to take. */}
      <Modal
        open={allocations !== null}
        onClose={() => setAllocations(null)}
        title="Batches allocated"
        description="Chosen automatically by earliest expiry (FEFO)"
        footer={
          <Button variant="primary" onClick={() => setAllocations(null)}>
            Done
          </Button>
        }
      >
        {splitPrices(allocations).length > 0 && (
          <p className="mb-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[12px] text-ink">
            <strong className="font-medium">Mixed printed prices.</strong>{" "}
            {splitPrices(allocations).join(", ")} came off batches carrying
            different MRPs. The order bills one price per line — check the
            invoice before it goes out.
          </p>
        )}
        <ul className="divide-y divide-line">
          {allocations?.map((row, index) => (
            <li key={index} className="flex items-center gap-3 py-2.5">
              <div className="min-w-0 flex-1">
                <p className="truncate text-[13px] font-medium text-ink">
                  {row.product_name}
                </p>
                <p className="truncate text-[11px] text-ink-faint">
                  Batch <span className="font-mono">{row.lot_code ?? "—"}</span>
                  {row.expiry_date && ` · expires ${date(row.expiry_date)}`}
                  {row.mrp && ` · MRP ${money(row.mrp)}`}
                </p>
              </div>
              <span className="shrink-0 text-sm font-medium tnum">
                {qty(row.quantity)}
              </span>
            </li>
          ))}
        </ul>
      </Modal>
    </>
  );
}

/* ================================================================ transfers */

export function Transfers() {
  const { can } = useAuth();
  const [page, setPage] = useState(1);
  const [formOpen, setFormOpen] = useState(false);
  const [acting, setActing] = useState<Transfer | null>(null);
  const [cancelling, setCancelling] = useState<Transfer | null>(null);
  const action = useAction(["transfers", "balances", "stock"]);

  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["transfers", page],
    queryFn: () =>
      api.get<Page<Transfer>>(`/api/v1/transfers${qs({ page, size: 25 })}`),
  });

  const columns: Column<Transfer>[] = [
    {
      key: "ref",
      header: "Transfer",
      card: "primary",
      render: (row) => (
        <span className="font-mono text-[13px] font-medium text-ink">
          {row.transfer_number}
        </span>
      ),
    },
    {
      key: "route",
      header: "Route",
      // Two branch names and an arrow. "Central Warehouse - Mumbai → Pune
      // Branch (Commercial)" is the longest pair the seed can produce and it
      // is the whole content of a transfer row, so it gets the width it needs
      // rather than an equal share.
      width: "36%",
      card: "secondary",
      render: (row) => (
        <div className="flex min-w-0 items-center gap-1.5 text-[13px] text-ink-soft">
          <span className="truncate">{row.from_warehouse_name}</span>
          <ArrowRight className="size-3 shrink-0 text-ink-faint" />
          <span className="truncate">{row.to_warehouse_name}</span>
        </div>
      ),
    },
    {
      key: "lines",
      header: "Items",
      numeric: true,
      hideBelow: "lg",
      card: "meta",
      render: (row) => <span>{row.lines.length}</span>,
    },
    // The list is newest first, and a column of transfer numbers is a poor way
    // to show that — they ascend with the id, so the order was correct and
    // read as arbitrary. A transfer bears no business date of its own, so the
    // clock is all there is.
    stampColumn<Transfer>("Raised", (row) => ({ stamp: row.created_at })),
    raisedByColumn<Transfer>(),
    {
      key: "status",
      header: "Status",
      card: "secondary",
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "actions",
      header: "",
      numeric: true,
      card: "actions",
      render: (row) => (
        <RowMenuButton
          label={`Actions for ${row.transfer_number}`}
          onOpen={() => setActing(row)}
        />
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Transfers"
        description="Central warehouse to branches. Stock stays visible as in-transit while on the road."
        actions={
          can("transfer.create") && (
            <Button variant="primary" onClick={() => setFormOpen(true)}>
              <Plus className="size-4" /> New transfer
            </Button>
          )
        }
      />
      <Card>
        <ErrorBanner message={action.error} />
        <DataTable
        even
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isPending}
          error={error}
          onRetry={refetch}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No transfers"
          emptyDescription="Stock moves between locations appear here."
        />
      </Card>

      {formOpen && <TransferForm open onClose={() => setFormOpen(false)} />}

      <Modal
        open={acting !== null}
        onClose={() => setActing(null)}
        title={acting?.transfer_number ?? "Transfer"}
        description={
          acting
            ? `${acting.from_warehouse_name} → ${acting.to_warehouse_name}`
            : undefined
        }
      >
        {acting && (
          <div className="space-y-3">
            <div className="divide-y divide-line rounded-lg border border-line px-3">
              <SheetFact label="Status" value={<StatusBadge status={acting.status} />} />
              <SheetFact
                label="Raised"
                value={`${date(acting.created_at)} · ${clock(acting.created_at)}`}
              />
              <SheetFact label="Raised by" value={acting.created_by_name ?? "—"} />
              {acting.approved_by_name && (
                <SheetFact label="Approved by" value={acting.approved_by_name} />
              )}
            </div>

            {/*
              What is actually being moved. The sheet used to say "Items: 3"
              and stop there, which is the one thing a person opening a
              transfer already knows they want spelled out — the API has
              carried the product names all along.
            */}
            <ul className="divide-y divide-line rounded-lg border border-line px-3">
              {acting.lines.map((line) => {
                // Batches with a code. An untracked product ships without one,
                // and printing "batch —" against a box of syringes invents a
                // gap where there is none.
                const sent = line.batches.filter((b) => b.lot_code);
                return (
                  <li key={line.id} className="py-2.5">
                    <div className="flex items-center gap-3">
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-[13px] font-medium text-ink">
                          {line.product_name ?? `Product ${line.product_id}`}
                        </p>
                        <p className="truncate text-[11px] text-ink-faint">
                          <span className="font-mono">{line.sku ?? "—"}</span>
                          {/* Before dispatch there is genuinely no answer —
                              FEFO chooses when the stock moves. Saying so
                              beats a blank, which reads as a batch nobody
                              bothered to record. */}
                          {!sent.length && acting.status === "DRAFT" && (
                            <> · batch chosen at dispatch</>
                          )}
                        </p>
                      </div>
                      <span className="shrink-0 text-sm font-medium tnum">
                        {/* Once it has landed, what arrived is the fact worth
                            showing; a short receipt is exactly what someone
                            opens this sheet to find. */}
                        {acting.status === "COMPLETED" &&
                        num(line.qty_received) !== num(line.quantity) ? (
                          <span className="text-warn">
                            {qty(line.qty_received)} of {qty(line.quantity)}
                          </span>
                        ) : (
                          qty(line.quantity)
                        )}
                      </span>
                    </div>

                    {/* One row per batch that actually left. Usually one; two
                        when the oldest lot did not cover the line, which is
                        ordinary FEFO and worth being able to see — a recall
                        traces by batch, so "which ones went to Pune" is a
                        question this sheet should be able to answer. */}
                    {sent.length > 0 && (
                      <ul className="mt-1 space-y-0.5">
                        {sent.map((batch, index) => (
                          <li
                            key={batch.lot_id ?? index}
                            className="flex items-baseline gap-2 text-[11px] text-ink-faint"
                          >
                            <span className="font-mono">{batch.lot_code}</span>
                            {batch.expiry_date && (
                              <span>exp {date(batch.expiry_date)}</span>
                            )}
                            <span className="flex-1 text-right tnum">
                              {qty(batch.quantity)}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </li>
                );
              })}
            </ul>

            {/*
              A transfer moves in three steps, and only one is ever available.
              Presented as one button each rather than a row of three, so the
              next step is unambiguous.
            */}
            {/* Centred, and on one line. Left-aligned they sat against the
                edge of a sheet whose every other element is a full-width row,
                which read as unfinished rather than deliberate. */}
            <div className="flex flex-wrap items-center justify-center gap-2">
              {/* DRAFT only, as the server has it. A transfer never takes
                  PENDING_APPROVAL — DRAFT, APPROVED, IN_TRANSIT, COMPLETED and
                  CANCELLED are the whole set — so widening this changed the
                  logic to cover a state that cannot happen. */}
              {acting.status === "DRAFT" && can("transfer.approve") && (
                  <Button
                    variant="primary"
                    loading={action.isPending}
                    onClick={() => {
                      action.mutate({
                        path: `/api/v1/transfers/${acting.id}/approve`,
                      });
                      setActing(null);
                    }}
                  >
                    <Check className="size-4" /> Approve transfer
                  </Button>
                )}
              {acting.status === "APPROVED" && can("transfer.approve") && (
                <Button
                  variant="primary"
                  loading={action.isPending}
                  onClick={() => {
                    action.mutate({ path: `/api/v1/transfers/${acting.id}/dispatch` });
                    setActing(null);
                  }}
                >
                  <Truck className="size-4" /> Dispatch
                </Button>
              )}
              {acting.status === "IN_TRANSIT" && can("stock.move") && (
                <Button
                  variant="primary"
                  loading={action.isPending}
                  onClick={() => {
                    action.mutate({ path: `/api/v1/transfers/${acting.id}/receive` });
                    setActing(null);
                  }}
                >
                  <PackageCheck className="size-4" /> Receive at destination
                </Button>
              )}
              {/* Before it ships, and only before: once the stock is on a road
                  the transfer has to land somewhere, and the API refuses. */}
              {can("transfer.create") &&
                (acting.status === "DRAFT" ||
                  acting.status === "PENDING_APPROVAL" ||
                  acting.status === "APPROVED") && (
                  <Button
                    onClick={() => {
                      const row = acting;
                      setActing(null);
                      setCancelling(row);
                    }}
                  >
                    <Ban className="size-4" /> Cancel transfer
                  </Button>
                )}
            </div>

            {/* COMPLETED, not RECEIVED — `receive_transfer` sets COMPLETED, so
                this line was guarded on a status transfers never reach and a
                finished transfer opened a sheet with nothing in it at all. */}
            {(acting.status === "COMPLETED" ||
              acting.status === "CANCELLED") && (
              <p className="text-center text-[13px] text-ink-faint">
                Nothing further to do on this transfer.
              </p>
            )}
          </div>
        )}
      </Modal>

      <ConfirmDialog
        open={cancelling !== null}
        onClose={() => setCancelling(null)}
        danger
        title={`Cancel ${cancelling?.transfer_number ?? "transfer"}?`}
        description="It stays in the list marked Cancelled. No stock moves, and nothing is deleted."
        confirmLabel="Cancel transfer"
        path={`/api/v1/transfers/${cancelling?.id}/cancel`}
        invalidate={["transfers"]}
      >
        {cancelling && (
          <div className="rounded-lg border border-line bg-muted/40 px-3 py-2.5 text-[13px]">
            <p className="text-ink-soft">
              {cancelling.from_warehouse_name} → {cancelling.to_warehouse_name}
            </p>
            <p className="mt-1 text-ink-soft">
              Nothing has left the source yet, so nothing is released. Raise a
              fresh transfer if the quantities need to change.
            </p>
          </div>
        )}
      </ConfirmDialog>
    </>
  );
}

/* ============================================================== adjustments */

export function Adjustments() {
  const { can, user } = useAuth();
  const [page, setPage] = useState(1);
  const [formOpen, setFormOpen] = useState(false);
  const [acting, setActing] = useState<Adjustment | null>(null);
  const [cancelling, setCancelling] = useState<Adjustment | null>(null);
  const action = useAction(["adjustments", "balances", "stock"]);

  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["adjustments", page],
    queryFn: () =>
      api.get<Page<Adjustment>>(`/api/v1/adjustments${qs({ page, size: 25 })}`),
  });

  const columns: Column<Adjustment>[] = [
    {
      key: "ref",
      header: "Adjustment",
      card: "primary",
      // The document number is the one thing on the row that identifies it, so
      // it is the last thing that should be clipped. An equal share stopped
      // being enough once this table gained a date column.
      width: "20%",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-mono text-[13px] font-medium text-ink">
            {row.adjustment_number}
          </p>
          <p className="truncate text-[11px] text-ink-faint">{row.reason_code}</p>
        </div>
      ),
    },
    {
      key: "lines",
      header: "Lines",
      numeric: true,
      hideBelow: "md",
      card: "meta",
      render: (row) => <span>{row.lines.length}</span>,
    },
    {
      key: "net",
      header: "Net qty",
      numeric: true,
      hideBelow: "lg",
      render: (row) => {
        const net = row.lines.reduce((sum, l) => sum + num(l.quantity), 0);
        return (
          <span className={net >= 0 ? "text-ok" : "text-danger"}>
            {net > 0 ? "+" : ""}
            {qty(net)}
          </span>
        );
      },
    },
    // This screen carried no date at all — a stock correction with no way to
    // say when it was made, which is the first thing anyone auditing one asks.
    stampColumn<Adjustment>("Raised", (row) => ({ stamp: row.created_at })),
    raisedByColumn<Adjustment>(),
    {
      key: "status",
      header: "Status",
      card: "secondary",
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "actions",
      header: "",
      numeric: true,
      card: "actions",
      render: (row) => (
        <RowMenuButton
          label={`Actions for ${row.adjustment_number}`}
          onOpen={() => setActing(row)}
        />
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Adjustments"
        description="Corrections post to the ledger only once a second person approves them."
        actions={
          can("stock.adjust") && (
            <Button variant="primary" onClick={() => setFormOpen(true)}>
              <Plus className="size-4" /> New adjustment
            </Button>
          )
        }
      />
      <Card>
        <ErrorBanner message={action.error} />
        <DataTable
        even
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isPending}
          error={error}
          onRetry={refetch}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No adjustments"
          emptyDescription="Stock corrections awaiting approval appear here."
        />
      </Card>

      {formOpen && <AdjustmentForm open onClose={() => setFormOpen(false)} />}

      <ConfirmDialog
        open={cancelling !== null}
        onClose={() => setCancelling(null)}
        danger
        title={`Cancel ${cancelling?.adjustment_number ?? "adjustment"}?`}
        description="It stays in the list marked Cancelled. No stock moves, and nothing is deleted."
        confirmLabel="Cancel adjustment"
        path={`/api/v1/adjustments/${cancelling?.id}/cancel`}
        invalidate={["adjustments"]}
      >
        {cancelling && (
          <div className="rounded-lg border border-line bg-muted/40 px-3 py-2.5 text-[13px]">
            <p className="text-ink-soft">
              {cancelling.reason_code} · {cancelling.lines.length}{" "}
              {cancelling.lines.length === 1 ? "line" : "lines"}
            </p>
            <p className="mt-1 text-ink-soft">
              Raise a fresh adjustment if the figures need to change. A
              correction that has already posted is undone by a reversing
              entry, never by editing this one.
            </p>
          </div>
        )}
      </ConfirmDialog>

      <Modal
        open={acting !== null}
        onClose={() => setActing(null)}
        title={acting?.adjustment_number ?? "Adjustment"}
        description={acting?.reason_code ?? undefined}
      >
        {acting && (
          <div className="space-y-3">
            <div className="divide-y divide-line rounded-lg border border-line px-3">
              <SheetFact label="Status" value={<StatusBadge status={acting.status} />} />
              <SheetFact
                label="Raised"
                value={`${date(acting.created_at)} · ${clock(acting.created_at)}`}
              />
              <SheetFact label="Raised by" value={acting.created_by_name ?? "—"} />
              {acting.approved_by_name && (
                <SheetFact label="Approved by" value={acting.approved_by_name} />
              )}
            </div>

            {can("adjustment.approve") &&
              acting.status === "PENDING_APPROVAL" &&
              !!user &&
              acting.created_by === user.id && <SelfApprovalNote />}

            <div className="flex flex-col gap-2">
              {can("adjustment.approve") &&
                acting.status === "PENDING_APPROVAL" &&
                (!user || acting.created_by !== user.id) && (
                  <Button
                    variant="primary"
                    loading={action.isPending}
                    onClick={() => {
                      action.mutate({
                        path: `/api/v1/adjustments/${acting.id}/approve`,
                      });
                      setActing(null);
                    }}
                  >
                    <Check className="size-4" /> Approve adjustment
                  </Button>
                )}
              {/* The way out of the queue. Some adjustments cannot be approved
                  however long they sit there — the stock they would take out
                  has since been sold, or the approver simply disagrees — and
                  without this they stayed at the top of the list forever. */}
              {can("stock.adjust") && acting.status === "PENDING_APPROVAL" && (
                <Button
                  onClick={() => {
                    const row = acting;
                    setActing(null);
                    setCancelling(row);
                  }}
                >
                  <Ban className="size-4" /> Cancel adjustment
                </Button>
              )}
              {acting.status !== "PENDING_APPROVAL" && (
                <p className="text-center text-[13px] text-ink-faint">
                  Nothing further to do on this adjustment.
                </p>
              )}
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}
