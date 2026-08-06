/**
 * Replenishment.
 *
 * The screen where the forecast and the lead-time analysis turn into money.
 * Each row says what to buy, how much, and — in a sentence — why, with the
 * whole calculation available behind it.
 *
 * "Raise draft order" is the only action, and it produces a DRAFT that still
 * needs approving by someone else. Nothing here places an order, which is a
 * deliberate limit and not a missing feature: an automatic ordering loop is
 * fine for a warehouse of screws and wrong for a pharmacy.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowRight,
  CircleAlert,
  FileText,
  IndianRupee,
  PackageCheck,
  ShoppingCart,
  TriangleAlert,
} from "lucide-react";
import { api, ApiError, qs } from "@/lib/api";
import { date, money, qty } from "@/lib/format";
import type { DraftOrder, Recommendation, ReorderReport, Warehouse } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { useAuth } from "@/lib/auth";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  ErrorState,
  Input,
  Modal,
  Select,
} from "@/components/ui";

type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "brand";

const URGENCY: Record<
  Recommendation["urgency"],
  { label: string; tone: Tone; blurb: string }
> = {
  stockout: { label: "Out of stock", tone: "danger", blurb: "Shelf is empty now" },
  critical: {
    label: "Order today",
    tone: "danger",
    blurb: "Cover runs out before a delivery could arrive",
  },
  soon: { label: "Order soon", tone: "warn", blurb: "At or near the reorder point" },
  ok: { label: "Comfortable", tone: "ok", blurb: "Above the reorder point" },
};

const SERVICE_LABEL: Record<Recommendation["service_level"], string> = {
  critical: "Critical — 99% service level",
  high: "High — 97% service level",
  standard: "Standard — 95% service level",
};

const CONFIDENCE_TONE: Record<string, Tone> = {
  high: "ok",
  medium: "warn",
  low: "danger",
};

/** Keys in `workings` renamed to something a buyer reads without a glossary. */
const WORKING_LABELS: Record<string, string> = {
  daily_demand: "Forecast demand per day",
  lead_time_days: "Lead time (median)",
  demand_over_lead_time: "Demand while waiting for delivery",
  service_level: "Service level",
  z: "Safety factor (z)",
  sigma_demand: "Demand variability (σ)",
  sigma_lead_time: "Lead-time variability (σ)",
  safety_stock: "Safety stock",
  reorder_point: "Reorder point",
  order_up_to: "Order up to",
  position: "In stock + on order",
  on_draft_orders: "Already on draft orders",
  review_period_days: "Review period",
  rounded_to_pack: "Pack size",
  minimum_order: "Minimum order",
};

/* ------------------------------------------------------------------ detail */

function Workings({ rec }: { rec: Recommendation }) {
  const rows = Object.entries(rec.workings).filter(
    ([, value]) => value !== null && value !== undefined,
  );
  return (
    <div className="divide-y divide-line rounded-lg border border-line">
      {rows.map(([key, value]) => (
        <div key={key} className="flex items-baseline justify-between gap-3 px-3 py-2">
          <span className="text-[13px] text-ink-soft">
            {WORKING_LABELS[key] ?? key.replace(/_/g, " ")}
          </span>
          <span className="tnum text-[13px] text-ink">
            {typeof value === "number" ? qty(value) : String(value)}
          </span>
        </div>
      ))}
    </div>
  );
}

