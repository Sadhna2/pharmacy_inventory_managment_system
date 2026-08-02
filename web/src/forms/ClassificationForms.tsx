/**
 * The three small setup records: therapeutic category, unit of measure, and
 * storage bin.
 *
 * They are grouped in one file because each is three or four fields, and each
 * carries exactly one rule worth stating in the UI:
 *
 *   Category — a tree, so it can nest, but never inside itself.
 *   UOM      — create-only; a quantity's unit cannot be redefined afterwards.
 *   Bin      — the code is the label on the shelf, so it is fixed once created.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle } from "lucide-react";
import { api } from "@/lib/api";
import type { Bin, Category, Warehouse } from "@/lib/types";
import { FormError, FormGrid, useSubmit } from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";

/* ------------------------------------------------------------- category */

export function CategoryForm({
  open,
  category,
  onClose,
}: {
  open: boolean;
  category: Category | null;
  onClose: () => void;
}) {
  const editing = category !== null;
  const [name, setName] = useState("");
  const [parentId, setParentId] = useState("");

  const categories = useQuery({
    queryKey: ["categories"],
    queryFn: () => api.get<Category[]>("/api/v1/categories?is_active=true"),
    enabled: open,
  });

  const submit = useSubmit(
    editing ? `/api/v1/categories/${category.id}` : "/api/v1/categories",
    {
      invalidate: ["categories", "products"],
      onDone: onClose,
      method: editing ? "patch" : "post",
    },
  );

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setName(category?.name ?? "");
    setParentId(category?.parent_id ? String(category.parent_id) : "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, category]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={editing ? `Edit ${category.name}` : "New category"}
      description="Therapeutic class. Categories can nest — Antibiotics → Cephalosporins."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!name.trim()}
            onClick={() =>
              submit.mutate({
                name: name.trim(),
                parent_id: parentId ? Number(parentId) : null,
              })
            }
          >
            {editing ? "Save changes" : "Create category"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <Field label="Name" required error={submit.fieldErrors.name}>
          <Input
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="Antidiabetics"
          />
        </Field>

        <Field label="Parent category" hint="Leave empty for a top-level class">
          <Select value={parentId} onChange={(e) => setParentId(e.target.value)}>
            <option value="">None — top level</option>
            {categories.data
              // A category cannot be its own parent; the server also refuses
              // deeper cycles, which the dropdown alone cannot prevent.
              ?.filter((c) => c.id !== category?.id)
              .map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
          </Select>
        </Field>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ UOM */

export function UomForm({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [code, setCode] = useState("");
  const [name, setName] = useState("");

  const submit = useSubmit("/api/v1/uoms", {
    invalidate: ["uoms", "products"],
    onDone: onClose,
  });

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setCode("");
    setName("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="New unit of measure"
      description="How a product is counted — strips, bottles, vials."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!code.trim() || !name.trim()}
            onClick={() =>
              submit.mutate({ code: code.trim(), name: name.trim() })
            }
          >
            Create unit
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <FormGrid>
          <Field label="Code" required error={submit.fieldErrors.code}>
            <Input
              value={code}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="VIAL"
              className="font-mono"
            />
          </Field>
          <Field label="Name" required error={submit.fieldErrors.name}>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Vial"
            />
          </Field>
        </FormGrid>

        <div className="flex gap-2.5 rounded-lg border border-warn/20 bg-warn-soft px-3 py-2.5">
          <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
          <p className="text-[13px] text-ink-soft">
            Units cannot be renamed or removed later. Every quantity ever
            recorded means what its unit says it means — changing STRIP to BOX
            would silently reinterpret years of stock history.
          </p>
        </div>
      </div>
    </Modal>
  );
}

/* ------------------------------------------------------------------ bin */

export function BinForm({
  open,
  bin,
  warehouseId,
  onClose,
}: {
  open: boolean;
  /** Null creates a new bin in `warehouseId`. */
  bin: Bin | null;
  warehouseId: number | null;
  onClose: () => void;
}) {
  const editing = bin !== null;
  const [code, setCode] = useState("");
  const [zone, setZone] = useState("");
  const [coldChain, setColdChain] = useState(false);
  const [quarantine, setQuarantine] = useState(false);

  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
    enabled: open,
  });
  const warehouse = warehouses.data?.find(
    (w) => w.id === (bin?.warehouse_id ?? warehouseId),
  );

  const submit = useSubmit(
    editing
      ? `/api/v1/bins/${bin.id}`
      : `/api/v1/warehouses/${warehouseId}/bins`,
    { invalidate: ["bins"], onDone: onClose, method: editing ? "patch" : "post" },
  );

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setCode(bin?.code ?? "");
    setZone(bin?.zone ?? "");
    setColdChain(bin?.is_cold_chain ?? false);
    setQuarantine(bin?.is_quarantine ?? false);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, bin]);

  const save = () => {
    const body: Record<string, unknown> = {
      zone: zone.trim() || null,
      is_cold_chain: coldChain,
      is_quarantine: quarantine,
    };
    if (!editing) body.code = code.trim();
    submit.mutate(body);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={editing ? `Edit bin ${bin.code}` : "New bin"}
      description={
        warehouse
          ? `A shelf location inside ${warehouse.name}.`
          : "A shelf location inside a warehouse."
      }
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            loading={submit.isPending}
            disabled={!editing && !code.trim()}
            onClick={save}
          >
            {editing ? "Save changes" : "Create bin"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <FormGrid>
          <Field
            label="Code"
            required={!editing}
            error={submit.fieldErrors.code}
            hint={editing ? "Cannot be changed" : "The label on the shelf, e.g. A-04"}
          >
            <Input
              value={code}
              disabled={editing}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="A-04"
              className="font-mono"
            />
          </Field>
          <Field label="Zone" hint="Optional grouping — A, B, COLD, QUAR">
            <Input
              value={zone}
              onChange={(e) => setZone(e.target.value.toUpperCase())}
              placeholder="A"
              className="font-mono"
            />
          </Field>
        </FormGrid>

        <div className="space-y-2">
          <label className="flex items-start gap-2.5 rounded-lg border border-line px-3 py-2.5">
            <input
              type="checkbox"
              checked={coldChain}
              onChange={(e) => setColdChain(e.target.checked)}
              className="mt-0.5 size-4 accent-brand"
            />
            <span className="text-[13px]">
              <span className="font-medium text-ink">Cold chain</span>
              <span className="block text-ink-soft">
                2–8 °C. Products needing refrigeration may only be binned here.
              </span>
            </span>
          </label>

          <label className="flex items-start gap-2.5 rounded-lg border border-line px-3 py-2.5">
            <input
              type="checkbox"
              checked={quarantine}
              onChange={(e) => setQuarantine(e.target.checked)}
              className="mt-0.5 size-4 accent-brand"
            />
            <span className="text-[13px]">
              <span className="font-medium text-ink">Quarantine</span>
              <span className="block text-ink-soft">
                Where recalled and damaged stock is held — never dispensed from.
              </span>
            </span>
          </label>
        </div>

        {editing && coldChain && !bin.is_cold_chain && (
          <div className="flex gap-2.5 rounded-lg border border-warn/20 bg-warn-soft px-3 py-2.5">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
            <p className="text-[13px] text-ink-soft">
              The bin has to be empty first. Stock already sitting here was
              never stored at 2–8 °C, and marking the bin cold-chain would say
              otherwise.
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}
