import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  Ban,
  Check,
  PackageCheck,
  Plus,
  Send,
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

/* =========================================================== purchase orders */

export function PurchaseOrders() {
  const { can } = useAuth();
  const [page, setPage] = useState(1);
  const [formOpen, setFormOpen] = useState(false);
  const [grnOpen, setGrnOpen] = useState(false);
  const [cancelling, setCancelling] = useState<PurchaseOrder | null>(null);
  const action = useAction(["purchase-orders", "balances", "stock"]);

  const { data, isLoading } = useQuery({
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
      render: (row) => {
        const awaiting = row.status === "DRAFT" || row.status === "PENDING_APPROVAL";
        // The server refuses once goods have landed; mirror that so the button
        // never appears where it cannot work.
        const stoppable = row.status !== "RECEIVED" && row.status !== "CANCELLED";
        return (
          <div className="flex items-center justify-end gap-1.5">
            {can("po.create") && stoppable && (
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Cancel ${row.po_number}`}
                onClick={(e) => {
                  e.stopPropagation();
                  setCancelling(row);
                }}
              >
                <Ban className="size-3.5" />
                <span className="md:hidden">Cancel</span>
              </Button>
            )}
            {can("po.approve") && awaiting && (
              <Button
                size="sm"
                variant="primary"
                loading={action.isPending}
                onClick={(e) => {
                  e.stopPropagation();
                  action.mutate({
                    path: `/api/v1/purchase-orders/${row.id}/approve`,
                  });
                }}
              >
                <Check className="size-3.5" /> Approve
              </Button>
            )}
          </div>
        );
      },
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
      <Card>
        <ErrorBanner message={action.error} />
        <DataTable
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isLoading}
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
  const action = useAction(["sales-orders", "balances", "stock"]);

  const { data, isLoading } = useQuery({
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
      render: (row) => {
        // Cancelling releases any batches already held, so it stays available
        // right up until the goods leave the building.
        const stoppable =
          row.status !== "COMPLETED" &&
          row.status !== "CANCELLED" &&
          row.status !== "SHIPPED";
        return (
          <div className="flex items-center justify-end gap-1.5">
            {can("so.create") && stoppable && (
              <Button
                size="sm"
                variant="ghost"
                aria-label={`Cancel ${row.so_number}`}
                onClick={(e) => {
                  e.stopPropagation();
                  setCancelling(row);
                }}
              >
                <Ban className="size-3.5" />
                <span className="md:hidden">Cancel</span>
              </Button>
            )}
            {can("so.fulfil") && row.status === "DRAFT" && (
              <Button
                size="sm"
                onClick={(e) => {
                  e.stopPropagation();
                  allocate(row.id);
                }}
              >
                <PackageCheck className="size-3.5" /> Allocate
              </Button>
            )}
            {can("so.fulfil") && row.status === "ALLOCATED" && (
              <Button
                size="sm"
                variant="primary"
                loading={action.isPending}
                onClick={(e) => {
                  e.stopPropagation();
                  action.mutate({ path: `/api/v1/sales-orders/${row.id}/ship` });
                }}
              >
                <Send className="size-3.5" /> Ship
              </Button>
            )}
          </div>
        );
      },
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
          loading={isLoading}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No sales orders"
          emptyDescription="Orders from hospitals and clinics appear here."
        />
      </Card>

      <SalesOrderForm open={formOpen} onClose={() => setFormOpen(false)} />

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
  const action = useAction(["transfers", "balances", "stock"]);

  const { data, isLoading } = useQuery({
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
      render: (row) => {
        if (row.status === "DRAFT" && can("transfer.approve")) {
          return (
            <Button
              size="sm"
              onClick={() =>
                action.mutate({ path: `/api/v1/transfers/${row.id}/approve` })
              }
            >
              <Check className="size-3.5" /> Approve
            </Button>
          );
        }
        if (row.status === "APPROVED" && can("transfer.approve")) {
          return (
            <Button
              size="sm"
              variant="primary"
              onClick={() =>
                action.mutate({ path: `/api/v1/transfers/${row.id}/dispatch` })
              }
            >
              <Truck className="size-3.5" /> Dispatch
            </Button>
          );
        }
        if (row.status === "IN_TRANSIT" && can("stock.move")) {
          return (
            <Button
              size="sm"
              variant="primary"
              onClick={() =>
                action.mutate({ path: `/api/v1/transfers/${row.id}/receive` })
              }
            >
              <PackageCheck className="size-3.5" /> Receive
            </Button>
          );
        }
        return null;
      },
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
          loading={isLoading}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No transfers"
          emptyDescription="Stock moves between locations appear here."
        />
      </Card>

      <TransferForm open={formOpen} onClose={() => setFormOpen(false)} />
    </>
  );
}

/* ============================================================== adjustments */

export function Adjustments() {
  const { can } = useAuth();
  const [page, setPage] = useState(1);
  const [formOpen, setFormOpen] = useState(false);
  const action = useAction(["adjustments", "balances", "stock"]);

  const { data, isLoading } = useQuery({
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
      render: (row) =>
        row.status === "PENDING_APPROVAL" && can("adjustment.approve") ? (
          <Button
            size="sm"
            variant="primary"
            onClick={() =>
              action.mutate({ path: `/api/v1/adjustments/${row.id}/approve` })
            }
          >
            <Check className="size-3.5" /> Approve
          </Button>
        ) : null,
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
          loading={isLoading}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No adjustments"
          emptyDescription="Stock corrections awaiting approval appear here."
        />
      </Card>

      <AdjustmentForm open={formOpen} onClose={() => setFormOpen(false)} />
    </>
  );
}
