/**
 * Record a delivery arriving.
 *
 * This is the one document a Staff user creates, because it is the person at
 * the loading bay with the carton in front of them. The batch and expiry
 * columns appear only for products that are batch-tracked, so the form asks
 * for exactly what the box shows and nothing more.
 *
 * THE ORDER COMES FIRST
 * ---------------------
 * Picking the purchase order is the first field, not an optional afterthought
 * near the invoice box, and choosing one fills in everything the order already
 * knows: the destination, the products, the agreed unit cost, and a quantity
 * defaulted to whatever is still outstanding. What is left to type is only
 * what the order could not know — the supplier's invoice number, and the batch
 * and expiry printed on the cartons, which do not exist until the goods are
 * made.
 *
 * The previous arrangement defaulted to "No linked order" and buried the
 * picker to one side, so the ordinary path looked like it was missing. People
 * retyped an order they had already placed, the receipt was never linked, and
 * the order sat on Approved forever while its stock quietly arrived anyway.
 *
 * Anything that turns up but was not ordered — scheme goods, a sample, a
 * substitution — is still added by hand with "Add line". It arrived in the
 * same carton on the same invoice, so it belongs on the same receipt.
 *
 * Receiving into quarantine is offered rather than assumed: stock pending QC
 * exists and is countable, but cannot be dispensed.
 *
 * Cross-docking lives here rather than on its own screen. When 500 strips
 * arrive at central and 100 are already spoken for by Andheri, marking that
 * line "for Andheri" raises the outbound transfer in the same action — the
 * goods are still received here, but never reach a shelf. One column on a
 * form people already use beats a second document type to learn.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PackageCheck, Plus, ShieldQuestion, Split } from "lucide-react";
import { api, qs } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { plain } from "@/lib/format";
import type {
  Page,
  Product,
  PurchaseOrder,
  PurchaseOrderLine,
  Warehouse,
} from "@/lib/types";
import { ProductForm } from "@/forms/ProductForm";
import {
  FormError,
  FormGrid,
  LineItems,
  emptyLine,
  useSubmit,
  type Line,
} from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";
import { InvoiceDownload } from "@/forms/InvoiceScan";

/** What the order still expects on this line. Never negative. */
const outstanding = (line: PurchaseOrderLine) =>
  Math.max(0, Number(line.qty_ordered) - Number(line.qty_received));

/** An order line, ready to be received. */
const lineFromOrder = (source: PurchaseOrderLine): Line => ({
  ...emptyLine(),
  product: {
    id: source.product_id,
    sku: source.sku ?? "",
    name: source.product_name ?? `Product ${source.product_id}`,
    tracking_mode: source.tracking_mode ?? "NONE",
  },
  values: {
    qty: plain(outstanding(source)),
    cost: plain(source.unit_price),
    // Binds the receipt line to the order line it satisfies, so the server
    // does not have to guess by product when an order lists the same product
    // twice at two prices.
    poLine: String(source.id),
  },
});