function Detail({
  rec,
  onClose,
  onOrder,
}: {
  rec: Recommendation;
  onClose: () => void;
  onOrder: (rec: Recommendation, quantity: number) => void;
}) {
  const [quantity, setQuantity] = useState(String(rec.suggested_qty));
  const { user } = useAuth();
  const canOrder = user?.permissions.includes("ai.act") ?? false;
  const parsed = Number(quantity);
  const valid = Number.isFinite(parsed) && parsed > 0;

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={rec.product_name}
      description={`${rec.sku} · ${rec.warehouse_name}`}
      footer={
        canOrder && rec.sourcing.supplier_id !== null ? (
          <>
            <Button variant="ghost" onClick={onClose}>
              Close
            </Button>
            <Button
              variant="primary"
              disabled={!valid}
              onClick={() => onOrder(rec, parsed)}
            >
              <ShoppingCart className="size-4" />
              Raise draft order
            </Button>
          </>
        ) : (
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
        )
      }
    >
      <div className="space-y-5">
        <p className="text-sm leading-relaxed text-ink">{rec.reason}</p>

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {[
            { label: "In stock", value: qty(rec.on_hand) },
            { label: "On order", value: qty(rec.on_order) },
            { label: "Reorder point", value: qty(rec.reorder_point) },
            {
              label: "Runs out",
              value: rec.stockout_date ? date(rec.stockout_date) : "—",
            },
          ].map((cell) => (
            <div key={cell.label}>
              <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                {cell.label}
              </p>
              <p className="tnum mt-0.5 text-[15px] font-semibold text-ink">
                {cell.value}
              </p>
            </div>
          ))}
        </div>

        <div className="rounded-lg border border-line bg-muted/40 px-4 py-3">
          <div className="flex flex-wrap items-center gap-x-6 gap-y-2 text-[13px]">
            <span className="text-ink-soft">
              Supplier{" "}
              <span className="font-medium text-ink">{rec.sourcing.supplier_name}</span>
            </span>
            <span className="text-ink-soft">
              Lead time{" "}
              <span className="tnum font-medium text-ink">
                {rec.sourcing.lead_time_days}d
              </span>
              <span className="text-ink-faint">
                {" "}
                (worst {rec.sourcing.p90_days}d)
              </span>
            </span>
            <Badge tone={rec.sourcing.measured ? "ok" : "neutral"}>
              {rec.sourcing.measured ? "Measured" : "Quoted, not measured"}
            </Badge>
            {rec.sourcing.via_central && <Badge tone="info">Routes via central</Badge>}
          </div>
        </div>

        {/*
          The forecast and the safety factor are the two judgement calls in the
          whole calculation, so they get stated rather than buried in the table.
        */}
        <div className="flex flex-wrap gap-2">
          <Badge tone={CONFIDENCE_TONE[rec.forecast_confidence] ?? "neutral"}>
            Forecast confidence: {rec.forecast_confidence}
          </Badge>
          <Badge tone="neutral">Method: {rec.forecast_method.replace(/_/g, " ")}</Badge>
          <Badge tone="neutral">{SERVICE_LABEL[rec.service_level]}</Badge>
        </div>

        <div>
          <h4 className="mb-2 text-[13px] font-semibold text-ink">
            How the quantity was worked out
          </h4>
          <Workings rec={rec} />
        </div>

        {rec.draft_po_numbers.length > 0 && (
          <div className="rounded-lg border border-info/20 bg-info-soft px-4 py-3">
            <p className="text-[13px] text-info">
              {qty(rec.drafted_qty)} units are already on{" "}
              {rec.draft_po_numbers.join(", ")}, waiting for approval. That is not
              counted as stock — a draft is paper — but it is why nothing more is
              suggested here.
            </p>
          </div>
        )}

        {canOrder && rec.sourcing.supplier_id !== null && (
          <div className="border-t border-line pt-4">
            <label className="block">
              <span className="mb-1.5 block text-[13px] font-medium text-ink">
                Quantity to order
              </span>
              <div className="flex items-center gap-3">
                <Input
                  type="number"
                  min={1}
                  step={rec.sourcing.pack_qty || 1}
                  value={quantity}
                  onChange={(e) => setQuantity(e.target.value)}
                  className="sm:w-40"
                />
                <span className="text-[13px] text-ink-soft">
                  ≈ {money(parsed * rec.sourcing.unit_cost)} at{" "}
                  {money(rec.sourcing.unit_cost)}/unit
                </span>
              </div>
              <span className="mt-1 block text-xs text-ink-faint">
                Packs of {qty(rec.sourcing.pack_qty)}, minimum{" "}
                {qty(rec.sourcing.moq)}. Creates a DRAFT — someone else still has to
                approve it.
              </span>
            </label>
          </div>
        )}
      </div>
    </Modal>
  );
}

/* --------------------------------------------------------------- basket */

