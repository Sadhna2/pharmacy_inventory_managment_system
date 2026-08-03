import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, Undo2 } from "lucide-react";
import { api, qs } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useDebounced } from "@/lib/hooks";
import { cn, date, money, num, qty, relativeDays } from "@/lib/format";
import type { Balance, Movement, Page, Warehouse } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { ConfirmDialog } from "@/components/confirm";
import { Badge, Button, Card, Input, Select, StatusBadge } from "@/components/ui";

/**
 * Expiry presets, with a date of your own at the end.
 *
 * Nobody stands at a shelf and computes a cut-off date. They ask "what goes
 * bad this month" — and, separately, "what is already dead and needs writing
 * off". The last one is its own filter because expired stock is not a longer
 * window, it is the opposite question.
 *
 * The presets cover the shelf. They do not cover the calendar: a quarter end,
 * an audit date, the day a tender closes. `custom` is for those, and it sends
 * the chosen date to the server as a date — converting it to a day count here
 * would resolve "today" in the browser's timezone and the server would resolve
 * it again in its own, which is an off-by-one nobody would ever look for.
 */
const EXPIRY_WINDOWS = [
  { value: "", label: "Any expiry" },
  { value: "30", label: "Expiring ≤ 30 days" },
  { value: "60", label: "Expiring ≤ 60 days" },
  { value: "90", label: "Expiring ≤ 90 days" },
  { value: "180", label: "Expiring ≤ 6 months" },
  { value: "expired", label: "Already expired" },
  { value: "custom", label: "Expiring before…" },
] as const;