export function GoodsReceiptForm({
  open,
  onClose,
  purchaseOrder,
}: {
  open: boolean;
  onClose: () => void;
  /** Pre-links the receipt when opened from a specific order. */
  purchaseOrder?: PurchaseOrder | null;
}) {
  const { user, can } = useAuth();
  const [warehouseId, setWarehouseId] = useState("");
  const [poId, setPoId] = useState("");
  const [invoiceNo, setInvoiceNo] = useState("");
  const [invoiceDate, setInvoiceDate] = useState("");
  const [quarantine, setQuarantine] = useState(false);
  const [crossDockAll, setCrossDockAll] = useState("");
  const [lines, setLines] = useState<Line[]>([emptyLine()]);
  /**
   * The row whose product is being created right now, by `key`.
   *
   * A delivery routinely carries something the catalogue has never held, and
   * until this existed the only way through was to abandon the half-filled
   * receipt, add the product in Master data, come back and scan again. Nobody
   * does that. They type the row by hand, the catalogue never learns, and the
   * next invoice from the same distributor asks the same question.
   */
  const [creatingFor, setCreatingFor] = useState<number | null>(null);

  const warehouses = useQuery({
    queryKey: ["warehouses", "active"],
    queryFn: () =>
      api.get<Warehouse[]>("/api/v1/warehouses?is_active=true"),
    enabled: open,
  });

  const openOrders = useQuery({
    queryKey: ["purchase-orders", "receivable"],
    queryFn: () =>
      api.get<Page<PurchaseOrder>>(
        `/api/v1/purchase-orders${qs({ size: 100 })}`,
      ),
    enabled: open,
  });

  const submit = useSubmit("/api/v1/goods-receipts", {
    invalidate: ["goods-receipts", "purchase-orders"],
    onDone: onClose,
  });

  useEffect(() => {
    if (!open) return;
    submit.reset();
    // Staff are pinned to one branch, so default to it rather than making
    // them choose from a list of one.
    setWarehouseId(
      purchaseOrder
        ? String(purchaseOrder.warehouse_id)
        : user?.warehouse_id
          ? String(user.warehouse_id)
          : "",
    );
    setPoId(purchaseOrder ? String(purchaseOrder.id) : "");
    setInvoiceNo("");
    setInvoiceDate("");
    setQuarantine(false);
    setCrossDockAll("");
    setLines(
      purchaseOrder ? linesFor(purchaseOrder) : [emptyLine()],
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, purchaseOrder]);

  // Only orders that have cleared approval can receive stock — a draft has not
  // been committed to yet, so there is nothing legitimate to deliver against.
  const awaiting = openOrders.data?.items.filter(
    (po) => po.status === "APPROVED" || po.status === "PARTIALLY_RECEIVED",
  );
  const chosen = awaiting?.find((po) => String(po.id) === poId) ?? null;

  /** The order's still-outstanding lines, or one blank row if it is complete. */
  const linesFor = (po: PurchaseOrder): Line[] => {
    const rows = (po.lines ?? []).filter((l) => outstanding(l) > 0);
    return rows.length ? rows.map(lineFromOrder) : [emptyLine()];
  };

  /** Look the order line back up, so a row can say what was expected of it. */
  const orderLine = (line: Line): PurchaseOrderLine | null =>
    (chosen?.lines ?? []).find((l) => String(l.id) === line.values.poLine) ??
    null;

  /**
   * Choosing an order rewrites the form from it.
   *
   * Deliberately destructive: the lines belong to the order that was picked,
   * and silently keeping rows from a previous choice is how stock ends up
   * booked against the wrong delivery.
   */
  const chooseOrder = (id: string) => {
    setPoId(id);
    const po = awaiting?.find((p) => String(p.id) === id);
    if (!po) {
      setLines([emptyLine()]);
      return;
    }
    setWarehouseId(String(po.warehouse_id));
    setLines(linesFor(po));
  };

  /**
   * Start a product from what the row already says about it.
   *
   * The MRP is on the carton and has been typed in; how the thing is *stocked*
   * — its schedule, its storage, where it is sourced from — is not, and is
   * left at the form's defaults for a person to set, because a default drug
   * schedule nobody looked at is worse than an empty one.
   *
   * The tracking mode is the exception, and it is an inference rather than a
   * default: a row carrying an expiry is a batch with an expiry, and one
   * carrying only a batch code is a batch. That is evidence, so it is used.
   */
  const prefillFor = (line: Line): Record<string, string | boolean> => {
    const fields: Record<string, string | boolean> = {
      tracking_mode: line.values.expiry
        ? "LOT_EXPIRY"
        : line.values.lot
          ? "LOT"
          : "NONE",
    };
    if (line.values.mrp) fields.mrp = line.values.mrp;
    return fields;
  };

  /** Put a freshly created product into the row that prompted it. */
  const adoptCreated = (created: Product) => {
    setLines((current) =>
      current.map((l) =>
        l.key === creatingFor
          ? {
              ...l,
              product: {
                id: created.id,
                sku: created.sku,
                name: created.name,
                tracking_mode: created.tracking_mode,
              },
            }
          : l,
      ),
    );
    setCreatingFor(null);
  };

  const usable = lines.filter((l) => l.product && Number(l.values.qty) > 0);

  /**
   * Refuse a line here rather than letting the server refuse it.
   *
   * The batch rule in particular was only enforced on the API, so a missing
   * batch number came back as a red banner at the top of the modal naming a
   * SKU, nowhere near the empty cell that caused it.
   */
  const lineProblem = (line: Line): string | null => {
    if (!line.product) return null;
    const amount = Number(line.values.qty);
    if (!line.values.qty || Number.isNaN(amount)) return null;
    if (amount <= 0) return "Quantity must be more than zero.";
    if (line.product.tracking_mode !== "NONE" && !line.values.lot?.trim()) {
      return "Batch number required — it is printed on the carton.";
    }
    if (line.product.tracking_mode === "LOT_EXPIRY" && !line.values.expiry) {
      return "Expiry required — this product is dispensed earliest-first.";
    }
    return null;
  };

  const anyBadLine = lines.some((l) => lineProblem(l) !== null);
  const ready = warehouseId && usable.length > 0 && !anyBadLine;

  // Only a manager can redirect a shipment; the server enforces the same rule.
  // A branch pharmacist signs for what arrived — that is a different authority
  // from deciding it should go somewhere else.
  const canCrossDock = can("transfer.create");
  const crossDocked = usable.filter((l) => l.values.xdock).length;

  // Every location except the one receiving. Cross-docking to yourself is not
  // a shortcut, it is a no-op the server rejects.
  const destinations = (warehouses.data ?? [])
    .filter((w) => w.is_active && String(w.id) !== warehouseId)
    .map((w) => ({ value: String(w.id), label: w.name }));

  /** Header shortcut: fill the branch column on every line at once. */
  const applyToAll = (value: string) => {
    setCrossDockAll(value);
    setLines((current) =>
      current.map((l) => ({ ...l, values: { ...l.values, xdock: value } })),
    );
  };

  const save = () => {
    submit.mutate({
      warehouse_id: Number(warehouseId),
      purchase_order_id: poId ? Number(poId) : null,
      supplier_invoice_no: invoiceNo.trim() || null,
      supplier_invoice_date: invoiceDate || null,
      lines: usable.map((l) => ({
        product_id: l.product!.id,
        quantity: l.values.qty,
        unit_cost: l.values.cost || "0",
        lot_code: l.values.lot?.trim() || null,
        expiry_date: l.values.expiry || null,
        mrp: l.values.mrp || null,
        quarantine,
        cross_dock_warehouse_id: l.values.xdock ? Number(l.values.xdock) : null,
        // Named explicitly rather than left to the server's product match, so
        // an order listing the same product on two lines settles the right one.
        purchase_order_line_id: l.values.poLine
          ? Number(l.values.poLine)
          : null,
      })),
    });
  };

  // The receiving location just became an invalid destination for its own
  // lines, so anything chosen against the previous one is stale.
  useEffect(() => {
    applyToAll("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [warehouseId]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide="xl"
      title="Receive goods"
      description="Record what physically arrived, batch by batch."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!ready || (quarantine && crossDocked > 0)}
            onClick={save}
          >
            Receive into stock
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <FormGrid>
          <Field
            label="Against purchase order"
            hint={
              chosen
                ? "Products, cost and outstanding quantity filled in below"
                : "Pick the order this delivery is for, or leave blank for an unordered delivery"
            }
          >
            <Select value={poId} onChange={(e) => chooseOrder(e.target.value)}>
              <option value="">No linked order</option>
              {awaiting?.map((po) => (
                <option key={po.id} value={po.id}>
                  {po.po_number} — {po.supplier_name}
                </option>
              ))}
            </Select>
            {/*
              The invoice the order was raised from, if it was raised by
              scanning one. This is where it is wanted: the cartons are open,
              and the question is whether what is on the floor is what the
              paper says. Reading it, not re-scanning it — the numbers below
              already came off this document by way of the order.
            */}
            {chosen?.has_invoice && (
              <InvoiceDownload poId={chosen.id} number={chosen.po_number} />
            )}
          </Field>

          <Field
            label="Received at"
            required
            error={submit.fieldErrors.warehouse_id}
            hint={chosen ? `Set by ${chosen.po_number}` : undefined}
          >
            <Select
              value={warehouseId}
              onChange={(e) => setWarehouseId(e.target.value)}
              // The order names where it is being delivered; receiving it
              // elsewhere is a mis-picked order, not a decision. The server
              // rejects the mismatch too.
              disabled={Boolean(chosen) || Boolean(user?.warehouse_id)}
            >
              <option value="">Select a location…</option>
              {warehouses.data?.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Supplier invoice no."
            error={submit.fieldErrors.supplier_invoice_no}
            hint="From the distributor's paperwork in the box"
          >
            <Input
              value={invoiceNo}
              onChange={(e) => setInvoiceNo(e.target.value)}
              placeholder="APX/2026/8891"
            />
          </Field>

          <Field label="Invoice date">
            <Input
              type="date"
              value={invoiceDate}
              onChange={(e) => setInvoiceDate(e.target.value)}
            />
          </Field>
        </FormGrid>

        <LineItems
          lines={lines}
          onChange={setLines}
          fieldErrors={submit.fieldErrors}
          validate={lineProblem}
          rowAction={(line) =>
            !line.product && line.values.printed ? (
              <Button
                size="sm"
                className="mt-1.5"
                onClick={() => setCreatingFor(line.key)}
              >
                <Plus className="size-3.5" />
                Add “{line.values.printed}” to the catalogue
              </Button>
            ) : null
          }
          lockProduct={(line) => Boolean(line.values.poLine)}
          note={(line) => {
            const source = orderLine(line);
            if (!source) {
              return chosen && line.product
                ? "Not on this order — will be received as an extra."
                : null;
            }
            const left = outstanding(source);
            const receiving = Number(line.values.qty || 0);
            const already = Number(source.qty_received);
            // Facts first, separated by dots; the consequence of what is being
            // typed reads as a clause after a dash, so it does not look like
            // another fact about the order.
            const facts = [`Ordered ${plain(source.qty_ordered)}`];
            if (already > 0) facts.push(`already received ${plain(already)}`);
            facts.push(`outstanding ${plain(left)}`);
            const effect =
              receiving > left
                ? ` — ${plain(receiving - left)} more than ordered`
                : receiving > 0 && receiving < left
                  ? ` — ${plain(left - receiving)} will stay outstanding`
                  : "";
            return facts.join(" · ") + effect;
          }}
          columns={[
            {
              name: "qty",
              header: "Quantity",
              type: "number",
              placeholder: "0",
              width: "5rem",
            },
            {
              name: "cost",
              header: "Unit cost ₹",
              type: "number",
              placeholder: "0.00",
              width: "6rem",
            },
            {
              // Printed on this carton, so it is captured while the carton is
              // in hand. It sticks to the batch — older stock on the shelf
              // goes on selling at the price printed on it.
              name: "mrp",
              header: "MRP ₹",
              type: "number",
              placeholder: "Printed",
              width: "6rem",
            },
            {
              name: "lot",
              header: "Batch no.",
              placeholder: "On the box",
              width: "7rem",
              showFor: (p) => !p || p.tracking_mode !== "NONE",
            },
            {
              name: "expiry",
              header: "Expiry",
              type: "date",
              width: "9.5rem",
              showFor: (p) => !p || p.tracking_mode === "LOT_EXPIRY",
            },
            ...(canCrossDock
              ? [
                  {
                    name: "xdock",
                    header: "For branch",
                    type: "select" as const,
                    width: "11rem",
                    emptyLabel: "Keep here",
                    options: () => destinations,
                  },
                ]
              : []),
          ]}
        />

        {chosen && (
          <div className="flex flex-wrap items-center gap-2.5 rounded-lg border border-line bg-muted/40 px-3 py-2.5">
            <PackageCheck className="size-4 shrink-0 text-ink-faint" />
            <span className="text-[13px] text-ink-soft">
              {(() => {
                // What this order will still be waiting for once this receipt
                // is posted. Stated before saving, because "did that finish
                // the order?" is the question the status badge answers a
                // moment too late.
                const left = (chosen.lines ?? []).reduce((total, source) => {
                  const receiving = lines
                    .filter((l) => l.values.poLine === String(source.id))
                    .reduce((sum, l) => sum + Number(l.values.qty || 0), 0);
                  return total + Math.max(0, outstanding(source) - receiving);
                }, 0);
                const extras = usable.filter((l) => !l.values.poLine).length;
                const tail = extras
                  ? ` ${extras} extra line${extras === 1 ? "" : "s"} not on the order.`
                  : "";
                return left === 0 ? (
                  <>
                    This receipt <span className="font-medium text-ink">completes </span>
                    {chosen.po_number}.{tail}
                  </>
                ) : (
                  <>
                    <span className="font-medium text-ink">{plain(left)} units</span>{" "}
                    will still be outstanding on {chosen.po_number} — it stays
                    partially received.{tail}
                  </>
                );
              })()}
            </span>
          </div>
        )}

        {canCrossDock && destinations.length > 0 && (
          <div className="flex flex-wrap items-center gap-2.5 rounded-lg border border-line bg-muted/40 px-3 py-2.5">
            <Split className="size-4 shrink-0 text-ink-faint" />
            <span className="text-[13px] text-ink-soft">
              Whole shipment for one branch?
            </span>
            <Select
              value={crossDockAll}
              onChange={(e) => applyToAll(e.target.value)}
              className="h-9 w-auto min-w-[12rem]"
            >
              <option value="">Keep it all here</option>
              {destinations.map((d) => (
                <option key={d.value} value={d.value}>
                  Send everything to {d.label}
                </option>
              ))}
            </Select>
            {crossDocked > 0 && (
              <span className="text-[13px] text-ink-soft">
                <span className="font-medium text-ink">{crossDocked}</span> of{" "}
                {usable.length} line{usable.length === 1 ? "" : "s"} will go
                straight out — received here, never shelved.
              </span>
            )}
          </div>
        )}

        <label className="flex cursor-pointer items-start gap-2.5 rounded-lg border border-line px-3 py-2.5 hover:bg-muted/50">
          <input
            type="checkbox"
            checked={quarantine}
            onChange={(e) => setQuarantine(e.target.checked)}
            className="mt-0.5 size-4 accent-[var(--color-brand)]"
          />
          <span className="min-w-0">
            <span className="flex items-center gap-1.5 text-[13px] font-medium text-ink">
              <ShieldQuestion className="size-3.5 text-ink-faint" />
              Hold for QC
            </span>
            <span className="mt-0.5 block text-xs text-ink-soft">
              Receives into quarantine instead of available stock. It will be
              counted but cannot be dispensed until released.
            </span>
            {quarantine && crossDocked > 0 && (
              <span className="mt-1.5 block text-xs text-danger">
                Quarantined stock cannot be cross-docked — it has not been
                checked yet. Clear one or the other.
              </span>
            )}
          </span>
        </label>
      </div>

      {/*
        Stacked over the receipt rather than replacing it. The half-filled
        delivery is still there underneath, which is the whole point — the
        person is mid-receipt and adding this product is a detour, not a
        different task. Closing either way lands them back on the row.
      */}
      <ProductForm
        open={creatingFor !== null}
        onClose={() => setCreatingFor(null)}
        prefill={
          creatingFor !== null
            ? prefillFor(lines.find((l) => l.key === creatingFor) ?? emptyLine())
            : undefined
        }
        onCreated={adoptCreated}
      />
    </Modal>
  );
}
