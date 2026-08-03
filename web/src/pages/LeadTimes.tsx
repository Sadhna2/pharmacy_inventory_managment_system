/**
 * Supplier lead times.
 *
 * Measured, not quoted. Every number on this screen comes from purchase orders
 * and goods receipts already in the ledger, which is why each supplier opens
 * into the actual deliveries behind its percentiles — a buyer about to phone a
 * distributor needs the order numbers, not a score.
 *
 * Two numbers do the work: the median is when to expect the goods, the p90 is
 * what to survive. The gap between them is the cover that supplier costs you,
 * and it is shown as its own column because that is the number the reorder
 * engine consumes.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  CalendarClock,
  Timer,
  TrendingDown,
  TrendingUp,
  Truck,
} from "lucide-react";
import { api, qs } from "@/lib/api";
import { cn, date, qty } from "@/lib/format";
import type { LeadTimeDetail, LeadTimeList, LeadTimeStats } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import {
  Badge,
  Card,
  CardHeader,
  ErrorState,
  Modal,
  Select,
  Spinner,
} from "@/components/ui";

/** Windows, not a free-text day count — nobody wants to type "547". */
const WINDOWS = [
  { value: 180, label: "Last 6 months" },
  { value: 365, label: "Last year" },
  { value: 730, label: "Last 2 years" },
];

type Tone = "neutral" | "ok" | "warn" | "danger" | "info";

/**
 * Rated on spread, not on speed.
 *
 * A supplier who always takes nine days is easy to plan around; one who takes
 * three days or fifteen is not, even though their median looks better. Spread
 * is what forces you to hold stock, so spread is what gets graded.
 */
function rating(stats: LeadTimeStats): { tone: Tone; label: string } {
  if (!stats.reliable) return { tone: "neutral", label: "Too few orders" };
  const spread = stats.p90_days - stats.median_days;
  if (spread <= 2 && stats.on_time_rate >= 0.9) return { tone: "ok", label: "Dependable" };
  if (spread >= 5) return { tone: "danger", label: "Erratic" };
  if (stats.on_time_rate < 0.7) return { tone: "warn", label: "Often late" };
  return { tone: "info", label: "Predictable" };
}

const pct = (value: number) => `${Math.round(value * 100)}%`;

/** Small metric block used across the summary strip and the detail modal. */
function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: Tone;
}) {
  const colour =
    tone === "danger"
      ? "text-danger"
      : tone === "warn"
        ? "text-warn"
        : tone === "ok"
          ? "text-ok"
          : "text-ink";
  return (
    <div className="min-w-0">
      <p className="text-[11px] font-medium tracking-wide text-ink-faint uppercase">
        {label}
      </p>
      <p className={cn("tnum mt-0.5 text-lg font-semibold", colour)}>{value}</p>
      {hint && <p className="mt-0.5 text-[11px] text-ink-faint">{hint}</p>}
    </div>
  );
}

/* ------------------------------------------------------------------ detail */

