/**
 * Create or edit a location — the central warehouse or a branch.
 *
 * A location is not just a label: stock physically sits in it, staff are
 * pinned to it, and its state code decides the GST split on everything that
 * moves in or out. So the code is fixed once created, and the "central"
 * flag is called out rather than buried, because it changes how the whole
 * replenishment story reads.
 */

import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import type { Warehouse } from "@/lib/types";
import { FormError, FormGrid, useSubmit } from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";
import { STATES } from "@/lib/states";

export function WarehouseForm({
  open,
  warehouse,
  onClose,
}: {
  open: boolean;
  /** Null creates a new location. */
  warehouse: Warehouse | null;
  onClose: () => void;
}) {
  const editing = warehouse !== null;
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [isCentral, setIsCentral] = useState(false);
  const [stateCode, setStateCode] = useState("MH");
  const [gstin, setGstin] = useState("");
  const [address, setAddress] = useState("");

  const submit = useSubmit(
    editing ? `/api/v1/warehouses/${warehouse.id}` : "/api/v1/warehouses",
    { invalidate: ["warehouses"], onDone: onClose, method: editing ? "patch" : "post" },
  );

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setCode(warehouse?.code ?? "");
    setName(warehouse?.name ?? "");
    setIsCentral(warehouse?.is_central ?? false);
    setStateCode(warehouse?.state_code ?? "MH");
    setGstin(warehouse?.gstin ?? "");
    setAddress(warehouse?.address ?? "");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, warehouse]);

  const ready = name.trim() && (editing || code.trim());
  // A GSTIN opens with its state's numeric code, so the form can say which
  // digits to expect rather than waiting for the server to reject the paste.
  const gstinPrefix = STATES.find((s) => s.code === stateCode)?.gstPrefix;
  const stateChanged = editing && stateCode !== warehouse.state_code;

  const save = () => {
    const body: Record<string, unknown> = {
      name: name.trim(),
      is_central: isCentral,
      state_code: stateCode,
      gstin: gstin.trim() || null,
      address: address.trim() || null,
    };
    if (!editing) body.code = code.trim();
    submit.mutate(body);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={editing ? `Edit ${warehouse.name}` : "New location"}
      description={
        editing
          ? "Stock already here is unaffected."
          : "A branch or the central warehouse. The code cannot be changed later."
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
            {editing ? "Save changes" : "Create location"}
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
            hint={editing ? "Fixed — documents refer to it" : "Short, unique"}
          >
            <Input
              value={code}
              disabled={editing}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="BR-THN"
            />
          </Field>

          <Field label="Name" required error={submit.fieldErrors.name}>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Thane Branch"
            />
          </Field>

          <Field
            label="Role"
            hint="Central receives bulk and redistributes to branches"
          >
            <Select
              value={isCentral ? "central" : "branch"}
              onChange={(e) => setIsCentral(e.target.value === "central")}
            >
              <option value="branch">Branch — dispenses to customers</option>
              <option value="central">Central warehouse</option>
            </Select>
          </Field>

          <Field
            label="State"
            required
            error={submit.fieldErrors.state_code}
            hint="Decides IGST versus CGST + SGST on new documents"
          >
            <Select
              value={stateCode}
              onChange={(e) => setStateCode(e.target.value)}
            >
              {STATES.map((s) => (
                <option key={s.code} value={s.code}>
                  {s.code} — {s.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="GSTIN"
            error={submit.fieldErrors.gstin}
            hint={
              gstinPrefix
                ? `Registration for ${stateCode} — starts ${gstinPrefix}`
                : "This branch's own GST registration"
            }
          >
            <Input
              value={gstin}
              onChange={(e) => setGstin(e.target.value.toUpperCase())}
              placeholder={gstinPrefix ? `${gstinPrefix}AABCS9876P1Z_` : "15 characters"}
            />
          </Field>
        </FormGrid>

        {/*
          Said here rather than left to the server's refusal, because the
          mistake is easy and the reason is not obvious: GST registers per
          state, so a branch in another state is a separately registered
          person. Pasting head office's number here produces a perfectly valid
          GSTIN that belongs to somewhere else, and the invoice it prints
          contradicts the state printed beside it.
        */}
        {!gstin.trim() && (
          <p className="text-[12px] text-ink-faint">
            Leave blank and invoices from this branch use the firm's configured
            GSTIN. That is right for a chain trading in one state only.
          </p>
        )}

        <Field label="Address">
          <Input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Ghodbunder Road, Thane West 400607"
          />
        </Field>

        {stateChanged && (
          <div className="flex items-start gap-2.5 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2.5">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
            <p className="text-[13px] text-warn">
              Moving this location from {warehouse.state_code} to {stateCode}{" "}
              changes the GST split on <em>future</em> documents. Stock already
              here and orders already raised are untouched.
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}
