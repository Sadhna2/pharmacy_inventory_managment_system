/**
 * Create or edit a customer — a hospital, clinic, or the walk-in counter.
 *
 * Institutional customers are the ones that get invoiced and carry a credit
 * limit; the retail counter is a single record standing in for everyone who
 * pays at the till, so its GSTIN and credit limit stay hidden.
 */

import { useEffect, useState } from "react";
import type { Customer } from "@/lib/types";
import { FormError, FormGrid, useSubmit } from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";
import { STATES } from "@/lib/states";

export function CustomerForm({
  open,
  customer,
  institutionalByDefault = true,
  onClose,
}: {
  open: boolean;
  /** Null creates a new customer. */
  customer: Customer | null;
  /**
   * Which kind a *new* record starts as.
   *
   * The two live on separate tabs now, so the tab already answers this:
   * pressing "New retail buyer" and being handed a form defaulted to
   * institutional is the screen contradicting the button that opened it. Only
   * a default — the toggle is still there, because it is the one field that
   * decides whether a credit limit means anything.
   */
  institutionalByDefault?: boolean;
  onClose: () => void;
}) {
  const editing = customer !== null;
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [institutional, setInstitutional] = useState(true);
  const [gstin, setGstin] = useState("");
  const [stateCode, setStateCode] = useState("MH");
  const [phone, setPhone] = useState("");
  const [email, setEmail] = useState("");
  const [address, setAddress] = useState("");
  const [creditLimit, setCreditLimit] = useState("0");

  const submit = useSubmit(
    editing ? `/api/v1/customers/${customer.id}` : "/api/v1/customers",
    { invalidate: ["customers"], onDone: onClose, method: editing ? "patch" : "post" },
  );

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setCode(customer?.code ?? "");
    setName(customer?.name ?? "");
    setInstitutional(customer?.is_institutional ?? institutionalByDefault);
    setGstin(customer?.gstin ?? "");
    setStateCode(customer?.state_code ?? "MH");
    setPhone(customer?.phone ?? "");
    setEmail(customer?.email ?? "");
    setAddress(customer?.address ?? "");
    setCreditLimit(customer?.credit_limit ?? "0");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, customer]);

  const gstinValid = gstin.trim() === "" || gstin.trim().length === 15;
  const ready = name.trim() && (editing || code.trim()) && gstinValid;

  const save = () => {
    const body: Record<string, unknown> = {
      name: name.trim(),
      is_institutional: institutional,
      gstin: institutional ? gstin.trim() || null : null,
      state_code: stateCode,
      phone: phone.trim() || null,
      email: email.trim() || null,
      address: address.trim() || null,
      credit_limit: institutional ? creditLimit || "0" : "0",
    };
    if (!editing) body.code = code.trim();
    submit.mutate(body);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={editing ? `Edit ${customer.name}` : "New customer"}
      description={
        editing
          ? "Past sales orders keep the details they were raised with."
          : "Who you sell to. The code cannot be changed later."
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
            {editing ? "Save changes" : "Create customer"}
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
            hint={editing ? "Fixed — sales orders refer to it" : "Short, unique"}
          >
            <Input
              value={code}
              disabled={editing}
              onChange={(e) => setCode(e.target.value.toUpperCase())}
              placeholder="CUST-004"
            />
          </Field>

          <Field label="Name" required error={submit.fieldErrors.name}>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Sunrise Nursing Home"
            />
          </Field>

          <Field label="Type" hint="Institutional customers are invoiced on terms">
            <Select
              value={institutional ? "yes" : "no"}
              onChange={(e) => setInstitutional(e.target.value === "yes")}
            >
              <option value="yes">Institutional — hospital or clinic</option>
              <option value="no">Retail — walk-in counter</option>
            </Select>
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

          {institutional && (
            <>
              <Field
                label="GSTIN"
                error={submit.fieldErrors.gstin}
                hint={!gstinValid ? "A GSTIN is exactly 15 characters" : undefined}
              >
                <Input
                  value={gstin}
                  onChange={(e) => setGstin(e.target.value.toUpperCase())}
                  placeholder="27AACCS5678M1Z3"
                  className={!gstinValid ? "border-danger" : undefined}
                />
              </Field>

              <Field label="Credit limit ₹" error={submit.fieldErrors.credit_limit}>
                <Input
                  type="number"
                  inputMode="decimal"
                  value={creditLimit}
                  onChange={(e) => setCreditLimit(e.target.value)}
                />
              </Field>
            </>
          )}

          <Field label="Phone">
            <Input
              value={phone}
              onChange={(e) => setPhone(e.target.value)}
              placeholder="+91 22 2841 5566"
            />
          </Field>

          <Field label="Email" error={submit.fieldErrors.email}>
            <Input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="purchase@sunrisenh.in"
            />
          </Field>
        </FormGrid>

        <Field label="Address">
          <Input
            value={address}
            onChange={(e) => setAddress(e.target.value)}
            placeholder="Linking Road, Bandra West, Mumbai 400050"
          />
        </Field>
      </div>
    </Modal>
  );
}
