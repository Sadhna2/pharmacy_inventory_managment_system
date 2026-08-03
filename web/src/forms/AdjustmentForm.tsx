/**
 * Correct a discrepancy between the system and the shelf.
 *
 * Quantities are signed: negative writes stock off, positive adds it back.
 * Nothing posts to the ledger until a second person approves — the person who
 * raises an adjustment can never approve their own, so no one can quietly
 * write off their own shortfall.
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import type { Warehouse } from "@/lib/types";
import {
  FormError,
  FormGrid,
  LineItems,
  emptyLine,
  useSubmit,
  type Line,
} from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";

const REASONS = [
  { value: "CYCLE_COUNT", label: "Cycle count — physical count differs" },
  { value: "DAMAGE", label: "Damage — broken in handling" },
  { value: "EXPIRY", label: "Expiry — write off expired stock" },
  { value: "THEFT", label: "Shrinkage — unexplained loss" },
  { value: "SAMPLE", label: "Sample — given out free" },
  { value: "FOUND", label: "Found — stock not previously recorded" },
];

export function AdjustmentForm({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { user } = useAuth();
  const [warehouseId, setWarehouseId] = useState("");
  const [reason, setReason] = useState("CYCLE_COUNT");
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<Line[]>([emptyLine()]);

  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
    enabled: open,
  });

  const submit = useSubmit("/api/v1/adjustments", {
    invalidate: ["adjustments"],
    onDone: onClose,
  });

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setWarehouseId(user?.warehouse_id ? String(user.warehouse_id) : "");
    setReason("CYCLE_COUNT");
    setNotes("");
    setLines([emptyLine()]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const usable = lines.filter(
    (l) => l.product && l.values.qty && Number(l.values.qty) !== 0,
  );
  const net = useMemo(
    () => usable.reduce((sum, l) => sum + Number(l.values.qty || 0), 0),
    [usable],
  );
  const ready = warehouseId && reason && usable.length > 0;

  const save = () =>
    submit.mutate({
      warehouse_id: Number(warehouseId),
      reason_code: reason,
      notes: notes.trim() || null,
      lines: usable.map((l) => ({
        product_id: l.product!.id,
        quantity: l.values.qty,
      })),
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="New adjustment"
      description="Nothing moves until a second person approves it."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!ready}
            onClick={save}
          >
            Submit for approval
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <FormGrid>
          <Field label="Location" required error={submit.fieldErrors.warehouse_id}>
            <Select
              value={warehouseId}
              onChange={(e) => setWarehouseId(e.target.value)}
              disabled={Boolean(user?.warehouse_id)}
            >
              <option value="">Select a location…</option>
              {warehouses.data?.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field label="Reason" required error={submit.fieldErrors.reason_code}>
            <Select value={reason} onChange={(e) => setReason(e.target.value)}>
              {REASONS.map((r) => (
                <option key={r.value} value={r.value}>
                  {r.label}
                </option>
              ))}
            </Select>
          </Field>
        </FormGrid>

        <Field label="Explanation" hint="What was found, and how">
          <Input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Counted shelf A-03 on Monday, 5 strips short"
          />
        </Field>

        <LineItems
          lines={lines}
          onChange={setLines}
          fieldErrors={submit.fieldErrors}
          columns={[
            {
              name: "qty",
              header: "Change",
              type: "number",
              placeholder: "-5",
              width: "8rem",
            },
          ]}
        />

        <div className="flex items-start gap-2.5 rounded-lg border border-line bg-muted/40 px-3 py-2.5">
          <Info className="mt-0.5 size-4 shrink-0 text-ink-faint" />
          <p className="text-[13px] text-ink-soft">
            Use a negative number to write stock off and a positive one to add it.
            {usable.length > 0 && (
              <>
                {" "}
                Net change{" "}
                <span className="font-medium tnum text-ink">
                  {net > 0 ? "+" : ""}
                  {net}
                </span>{" "}
                across {usable.length} {usable.length === 1 ? "line" : "lines"}.
              </>
            )}
          </p>
        </div>
      </div>
    </Modal>
  );
}
