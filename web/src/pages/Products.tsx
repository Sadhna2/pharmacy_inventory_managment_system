import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import {
  Archive,
  ArchiveRestore,
  Pencil,
  Plus,
  Search,
  Snowflake,
  Thermometer,
} from "lucide-react";
import { api, qs } from "@/lib/api";
import { cn, money, num, qty } from "@/lib/format";
import type { Page, Product } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { ConfirmDialog } from "@/components/confirm";
import { Badge, Button, Card, Input, Select } from "@/components/ui";
import { useAuth } from "@/lib/auth";
import { ProductForm } from "@/forms/ProductForm";

/** Schedule H/H1/X are prescription-controlled; X is the strictest. */
function ScheduleBadge({ schedule }: { schedule: string }) {
  if (schedule === "OTC") return <Badge>OTC</Badge>;
  const tone = schedule === "X" ? "danger" : schedule === "H1" ? "warn" : "info";
  return <Badge tone={tone}>Schedule {schedule}</Badge>;
}

function TrackingBadge({ mode }: { mode: string }) {
  const label = {
    NONE: "Quantity",
    LOT: "Batch",
    LOT_EXPIRY: "Batch + expiry",
    SERIAL: "Serial",
  }[mode];
  return <Badge tone={mode === "NONE" ? "neutral" : "brand"}>{label}</Badge>;
}