function SupplierDetail({
  supplierId,
  lookback,
  onClose,
}: {
  supplierId: number;
  lookback: number | "";
  onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["lead-time", supplierId, lookback],
    queryFn: () =>
      api.get<LeadTimeDetail>(
        `/api/v1/ai/lead-times/${supplierId}${qs({ lookback_days: lookback })}`,
      ),
  });

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={data?.stats.supplier_name ?? "Supplier"}
      description={data?.stats.verdict}
    >
      {isLoading ? (
        <div className="flex justify-center py-10">
          <Spinner className="size-5" />
        </div>
      ) : error ? (
        <ErrorState error={error} />
      ) : data ? (
        <div className="space-y-5">
          {/* What to do about it, before the evidence for it. */}
          <div className="rounded-lg border border-brand/20 bg-brand-soft/60 px-4 py-3">
            <div className="flex flex-wrap gap-x-8 gap-y-3">
              <Metric
                label="Order today, expect"
                value={date(data.expected_date)}
                hint={`${data.stats.median_days} days — the usual wait`}
              />
              <Metric
                label="Don't run dry before"
                value={date(data.plan_for_date)}
                hint={`${data.stats.p90_days} days — 1 order in 10 takes this long`}
              />
              <Metric
                label="Cover this costs you"
                value={`${data.safety_days} days`}
                hint="Extra stock to hold against their spread"
                tone={data.safety_days >= 5 ? "danger" : data.safety_days >= 3 ? "warn" : "ok"}
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
            <Metric label="Deliveries" value={String(data.stats.deliveries)} />
            <Metric
              label="Met promised date"
              value={pct(data.stats.on_time_rate)}
              tone={data.stats.on_time_rate >= 0.9 ? "ok" : data.stats.on_time_rate < 0.7 ? "warn" : undefined}
            />
            <Metric
              label="Fastest / slowest"
              value={`${data.stats.min_days}–${data.stats.max_days}d`}
            />
            <Metric
              label="Trend"
              value={
                data.stats.trend_days === 0
                  ? "Flat"
                  : `${data.stats.trend_days > 0 ? "+" : ""}${data.stats.trend_days}d`
              }
              hint="Recent third vs oldest"
              tone={data.stats.trend_days >= 1 ? "warn" : undefined}
            />
          </div>

          {data.products.length > 0 && (
            <div>
              <h4 className="mb-2 text-[13px] font-semibold text-ink">
                What they supply
              </h4>
              <p className="mb-2 text-[12px] text-ink-soft">
                A slow supplier matters more on a fast-moving line. These are the
                products exposed to the numbers above.
              </p>
              <div className="divide-y divide-line rounded-lg border border-line">
                {data.products.slice(0, 8).map((product) => (
                  <div
                    key={product.product_id}
                    className="flex items-baseline justify-between gap-3 px-3 py-2"
                  >
                    <span className="min-w-0 truncate text-[13px] text-ink">
                      {product.product_name}
                      <span className="ml-2 font-mono text-[11px] text-ink-faint">
                        {product.sku}
                      </span>
                    </span>
                    <span className="tnum shrink-0 text-[13px] text-ink-soft">
                      {qty(product.units)} units · {product.receipts} receipts
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}

          <div>
            <h4 className="mb-2 text-[13px] font-semibold text-ink">
              Recent deliveries
            </h4>
            <div className="scroll-x rounded-lg border border-line">
              <table className="w-full text-[13px]">
                <thead className="bg-muted/50 text-left text-[11px] tracking-wide text-ink-faint uppercase">
                  <tr>
                    <th className="px-3 py-2 font-medium">Order</th>
                    <th className="px-3 py-2 font-medium">Placed</th>
                    <th className="px-3 py-2 font-medium">Promised</th>
                    <th className="px-3 py-2 font-medium">Arrived</th>
                    <th className="px-3 py-2 text-right font-medium">Took</th>
                    <th className="px-3 py-2 text-right font-medium">vs promise</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {data.deliveries.map((delivery) => (
                    <tr key={delivery.po_id}>
                      <td className="px-3 py-1.5 font-mono text-[12px] whitespace-nowrap">
                        {delivery.po_number}
                      </td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-ink-soft">
                        {date(delivery.ordered)}
                      </td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-ink-soft">
                        {delivery.promised ? date(delivery.promised) : "—"}
                      </td>
                      <td className="px-3 py-1.5 whitespace-nowrap text-ink-soft">
                        {date(delivery.received)}
                      </td>
                      <td className="tnum px-3 py-1.5 text-right">
                        {delivery.days}d
                      </td>
                      <td className="tnum px-3 py-1.5 text-right">
                        {delivery.late_by == null ? (
                          // Nullish, not `=== null`: the field is optional in
                          // the contract, so an omitted one arrives as
                          // undefined and would otherwise fall through to the
                          // comparison below and render "undefinedd".
                          <span className="text-ink-faint">—</span>
                        ) : delivery.late_by > 0 ? (
                          <span className="text-danger">+{delivery.late_by}d</span>
                        ) : (
                          <span className="text-ok">{delivery.late_by}d</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}

/* -------------------------------------------------------------------- page */

export function LeadTimes() {
  // "" sends no window at all, so the server answers with the one set under
  // Settings — otherwise an administrator's choice is invisible to anyone who
  // opens this screen and leaves the picker alone.
  const [lookback, setLookback] = useState<number | "">("");
  const [selected, setSelected] = useState<number | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["lead-times", lookback],
    queryFn: () =>
      api.get<LeadTimeList>(`/api/v1/ai/lead-times${qs({ lookback_days: lookback })}`),
  });

  const suppliers = data?.suppliers ?? [];
  const measured = suppliers.filter((s) => s.reliable);
  const worst = measured[0];
  const totalCover = measured.reduce(
    (sum, s) => sum + (s.p90_days - s.median_days),
    0,
  );

  const columns: Column<LeadTimeStats>[] = [
    {
      key: "supplier",
      header: "Supplier",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink">
            {row.supplier_name}
          </p>
          <p className="truncate text-[11px] text-ink-faint">
            {row.deliveries} deliveries measured
          </p>
        </div>
      ),
    },
    {
      key: "rating",
      header: "Rating",
      card: "secondary",
      render: (row) => {
        const { tone, label } = rating(row);
        return <Badge tone={tone}>{label}</Badge>;
      },
    },
    {
      key: "median",
      header: "Usually",
      numeric: true,
      card: "secondary",
      render: (row) => <span>{row.median_days}d</span>,
    },
    {
      key: "p90",
      header: "Worst case (p90)",
      numeric: true,
      card: "secondary",
      render: (row) => (
        <span className={row.p90_days - row.median_days >= 5 ? "text-danger" : undefined}>
          {row.p90_days}d
        </span>
      ),
    },
    {
      key: "cover",
      header: "Cover needed",
      numeric: true,
      hideBelow: "sm",
      render: (row) => {
        const spread = Math.round((row.p90_days - row.median_days) * 10) / 10;
        return (
          <span className={spread >= 5 ? "font-medium text-danger" : "text-ink-soft"}>
            +{spread}d
          </span>
        );
      },
    },
    {
      key: "ontime",
      header: "Met promise",
      numeric: true,
      hideBelow: "md",
      render: (row) => (
        <span className={row.on_time_rate < 0.7 ? "text-warn" : "text-ink-soft"}>
          {pct(row.on_time_rate)}
        </span>
      ),
    },
    {
      key: "trend",
      header: "Trend",
      hideBelow: "lg",
      render: (row) => {
        if (row.trend_days === 0) {
          return <span className="text-[13px] text-ink-faint">Flat</span>;
        }
        const slowing = row.trend_days > 0;
        const Icon = slowing ? TrendingUp : TrendingDown;
        return (
          <span
            className={cn(
              "inline-flex items-center gap-1 text-[13px]",
              slowing ? "text-warn" : "text-ok",
            )}
            title={slowing ? "Getting slower" : "Getting faster"}
          >
            <Icon className="size-3.5" />
            {slowing ? "+" : ""}
            {row.trend_days}d
          </span>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader
        title="Supplier lead times"
        description="How long each distributor actually takes, measured from your own purchase orders and receipts."
        actions={
          <Select
            value={lookback}
            onChange={(e) =>
              setLookback(e.target.value ? Number(e.target.value) : "")
            }
            className="sm:w-auto"
            aria-label="Measurement window"
          >
            <option value="">
              {data ? `Last ${data.lookback_days} days` : "As configured"}
            </option>
            {WINDOWS.filter((w) => w.value !== data?.lookback_days).map(
              (window) => (
                <option key={window.value} value={window.value}>
                  {window.label}
                </option>
              ),
            )}
          </Select>
        }
      />

      {error ? (
        <ErrorState error={error} />
      ) : (
        <div className="space-y-4">
          {measured.length > 0 && (
            <div className="grid gap-3 sm:grid-cols-3">
              <Card className="flex items-center gap-3 px-4 py-3">
                <div className="rounded-lg bg-muted p-2">
                  <Truck className="size-4 text-ink-faint" />
                </div>
                <Metric
                  label="Suppliers measured"
                  value={String(measured.length)}
                  hint={`${suppliers.reduce((n, s) => n + s.deliveries, 0)} deliveries`}
                />
              </Card>
              <Card className="flex items-center gap-3 px-4 py-3">
                <div className="rounded-lg bg-muted p-2">
                  <Timer className="size-4 text-ink-faint" />
                </div>
                <Metric
                  label="Least predictable"
                  value={worst ? `${worst.supplier_name.split(" ")[0]}` : "—"}
                  hint={
                    worst
                      ? `${worst.median_days}d usually, ${worst.p90_days}d at worst`
                      : undefined
                  }
                  tone={worst && worst.p90_days - worst.median_days >= 5 ? "danger" : undefined}
                />
              </Card>
              <Card className="flex items-center gap-3 px-4 py-3">
                <div className="rounded-lg bg-muted p-2">
                  <CalendarClock className="size-4 text-ink-faint" />
                </div>
                <Metric
                  label="Total cover carried"
                  value={`${Math.round(totalCover * 10) / 10} days`}
                  hint="Summed across suppliers — stock held purely for their spread"
                />
              </Card>
            </div>
          )}

          <Card>
            <CardHeader
              title="By supplier"
              description="Slowest worst case first. Open one to see the orders behind its numbers."
            />
            <DataTable
              columns={columns}
              rows={suppliers}
              rowKey={(row) => row.supplier_id}
              loading={isLoading}
              onRowClick={(row) => setSelected(row.supplier_id)}
              emptyTitle="No deliveries in this window"
              emptyDescription="Lead times are measured from received purchase orders. Widen the window, or receive some goods first."
            />
          </Card>
        </div>
      )}

      {selected !== null && (
        <SupplierDetail
          supplierId={selected}
          lookback={lookback}
          onClose={() => setSelected(null)}
        />
      )}
    </>
  );
}