export function Stock() {
  const [warehouse, setWarehouse] = useState("");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [expiry, setExpiry] = useState("");
  const [expiryBefore, setExpiryBefore] = useState("");
  const [page, setPage] = useState(1);

  // A chosen-but-empty date would otherwise drop the filter silently and show
  // every batch under a control that says it is narrowing them.
  const custom = expiry === "custom";
  const preset = custom || expiry === "expired" ? "" : expiry;

  // Typing sends a request per keystroke otherwise. 300ms is long enough to
  // finish a batch code and short enough that it still feels live.
  const q = useDebounced(search, 300);

  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
  });

  const { data, isLoading } = useQuery({
    queryKey: ["balances", { warehouse, status, q, expiry, expiryBefore, page }],
    queryFn: () =>
      api.get<Page<Balance>>(
        `/api/v1/stock/balances${qs({
          warehouse_id: warehouse,
          status,
          q,
          expiry_within_days: preset,
          expiry_before: custom ? expiryBefore : "",
          expired: expiry === "expired" ? "true" : "",
          page,
          size: 25,
        })}`,
      ),
  });

  const columns: Column<Balance>[] = [
    {
      key: "product",
      header: "Product",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-medium text-ink">{row.product_name}</p>
          <p className="truncate font-mono text-[11px] text-ink-faint">{row.sku}</p>
        </div>
      ),
    },
    {
      key: "location",
      header: "Location",
      card: "secondary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] text-ink">{row.warehouse_name}</p>
          {row.bin_code && (
            <p className="truncate font-mono text-[11px] text-ink-faint">
              {row.bin_code}
            </p>
          )}
        </div>
      ),
    },
    {
      key: "batch",
      header: "Batch",
      hideBelow: "md",
      render: (row) =>
        row.lot_code ? (
          <div className="min-w-0">
            <p className="truncate font-mono text-[13px] text-ink">{row.lot_code}</p>
            {row.expiry_date && (
              <p
                className={cn(
                  "truncate text-[11px]",
                  isNearExpiry(row.expiry_date) ? "text-warn" : "text-ink-faint",
                )}
              >
                exp {date(row.expiry_date)}
              </p>
            )}
          </div>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "expiry",
      header: "Expires",
      hideBelow: "xl",
      render: (row) =>
        row.expiry_date ? (
          <Badge tone={expiryTone(row.expiry_date)}>
            {relativeDays(daysUntil(row.expiry_date))}
          </Badge>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      // Per batch, not per product: MRP is printed on the pack and is a legal
      // ceiling for that pack, so two batches on the same shelf can carry
      // different prices and each must be sold at its own.
      key: "mrp",
      header: "MRP",
      numeric: true,
      hideBelow: "lg",
      render: (row) =>
        row.mrp ? (
          <span className="text-ink">{money(row.mrp)}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "status",
      header: "Status",
      card: "secondary",
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "reserved",
      header: "Reserved",
      numeric: true,
      hideBelow: "lg",
      render: (row) =>
        num(row.qty_reserved) > 0 ? (
          <span className="text-warn">{qty(row.qty_reserved)}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "available",
      header: "Available",
      numeric: true,
      card: "meta",
      render: (row) => (
        <span className="font-medium text-ink">{qty(row.qty_available)}</span>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Stock"
        description="Balances by location, batch and status — derived from the ledger"
      />

      <Card>
        <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
          <div className="relative w-full sm:w-auto sm:flex-1 sm:min-w-[14rem]">
            <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-ink-faint" />
            <Input
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
                setPage(1);
              }}
              placeholder="Batch code, product or SKU…"
              aria-label="Search stock"
              className="pl-9"
            />
          </div>

          <Select
            value={warehouse}
            onChange={(e) => {
              setWarehouse(e.target.value);
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

          <Select
            value={expiry}
            onChange={(e) => {
              setExpiry(e.target.value);
              setPage(1);
            }}
            aria-label="Expiry window"
            className="min-w-0 flex-1 sm:w-auto sm:flex-none sm:min-w-[11rem]"
          >
            {EXPIRY_WINDOWS.map((w) => (
              <option key={w.value} value={w.value}>
                {w.label}
              </option>
            ))}
          </Select>

          {custom && (
            <Input
              type="date"
              value={expiryBefore}
              onChange={(e) => {
                setExpiryBefore(e.target.value);
                setPage(1);
              }}
              aria-label="Expiring on or before"
              className="min-w-0 flex-1 sm:w-auto sm:flex-none sm:min-w-[10rem]"
            />
          )}

          <Select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
              setPage(1);
            }}
            className="min-w-0 flex-1 sm:w-auto sm:flex-none sm:min-w-[10rem]"
          >
            <option value="">All statuses</option>
            <option value="AVAILABLE">Available</option>
            <option value="QUARANTINE">Quarantine</option>
            <option value="IN_TRANSIT">In transit</option>
            <option value="DAMAGED">Damaged</option>
            <option value="RETURNED_PENDING">Returned — pending</option>
          </Select>
        </div>

        <DataTable
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) =>
            `${row.product_id}-${row.warehouse_id}-${row.bin_id}-${row.lot_id}-${row.status}`
          }
          loading={isLoading}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No stock here"
          emptyDescription="Nothing matches these filters."
        />
      </Card>
    </>
  );
}

const daysUntil = (iso: string) =>
  Math.ceil((new Date(iso).getTime() - Date.now()) / 86_400_000);
const isNearExpiry = (iso: string) => daysUntil(iso) <= 30;
const expiryTone = (iso: string) => {
  const days = daysUntil(iso);
  return days < 0 ? "danger" : days <= 30 ? "warn" : "neutral";
};

