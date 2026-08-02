/**
 * Raise a purchase order on a distributor.
 *
 * The GST preview is the interesting part: the split between CGST+SGST and
 * IGST depends on whether the supplier and the delivery warehouse are in the
 * same state, so the form shows which regime applies before you commit. The
 * server recomputes it authoritatively — this is a preview, not the source of
 * truth.
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { money } from "@/lib/format";
import type { Supplier, Warehouse } from "@/lib/types";
import {
  FormError,
  FormGrid,
  LineItems,
  emptyLine,
  useSubmit,
  type Line,
} from "@/components/form";
import { Badge, Button, Field, Input, Modal, Select } from "@/components/ui";

export function PurchaseOrderForm({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [supplierId, setSupplierId] = useState("");
  const [warehouseId, setWarehouseId] = useState("");
  const [expected, setExpected] = useState("");
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<Line[]>([emptyLine()]);

  const suppliers = useQuery({
    queryKey: ["suppliers"],
    queryFn: () => api.get<Supplier[]>("/api/v1/suppliers"),
    enabled: open,
  });
  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
    enabled: open,
  });

  const submit = useSubmit("/api/v1/purchase-orders", {
    invalidate: ["purchase-orders"],
    onDone: onClose,
  });

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setSupplierId("");
    setWarehouseId("");
    setExpected("");
    setNotes("");
    setLines([emptyLine()]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const supplier = suppliers.data?.find((s) => String(s.id) === supplierId);
  const warehouse = warehouses.data?.find((w) => String(w.id) === warehouseId);
  const interState =
    supplier && warehouse && supplier.state_code !== warehouse.state_code;

  const subtotal = useMemo(
    () =>
      lines.reduce((sum, l) => {
        const qty = Number(l.values.qty ?? 0);
        const price = Number(l.values.price ?? 0);
        return sum + (Number.isFinite(qty * price) ? qty * price : 0);
      }, 0),
    [lines],
  );

  const usable = lines.filter((l) => l.product && Number(l.values.qty) > 0);
  const ready = supplierId && warehouseId && usable.length > 0;

  const save = () =>
    submit.mutate({
      supplier_id: Number(supplierId),
      warehouse_id: Number(warehouseId),
      expected_date: expected || null,
      notes: notes.trim() || null,
      lines: usable.map((l) => ({
        product_id: l.product!.id,
        qty_ordered: l.values.qty,
        unit_price: l.values.price || "0",
      })),
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="New purchase order"
      description="Someone other than you will have to approve it."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!ready}
            onClick={save}
          >
            Create order
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <FormGrid>
          <Field label="Distributor" required error={submit.fieldErrors.supplier_id}>
            <Select
              value={supplierId}
              onChange={(e) => setSupplierId(e.target.value)}
            >
              <option value="">Select a distributor…</option>
              {suppliers.data?.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name} ({s.state_code})
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Deliver to"
            required
            error={submit.fieldErrors.warehouse_id}
            hint="A branch here means the distributor delivers direct"
          >
            <Select
              value={warehouseId}
              onChange={(e) => setWarehouseId(e.target.value)}
            >
              <option value="">Select a location…</option>
              {warehouses.data?.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Expected delivery" error={submit.fieldErrors.expected_date}>
            <Input
              type="date"
              value={expected}
              onChange={(e) => setExpected(e.target.value)}
            />
          </Field>

          <Field label="Notes">
            <Input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Anything the distributor should know"
            />
          </Field>
        </FormGrid>

        <LineItems
          lines={lines}
          onChange={setLines}
          fieldErrors={submit.fieldErrors}
          columns={[
            { name: "qty", header: "Quantity", type: "number", placeholder: "0" },
            {
              name: "price",
              header: "Unit price ₹",
              type: "number",
              placeholder: "0.00",
              width: "md:w-32",
            },
          ]}
        />

        {supplier && warehouse && (
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-line bg-muted/40 px-3 py-2.5">
            <div className="flex items-center gap-2">
              <Badge tone={interState ? "info" : "neutral"}>
                {interState ? "IGST" : "CGST + SGST"}
              </Badge>
              <span className="text-[13px] text-ink-soft">
                {interState
                  ? `${supplier.state_code} to ${warehouse.state_code} — inter-state`
                  : `Both in ${warehouse.state_code} — intra-state`}
              </span>
            </div>
            <span className="text-[13px] text-ink-soft">
              Subtotal before tax{" "}
              <span className="font-medium tnum text-ink">{money(subtotal)}</span>
            </span>
          </div>
        )}
      </div>
    </Modal>
  );
}
