/**
 * The reference data everything else points at: who you buy from, who you
 * sell to, and where stock lives.
 *
 * All three behave the same way on purpose — the code is fixed, everything
 * else is editable, and retiring hides a record from new documents without
 * touching the history that names it. Retired rows are hidden by default
 * because a picker full of dead suppliers is worse than one that is short.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Archive, ArchiveRestore, Pencil, Plus, Search, X } from "lucide-react";
import { api, qs } from "@/lib/api";
import { useDebounced } from "@/lib/hooks";
import { useAuth } from "@/lib/auth";
import { cn, money } from "@/lib/format";
import { stateName } from "@/lib/states";
import type { Bin, Category, Customer, Supplier, Uom, Warehouse } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { ConfirmDialog } from "@/components/confirm";
import { Badge, Button, Card } from "@/components/ui";
import { Select } from "@/components/ui";
import { SupplierForm } from "@/forms/SupplierForm";
import { BinForm, CategoryForm, UomForm } from "@/forms/ClassificationForms";
import { CustomerForm } from "@/forms/CustomerForm";
import { WarehouseForm } from "@/forms/WarehouseForm";

type TabKey =
  | "suppliers"
  | "customers"
  | "retail"
  | "warehouses"
  | "bins"
  | "categories"
  | "uoms";

//: Institutional and retail buyers are two lists, not one filtered list.
//: They are different in every way that matters on this screen — an
//: institution has a GSTIN, a credit limit and a purchasing contact, a retail
//: buyer settles at the counter and often has none of those — and they grow at
//: completely different rates. A chain accumulates dozens of institutions and
//: thousands of walk-ins, so one combined list buries the twenty rows anybody
//: actually maintains under everyone who ever bought a strip of paracetamol.
const TABS: { key: TabKey; label: string }[] = [
  { key: "suppliers", label: "Distributors" },
  { key: "customers", label: "Institutions" },
  { key: "retail", label: "Retail buyers" },
  { key: "warehouses", label: "Locations" },
  { key: "bins", label: "Bins" },
  { key: "categories", label: "Categories" },
  { key: "uoms", label: "Units" },
];

//: Tabs whose list is long enough to be worth searching. The rest — locations,
//: bins, categories, units — are short by nature, and a search box over eight
//: rows is furniture.
const SEARCHABLE: TabKey[] = ["suppliers", "customers", "retail"];

/** Any master record shares just enough shape for the retire dialog. */
interface Retirable {
  id: number;
  code: string;
  name: string;
  is_active: boolean;
}

/** Bins carry a code but no name; categories a name but no code. */
const asRetirable = (r: Bin): Retirable => ({ ...r, name: `bin ${r.code}` });
const categoryAsRetirable = (c: Category): Retirable => ({ ...c, code: c.name });

/** Name over code, struck through once retired — identical on all three tabs. */
function Identity({ row, sub }: { row: Retirable; sub?: string | null }) {
  return (
    <div className="min-w-0">
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            "truncate font-medium",
            row.is_active ? "text-ink" : "text-ink-faint line-through",
          )}
        >
          {row.name}
        </span>
        {!row.is_active && <Badge tone="neutral">Retired</Badge>}
      </div>
      <p className="truncate text-[11px] text-ink-faint">
        <span className="font-mono">{row.code}</span>
        {sub && ` · ${sub}`}
      </p>
    </div>
  );
}

