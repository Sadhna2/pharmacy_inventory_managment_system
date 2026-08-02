/**
 * Create or edit a distributor.
 *
 * The code is fixed once created: purchase orders name it, so changing it
 * would silently rewrite what past documents appear to refer to. Everything a
 * distributor can actually change about itself — GSTIN, contact, terms — is
 * editable, because those change in real life and re-keying a whole supplier
 * to fix a phone number is how duplicate records get born.
 */

import { useEffect, useState } from "react";
import { AlertTriangle } from "lucide-react";
import type { Supplier } from "@/lib/types";
import { FormError, FormGrid, useSubmit } from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";
import { STATES } from "@/lib/states";

export function SupplierForm({
  open,
  supplier,
  onClose,
}: {
  open: boolean;
  /** Null creates a new distributor. */
  supplier: Supplier | null;
  onClose: () => void;
}) {
  const editing = supplier !== null;
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [gstin, setGstin] = useState("");
  const [stateCode, setStateCode] = useState("MH");
  const [contact, setContact] = useState("");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [address, setAddress] = useState("");
  const [terms, setTerms] = useState("30");

  const submit = useSubmit(
    editing ? `/api/v1/suppliers/${supplier.id}` : "/api/v1/suppliers",
    { invalidate: ["suppliers"], onDone: onClose, method: editing ? "patch" : "post" },
  );

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setCode(supplier?.code ?? "");
    setName(supplier?.name ?? "");
    setGstin(supplier?.gstin ?? "");
    setStateCode(supplier?.state_code ?? "MH");
    setContact(supplier?.contact_person ?? "");
    setPhone(supplier?.phone ?? "");
    setEmail(supplier?.email ?? "");
    setAddress(supplier?.address ?? "");
    setTerms(String(supplier?.payment_terms_days ?? 30));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, supplier]);

  // GSTIN is exactly 15 characters; a partial one is a typo, not a value.
  const gstinValid = gstin.trim() === "" || gstin.trim().length === 15;
  const ready = name.trim() && (editing || code.trim()) && gstinValid;
  const stateChanged = editing && stateCode !== supplier.state_code;

  const save = () => {
    const body: Record<string, unknown> = {
      name: name.trim(),
      gstin: gstin.trim() || null,
      state_code: stateCode,
      contact_person: contact.trim() || null,
      phone: phone.trim() || null,
      email: email.trim() || null,
      address: address.trim() || null,
      payment_terms_days: Number(terms || 0),
    };
    if (!editing) body.code = code.trim();
    submit.mutate(body);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={editing ? `Edit ${supplier.name}` : "New distributor"}
      description={
        editing
          ? "Past purchase orders keep the details they were raised with."
          : "Who you buy from. The code cannot be changed later."
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
            {editing ? "Save changes" : "Create distributor"}
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
            hint={editing ? "Fixed — purchase orders refer to it" : "Short, unique"}
          >
            <Input
              value={code}
              disabled={editing}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="DIST-004"
            />
          </Field>

          <Field label="Name" required error={submit.fieldErrors.name}>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Meridian Healthcare Distributors"
            />
          </Field>

          <Field
            label="GSTIN"
            error={submit.fieldErrors.gstin}
            hint={!gstinValid ? "A GSTIN is exactly 15 characters" : undefined}
          >
            <Input
              value={gstin}
              onChange={(e) => setGstin(e.target.value.toUpperCase())}
              placeholder="27AABCM1234K1Z9"
              className={!gstinValid ? "border-danger" : undefined}
            />
          </Field>

          <Field
            label="State"
            required
            error={submit.fieldErrors.state_code}
            hint="Decides IGST versus CGST + SGST on new orders"
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

          <Field label="Contact person">
            <Input
              value={contact}
              onChange={(e) => setContact(e.target.value)}
              placeholder="Nikhil Shah"
            />
          </Field>

          <Field label="Phone">
            <Input
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+91 98200 11223"
            />
          </Field>

          <Field label="Email" error={submit.fieldErrors.email}>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="orders@meridianhc.co.in"
            />
          </Field>

          <Field
            label="Payment terms"
            error={submit.fieldErrors.payment_terms_days}
            hint="Days from invoice"
          >
            <Input
              type="number"
              inputMode="numeric"
              value={terms}
              onChange={(e) => setTerms(e.target.value)}
            />
          </Field>
        </FormGrid>

        <Field label="Address">
          <Input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Warehouse 4, MIDC Andheri East, Mumbai 400093"
          />
        </Field>

        {stateChanged && (
          <div className="flex items-start gap-2.5 rounded-lg border border-warn/25 bg-warn-soft px-3 py-2.5">
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warn" />
            <p className="text-[13px] text-warn">
              Moving this distributor from {supplier.state_code} to {stateCode}{" "}
              changes the GST split on <em>future</em> orders. Orders already
              raised keep the regime they were created under.
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}
