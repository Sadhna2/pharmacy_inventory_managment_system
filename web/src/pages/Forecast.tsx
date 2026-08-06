/**
 * Demand forecast.
 *
 * Every series is backtested before it is shown, so this screen leads with how
 * wrong the forecast was on data it had not seen. That ordering is deliberate:
 * a forecast without an error bar is a guess with a chart attached, and a buyer
 * about to commit ₹2 lakh needs to know which of these numbers to trust.
 *
 * The methods that lost the backtest are listed too. If the moving average
 * beats Holt-Winters on a product, that is worth knowing — it usually means
 * the demand has no pattern to find.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { Activity, ChartSpline, Target } from "lucide-react";
import { api, qs } from "@/lib/api";
import { cn, qty } from "@/lib/format";
import type { DemandForecast, ForecastList, Warehouse } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import {
  Badge,
  Card,
  CardHeader,
  ErrorState,
  Modal,
  Select,
} from "@/components/ui";

type Tone = "neutral" | "ok" | "warn" | "danger" | "info";

const CONFIDENCE: Record<string, { tone: Tone; label: string }> = {
  high: { tone: "ok", label: "High" },
  medium: { tone: "warn", label: "Medium" },
  low: { tone: "danger", label: "Low" },
};

const METHOD_LABEL: Record<string, string> = {
  holt_winters: "Holt-Winters",
  seasonal_naive: "Seasonal naive",
  moving_average: "Moving average",
};

const METHOD_BLURB: Record<string, string> = {
  holt_winters: "Level, trend and weekly seasonality, with a damped trend",
  seasonal_naive: "Each weekday predicted from recent same weekdays",
  moving_average: "The last four weeks, flat — the baseline everything must beat",
};

/**
 * The blank option is the important one. It sends no horizon at all, so the
 * server answers with whatever an administrator set under Settings — which is
 * the only way the setting is visible to somebody who opens this screen and
 * never touches the picker.
 */
const HORIZONS = [
  { value: 14, label: "Next 2 weeks" },
  { value: 30, label: "Next 30 days" },
  { value: 60, label: "Next 60 days" },
];

/** WAPE as something readable: 0.18 → "off by 18% of volume". */
const wapeLabel = (wape: number) => `${Math.round(wape * 100)}%`;

/* ------------------------------------------------------------------ detail */

