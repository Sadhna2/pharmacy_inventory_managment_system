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
import { cn, date, money, num, qty } from "@/lib/format";
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
    onError: (err) =>
      setError(err instanceof ApiError ? err.problem.detail : "Action failed"),
  });

  return { ...mutation, error, clearError: () => setError(null) };
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
    // Two short names. Left to share the spare width it opened a gap between
    // itself and Status wide enough that the two stopped reading as one row.
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
    {
      key: "date",
      header: "Ordered",
      hideBelow: "md",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{date(row.order_date)}</span>
      ),
    },
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
      // A badge is as wide as its word. Sharing the spare width put a corridor
      // of dead space between it and the column before it on every one of
      // these four tables.
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

        Gated on grn.create, the same permission the intake endpoint demands,
        so this never advertises a button that would be refused.
      */}
      {can("grn.create") && (
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
                  Photograph the invoice and the batch codes, expiries,
                  quantities and rates fill in the goods receipt — including
                  trade names no rule can reach, like OMEZ-20 for omeprazole.
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
              onClick={() => setGrnOpen(true)}
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

      <PurchaseOrderForm open={formOpen} onClose={() => setFormOpen(false)} />
      <GoodsReceiptForm open={grnOpen} onClose={() => setGrnOpen(false)} />

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

  async function allocate(id: number) {
    try {
      const result = await api.post<Allocation[]>(
        `/api/v1/sales-orders/${id}/allocate`,
      );
      setAllocations(result);
      action.reset();
    } catch (err) {
      // Surfaced through the shared banner.
      action.mutate({ path: `/api/v1/sales-orders/${id}/allocate` });
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
    {
      key: "date",
      header: "Ordered",
      hideBelow: "md",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{date(row.order_date)}</span>
      ),
    },
    {
      key: "status",
      header: "Status",
      card: "secondary",
      // A badge is as wide as its word. Sharing the spare width put a corridor
      // of dead space between it and the column before it on every one of
      // these four tables.
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

      <SalesOrderForm open={formOpen} onClose={() => setFormOpen(false)} />

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
              <SheetFact label="Ordered" value={date(acting.order_date)} />
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
              {/* A read, not a workflow step, so it stays available on a
                  shipped or completed order — a reprint is usually asked for
                  long after the goods have gone. Hidden on a DRAFT: nothing is
                  held for it yet and its lines can still change, so anything
                  printed now is a document that may not survive the morning.
                  No permission check because the list itself needs `so.view`,
                  which is all the endpoint asks for. */}
              {acting.status !== "DRAFT" && (
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
                  <Printer className="size-4" /> Print invoice
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
    raisedByColumn<Transfer>(),
    {
      key: "status",
      header: "Status",
      card: "secondary",
      // A badge is as wide as its word. Sharing the spare width put a corridor
      // of dead space between it and the column before it on every one of
      // these four tables.
      shrink: true,
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

      <TransferForm open={formOpen} onClose={() => setFormOpen(false)} />

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
              <SheetFact label="Raised by" value={acting.created_by_name ?? "—"} />
              {acting.approved_by_name && (
                <SheetFact label="Approved by" value={acting.approved_by_name} />
              )}
              <SheetFact label="Items" value={acting.lines.length} />
            </div>

            {/*
              A transfer moves in three steps, and only one is ever available.
              Presented as one button each rather than a row of three, so the
              next step is unambiguous.
            */}
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
    raisedByColumn<Adjustment>(),
    {
      key: "status",
      header: "Status",
      card: "secondary",
      // A badge is as wide as its word. Sharing the spare width put a corridor
      // of dead space between it and the column before it on every one of
      // these four tables.
      shrink: true,
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

      <AdjustmentForm open={formOpen} onClose={() => setFormOpen(false)} />

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