function DraftOrders({
  orders,
  onRaise,
  pending,
}: {
  orders: DraftOrder[];
  onRaise: (order: DraftOrder) => void;
  pending: boolean;
}) {
  const { user } = useAuth();
  const canOrder = user?.permissions.includes("ai.act") ?? false;
  if (orders.length === 0) return null;

  return (
    <Card>
      <CardHeader
        title="Orders this would become"
        description="Grouped by supplier and destination — one order per distributor per branch, rather than six raised the same morning."
      />
      <div className="divide-y divide-line">
        {orders.map((order) => (
          <div
            key={`${order.supplier_id}:${order.warehouse_id}`}
            className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-5"
          >
            <div className="min-w-0">
              <p className="flex flex-wrap items-center gap-1.5 text-[13px] font-medium text-ink">
                {order.supplier_name}
                <ArrowRight className="size-3.5 text-ink-faint" />
                {order.warehouse_name}
              </p>
              <p className="mt-0.5 text-[12px] text-ink-faint">
                {order.lines} line{order.lines > 1 && "s"} · {qty(order.units)} units
              </p>
            </div>
            <div className="flex items-center gap-4">
              <span className="tnum text-[13px] font-semibold text-ink">
                {money(order.estimated_cost)}
              </span>
              {canOrder && (
                <Button size="sm" loading={pending} onClick={() => onRaise(order)}>
                  <FileText className="size-3.5" />
                  Raise draft
                </Button>
              )}
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

/* -------------------------------------------------------------------- page */

export function Reorder() {
  const [warehouse, setWarehouse] = useState("");
  const [includeOk, setIncludeOk] = useState(false);
  const [selected, setSelected] = useState<Recommendation | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
  });

  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["reorder", { warehouse, includeOk }],
    queryFn: () =>
      api.get<ReorderReport>(
        `/api/v1/ai/reorder${qs({
          warehouse_id: warehouse,
          include_ok: includeOk || undefined,
        })}`,
      ),
  });

  const raise = useMutation({
    mutationFn: (body: {
      supplier_id: number;
      warehouse_id: number;
      lines: { product_id: number; quantity: number }[];
    }) => api.post<{ po_number: string }>("/api/v1/ai/reorder/orders", body),
    onSuccess: (po) => {
      setFailure(null);
      setNotice(`${po.po_number} created as a draft. It still needs approving.`);
      setSelected(null);
      queryClient.invalidateQueries({ queryKey: ["reorder"] });
      queryClient.invalidateQueries({ queryKey: ["purchase-orders"] });
    },
    onError: (err) => {
      setNotice(null);
      setFailure(err instanceof ApiError ? err.message : "Could not raise the order");
    },
  });

  const summary = data?.summary;
  const rows = data?.recommendations ?? [];

  // Not memoised: `rows` is a fresh array on every render, so a useMemo here
  // would recompute anyway and only add a dependency to get wrong.
  const urgentValue = rows
    .filter((r) => r.urgency === "stockout" || r.urgency === "critical")
    .reduce((sum, r) => sum + r.estimated_cost, 0);

  const columns: Column<Recommendation>[] = [
    {
      key: "product",
      header: "Product",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink">
            {row.product_name}
          </p>
          <p className="truncate text-[11px] text-ink-faint">
            {row.sku} · {row.warehouse_name}
          </p>
        </div>
      ),
    },
    {
      key: "urgency",
      header: "Status",
      card: "secondary",
      render: (row) => (
        <Badge tone={URGENCY[row.urgency].tone}>{URGENCY[row.urgency].label}</Badge>
      ),
    },
    {
      key: "cover",
      header: "Cover",
      numeric: true,
      card: "secondary",
      render: (row) => (
        <span className={row.days_of_cover < row.lead_time_days ? "text-danger" : ""}>
          {row.days_of_cover}d
        </span>
      ),
    },
    {
      key: "stock",
      header: "In stock",
      numeric: true,
      hideBelow: "md",
      render: (row) => (
        <span className="text-ink-soft">
          {qty(row.on_hand)}
          {row.on_order > 0 && (
            <span className="text-ink-faint"> +{qty(row.on_order)}</span>
          )}
        </span>
      ),
    },
    {
      key: "rop",
      header: "Reorder at",
      numeric: true,
      hideBelow: "lg",
      render: (row) => <span className="text-ink-soft">{qty(row.reorder_point)}</span>,
    },
    {
      key: "suggested",
      header: "Order",
      numeric: true,
      card: "secondary",
      render: (row) =>
        row.suggested_qty > 0 ? (
          <span className="font-medium">{qty(row.suggested_qty)}</span>
        ) : (
          <span className="text-ink-faint">
            {row.drafted_qty > 0 ? "drafted" : "—"}
          </span>
        ),
    },
    {
      key: "cost",
      header: "Est. cost",
      numeric: true,
      hideBelow: "sm",
      render: (row) =>
        row.estimated_cost > 0 ? (
          money(row.estimated_cost)
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Replenishment"
        description="What to buy, how much, and why — from the demand forecast and how long each distributor actually takes."
        actions={
          <div className="flex items-center gap-2">
            <Select
              value={warehouse}
              onChange={(e) => setWarehouse(e.target.value)}
              className="sm:w-auto"
              aria-label="Location"
            >
              <option value="">All locations</option>
              {(warehouses.data ?? []).map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
            <Button
              variant={includeOk ? "primary" : "secondary"}
              size="sm"
              onClick={() => setIncludeOk((v) => !v)}
            >
              <PackageCheck className="size-3.5" />
              {includeOk ? "Showing all" : "Show healthy too"}
            </Button>
          </div>
        }
      />

      {notice && (
        <div className="mb-4 rounded-lg border border-ok/20 bg-ok-soft px-4 py-3">
          <p className="text-[13px] font-medium text-ok">{notice}</p>
        </div>
      )}
      {failure && (
        <div className="mb-4 rounded-lg border border-danger/20 bg-danger-soft px-4 py-3">
          <p className="text-[13px] font-medium text-danger">{failure}</p>
        </div>
      )}

      {error ? (
        <ErrorState error={error} />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-danger-soft p-2">
                <TriangleAlert className="size-4 text-danger" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Needs ordering today
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {(summary?.stockout ?? 0) + (summary?.critical ?? 0)}
                </p>
                <p className="text-[11px] text-ink-faint">
                  {summary?.stockout ?? 0} already out · {money(urgentValue)} to fix
                </p>
              </div>
            </Card>
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-warn-soft p-2">
                <CircleAlert className="size-4 text-warn" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Approaching reorder point
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {summary?.soon ?? 0}
                </p>
                <p className="text-[11px] text-ink-faint">
                  Order within the next review cycle
                </p>
              </div>
            </Card>
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-muted p-2">
                <IndianRupee className="size-4 text-ink-faint" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Total suggested spend
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {money(summary?.estimated_cost ?? 0)}
                </p>
                <p className="text-[11px] text-ink-faint">
                  Across {summary?.lines ?? 0} lines
                </p>
              </div>
            </Card>
          </div>

          <Card>
            <CardHeader
              title="Recommendations"
              description="Most urgent first. Open one to see the arithmetic and adjust the quantity."
            />
            <DataTable
              columns={columns}
              rows={rows}
              rowKey={(row) => row.key}
              loading={isPending}
              error={error}
              onRetry={refetch}
              onRowClick={(row) => setSelected(row)}
              emptyTitle="Nothing needs ordering"
              emptyDescription="Every product is above its reorder point for the locations in view."
            />
          </Card>

          <DraftOrders
            orders={data?.draft_orders ?? []}
            pending={raise.isPending}
            onRaise={(order) =>
              raise.mutate({
                supplier_id: order.supplier_id,
                warehouse_id: order.warehouse_id,
                lines: order.items
                  .filter((item) => item.suggested_qty > 0)
                  .map((item) => ({
                    product_id: item.product_id,
                    quantity: item.suggested_qty,
                  })),
              })
            }
          />
        </div>
      )}

      {selected && (
        <Detail
          rec={selected}
          onClose={() => setSelected(null)}
          onOrder={(rec, quantity) =>
            raise.mutate({
              supplier_id: rec.sourcing.supplier_id!,
              warehouse_id: rec.warehouse_id,
              lines: [{ product_id: rec.product_id, quantity }],
            })
          }
        />
      )}
    </>
  );
}
