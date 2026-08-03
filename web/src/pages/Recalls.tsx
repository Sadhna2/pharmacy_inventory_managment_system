/**
 * Batch recall.
 *
 * One lot code in; every branch holding that batch frozen, and every customer
 * who already received it listed. The whole feature is ~30 lines of backend
 * service code because the ledger is append-only and lot-aware.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Building2, PackageX, ShieldAlert, Snowflake, Users } from "lucide-react";
import { api } from "@/lib/api";
import { ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { date, qty } from "@/lib/format";
import type { Lot, Page, Product, Recall, RecallImpact } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { ConfirmDialog } from "@/components/confirm";
import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Modal,
  Select,
  StatusBadge,
} from "@/components/ui";

export function Recalls() {
  const { can } = useAuth();
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [impact, setImpact] = useState<RecallImpact | null>(null);
  const [closing, setClosing] = useState<Recall | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [productId, setProductId] = useState("");
  const [lotId, setLotId] = useState("");
  const [reason, setReason] = useState("");
  const [ref, setRef] = useState("");

  const recalls = useQuery({
    queryKey: ["recalls"],
    queryFn: () => api.get<Recall[]>("/api/v1/recalls"),
  });

  const products = useQuery({
    queryKey: ["products", "batch-tracked"],
    queryFn: () =>
      api.get<Page<Product>>("/api/v1/products?tracking_mode=LOT_EXPIRY&size=200"),
    enabled: open,
  });

  const lots = useQuery({
    queryKey: ["lots", productId],
    queryFn: () => api.get<Lot[]>(`/api/v1/lots?product_id=${productId}`),
    enabled: open && !!productId,
  });

  const initiate = useMutation({
    mutationFn: () =>
      api.post<RecallImpact>("/api/v1/recalls", {
        lot_id: Number(lotId),
        reason,
        regulator_ref: ref || null,
      }),
    onSuccess: (result) => {
      setImpact(result);
      setOpen(false);
      setProductId("");
      setLotId("");
      setReason("");
      setRef("");
      setError(null);
      qc.invalidateQueries({ queryKey: ["recalls"] });
      qc.invalidateQueries({ queryKey: ["balances"] });
      qc.invalidateQueries({ queryKey: ["stock"] });
    },
    onError: (err) =>
      setError(
        err instanceof ApiError ? err.problem.detail : "Could not start the recall",
      ),
  });

  const columns: Column<Recall>[] = [
    {
      key: "batch",
      header: "Batch",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-mono text-[13px] font-medium text-ink">
            {row.lot_code}
          </p>
          <p className="truncate text-[11px] text-ink-faint">{row.product_sku}</p>
        </div>
      ),
    },
    {
      key: "reason",
      header: "Reason",
      card: "secondary",
      render: (row) => (
        <span className="line-clamp-1 text-[13px] text-ink-soft">{row.reason}</span>
      ),
    },
    {
      key: "ref",
      header: "Regulator ref",
      hideBelow: "lg",
      render: (row) => (
        <span className="font-mono text-[13px] text-ink-soft">
          {row.regulator_ref ?? "—"}
        </span>
      ),
    },
    {
      key: "when",
      header: "Initiated",
      hideBelow: "md",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{date(row.initiated_at)}</span>
      ),
    },
    {
      key: "status",
      header: "Status",
      card: "secondary",
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "qty",
      header: "Frozen",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="font-medium text-danger">
          {qty(row.qty_quarantined)}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      width: "7rem",
      render: (row) =>
        // A recall ends one way: the frozen stock is scrapped and the record
        // is closed. Until this existed a recall could be started and never
        // resolved, so the register only ever grew.
        row.status === "CLOSED" || !can("recall.initiate") ? null : (
          <Button size="sm" variant="danger" onClick={() => setClosing(row)}>
            <PackageX className="size-3.5" /> Close
          </Button>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Batch Recalls"
        description="Freeze a batch across every branch, and trace who already received it"
        actions={
          can("recall.initiate") && (
            <Button variant="danger" onClick={() => setOpen(true)}>
              <ShieldAlert className="size-4" /> Start recall
            </Button>
          )
        }
      />

      <Card>
        <DataTable
          columns={columns}
          rows={recalls.data ?? []}
          rowKey={(row) => row.id}
          loading={recalls.isLoading}
          emptyTitle="No recalls on record"
          emptyDescription="Batches withdrawn from circulation will be listed here."
        />
      </Card>

      {/* ------------------------------------------------------ start recall */}
      <Modal
        open={open}
        onClose={() => setOpen(false)}
        title="Start a batch recall"
        description="This immediately quarantines the batch at every location."
        footer={
          <>
            <Button onClick={() => setOpen(false)}>Cancel</Button>
            <Button
              variant="danger"
              loading={initiate.isPending}
              disabled={!lotId || reason.trim().length < 3}
              onClick={() => initiate.mutate()}
            >
              Freeze this batch
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Field label="Product" required>
            <Select
              value={productId}
              onChange={(e) => {
                setProductId(e.target.value);
                setLotId("");
              }}
            >
              <option value="">Select a product…</option>
              {products.data?.items.map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name} ({p.sku})
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Batch"
            required
            hint={productId ? undefined : "Choose a product first"}
          >
            <Select
              value={lotId}
              onChange={(e) => setLotId(e.target.value)}
              disabled={!productId}
            >
              <option value="">Select a batch…</option>
              {lots.data?.map((lot) => (
                <option key={lot.id} value={lot.id}>
                  {lot.lot_code}
                  {lot.expiry_date && ` — expires ${date(lot.expiry_date)}`}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Reason" required>
            <Input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="e.g. Contamination detected during QC"
            />
          </Field>

          <Field label="Regulator reference" hint="Optional — e.g. a CDSCO notice number">
            <Input
              value={ref}
              onChange={(e) => setRef(e.target.value)}
              placeholder="CDSCO-2026-114"
            />
          </Field>

          {error && (
            <div className="rounded-lg border border-danger/20 bg-danger-soft px-3 py-2 text-[13px] text-danger">
              {error}
            </div>
          )}
        </div>
      </Modal>

      {/* ------------------------------------------------------- close recall */}
      <ConfirmDialog
        open={closing !== null}
        onClose={() => setClosing(null)}
        title="Close this recall"
        description="The quarantined stock is scrapped and the batch leaves inventory for good."
        confirmLabel="Scrap and close"
        danger
        path={`/api/v1/recalls/${closing?.id}/close`}
        invalidate={["recalls", "lots"]}
      >
        {closing && (
          <div className="space-y-2 rounded-lg border border-line bg-muted/40 px-3 py-2.5 text-[13px]">
            <div className="flex justify-between gap-3">
              <span className="text-ink-soft">Batch</span>
              <span className="font-mono text-ink">{closing.lot_code}</span>
            </div>
            <div className="flex justify-between gap-3">
              <span className="text-ink-soft">Currently frozen</span>
              <span className="font-medium tnum text-danger">
                {qty(closing.qty_quarantined)}
              </span>
            </div>
            <p className="pt-1 text-ink-soft">
              This writes a scrap line against every location still holding the
              batch. The recall itself stays on the register — closing it is
              recorded, not erased.
            </p>
          </div>
        )}
      </ConfirmDialog>

      {/* ----------------------------------------------------- impact report */}
      <Modal
        open={impact !== null}
        onClose={() => setImpact(null)}
        title="Recall complete"
        description={
          impact
            ? `Batch ${impact.lot_code} of ${impact.product_name} is now quarantined`
            : undefined
        }
        wide
        footer={
          <Button variant="primary" onClick={() => setImpact(null)}>
            Done
          </Button>
        }
      >
        {impact && (
          <div className="space-y-5">
            <div className="flex items-center gap-3 rounded-xl border border-danger/20 bg-danger-soft px-4 py-3">
              <Snowflake className="size-5 shrink-0 text-danger" />
              <div>
                <p className="text-lg font-semibold tnum text-danger">
                  {qty(impact.total_quarantined)} units frozen
                </p>
                <p className="text-[13px] text-ink-soft">
                  Across {impact.locations.length}{" "}
                  {impact.locations.length === 1 ? "location" : "locations"} — no
                  longer dispensable
                </p>
              </div>
            </div>

            <section>
              <h4 className="mb-2 flex items-center gap-1.5 text-[13px] font-semibold text-ink">
                <Building2 className="size-3.5 text-ink-faint" />
                Stock frozen on our shelves
              </h4>
              {impact.locations.length === 0 ? (
                <p className="text-[13px] text-ink-soft">
                  None of this batch remained in stock.
                </p>
              ) : (
                <ul className="divide-y divide-line rounded-lg border border-line">
                  {impact.locations.map((loc) => (
                    <li
                      key={loc.warehouse_id}
                      className="flex items-center justify-between gap-3 px-3 py-2"
                    >
                      <span className="truncate text-[13px] text-ink">
                        {loc.warehouse_name}
                      </span>
                      <span className="shrink-0 text-[13px] font-medium tnum text-danger">
                        {qty(loc.qty_quarantined)}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
            </section>

            <section>
              <h4 className="mb-2 flex items-center gap-1.5 text-[13px] font-semibold text-ink">
                <Users className="size-3.5 text-ink-faint" />
                Customers who already received it
              </h4>
              {impact.customers.length === 0 ? (
                <p className="text-[13px] text-ink-soft">
                  None of this batch has been dispatched.
                </p>
              ) : (
                <ul className="divide-y divide-line rounded-lg border border-line">
                  {impact.customers.map((c) => (
                    <li key={c.customer_id} className="px-3 py-2">
                      <div className="flex items-center justify-between gap-3">
                        <span className="truncate text-[13px] font-medium text-ink">
                          {c.customer_name}
                        </span>
                        <span className="shrink-0 text-[13px] font-medium tnum text-ink">
                          {qty(c.quantity)}
                        </span>
                      </div>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {c.shipment_numbers.map((s) => (
                          <Badge key={s} tone="neutral">
                            <span className="font-mono">{s}</span>
                          </Badge>
                        ))}
                      </div>
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        )}
      </Modal>
    </>
  );
}
