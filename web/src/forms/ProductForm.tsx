/**
 * Create or edit a product.
 *
 * The drug master is where most of the domain rules originate, so the form
 * teaches them as you fill it in: choosing batch tracking explains what it
 * commits you to, and choosing cold-chain storage warns that the product will
 * then only be accepted into cold-chain bins.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Snowflake } from "lucide-react";
import { api } from "@/lib/api";
import type { Category, Product, Uom } from "@/lib/types";
import { FormError, FormGrid, useSubmit } from "@/components/form";
import {
  Button,
  Field,
  Input,
  Modal,
  Select,
  Textarea,
} from "@/components/ui";

const TRACKING = [
  { value: "LOT_EXPIRY", label: "Batch + expiry — most medicines" },
  { value: "LOT", label: "Batch only — no expiry date" },
  { value: "NONE", label: "Quantity only — devices, consumables" },
];

const SCHEDULES = [
  { value: "OTC", label: "OTC — no prescription" },
  { value: "G", label: "Schedule G" },
  { value: "H", label: "Schedule H — prescription only" },
  { value: "H1", label: "Schedule H1 — prescription + register" },
  { value: "X", label: "Schedule X — narcotics, strictest" },
];

const SOURCING = [
  {
    value: "EITHER",
    label: "Either route",
    hint: "The reorder engine picks whichever is cheaper or faster",
  },
  {
    value: "VIA_CENTRAL",
    label: "Via central warehouse",
    hint: "Buy in bulk centrally, transfer out. Lets near-expiry stock be moved between branches.",
  },
  {
    value: "DIRECT",
    label: "Direct to branch",
    hint: "Distributor delivers to the branch. Saves a handling day on fast movers.",
  },
];

const STORAGE = [
  { value: "AMBIENT", label: "Ambient — room temperature" },
  { value: "COOL", label: "Cool — 8 to 15°C" },
  { value: "COLD_CHAIN", label: "Cold chain — 2 to 8°C" },
  { value: "FROZEN", label: "Frozen" },
];

type Draft = Record<string, string | boolean>;

const blank: Draft = {
  sku: "",
  name: "",
  composition: "",
  manufacturer: "",
  pack_size: "",
  description: "",
  category_id: "",
  uom_id: "",
  tracking_mode: "LOT_EXPIRY",
  drug_schedule: "OTC",
  storage_condition: "AMBIENT",
  is_prescription_required: false,
  hsn_code: "",
  gst_rate: "12",
  barcode: "",
  reorder_point: "0",
  safety_stock_days: "7",
  mrp: "",
  sourcing_policy: "EITHER",
};

export function ProductForm({
  open,
  onClose,
  product,
  prefill,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  /** Present when editing; absent when creating. */
  product?: Product | null;
  /**
   * Fields to start a new product from — used when one is being created off a
   * supplier invoice, where the paper already states the name, pack, HSN, GST
   * rate and MRP. Ignored when editing.
   *
   * Deliberately a partial draft rather than a whole one: the invoice knows
   * some of a product and nothing about how it is stocked, and pretending
   * otherwise would put a default schedule and storage condition on a drug
   * without anyone having looked.
   */
  prefill?: Draft;
  /**
   * The product that was just created. Lets a caller adopt it immediately —
   * the receiver who created it did so to fill a row on the form they are
   * still standing in, and making them find it in a picker afterwards is the
   * step where the whole detour stops being worth it.
   */
  onCreated?: (created: Product) => void;
}) {
  const editing = Boolean(product);
  const [draft, setDraft] = useState<Draft>(blank);

  const uoms = useQuery({
    queryKey: ["uoms"],
    queryFn: () => api.get<Uom[]>("/api/v1/uoms"),
    enabled: open,
  });
  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => api.get<Category[]>("/api/v1/categories"),
    enabled: open,
  });

  const submit = useSubmit(
    editing ? `/api/v1/products/${product!.id}` : "/api/v1/products",
    {
      invalidate: ["products"],
      onDone: (result) => {
        if (!editing && onCreated) onCreated(result as Product);
        onClose();
      },
      method: editing ? "patch" : "post",
    },
  );

  // Reset whenever the modal opens, so a cancelled edit never leaks into the
  // next one.
  useEffect(() => {
    if (!open) return;
    submit.reset();
    if (product) {
      setDraft({
        sku: product.sku,
        name: product.name,
        composition: product.composition ?? "",
        manufacturer: product.manufacturer ?? "",
        pack_size: product.pack_size ?? "",
        description: product.description ?? "",
        category_id: product.category_id ? String(product.category_id) : "",
        uom_id: String(product.uom_id),
        tracking_mode: product.tracking_mode,
        drug_schedule: product.drug_schedule,
        storage_condition: product.storage_condition,
        is_prescription_required: product.is_prescription_required,
        hsn_code: product.hsn_code ?? "",
        gst_rate: product.gst_rate,
        barcode: product.barcode ?? "",
        reorder_point: product.reorder_point,
        safety_stock_days: String(product.safety_stock_days),
        mrp: product.mrp ?? "",
        sourcing_policy: product.sourcing_policy,
      });
    } else {
      setDraft({ ...blank, ...prefill });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, product]);

  const set = (name: string, value: string | boolean) =>
    setDraft((d) => ({ ...d, [name]: value }));

  const text = (name: string) => String(draft[name] ?? "");

  const save = () => {
    const optionalNumber = (v: string) => (v.trim() === "" ? null : v.trim());
    submit.mutate({
      sku: text("sku").trim(),
      name: text("name").trim(),
      description: optionalNumber(text("description")),
      category_id: text("category_id") ? Number(text("category_id")) : null,
      uom_id: Number(text("uom_id")),
      tracking_mode: text("tracking_mode"),
      composition: optionalNumber(text("composition")),
      manufacturer: optionalNumber(text("manufacturer")),
      pack_size: optionalNumber(text("pack_size")),
      drug_schedule: text("drug_schedule"),
      storage_condition: text("storage_condition"),
      is_prescription_required: Boolean(draft.is_prescription_required),
      hsn_code: optionalNumber(text("hsn_code")),
      gst_rate: text("gst_rate") || "0",
      barcode: optionalNumber(text("barcode")),
      reorder_point: text("reorder_point") || "0",
      safety_stock_days: Number(text("safety_stock_days") || 7),
      sourcing_policy: text("sourcing_policy"),
      mrp: optionalNumber(text("mrp")),
    });
  };

  const err = (f: string) => submit.fieldErrors[f];
  const coldChain = text("storage_condition") === "COLD_CHAIN";
  const ready = text("sku").trim() && text("name").trim() && text("uom_id");

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={editing ? `Edit ${product!.name}` : "New product"}
      description={
        editing
          ? "Changes apply to future transactions. History is untouched."
          : "Add a drug or consumable to the master list."
      }
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!ready}
            onClick={save}
          >
            {editing ? "Save changes" : "Create product"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <FormGrid>
          <Field label="SKU" required error={err("sku")} hint="Unique code, e.g. PAR-650">
            <Input
              value={text("sku")}
              onChange={(e) => set("sku", e.target.value.toUpperCase())}
              placeholder="PAR-650"
              disabled={editing}
            />
          </Field>
          <Field label="Name" required error={err("name")}>
            <Input
              value={text("name")}
              onChange={(e) => set("name", e.target.value)}
              placeholder="Paracetamol 650mg"
            />
          </Field>
          <Field label="Composition" error={err("composition")} hint="The salt — used to spot duplicates">
            <Input
              value={text("composition")}
              onChange={(e) => set("composition", e.target.value)}
              placeholder="Paracetamol"
            />
          </Field>
          <Field label="Manufacturer" error={err("manufacturer")}>
            <Input
              value={text("manufacturer")}
              onChange={(e) => set("manufacturer", e.target.value)}
              placeholder="Cipla"
            />
          </Field>
          <Field label="Pack size" error={err("pack_size")}>
            <Input
              value={text("pack_size")}
              onChange={(e) => set("pack_size", e.target.value)}
              placeholder="15 tablets"
            />
          </Field>
          <Field label="Unit of measure" required error={err("uom_id")}>
            <Select
              value={text("uom_id")}
              onChange={(e) => set("uom_id", e.target.value)}
            >
              <option value="">Select…</option>
              {uoms.data?.map((u) => (
                <option key={u.id} value={u.id}>
                  {u.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Category" error={err("category_id")}>
            <Select
              value={text("category_id")}
              onChange={(e) => set("category_id", e.target.value)}
            >
              <option value="">Uncategorised</option>
              {categories.data?.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="Tracking"
            required
            error={err("tracking_mode")}
            hint={
              text("tracking_mode") === "NONE"
                ? "No batch numbers will be recorded"
                : "Every receipt will require a batch number"
            }
          >
            <Select
              value={text("tracking_mode")}
              onChange={(e) => set("tracking_mode", e.target.value)}
              disabled={editing}
            >
              {TRACKING.map((t) => (
                <option key={t.value} value={t.value}>
                  {t.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label="Drug schedule" error={err("drug_schedule")}>
            <Select
              value={text("drug_schedule")}
              onChange={(e) => {
                set("drug_schedule", e.target.value);
                if (e.target.value !== "OTC") set("is_prescription_required", true);
              }}
            >
              {SCHEDULES.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="Storage"
            error={err("storage_condition")}
            hint={coldChain ? undefined : "Where this may be binned"}
          >
            <Select
              value={text("storage_condition")}
              onChange={(e) => set("storage_condition", e.target.value)}
            >
              {STORAGE.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </Select>
          </Field>
        </FormGrid>

        {coldChain && (
          <div className="flex items-start gap-2.5 rounded-lg border border-info/20 bg-info-soft px-3 py-2.5">
            <Snowflake className="mt-0.5 size-4 shrink-0 text-info" />
            <p className="text-[13px] text-ink-soft">
              This product will only be accepted into cold-chain bins. Putting it on
              an ambient shelf is rejected by the server.
            </p>
          </div>
        )}

        <Field
          label="How it reaches a branch"
          error={err("sourcing_policy")}
          hint={SOURCING.find((s) => s.value === text("sourcing_policy"))?.hint}
        >
          <Select
            value={text("sourcing_policy")}
            onChange={(e) => set("sourcing_policy", e.target.value)}
          >
            {SOURCING.map((s) => (
              <option key={s.value} value={s.value}>
                {s.label}
              </option>
            ))}
          </Select>
        </Field>

        <FormGrid>
          <Field label="HSN code" error={err("hsn_code")} hint="For GST reporting">
            <Input
              value={text("hsn_code")}
              onChange={(e) => set("hsn_code", e.target.value)}
              placeholder="30049099"
            />
          </Field>
          <Field label="GST rate %" error={err("gst_rate")}>
            <Input
              type="number"
              inputMode="decimal"
              value={text("gst_rate")}
              onChange={(e) => set("gst_rate", e.target.value)}
            />
          </Field>
          <Field label="MRP ₹" error={err("mrp")}>
            <Input
              type="number"
              inputMode="decimal"
              value={text("mrp")}
              onChange={(e) => set("mrp", e.target.value)}
              placeholder="32.00"
            />
          </Field>
          <Field label="Barcode" error={err("barcode")}>
            <Input
              value={text("barcode")}
              onChange={(e) => set("barcode", e.target.value)}
              placeholder="8901234567890"
            />
          </Field>
          <Field
            label="Reorder point"
            error={err("reorder_point")}
            hint="Flagged on the dashboard below this"
          >
            <Input
              type="number"
              inputMode="decimal"
              value={text("reorder_point")}
              onChange={(e) => set("reorder_point", e.target.value)}
            />
          </Field>
          <Field label="Safety stock days" error={err("safety_stock_days")}>
            <Input
              type="number"
              value={text("safety_stock_days")}
              onChange={(e) => set("safety_stock_days", e.target.value)}
            />
          </Field>
        </FormGrid>

        <Field label="Notes" error={err("description")}>
          <Textarea
            value={text("description")}
            onChange={(e) => set("description", e.target.value)}
            placeholder="Anything worth knowing about this product"
          />
        </Field>
      </div>
    </Modal>
  );
}
