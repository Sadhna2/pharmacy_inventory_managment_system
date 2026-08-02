/**
 * Raise a sales order for an institutional customer.
 *
 * No batch is chosen here, deliberately. Allocation happens as a separate step
 * and picks batches by earliest expiry (FEFO), so the person taking the order
 * cannot accidentally reserve stock that expires next week.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Customer, Warehouse } from "@/lib/types";
import {
  FormError,
  FormGrid,
  LineItems,
  emptyLine,
  useSubmit,
  type Line,
} from "@/components/form";
import { Badge, Button, Field, Input, Modal, Select } from "@/components/ui";

export function SalesOrderForm({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [customerId, setCustomerId] = useState("");
  const [warehouseId, setWarehouseId] = useState("");
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<Line[]>([emptyLine()]);

  const customers = useQuery({
    queryKey: ["customers"],
    queryFn: () => api.get<Customer[]>("/api/v1/customers"),
    enabled: open,
  });
  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
    enabled: open,
  });

  const submit = useSubmit("/api/v1/sales-orders", {
    invalidate: ["sales-orders"],
    onDone: onClose,
  });

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setCustomerId("");
    setWarehouseId("");
    setNotes("");
    setLines([emptyLine()]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const customer = customers.data?.find((c) => String(c.id) === customerId);
  const warehouse = warehouses.data?.find((w) => String(w.id) === warehouseId);
  const interState =
    customer && warehouse && customer.state_code !== warehouse.state_code;

  const usable = lines.filter((l) => l.product && Number(l.values.qty) > 0);
  const ready = customerId && warehouseId && usable.length > 0;

  const save = () =>
    submit.mutate({
      customer_id: Number(customerId),
      warehouse_id: Number(warehouseId),
      notes: notes.trim() || null,
      lines: usable.map((l) => ({
        product_id: l.product!.id,
        qty_ordered: l.values.qty,
        unit_price: l.values.price || null,
      })),
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="New sales order"
      description="Batches are chosen automatically at allocation, earliest expiry first."
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
          <Field label="Customer" required error={submit.fieldErrors.customer_id}>
            <Select
              value={customerId}
              onChange={(e) => setCustomerId(e.target.value)}
            >
              <option value="">Select a customer…</option>
              {customers.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                  {c.is_institutional ? "" : " (retail)"}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Ship from" required error={submit.fieldErrors.warehouse_id}>
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
        </FormGrid>

        <Field label="Notes">
          <Input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Ward, department, delivery instructions"
          />
        </Field>

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
              placeholder: "MRP",
              width: "md:w-32",
            },
          ]}
        />

        {customer && warehouse && (
          <div className="flex items-center gap-2 rounded-lg border border-line bg-muted/40 px-3 py-2.5">
            <Badge tone={interState ? "info" : "neutral"}>
              {interState ? "IGST" : "CGST + SGST"}
            </Badge>
            <span className="text-[13px] text-ink-soft">
              {interState
                ? `${warehouse.state_code} to ${customer.state_code} — inter-state`
                : `Both in ${warehouse.state_code} — intra-state`}
            </span>
          </div>
        )}
      </div>
    </Modal>
  );
}