export function MasterData() {
  const { can } = useAuth();
  const [params, setParams] = useSearchParams();
  const [showRetired, setShowRetired] = useState(false);
  const [term, setTerm] = useState("");

  // Checked against the real list, not cast. `?tab=` comes off the URL bar,
  // which means it comes from anywhere — a stale bookmark, a typo, a link
  // someone edited by hand. Casting an unknown string to TabKey satisfies the
  // compiler and then indexes the lookup below with a key that isn't there,
  // so `active` is undefined and reading `active.columns` throws mid-render.
  // A bad query string should land on the default tab, not blank the app.
  const requested = params.get("tab");
  const tab: TabKey =
    TABS.find((t) => t.key === requested)?.key ?? "suppliers";
  const manage = can("master.manage");

  // One dialog serves all three tabs; the path is built from the active one.
  const [retiring, setRetiring] = useState<Retirable | null>(null);
  const [editing, setEditing] = useState<Retirable | null>(null);
  const [formOpen, setFormOpen] = useState(false);

  const closeForm = () => {
    setFormOpen(false);
    setEditing(null);
  };

  const openNew = () => {
    setEditing(null);
    setFormOpen(true);
  };

  const openEdit = (row: Retirable) => {
    setEditing(row);
    setFormOpen(true);
  };

  // Debounced, so typing "nursing" is one request rather than seven. The
  // undebounced value stays in the input, so the box never lags behind the
  // keyboard while the list catches up.
  const search = useDebounced(term.trim(), 300);

  const common = { is_active: showRetired ? undefined : true };
  const filter = qs(common);

  const suppliers = useQuery({
    queryKey: ["suppliers", { showRetired, search }],
    queryFn: () =>
      api.get<Supplier[]>(`/api/v1/suppliers${qs({ ...common, q: search })}`),
    enabled: tab === "suppliers",
  });

  // Two queries against one endpoint rather than one query filtered in the
  // browser: `is_institutional` decides which list a row belongs to, and a
  // retail list that fetched every institution to discard them would get
  // slower for a reason nobody could see.
  const customers = useQuery({
    queryKey: ["customers", { institutional: true, showRetired, search }],
    queryFn: () =>
      api.get<Customer[]>(
        `/api/v1/customers${qs({ ...common, q: search, is_institutional: true })}`,
      ),
    enabled: tab === "customers",
  });
  const retail = useQuery({
    queryKey: ["customers", { institutional: false, showRetired, search }],
    queryFn: () =>
      api.get<Customer[]>(
        `/api/v1/customers${qs({ ...common, q: search, is_institutional: false })}`,
      ),
    enabled: tab === "retail",
  });
  const warehouses = useQuery({
    queryKey: ["warehouses", { showRetired }],
    queryFn: () => api.get<Warehouse[]>(`/api/v1/warehouses${filter}`),
    enabled: tab === "warehouses",
  });

  // Bins belong to one location, so this tab needs a location before it can
  // show anything. Default to whichever comes first — the central warehouse,
  // since the list is ordered central-first.
  const allWarehouses = useQuery({
    queryKey: ["warehouses", "all-active"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses?is_active=true"),
    enabled: tab === "bins",
  });
  const binWarehouseId =
    Number(params.get("warehouse")) || allWarehouses.data?.[0]?.id || 0;

  const bins = useQuery({
    queryKey: ["bins", { binWarehouseId, showRetired }],
    queryFn: () =>
      api.get<Bin[]>(`/api/v1/warehouses/${binWarehouseId}/bins${filter}`),
    enabled: tab === "bins" && binWarehouseId > 0,
  });
  const categories = useQuery({
    queryKey: ["categories", { showRetired }],
    queryFn: () => api.get<Category[]>(`/api/v1/categories${filter}`),
    enabled: tab === "categories",
  });
  const uoms = useQuery({
    queryKey: ["uoms"],
    queryFn: () => api.get<Uom[]>("/api/v1/uoms"),
    enabled: tab === "uoms",
  });

  /** Edit + retire, shared by all three tables. */
  const actionsColumn = <T extends Retirable>(): Column<T>[] =>
    manage
      ? [
          {
            key: "actions",
            header: "",
            numeric: true,
            card: "actions",
            width: "9rem",
            render: (row: T) => (
              <div className="flex items-center justify-end gap-0.5">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Edit ${row.name}`}
                  onClick={() => openEdit(row)}
                  className="px-2"
                >
                  <Pencil className="size-3.5" />
                  <span className="md:hidden">Edit</span>
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={
                    row.is_active ? `Retire ${row.name}` : `Restore ${row.name}`
                  }
                  onClick={() => setRetiring(row)}
                  className="px-2"
                >
                  {row.is_active ? (
                    <Archive className="size-3.5" />
                  ) : (
                    <ArchiveRestore className="size-3.5" />
                  )}
                  <span className="md:hidden">
                    {row.is_active ? "Retire" : "Restore"}
                  </span>
                </Button>
              </div>
            ),
          },
        ]
      : [];

  const supplierColumns: Column<Supplier>[] = [
    {
      key: "name",
      header: "Distributor",
      card: "primary",
      render: (row) => <Identity row={row} sub={row.contact_person} />,
    },
    {
      key: "gstin",
      header: "GSTIN",
      hideBelow: "lg",
      card: "secondary",
      render: (row) =>
        row.gstin ? (
          <span className="font-mono text-[13px]">{row.gstin}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "state",
      header: "State",
      hideBelow: "xl",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{stateName(row.state_code)}</span>
      ),
    },
    {
      key: "phone",
      header: "Phone",
      hideBelow: "xl",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{row.phone ?? "—"}</span>
      ),
    },
    {
      key: "terms",
      header: "Terms",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {row.payment_terms_days} days
        </span>
      ),
    },
    ...actionsColumn<Supplier>(),
  ];

  // No "Type" column: the tab already says which of the two lists this is, and
  // a badge repeating it on every row of a list that cannot contain the other
  // kind is a column of the same word.
  const customerColumns: Column<Customer>[] = [
    {
      key: "name",
      header: tab === "retail" ? "Buyer" : "Institution",
      card: "primary",
      render: (row) => <Identity row={row} />,
    },
    {
      key: "phone",
      header: "Phone",
      card: "secondary",
      hideBelow: "lg",
      render: (row) =>
        row.phone ? (
          <span className="text-[13px] text-ink-soft">{row.phone}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "gstin",
      header: "GSTIN",
      hideBelow: "lg",
      render: (row) =>
        row.gstin ? (
          <span className="font-mono text-[13px]">{row.gstin}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "state",
      header: "State",
      hideBelow: "xl",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{stateName(row.state_code)}</span>
      ),
    },
    {
      key: "credit",
      header: "Credit limit",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {Number(row.credit_limit) > 0 ? money(row.credit_limit) : "—"}
        </span>
      ),
    },
    ...actionsColumn<Customer>(),
  ];

  const warehouseColumns: Column<Warehouse>[] = [
    {
      key: "name",
      header: "Location",
      card: "primary",
      render: (row) => <Identity row={row} />,
    },
    {
      key: "role",
      header: "Role",
      card: "secondary",
      render: (row) => (
        <Badge tone={row.is_central ? "brand" : "neutral"}>
          {row.is_central ? "Central" : "Branch"}
        </Badge>
      ),
    },
    {
      key: "state",
      header: "State",
      hideBelow: "lg",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{stateName(row.state_code)}</span>
      ),
    },
    {
      key: "address",
      header: "Address",
      hideBelow: "xl",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{row.address ?? "—"}</span>
      ),
    },
    ...actionsColumn<Warehouse>(),
  ];

  const binColumns: Column<Bin>[] = [
    {
      key: "code",
      header: "Bin",
      card: "primary",
      render: (row) => (
        <div className="flex items-center gap-1.5">
          <span
            className={cn(
              "truncate font-mono font-medium",
              row.is_active ? "text-ink" : "text-ink-faint line-through",
            )}
          >
            {row.code}
          </span>
          {!row.is_active && <Badge tone="neutral">Retired</Badge>}
        </div>
      ),
    },
    {
      key: "zone",
      header: "Zone",
      card: "secondary",
      render: (row) => (
        <span className="font-mono text-[13px] text-ink-soft">
          {row.zone ?? "—"}
        </span>
      ),
    },
    {
      key: "kind",
      header: "Designation",
      card: "secondary",
      render: (row) => (
        <div className="flex flex-wrap gap-1">
          {row.is_cold_chain && <Badge tone="info">Cold chain</Badge>}
          {row.is_quarantine && <Badge tone="danger">Quarantine</Badge>}
          {!row.is_cold_chain && !row.is_quarantine && (
            <span className="text-[13px] text-ink-faint">Ambient</span>
          )}
        </div>
      ),
    },
    ...(manage
      ? [
          {
            key: "actions",
            header: "",
            numeric: true,
            card: "actions" as const,
            width: "9rem",
            render: (row: Bin) => (
              <div className="flex items-center justify-end gap-0.5">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Edit bin ${row.code}`}
                  onClick={() => openEdit(asRetirable(row))}
                  className="px-2"
                >
                  <Pencil className="size-3.5" />
                  <span className="md:hidden">Edit</span>
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={
                    row.is_active ? `Retire bin ${row.code}` : `Restore bin ${row.code}`
                  }
                  onClick={() => setRetiring(asRetirable(row))}
                  className="px-2"
                >
                  {row.is_active ? (
                    <Archive className="size-3.5" />
                  ) : (
                    <ArchiveRestore className="size-3.5" />
                  )}
                  <span className="md:hidden">
                    {row.is_active ? "Retire" : "Restore"}
                  </span>
                </Button>
              </div>
            ),
          } as Column<Bin>,
        ]
      : []),
  ];

  const categoryColumns: Column<Category>[] = [
    {
      key: "name",
      header: "Category",
      card: "primary",
      render: (row) => (
        <div className="flex items-center gap-1.5">
          <span
            className={cn(
              "truncate font-medium",
              row.is_active ? "text-ink" : "text-ink-faint line-through",
            )}
          >
            {row.name}
          </span>
          {!row.is_active && <Badge tone="neutral">Retired</Badge>}
        </div>
      ),
    },
    {
      key: "parent",
      header: "Parent",
      card: "secondary",
      render: (row) => {
        const parent = categories.data?.find((c) => c.id === row.parent_id);
        return (
          <span className="text-[13px] text-ink-soft">
            {parent?.name ?? <span className="text-ink-faint">Top level</span>}
          </span>
        );
      },
    },
    {
      key: "products",
      header: "Products",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{row.product_count}</span>
      ),
    },
    ...(manage
      ? [
          {
            key: "actions",
            header: "",
            numeric: true,
            card: "actions" as const,
            width: "9rem",
            render: (row: Category) => (
              <div className="flex items-center justify-end gap-0.5">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Edit ${row.name}`}
                  onClick={() => openEdit(categoryAsRetirable(row))}
                  className="px-2"
                >
                  <Pencil className="size-3.5" />
                  <span className="md:hidden">Edit</span>
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={row.is_active ? `Retire ${row.name}` : `Restore ${row.name}`}
                  onClick={() => setRetiring(categoryAsRetirable(row))}
                  className="px-2"
                >
                  {row.is_active ? (
                    <Archive className="size-3.5" />
                  ) : (
                    <ArchiveRestore className="size-3.5" />
                  )}
                  <span className="md:hidden">
                    {row.is_active ? "Retire" : "Restore"}
                  </span>
                </Button>
              </div>
            ),
          } as Column<Category>,
        ]
      : []),
  ];

  const uomColumns: Column<Uom>[] = [
    {
      key: "code",
      header: "Unit",
      card: "primary",
      render: (row) => (
        <span className="font-mono font-medium text-ink">{row.code}</span>
      ),
    },
    {
      key: "name",
      header: "Name",
      card: "secondary",
      render: (row) => <span className="text-[13px] text-ink-soft">{row.name}</span>,
    },
    {
      key: "products",
      header: "Products",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{row.product_count}</span>
      ),
    },
  ];

  const active = {
    suppliers: {
      query: suppliers,
      columns: supplierColumns,
      newLabel: "New distributor",
      empty: "No distributors",
      path: "suppliers",
    },
    customers: {
      query: customers,
      columns: customerColumns,
      newLabel: "New institution",
      empty: search ? `No institution matches “${search}”` : "No institutions",
      path: "customers",
    },
    retail: {
      query: retail,
      columns: customerColumns,
      newLabel: "New retail buyer",
      empty: search ? `No buyer matches “${search}”` : "No retail buyers",
      path: "customers",
    },
    warehouses: {
      query: warehouses,
      columns: warehouseColumns,
      newLabel: "New location",
      empty: "No locations",
      path: "warehouses",
    },
    bins: {
      query: bins,
      columns: binColumns,
      newLabel: "New bin",
      empty: "No bins here",
      path: "bins",
    },
    categories: {
      query: categories,
      columns: categoryColumns,
      newLabel: "New category",
      empty: "No categories",
      path: "categories",
    },
    uoms: {
      query: uoms,
      columns: uomColumns,
      newLabel: "New unit",
      empty: "No units",
      path: "uoms",
    },
  }[tab];

  // Units have no is_active column, so there is nothing to hide or restore.
  const retirable = tab !== "uoms";

  return (
    <>
      <PageHeader
        title="Master data"
        description="Distributors, customers, locations and the classifications products hang off. Records are retired, never deleted."
        actions={
          manage && (
            <Button variant="primary" onClick={openNew}>
              <Plus className="size-4" /> {active.newLabel}
            </Button>
          )
        }
      />

      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-line p-3">
          <div className="flex gap-1">
            {TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => {
                  // Cleared on the way out. Carrying it over shows the new tab
                  // filtered by something the operator typed for the old one,
                  // and an empty list is indistinguishable from no records.
                  setTerm("");
                  const next = new URLSearchParams(params);
                  next.set("tab", t.key);
                  setParams(next);
                }}
                className={cn(
                  "rounded-lg px-3 py-1.5 text-[13px] font-medium transition-colors",
                  tab === t.key
                    ? "bg-brand-soft text-brand"
                    : "text-ink-soft hover:bg-muted hover:text-ink",
                )}
              >
                {t.label}
              </button>
            ))}
          </div>

          <div className="flex items-center gap-2">
            {SEARCHABLE.includes(tab) && (
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-ink-faint" />
                <input
                  value={term}
                  onChange={(e) => setTerm(e.target.value)}
                  placeholder="Search name or GSTIN"
                  aria-label="Search this list"
                  className="h-9 w-56 rounded-lg border border-line bg-surface pl-8 pr-8 text-[13px] text-ink placeholder:text-ink-faint focus:border-line-strong focus:outline-none"
                />
                {term && (
                  <button
                    onClick={() => setTerm("")}
                    aria-label="Clear the search"
                    className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded p-1 text-ink-faint hover:bg-muted hover:text-ink"
                  >
                    <X className="size-3.5" />
                  </button>
                )}
              </div>
            )}

            {tab === "bins" && (
              <Select
                value={String(binWarehouseId)}
                onChange={(e) => {
                  const next = new URLSearchParams(params);
                  next.set("warehouse", e.target.value);
                  setParams(next);
                }}
                className="w-auto min-w-[12rem]"
              >
                {allWarehouses.data?.map((w) => (
                  <option key={w.id} value={w.id}>
                    {w.name}
                  </option>
                ))}
              </Select>
            )}

            {retirable && (
              <button
                onClick={() => setShowRetired((v) => !v)}
                className={cn(
                  "inline-flex h-9 items-center gap-1.5 rounded-lg border px-3 text-[13px] font-medium transition-colors",
                  showRetired
                    ? "border-line-strong bg-muted text-ink"
                    : "border-line bg-surface text-ink-soft hover:bg-muted",
                )}
              >
                <Archive className="size-3.5" />
                Show retired
              </button>
            )}
          </div>
        </div>

        <DataTable
          columns={active.columns as Column<never>[]}
          rows={(active.query.data ?? []) as never[]}
          rowKey={(row) => (row as Retirable).id}
          loading={active.query.isPending}
          error={active.query.error}
          onRetry={active.query.refetch}
          emptyTitle={active.empty}
          emptyDescription="Add one to start referring to it on documents."
        />
      </Card>

      <SupplierForm
        open={formOpen && tab === "suppliers"}
        supplier={(editing as Supplier | null) ?? null}
        onClose={closeForm}
      />
      <CustomerForm
        open={formOpen && (tab === "customers" || tab === "retail")}
        customer={(editing as Customer | null) ?? null}
        institutionalByDefault={tab === "customers"}
        onClose={closeForm}
      />
      <WarehouseForm
        open={formOpen && tab === "warehouses"}
        warehouse={(editing as Warehouse | null) ?? null}
        onClose={closeForm}
      />
      <BinForm
        open={formOpen && tab === "bins"}
        bin={(editing as Bin | null) ?? null}
        warehouseId={binWarehouseId}
        onClose={closeForm}
      />
      <CategoryForm
        open={formOpen && tab === "categories"}
        category={(editing as Category | null) ?? null}
        onClose={closeForm}
      />
      <UomForm open={formOpen && tab === "uoms"} onClose={closeForm} />

      <ConfirmDialog
        open={retiring !== null}
        onClose={() => setRetiring(null)}
        danger={retiring?.is_active ?? false}
        title={
          retiring?.is_active
            ? `Retire ${retiring?.name}?`
            : `Bring back ${retiring?.name}?`
        }
        description={
          retiring?.is_active
            ? "It disappears from pickers on new documents. Everything that already names it is untouched."
            : "It becomes selectable again on new documents."
        }
        confirmLabel={retiring?.is_active ? "Retire" : "Restore"}
        path={`/api/v1/${active.path}/${retiring?.id}`}
        method={retiring?.is_active ? "delete" : "patch"}
        body={retiring?.is_active ? undefined : { is_active: true }}
        invalidate={[active.path]}
      />
    </>
  );
}