function Detail({
  forecast,
  onClose,
}: {
  forecast: DemandForecast;
  onClose: () => void;
}) {
  const start = new Date(forecast.start);
  const series = forecast.daily.map((value, i) => {
    const day = new Date(start);
    day.setDate(start.getDate() + i);
    return {
      label: day.toLocaleDateString("en-IN", { day: "2-digit", month: "short" }),
      forecast: value,
      // Recharts stacks an Area from a [low, high] tuple, which draws the band
      // without a second series fighting the line for the legend.
      band: [forecast.lower[i], forecast.upper[i]] as [number, number],
    };
  });

  return (
    <Modal
      open
      wide
      onClose={onClose}
      title={forecast.product_name}
      description={`${forecast.sku} · ${forecast.warehouse_name}`}
    >
      <div className="space-y-5">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {[
            { label: "Per day", value: `${qty(forecast.daily_mean)} units` },
            { label: "Over the horizon", value: `${qty(forecast.total)} units` },
            { label: "Typical error", value: `${qty(forecast.accuracy.mae)} units/day` },
            { label: "Off by", value: wapeLabel(forecast.accuracy.wape) },
          ].map((cell) => (
            <div key={cell.label}>
              <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                {cell.label}
              </p>
              <p className="tnum mt-0.5 text-[15px] font-semibold text-ink">
                {cell.value}
              </p>
            </div>
          ))}
        </div>

        <div className="h-56 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <ComposedChart data={series} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
              <CartesianGrid stroke="var(--color-line)" vertical={false} />
              <XAxis
                dataKey="label"
                tick={{ fontSize: 11, fill: "var(--color-ink-faint)" }}
                tickLine={false}
                axisLine={{ stroke: "var(--color-line)" }}
                interval="preserveStartEnd"
                minTickGap={28}
              />
              <YAxis
                tick={{ fontSize: 11, fill: "var(--color-ink-faint)" }}
                tickLine={false}
                axisLine={false}
                width={44}
              />
              <Tooltip
                contentStyle={{
                  borderRadius: 8,
                  border: "1px solid var(--color-line)",
                  fontSize: 12,
                }}
                // Recharts types this loosely (the band series hands back a
                // [low, high] tuple, the line a scalar), so the branch is on
                // the runtime shape rather than on the declared type.
                formatter={(value, name) =>
                  Array.isArray(value)
                    ? [`${qty(value[0])} – ${qty(value[1])}`, "Likely range"]
                    : [qty(value as number), name === "forecast" ? "Forecast" : String(name ?? "")]
                }
              />
              <Area
                dataKey="band"
                stroke="none"
                fill="var(--color-brand)"
                fillOpacity={0.12}
                isAnimationActive={false}
              />
              <Line
                dataKey="forecast"
                stroke="var(--color-brand)"
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            </ComposedChart>
          </ResponsiveContainer>
        </div>
        <p className="-mt-3 text-[12px] text-ink-faint">
          The band widens with distance because error accumulates — a stated
          approximation scaled on this series' own held-out error, not a formal
          prediction interval.
        </p>

        <div>
          <h4 className="mb-1.5 text-[13px] font-semibold text-ink">
            Why {METHOD_LABEL[forecast.method] ?? forecast.method}
          </h4>
          <p className="mb-2 text-[12px] text-ink-soft">
            {METHOD_BLURB[forecast.method]}. Chosen because it had the lowest error
            on {forecast.history_days} days of history, on a held-out tail it never
            saw during fitting.
          </p>
          <div className="scroll-x rounded-lg border border-line">
            <table className="w-full text-[13px]">
              <thead className="bg-muted/50 text-left text-[11px] tracking-wide text-ink-faint uppercase">
                <tr>
                  <th className="px-3 py-2 font-medium">Method</th>
                  <th className="px-3 py-2 text-right font-medium">Off by</th>
                  <th className="px-3 py-2 text-right font-medium">Error/day</th>
                  <th className="px-3 py-2 text-right font-medium">Within ±20%</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-line">
                {[forecast.accuracy, ...forecast.alternatives].map((row, index) => (
                  <tr key={row.method} className={index === 0 ? "bg-brand-soft/40" : ""}>
                    <td className="px-3 py-1.5">
                      {METHOD_LABEL[row.method] ?? row.method}
                      {index === 0 && (
                        <span className="ml-2 text-[11px] text-brand">chosen</span>
                      )}
                    </td>
                    <td className="tnum px-3 py-1.5 text-right">
                      {wapeLabel(row.wape)}
                    </td>
                    <td className="tnum px-3 py-1.5 text-right">{qty(row.mae)}</td>
                    <td className="tnum px-3 py-1.5 text-right">
                      {Math.round(row.hit_rate * 100)}%
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        {forecast.stockout_days > 0 && (
          <div className="rounded-lg border border-info/20 bg-info-soft px-4 py-3">
            <p className="text-[13px] text-info">
              {forecast.stockout_days} day
              {forecast.stockout_days > 1 && "s"} in this history had no sellable
              stock. Those were treated as unobserved rather than as zero demand —
              training on them would teach the model that running out is normal.
            </p>
          </div>
        )}
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------- page */

export function Forecast() {
  const [horizon, setHorizon] = useState<number | "">("");
  const [warehouse, setWarehouse] = useState("");
  const [selected, setSelected] = useState<DemandForecast | null>(null);

  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
  });

  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["forecast", { horizon, warehouse }],
    queryFn: () =>
      api.get<ForecastList>(
        `/api/v1/ai/forecast${qs({
          horizon_days: horizon,
          warehouse_id: warehouse,
        })}`,
      ),
  });

  const rows = data?.forecasts ?? [];
  // The horizon box is empty until someone picks one, meaning "use the default
  // set under Setup → Settings". The response says which number that turned
  // out to be, so the labels below read it from there — a hardcoded 30 here
  // would quietly disagree with the figures the moment the setting changed.
  const effectiveHorizon = horizon || data?.horizon_days;
  const reliable = rows.filter((f) => f.confidence === "high").length;
  const totalUnits = rows.reduce((sum, f) => sum + f.total, 0);
  const medianWape = rows.length
    ? [...rows].sort((a, b) => a.accuracy.wape - b.accuracy.wape)[
        Math.floor(rows.length / 2)
      ].accuracy.wape
    : 0;

  const columns: Column<DemandForecast>[] = [
    {
      key: "product",
      header: "Product",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] font-medium text-ink">
            {row.product_name}
          </p>
          <p className="truncate text-[11px] text-ink-faint">
            {row.sku} · {row.warehouse_name}
          </p>
        </div>
      ),
    },
    {
      key: "confidence",
      header: "Confidence",
      card: "secondary",
      render: (row) => {
        const meta = CONFIDENCE[row.confidence] ?? CONFIDENCE.low;
        return <Badge tone={meta.tone}>{meta.label}</Badge>;
      },
    },
    {
      key: "perday",
      header: "Per day",
      numeric: true,
      card: "secondary",
      render: (row) => <span>{qty(row.daily_mean)}</span>,
    },
    {
      key: "total",
      header: effectiveHorizon ? `Next ${effectiveHorizon}d` : "Next",
      numeric: true,
      card: "secondary",
      render: (row) => <span className="font-medium">{qty(row.total)}</span>,
    },
    {
      key: "error",
      header: "Off by",
      numeric: true,
      hideBelow: "sm",
      render: (row) => (
        <span
          className={cn(
            row.accuracy.wape > 0.45
              ? "text-danger"
              : row.accuracy.wape > 0.25
                ? "text-warn"
                : "text-ink-soft",
          )}
        >
          {wapeLabel(row.accuracy.wape)}
        </span>
      ),
    },
    {
      key: "method",
      header: "Method",
      hideBelow: "lg",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {METHOD_LABEL[row.method] ?? row.method}
        </span>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Demand forecast"
        description="Units expected per product and branch, chosen per series by whichever method did best on data it had not seen."
        actions={
          <div className="flex items-center gap-2">
            <Select
              value={warehouse}
              onChange={(e) => setWarehouse(e.target.value)}
              className="sm:w-auto"
              aria-label="Location"
            >
              <option value="">All locations</option>
              {(warehouses.data ?? []).map((w) => (
                <option key={w.id} value={w.id}>
                  {w.name}
                </option>
              ))}
            </Select>
            <Select
              value={horizon}
              onChange={(e) =>
                setHorizon(e.target.value ? Number(e.target.value) : "")
              }
              className="sm:w-auto"
              aria-label="Horizon"
            >
              <option value="">
                {data ? `Next ${data.horizon_days} days` : "As configured"}
              </option>
              {HORIZONS.filter((h) => h.value !== data?.horizon_days).map((h) => (
                <option key={h.value} value={h.value}>
                  {h.label}
                </option>
              ))}
            </Select>
          </div>
        }
      />

      {error ? (
        <ErrorState error={error} />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-muted p-2">
                <ChartSpline className="size-4 text-ink-faint" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Series forecast
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {rows.length}
                </p>
                <p className="text-[11px] text-ink-faint">
                  {reliable} with high confidence
                </p>
              </div>
            </Card>
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-muted p-2">
                <Activity className="size-4 text-ink-faint" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Units expected
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {qty(Math.round(totalUnits))}
                </p>
                <p className="text-[11px] text-ink-faint">
                  {effectiveHorizon
                    ? `Across the chain, next ${effectiveHorizon} days`
                    : "Across the chain"}
                </p>
              </div>
            </Card>
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-muted p-2">
                <Target className="size-4 text-ink-faint" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Typical accuracy
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {wapeLabel(medianWape)}
                </p>
                <p className="text-[11px] text-ink-faint">
                  Median error, measured on held-out data
                </p>
              </div>
            </Card>
          </div>

          <Card>
            <CardHeader
              title="By product and branch"
              description="Busiest first. Open one for its chart and the methods it beat."
            />
            <DataTable
              columns={columns}
              rows={rows}
              rowKey={(row) => `${row.warehouse_id}:${row.product_id}`}
              loading={isPending}
              error={error}
              onRetry={refetch}
              onRowClick={(row) => setSelected(row)}
              emptyTitle="Nothing to forecast yet"
              emptyDescription="A series needs about three weeks of sales before it can be fitted."
            />
          </Card>
        </div>
      )}

      {selected && (
        <Detail forecast={selected} onClose={() => setSelected(null)} />
      )}
    </>
  );
}
