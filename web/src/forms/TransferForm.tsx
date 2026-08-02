/**
 * Move stock between locations.
 *
 * This is the mechanism that makes a central warehouse worth having: stock
 * approaching expiry at a branch that will not sell it can be moved to one
 * that will, instead of being written off.
 *
 * Batch is choosable per line, and that is the entire point of the screen.
 * Leaving it blank picks by earliest expiry, same as a sale — correct for
 * routine replenishment. But the deliberate move is the opposite one: send the
 * batch with 25 days left to the busy branch where it will actually sell, and
 * keep the fresh one where stock turns slowly. FEFO cannot decide that; a
 * person looking at the branches can.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight } from "lucide-react";
import { api, qs } from "@/lib/api";
import { date } from "@/lib/format";
import type { Balance, Page, Warehouse } from "@/lib/types";
import {
  FormError,
  LineItems,
  emptyLine,
  useSubmit,
  type Line,
  type LineProduct,
} from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";

export function TransferForm({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [fromId, setFromId] = useState("");
  const [toId, setToId] = useState("");
  const [notes, setNotes] = useState("");
  const [lines, setLines] = useState<Line[]>([emptyLine()]);

  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
    enabled: open,
  });

  // Every batch on the shelf at the source, in one request rather than one
  // per line. Indexed by product below so each row can offer only what is
  // actually there — a picker listing batches the branch does not hold is
  // worse than no picker.
  const stock = useQuery({
    queryKey: ["balances", "transfer-source", fromId],
    queryFn: () =>
      api.get<Page<Balance>>(
        `/api/v1/stock/balances${qs({
          warehouse_id: fromId,
          status: "AVAILABLE",
          size: 200,
        })}`,
      ),
    enabled: open && !!fromId,
  });

  const rowsFor = (product: LineProduct | null) =>
    !product || !stock.data
      ? []
      : stock.data.items.filter((b) => b.product_id === product.id && b.lot_id);

  const batchesFor = (product: LineProduct | null) =>
    rowsFor(product)
      .sort((a, b) => (a.expiry_date ?? "").localeCompare(b.expiry_date ?? ""))
      .map((b) => ({
        value: String(b.lot_id),
        label: `${b.lot_code}${b.expiry_date ? ` · exp ${date(b.expiry_date)}` : ""}`
          + ` · ${Number(b.qty_available)} avail`,
      }));

  /**
   * How much of this product the source can actually give — for the chosen
   * batch, or across every batch when the line is left on FEFO.
   */
  const availableFor = (line: Line) => {
    const rows = rowsFor(line.product);
    const chosen = line.values.lot;
    const relevant = chosen
      ? rows.filter((b) => String(b.lot_id) === chosen)
      : rows;
    return relevant.reduce((sum, b) => sum + Number(b.qty_available), 0);
  };

  /**
   * Refuse the line here rather than letting it become a draft.
   *
   * Previously the form happily accepted 30 of a batch holding 25: the draft
   * saved, approval passed, and it only failed at dispatch — by which point
   * the number was three screens away from the field that was wrong.
   */
  const lineProblem = (line: Line): string | null => {
    if (!line.product || !fromId) return null;
    const want = Number(line.values.qty);
    if (!line.values.qty || Number.isNaN(want)) return null;
    if (want <= 0) return "Quantity must be more than zero.";

    const have = availableFor(line);
    if (want > have) {
      const where = line.values.lot ? "in that batch" : "at this location";
      return have === 0
        ? `None of this product is available ${where}.`
        : `Only ${have} available ${where} — you asked for ${want}.`;
    }
    return null;
  };

  const submit = useSubmit("/api/v1/transfers", {
    invalidate: ["transfers"],
    onDone: onClose,
  });

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setFromId("");
    setToId("");
    setNotes("");
    setLines([emptyLine()]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const usable = lines.filter((l) => l.product && Number(l.values.qty) > 0);
  const sameBothEnds = fromId !== "" && fromId === toId;
  const anyBadLine = lines.some((l) => lineProblem(l) !== null);
  const ready =
    fromId && toId && !sameBothEnds && usable.length > 0 && !anyBadLine;

  const save = () =>
    submit.mutate({
      from_warehouse_id: Number(fromId),
      to_warehouse_id: Number(toId),
      notes: notes.trim() || null,
      lines: usable.map((l) => ({
        product_id: l.product!.id,
        quantity: l.values.qty,
        // Omitted rather than null when blank, so the server keeps its FEFO
        // default instead of treating "no batch" as a choice.
        ...(l.values.lot ? { lot_id: Number(l.values.lot) } : {}),
      })),
    });

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="New transfer"
      description="Stock stays visible as in-transit while it is on the road."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!ready}
            onClick={save}
          >
            Create transfer
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <div className="grid items-end gap-3 sm:grid-cols-[1fr_auto_1fr]">
          <Field label="From" required error={submit.fieldErrors.from_warehouse_id}>
            <Select value={fromId} onChange={(e) => setFromId(e.target.value)}>
              <option value="">Select a location…</option>
              {warehouses.data?.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
          </Field>

          <ArrowRight className="mb-2.5 hidden size-4 shrink-0 text-ink-faint sm:block" />

          <Field
            label="To"
            required
            error={
              sameBothEnds
                ? "Pick a different destination"
                : submit.fieldErrors.to_warehouse_id
            }
          >
            <Select value={toId} onChange={(e) => setToId(e.target.value)}>
              <option value="">Select a location…</option>
              {warehouses.data
                ?.filter((w) => String(w.id) !== fromId)
                .map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
            </Select>
          </Field>
        </div>

        <Field label="Reason">
          <Input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Weekly replenishment, near-expiry redistribution…"
          />
        </Field>

        {fromId && stock.data?.items.length === 0 && (
          <p className="text-[13px] text-ink-soft">
            No available stock at this location.
          </p>
        )}

        <LineItems
          lines={lines}
          onChange={setLines}
          fieldErrors={submit.fieldErrors}
          validate={lineProblem}
          columns={[
            {
              name: "lot",
              header: "Batch",
              type: "select",
              width: "md:w-56",
              emptyLabel: "Earliest expiry (FEFO)",
              options: batchesFor,
            },
            { name: "qty", header: "Quantity", type: "number", placeholder: "0" },
          ]}
        />
      </div>
    </Modal>
  );
}