export function Products() {
  const { can } = useAuth();
  const [editing, setEditing] = useState<Product | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [params, setParams] = useSearchParams();
  const [search, setSearch] = useState("");
  const [tracking, setTracking] = useState("");
  const [page, setPage] = useState(1);
  // Retired products are hidden rather than gone; this reveals them so they
  // can be checked or brought back.
  const [showRetired, setShowRetired] = useState(false);
  const [retiring, setRetiring] = useState<Product | null>(null);

  const belowReorder = params.get("below_reorder") === "true";

  const { data, isLoading } = useQuery({
    queryKey: ["products", { search, tracking, belowReorder, showRetired, page }],
    queryFn: () =>
      api.get<Page<Product>>(
        `/api/v1/products${qs({
          q: search,
          tracking_mode: tracking,
          below_reorder: belowReorder || undefined,
          is_active: showRetired ? undefined : true,
          page,
          size: 25,
        })}`,
      ),
  });

  const columns: Column<Product>[] = [
    {
      key: "product",
      header: "Product",
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
              {row.name}
            </span>
            {row.storage_condition === "COLD_CHAIN" && (
              <Snowflake className="size-3.5 shrink-0 text-info" aria-label="Cold chain" />
            )}
            {!row.is_active && <Badge tone="neutral">Retired</Badge>}
          </div>
          <p className="truncate text-[11px] text-ink-faint">
            <span className="font-mono">{row.sku}</span>
            {row.composition && ` · ${row.composition}`}
          </p>
        </div>
      ),
    },
    {
      key: "schedule",
      header: "Schedule",
      hideBelow: "lg",
      card: "secondary",
      render: (row) => <ScheduleBadge schedule={row.drug_schedule} />,
    },
    {
      key: "tracking",
      header: "Tracking",
      hideBelow: "xl",
      render: (row) => <TrackingBadge mode={row.tracking_mode} />,
    },
    {
      key: "onhand",
      header: "On hand",
      numeric: true,
      card: "meta",
      render: (row) => {
        const onHand = num(row.qty_on_hand);
        const reorder = num(row.reorder_point);
        const low = reorder > 0 && onHand <= reorder;
        return (
          <span className={cn("font-medium", low ? "text-warn" : "text-ink")}>
            {qty(row.qty_on_hand)}
          </span>
        );
      },
    },
    {
      key: "reorder",
      header: "Reorder at",
      numeric: true,
      hideBelow: "md",
      render: (row) => (
        <span className="text-ink-soft">
          {num(row.reorder_point) > 0 ? qty(row.reorder_point) : "—"}
        </span>
      ),
    },
    {
      key: "mrp",
      header: "MRP",
      numeric: true,
      hideBelow: "lg",
      render: (row) => <span className="text-ink-soft">{money(row.mrp)}</span>,
    },
    ...(can("product.manage")
      ? [
          {
            key: "edit",
            header: "",
            width: "6rem",
            card: "actions",
            render: (row: Product) => (
              <div className="flex items-center justify-end gap-0.5">
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Edit ${row.name}`}
                  onClick={() => {
                    setEditing(row);
                    setFormOpen(true);
                  }}
                  className="px-2"
                >
                  <Pencil className="size-3.5" />
                  {/* A bare icon reads fine in a dense table; on a card it is
                      a guessing game, so the label appears there only. */}
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
          } satisfies Column<Product>,
        ]
      : []),
  ];

  return (
    <>
      <PageHeader
        title="Products"
        description="Drug master with batch tracking, schedules and storage rules"
        actions={
          can("product.manage") && (
            <Button
              variant="primary"
              onClick={() => {
                setEditing(null);
                setFormOpen(true);
              }}
            >
              <Plus className="size-4" /> New product
            </Button>
          )
        }
      />

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
          <div className="relative min-w-0 flex-1 sm:max-w-xs">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-faint" />
            <Input
              placeholder="Search name, SKU or salt…"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
              className="pl-9"
            />
          </div>

          <Select
            value={tracking}
            onChange={(e) => {
              setTracking(e.target.value);
              setPage(1);
            }}
            className="min-w-0 flex-1 sm:w-auto sm:flex-none sm:min-w-[9rem]"
          >
            <option value="">All tracking</option>
            <option value="LOT_EXPIRY">Batch + expiry</option>
            <option value="LOT">Batch only</option>
            <option value="NONE">Quantity only</option>
          </Select>

          <button
            onClick={() => {
              const next = new URLSearchParams(params);
              belowReorder
                ? next.delete("below_reorder")
                : next.set("below_reorder", "true");
              setParams(next);
              setPage(1);
            }}
            className={cn(
              "inline-flex h-9.5 items-center gap-1.5 rounded-lg border px-3 text-[13px] font-medium transition-colors",
              belowReorder
                ? "border-warn/30 bg-warn-soft text-warn"
                : "border-line bg-surface text-ink-soft hover:bg-muted",
            )}
          >
            <Thermometer className="size-3.5" />
            Below reorder
          </button>

          <button
            onClick={() => {
              setShowRetired((v) => !v);
              setPage(1);
            }}
            className={cn(
              "inline-flex h-9.5 items-center gap-1.5 rounded-lg border px-3 text-[13px] font-medium transition-colors",
              showRetired
                ? "border-line-strong bg-muted text-ink"
                : "border-line bg-surface text-ink-soft hover:bg-muted",
            )}
          >
            <Archive className="size-3.5" />
            Show retired
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
          emptyTitle="No products match"
          emptyDescription="Try a different search or clear the filters."
        />
      </Card>

      <ProductForm
        open={formOpen}
        product={editing}
        onClose={() => {
          setFormOpen(false);
          setEditing(null);
        }}
      />

      {/* Retiring is a PATCH back to active; only the outbound trip is a DELETE,
          which the API treats as "deactivate" rather than a real removal. */}
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
            ? "It disappears from new orders and searches. Every past sale and batch stays exactly as it is."
            : "It becomes selectable again on new documents."
        }
        confirmLabel={retiring?.is_active ? "Retire product" : "Restore product"}
        path={`/api/v1/products/${retiring?.id}`}
        method={retiring?.is_active ? "delete" : "patch"}
        body={retiring?.is_active ? undefined : { is_active: true }}
        invalidate={["products"]}
      >
        {retiring?.is_active && num(retiring.qty_on_hand) > 0 && (
          <div className="rounded-lg border border-warn/25 bg-warn-soft px-3 py-2.5 text-[13px] text-warn">
            There are still <span className="font-medium">{qty(retiring.qty_on_hand)}</span>{" "}
            units on the shelf. Retiring does not write them off — count or
            transfer them first if that is what you meant.
          </div>
        )}
      </ConfirmDialog>
    </>
  );
}