export function Movements() {
  const { can } = useAuth();
  const [page, setPage] = useState(1);
  const [reversing, setReversing] = useState<Movement | null>(null);

  const { data, isLoading } = useQuery({
    queryKey: ["movements", page],
    queryFn: () =>
      api.get<Page<Movement>>(
        `/api/v1/stock/movements${qs({ page, size: 25 })}`,
      ),
  });

  const columns: Column<Movement>[] = [
    {
      key: "product",
      header: "Product",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate font-medium text-ink">{row.product_name}</p>
          <p className="truncate font-mono text-[11px] text-ink-faint">{row.sku}</p>
        </div>
      ),
    },
    {
      key: "type",
      header: "Type",
      card: "secondary",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {row.movement_type
            .replace(/_/g, " ")
            .toLowerCase()
            .replace(/^./, (c) => c.toUpperCase())}
        </span>
      ),
    },
    {
      key: "location",
      header: "Location",
      hideBelow: "lg",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">{row.warehouse_name}</span>
      ),
    },
    {
      key: "batch",
      header: "Batch",
      hideBelow: "xl",
      render: (row) =>
        row.lot_code ? (
          <span className="font-mono text-[13px]">{row.lot_code}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      // Every line names the bucket it moved. A status change is two lines —
      // one leaving AVAILABLE, one entering QUARANTINE — so without this the
      // two halves of the pair look identical apart from the sign.
      key: "status",
      header: "Bucket",
      hideBelow: "lg",
      card: "secondary",
      render: (row) => <StatusBadge status={row.status} />,
    },
    {
      key: "when",
      header: "When",
      hideBelow: "md",
      card: "secondary",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {new Date(row.occurred_at).toLocaleDateString("en-IN", {
            day: "2-digit",
            month: "short",
          })}
        </span>
      ),
    },
    {
      key: "qty",
      header: "Qty",
      numeric: true,
      card: "meta",
      render: (row) => {
        const inbound = num(row.quantity) > 0;
        return (
          <span
            className={cn("font-medium", inbound ? "text-ok" : "text-ink")}
          >
            {inbound ? "+" : ""}
            {qty(row.quantity)}
          </span>
        );
      },
    },
    {
      // An entry can be corrected exactly once, and a correction cannot itself
      // be corrected — so both of those states show a label rather than a
      // button the server would only refuse.
      key: "actions",
      header: "",
      numeric: true,
      card: "actions",
      render: (row) => {
        if (row.reference_type === "REVERSAL")
          return <Badge tone="neutral">Correction</Badge>;
        if (row.reversed_by_id)
          return <Badge tone="warn">Reversed</Badge>;
        if (!can("stock.adjust")) return null;
        return (
          <Button
            size="sm"
            variant="ghost"
            onClick={(e) => {
              e.stopPropagation();
              setReversing(row);
            }}
          >
            <Undo2 className="size-3.5" /> Reverse
          </Button>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader
        title="Movements"
        description="The append-only ledger. Entries are never edited or deleted — only reversed."
      />
      <Card>
        <DataTable
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isLoading}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="No movements recorded"
          emptyDescription="Stock activity will appear here."
        />
      </Card>

      <ConfirmDialog
        open={reversing !== null}
        onClose={() => setReversing(null)}
        title="Reverse this movement"
        description="The original entry stays in the ledger. An opposite entry is added beside it."
        confirmLabel="Post reversal"
        path={`/api/v1/stock/movements/${reversing?.id}/reverse`}
        invalidate={["movements"]}
        reason={{
          label: "Why is this being reversed?",
          placeholder: "Delivery note said 300, only 30 arrived",
        }}
      >
        {reversing && (
          <div className="rounded-lg border border-line bg-muted/40 px-3 py-2.5 text-[13px]">
            <p className="font-medium text-ink">{reversing.product_name}</p>
            <p className="mt-0.5 text-ink-soft">
              {num(reversing.quantity) > 0 ? "+" : ""}
              {qty(reversing.quantity)} at {reversing.warehouse_name}
              {reversing.lot_code && ` · batch ${reversing.lot_code}`}
            </p>
            <p className="mt-1.5 text-ink-soft">
              Reversing posts{" "}
              <span className="font-medium tnum text-ink">
                {num(reversing.quantity) > 0 ? "−" : "+"}
                {qty(reversing.quantity).replace("-", "")}
              </span>{" "}
              against the same batch and location.
            </p>
          </div>
        )}
      </ConfirmDialog>
    </>
  );
}
