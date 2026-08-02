/**
 * Create or edit a user account.
 *
 * Two rules drive the whole shape of this form:
 *
 * 1. Branch staff must be pinned to one location — their stock visibility is
 *    scoped to it, so an unassigned staff account can log in and see nothing.
 *    Admins and managers oversee the chain and must NOT be pinned.
 * 2. The email is the login identity and appears in the audit trail, so it is
 *    fixed once created. Editing it would silently rewrite who past entries
 *    point at.
 *
 * The server enforces both independently; this form just makes the rule
 * visible before the request instead of after it.
 */

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Info } from "lucide-react";
import { api } from "@/lib/api";
import type { ManagedUser, Role, Warehouse } from "@/lib/types";
import { FormError, FormGrid, useSubmit } from "@/components/form";
import { Button, Field, Input, Modal, Select } from "@/components/ui";

/**
 * Mirrors `_validate_scope` on the server.
 *
 * STAFF must have a branch. ADMIN and MANAGER must not. CUSTOMER sits in
 * between — a hospital account may be tied to the branch that serves it, or to
 * none at all — so the field is offered but not demanded.
 */
const REQUIRES_BRANCH = new Set(["STAFF"]);
const ALLOWS_BRANCH = new Set(["STAFF", "CUSTOMER"]);

export function UserForm({
  open,
  user,
  onClose,
}: {
  open: boolean;
  /** Null creates a new account. */
  user: ManagedUser | null;
  onClose: () => void;
}) {
  const editing = user !== null;
  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [roleId, setRoleId] = useState("");
  const [warehouseId, setWarehouseId] = useState("");
  const [password, setPassword] = useState("");

  const roles = useQuery({
    queryKey: ["roles"],
    queryFn: () => api.get<Role[]>("/api/v1/roles"),
    enabled: open,
  });
  const warehouses = useQuery({
    queryKey: ["warehouses", { active: true }],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses?is_active=true"),
    enabled: open,
  });

  const submit = useSubmit(
    editing ? `/api/v1/users/${user.id}` : "/api/v1/users",
    { invalidate: ["users"], onDone: onClose, method: editing ? "patch" : "post" },
  );

  useEffect(() => {
    if (!open) return;
    submit.reset();
    setEmail(user?.email ?? "");
    setFullName(user?.full_name ?? "");
    setRoleId(user ? String(user.role_id) : "");
    setWarehouseId(user?.warehouse_id ? String(user.warehouse_id) : "");
    setPassword("");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, user]);

  const role = roles.data?.find((r) => String(r.id) === roleId);
  const needsBranch = role ? REQUIRES_BRANCH.has(role.code) : false;
  const canHaveBranch = role ? ALLOWS_BRANCH.has(role.code) : false;

  const ready =
    fullName.trim() &&
    roleId &&
    (!needsBranch || warehouseId) &&
    (editing || (email.trim() && password.length >= 8));

  const save = () => {
    const body: Record<string, unknown> = {
      full_name: fullName.trim(),
      role_id: Number(roleId),
      // Explicitly null rather than omitted: promoting a branch pharmacist to
      // manager has to clear the branch, and an absent key would leave it.
      warehouse_id: canHaveBranch && warehouseId ? Number(warehouseId) : null,
    };
    if (!editing) {
      body.email = email.trim();
      body.password = password;
    }
    submit.mutate(body);
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title={editing ? `Edit ${user.full_name}` : "New user"}
      description={
        editing
          ? "The email address is fixed — it is what the audit trail points at."
          : "They sign in with this email and the password you set here."
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
            {editing ? "Save changes" : "Create user"}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FormError message={submit.message} />

        <FormGrid>
          <Field label="Full name" required error={submit.fieldErrors.full_name}>
            <Input
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              placeholder="Kavita Menon"
            />
          </Field>

          <Field
            label="Email"
            required={!editing}
            error={submit.fieldErrors.email}
            hint={editing ? "Cannot be changed" : "This is their login"}
          >
            <Input
              type="email"
              value={email}
              disabled={editing}
              onChange={(e) => setEmail(e.target.value)}
              placeholder="kavita@pharmacy.co.in"
            />
          </Field>
        </FormGrid>

        <FormGrid>
          <Field label="Role" required error={submit.fieldErrors.role_id}>
            <Select
              value={roleId}
              onChange={(e) => {
                setRoleId(e.target.value);
                const next = roles.data?.find((r) => String(r.id) === e.target.value);
                if (next && !ALLOWS_BRANCH.has(next.code)) setWarehouseId("");
              }}
            >
              <option value="">Select a role…</option>
              {roles.data?.map((r) => (
                <option key={r.id} value={r.id}>
                  {r.name}
                </option>
              ))}
            </Select>
          </Field>

          <Field
            label="Location"
            required={needsBranch}
            error={submit.fieldErrors.warehouse_id}
            hint={
              !role
                ? "Depends on the role"
                : needsBranch
                  ? "They see stock at this branch only"
                  : canHaveBranch
                    ? "Optional — the branch that serves them"
                    : "Not applicable — this role covers the whole chain"
            }
          >
            <Select
              value={warehouseId}
              disabled={!canHaveBranch}
              onChange={(e) => setWarehouseId(e.target.value)}
            >
              <option value="">
                {needsBranch ? "Select a location…" : canHaveBranch ? "None" : "Whole chain"}
              </option>
              {warehouses.data?.map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
          </Field>
        </FormGrid>

        {!editing && (
          <Field
            label="Initial password"
            required
            error={submit.fieldErrors.password}
            hint="At least 8 characters. Ask them to change it after first sign-in."
          >
            <Input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoComplete="new-password"
            />
          </Field>
        )}

        {/* What the role grants, spelled out. Assigning "Manager" without
            knowing it carries po.approve is how separation of duties quietly
            stops meaning anything. */}
        {role && (
          <div className="rounded-lg border border-line bg-muted/50 px-3 py-2.5">
            <p className="mb-1.5 flex items-center gap-1.5 text-[13px] font-medium text-ink">
              <Info className="size-3.5 text-ink-faint" />
              {role.name} can do {role.permissions.length} things
            </p>
            <div className="flex flex-wrap gap-1">
              {role.permissions.map((p) => (
                <span
                  key={p}
                  className="rounded bg-surface px-1.5 py-0.5 font-mono text-[10px] text-ink-soft ring-1 ring-line ring-inset"
                >
                  {p}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>
    </Modal>
  );
}
