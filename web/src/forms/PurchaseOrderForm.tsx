/**
 * Raise a purchase order on a distributor.
 *
 * The GST preview is the interesting part: the split between CGST+SGST and
 * IGST depends on whether the supplier and the delivery warehouse are in the
 * same state, so the form shows which regime applies before you commit. The
 * server recomputes it authoritatively — this is a preview, not the source of
 * truth.
 *
 * WHY THE SCANNER IS HERE AND NOT ON RECEIVE GOODS
 * ------------------------------------------------
 * It used to be on the receipt, which was one step too late. The distributor's
 * invoice arrives with — or before — the goods, and it is the document that
 * says what was actually sent and at what price. Reading it here raises the
 * order it describes; the receipt then has an order to select, which fills its
 * quantities and its branch from the order rather than from a second scan of
 * the same paper.
 *
 * The file is kept against the order afterwards. The order's own quantities
 * and prices came off a document, so that document has to still be producible
 * when a line is queried three months later.
 */

import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { money, plain } from "@/lib/format";
import type {
  InvoiceIntake,
  PurchaseOrder,
  Supplier,
  Warehouse,
} from "@/lib/types";
import {
  FormError,
  FormGrid,
  LineItems,
  emptyLine,
  useSubmit,
  type Line,
} from "@/components/form";
import {
  InvoiceScanButton,
  ScanFindings,
  storeInvoiceAgainstOrder,
  type RowState,
} from "@/forms/InvoiceScan";
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
  /** What the reader made of the invoice, for the findings panel. */
  const [scan, setScan] = useState<InvoiceIntake | null>(null);
  /** The file itself, kept so it can be stored once the order has an id. */
  const [scanned, setScanned] = useState<File | null>(null);
  /**
   * The order once it exists.
   *
   * The form stays open past the create rather than closing on it, because
   * storing the invoice is a second request and it can fail on its own. The
   * order is raised either way, and closing on success would take the only
   * place left to say the file did not go with it.
   */
  const [raised, setRaised] = useState<PurchaseOrder | null>(null);
  const [storing, setStoring] = useState(false);
  const [storeFailed, setStoreFailed] = useState(false);

  const suppliers = useQuery({
    queryKey: ["suppliers", "active"],
    queryFn: () =>
      api.get<Supplier[]>("/api/v1/suppliers?is_active=true"),
    enabled: open,
  });
  const warehouses = useQuery({
    queryKey: ["warehouses", "active"],
    queryFn: () =>
      api.get<Warehouse[]>("/api/v1/warehouses?is_active=true"),
    enabled: open,
  });

  const submit = useSubmit("/api/v1/purchase-orders", {
    invalidate: ["purchase-orders"],
    onDone: (created) => {
      const order = created as PurchaseOrder;
      setRaised(order);
      // Nothing was scanned, so there is nothing to keep and no reason to
      // hold the form open.
      if (!scanned) {
        onClose();
        return;
      }
      void keepTheInvoice(order.id);
    },
  });

  /** Store the file the order was read from, against the order. */
  const keepTheInvoice = async (poId: number) => {
    setStoring(true);
    setStoreFailed(false);
    try {
      await storeInvoiceAgainstOrder(poId, scanned!);
    } catch {
      setStoreFailed(true);
    } finally {
      setStoring(false);
    }
  };

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setSupplierId("");
    setWarehouseId("");
    setExpected("");
    setNotes("");
    setLines([emptyLine()]);
    setScan(null);
    setScanned(null);
    setRaised(null);
    setStoring(false);
    setStoreFailed(false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  /**
   * Turn a scanned invoice into order lines.
   *
   * Destructive, deliberately: the rows belong to the document that was read,
   * and merging them into whatever was already typed is how an order ends up
   * half from the paper and half from an abandoned attempt.
   *
   * An unresolved line still becomes a row, with its quantity and price filled
   * in and the product left empty. That is the useful shape — the typing is
   * done, and the one decision left is sitting in an empty picker that already
   * blocks the button.
   *
   * Free goods are not added in here, unlike on a receipt. A receipt records
   * what physically arrived, and free stock is stock; an order records what is
   * being bought, and the free carton is not part of the price.
   */
  const applyScan = (result: InvoiceIntake, file: File) => {
    setScan(result);
    setScanned(file);

    // A page the reader got no rows out of is not a reason to keep the
    // previous document's rows under this document's file.
    if (!result.lines.length) {
      setLines([emptyLine()]);
      return;
    }

    setLines(
      result.lines.map((row) => ({
        ...emptyLine(),
        product: row.product_id
          ? {
              id: row.product_id,
              sku: row.sku ?? "",
              name: row.product_name ?? `Product ${row.product_id}`,
              tracking_mode: "NONE" as const,
            }
          : null,
        values: {
          qty: plain(row.quantity),
          price: row.rate ? plain(row.rate) : "",
          lineNo: String(row.line_no),
          // What the paper called it, so an unresolved row can say so
          // underneath itself rather than being an empty picker with no clue
          // which line of the invoice it was.
          printed: row.product_id ? "" : (row.printed_name ?? ""),
        },
      })),
    );
  };

  /** The scanned rows as they stand now, for the findings panel. */
  const scannedRows: RowState[] = lines.flatMap((l) =>
    l.values.lineNo
      ? [
          {
            lineNo: Number(l.values.lineNo),
            hasProduct: Boolean(l.product),
            batch: "",
            expiry: "",
          },
        ]
      : [],
  );

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

  /**
   * A scanned line the catalogue could not name.
   *
   * Without this the row is simply not `usable`, so pressing Create would
   * raise the lines that matched and drop the rest — fourteen items on the
   * paper, eleven on the order, and nothing on screen having said so.
   */
  const lineProblem = (line: Line): string | null => {
    if (!line.product && line.values.printed) {
      return `Invoice reads “${line.values.printed}” — choose the product it is, or remove the row.`;
    }
    return null;
  };

  const ready =
    supplierId &&
    warehouseId &&
    usable.length > 0 &&
    !lines.some((l) => lineProblem(l) !== null);

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
        raised ? (
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        ) : (
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
        )
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        {raised ? (
          <div className="rounded-lg border border-ok/40 bg-ok-soft px-3 py-2.5 text-[13px]">
            <p className="font-medium text-ink">
              {raised.po_number} raised. Someone else has to approve it before
              stock can be received against it.
            </p>
            {scanned && (
              <p className="mt-1 text-ink-soft">
                {storing
                  ? "Storing the invoice…"
                  : storeFailed
                    ? "The order is saved, but the invoice did not store."
                    : "The invoice is kept against it, and can be downloaded from Receive goods."}
              </p>
            )}
            {storeFailed && !storing && (
              <Button
                size="sm"
                className="mt-2"
                onClick={() => void keepTheInvoice(raised.id)}
              >
                Try storing it again
              </Button>
            )}
          </div>
        ) : (
          <>
            {/*
              Before the fields rather than beside them: when there is an
              invoice in hand, reading it is the first thing to do and
              everything below is filled in from it.
            */}
            <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-dashed border-line px-3 py-2">
              <p className="text-xs text-ink-soft">
                Have the distributor&rsquo;s invoice? Photograph it and the
                products, quantities and rates fill in below — and the file is
                kept against the order.
                {!supplierId && " Naming the distributor first makes the matching surer."}
              </p>
              <div className="shrink-0">
                <InvoiceScanButton
                  warehouseId={warehouseId}
                  poId=""
                  supplierId={supplierId}
                  onScanned={applyScan}
                />
              </div>
            </div>

            {scan && (
              <ScanFindings
                result={scan}
                rows={scannedRows}
                onDismiss={() => setScan(null)}
              />
            )}
          </>
        )}

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
          validate={lineProblem}
          columns={[
            { name: "qty", header: "Quantity", type: "number", placeholder: "0" },
            {
              name: "price",
              header: "Unit price ₹",
              type: "number",
              placeholder: "0.00",
              width: "8rem",
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
