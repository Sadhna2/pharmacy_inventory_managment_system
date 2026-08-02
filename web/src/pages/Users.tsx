/**
 * User administration.
 *
 * The `user.manage` permission has existed since Layer 0 and guarded nothing —
 * the only way to add a pharmacist was to edit the seed script. This is the
 * screen it was always for.
 *
 * Accounts are deactivated, never deleted: their id is on every ledger row and
 * audit entry they ever created, and removing the row would orphan all of it.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { KeyRound, Search, ShieldCheck, UserPlus, UserX } from "lucide-react";
import { ApiError, api, qs } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useDebounced } from "@/lib/hooks";
import { cn, dateTime } from "@/lib/format";
import type { ManagedUser, Page, Role, Warehouse } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { ConfirmDialog } from "@/components/confirm";
import {
  Badge,
  Button,
  Card,
  Field,
  Input,
  Modal,
  Select,
} from "@/components/ui";
import { UserForm } from "@/forms/UserForm";

const ROLE_TONE = {
  ADMIN: "brand",
  MANAGER: "info",
  STAFF: "neutral",
  CUSTOMER: "warn",
} as const;

/** Admin-initiated reset. Separate from the user's own change-password flow,
 *  which requires the current password; this one is for the locked-out call. */
function ResetPassword({
  user,
  onClose,
}: {
  user: ManagedUser | null;
  onClose: () => void;
}) {
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const reset = useMutation({
    mutationFn: () =>
      api.post<{ message: string }>(`/api/v1/users/${user!.id}/password`, {
        new_password: password,
      }),
    onSuccess: (result) => setDone(result.message),
    onError: (err) =>
      setError(
        err instanceof ApiError ? err.problem.detail : "Could not reset it",
      ),
  });

  const close = () => {
    setPassword("");
    setConfirm("");
    setError(null);
    setDone(null);
    reset.reset();
    onClose();
  };

  const mismatch = confirm.length > 0 && password !== confirm;

  return (
    <Modal
      open={user !== null}
      onClose={close}
      title={done ? "Password reset" : `Reset password for ${user?.full_name}`}
      description={
        done
          ? undefined
          : "They will need this to sign in. Every session they currently have is signed out."
      }
      footer={
        done ? (
          <Button variant="primary" onClick={close}>
            Done
          </Button>
        ) : (
          <>
            <Button onClick={close}>Cancel</Button>
            <Button
              variant="primary"
              loading={reset.isPending}
              disabled={password.length < 8 || mismatch}
              onClick={() => reset.mutate()}
            >
              Reset password
            </Button>
          </>
        )
      }
    >
      {done ? (
        <p className="text-[13px] text-ink-soft">{done}</p>
      ) : (
        <div className="space-y-4">
          {error && (
            <div className="rounded-lg border border-danger/20 bg-danger-soft px-3 py-2 text-[13px] text-danger">
              {error}
            </div>
          )}
          <Field label="New password" required hint="At least 8 characters">
            <Input
              type="password"
              value={password}
              autoComplete="new-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </Field>
          <Field
            label="Confirm"
            required
            error={mismatch ? "The two do not match" : undefined}
          >
            <Input
              type="password"
              value={confirm}
              autoComplete="new-password"
              onChange={(e) => setConfirm(e.target.value)}
            />
          </Field>
        </div>
      )}
    </Modal>
  );
}

export function Users() {
  const { user: me } = useAuth();
  const qc = useQueryClient();

  const [search, setSearch] = useState("");
  const [roleId, setRoleId] = useState("");
  const [warehouseId, setWarehouseId] = useState("");
  const [showInactive, setShowInactive] = useState(false);
  const [page, setPage] = useState(1);

  const [editing, setEditing] = useState<ManagedUser | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [resetting, setResetting] = useState<ManagedUser | null>(null);
  const [toggling, setToggling] = useState<ManagedUser | null>(null);

  const q = useDebounced(search, 300);

  const roles = useQuery({
    queryKey: ["roles"],
    queryFn: () => api.get<Role[]>("/api/v1/roles"),
  });
  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
  });

  const { data, isLoading } = useQuery({
    queryKey: ["users", { q, roleId, warehouseId, showInactive, page }],
    queryFn: () =>
      api.get<Page<ManagedUser>>(
        `/api/v1/users${qs({
          q,
          role_id: roleId,
          warehouse_id: warehouseId,
          is_active: showInactive ? undefined : true,
          page,
          size: 25,
        })}`,
      ),
  });

  const columns: Column<ManagedUser>[] = [
    {
      key: "person",
      header: "Name",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <div className="flex items-center gap-1.5">
            <span
              className={cn(
                "truncate font-medium",
                row.is_active ? "text-ink" : "text-ink-faint line-through",
              )}
            >
              {row.full_name}
            </span>
            {row.id === me?.id && <Badge tone="brand">You</Badge>}
            {!row.is_active && <Badge tone="neutral">Disabled</Badge>}
          </div>
          <p className="truncate text-[11px] text-ink-faint">{row.email}</p>
        </div>
      ),
    },
    {
      key: "role",
      header: "Role",
      card: "secondary",
      render: (row) => (
        <Badge tone={ROLE_TONE[row.role_code] ?? "neutral"}>{row.role_name}</Badge>
      ),
    },
    {
      key: "location",
      header: "Location",
      card: "secondary",
      hideBelow: "md",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {row.warehouse_name ?? (
            <span className="text-ink-faint">Whole chain</span>
          )}
        </span>
      ),
    },
    {
      key: "last-login",
      header: "Last sign-in",
      hideBelow: "lg",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {row.last_login_at ? (
            dateTime(row.last_login_at)
          ) : (
            <span className="text-ink-faint">Never</span>
          )}
        </span>
      ),
    },
    {
      key: "actions",
      header: "",
      numeric: true,
      card: "actions",
      width: "11rem",
      render: (row) => (
        <div className="flex items-center justify-end gap-0.5">
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Edit ${row.full_name}`}
            onClick={() => {
              setEditing(row);
              setFormOpen(true);
            }}
            className="px-2"
          >
            <ShieldCheck className="size-3.5" />
            <span className="md:hidden">Edit</span>
          </Button>
          <Button
            variant="ghost"
            size="sm"
            aria-label={`Reset password for ${row.full_name}`}
            onClick={() => setResetting(row)}
            className="px-2"
          >
            <KeyRound className="size-3.5" />
            <span className="md:hidden">Password</span>
          </Button>
          {/* Disabling your own account locks the last door on the way out.
              The server refuses it too; hiding the button avoids the 400. */}
          {row.id !== me?.id && (
            <Button
              variant="ghost"
              size="sm"
              aria-label={
                row.is_active
                  ? `Disable ${row.full_name}`
                  : `Re-enable ${row.full_name}`
              }
              onClick={() => setToggling(row)}
              className="px-2"
            >
              <UserX className="size-3.5" />
              <span className="md:hidden">
                {row.is_active ? "Disable" : "Enable"}
              </span>
            </Button>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Users"
        description="Who can sign in, what they may do, and which branch they see"
        actions={
          <Button
            variant="primary"
            onClick={() => {
              setEditing(null);
              setFormOpen(true);
            }}
          >
            <UserPlus className="size-4" /> New user
          </Button>
        }
      />

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
          <div className="relative w-full sm:w-auto sm:flex-1 sm:min-w-[13rem]">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-faint" />
            <Input
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
              placeholder="Name or email…"
              aria-label="Search users"
              className="pl-9"
            />
          </div>

          <Select
            value={roleId}
            onChange={(e) => {
              setRoleId(e.target.value);
              setPage(1);
            }}
            className="min-w-0 flex-1 sm:w-auto sm:flex-none sm:min-w-[10rem]"
          >
            <option value="">All roles</option>
            {roles.data?.map((r) => (
              <option key={r.id} value={r.id}>
                {r.name}
              </option>
            ))}
          </Select>

          <Select
            value={warehouseId}
            onChange={(e) => {
              setWarehouseId(e.target.value);
              setPage(1);
            }}
            className="min-w-0 flex-1 sm:w-auto sm:flex-none sm:min-w-[12rem]"
          >
            <option value="">All locations</option>
            {warehouses.data?.map((w) => (
              <option key={w.id} value={w.id}>
                {w.name}
              </option>
            ))}
          </Select>

          <button
            onClick={() => {
              setShowInactive((v) => !v);
              setPage(1);
            }}
            className={cn(
              "inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-[13px] font-medium transition-colors",
              showInactive
                ? "border-line-strong bg-muted text-ink"
                : "border-line bg-surface text-ink-soft hover:bg-muted",
            )}
          >
            <UserX className="size-3.5" />
            Show disabled
          </button>
        </div>

        <DataTable
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isLoading}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No users"
          emptyDescription="Nobody matches these filters."
        />
      </Card>

      <UserForm
        open={formOpen}
        user={editing}
        onClose={() => {
          setFormOpen(false);
          setEditing(null);
          qc.invalidateQueries({ queryKey: ["users"] });
        }}
      />

      <ResetPassword user={resetting} onClose={() => setResetting(null)} />

      <ConfirmDialog
        open={toggling !== null}
        onClose={() => setToggling(null)}
        danger={toggling?.is_active ?? false}
        title={
          toggling?.is_active
            ? `Disable ${toggling?.full_name}?`
            : `Re-enable ${toggling?.full_name}?`
        }
        description={
          toggling?.is_active
            ? "They can no longer sign in. Everything they recorded stays exactly as it is."
            : "They can sign in again with their existing password."
        }
        confirmLabel={toggling?.is_active ? "Disable" : "Re-enable"}
        path={`/api/v1/users/${toggling?.id}`}
        method="patch"
        body={{ is_active: !toggling?.is_active }}
        invalidate={["users"]}
      />
    </>
  );
}
