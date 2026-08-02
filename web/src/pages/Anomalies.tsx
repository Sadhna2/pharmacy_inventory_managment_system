/**
 * Exceptions.
 *
 * The ledger holds fifty thousand movements and nobody reads them. This is the
 * short list: the days, write-offs and out-of-hours sessions that broke from
 * the pattern around them.
 *
 * Every row states what it measured and what it measured against, because a
 * finding a manager cannot argue with is a finding they cannot act on either.
 * There is no "dismiss" button by design — findings are recomputed from the
 * ledger on every load, so the way to clear one is to fix or explain the
 * movement behind it, not to tick it off.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  Banknote,
  Clock,
  Moon,
  PackageX,
  Repeat,
  ShieldCheck,
  TrendingUpDown,
} from "lucide-react";
import { api, qs } from "@/lib/api";
import { cn, dateTime, money, qty } from "@/lib/format";
import type { Anomaly, AnomalyReport, Warehouse } from "@/lib/types";
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

const WINDOWS = [
  { value: 30, label: "Last 30 days" },
  { value: 90, label: "Last 90 days" },
  { value: 180, label: "Last 6 months" },
  { value: 365, label: "Last year" },
];

type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "brand";

/**
 * Each detector answers a different question and lands on a different desk,
 * so each keeps its own label and icon rather than being flattened into one
 * "alert" type.
 */
const KINDS: Record<
  Anomaly["kind"],
  { label: string; icon: React.ComponentType<{ className?: string }>; blurb: string }
> = {
  consumption: {
    label: "Demand break",
    icon: TrendingUpDown,
    blurb: "A day that broke from the four weeks before it",
  },
  shrinkage: {
    label: "Shrinkage",
    icon: PackageX,
    blurb: "Stock missing at a count, with no paperwork behind it",
  },
  write_off: {
    label: "Write-off",
    icon: Banknote,
    blurb: "Damage, scrap or expiry large enough to sign off",
  },
  after_hours: {
    label: "Out of hours",
    icon: Moon,
    blurb: "Stock moving when the counter was shut",
  },
  repeat_loss: {
    label: "Repeat loss",
    icon: Repeat,
    blurb: "The same product going missing at the same branch, again",
  },
};

const SEVERITY_TONE: Record<Anomaly["severity"], Tone> = {
  high: "danger",
  medium: "warn",
  low: "neutral",
};

function fieldLabel(key: string): string {
  return key
    .replace(/_/g, " ")
    .replace(/^./, (c) => c.toUpperCase());
}

function formatBaseline(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (Array.isArray(value)) return value.join(", ");
  if (typeof value === "number") return qty(value);
  return String(value);
}

/* ------------------------------------------------------------------ detail */

function Detail({ anomaly, onClose }: { anomaly: Anomaly; onClose: () => void }) {
  const meta = KINDS[anomaly.kind];
  return (
    <Modal
      open
      onClose={onClose}
      title={meta.label}
      description={meta.blurb}
    >
      <div className="space-y-4">
        <p className="text-sm leading-relaxed text-ink">{anomaly.explanation}</p>

        <div className="grid grid-cols-2 gap-x-6 gap-y-3 text-[13px]">
          <div>
            <p className="text-[11px] tracking-wide text-ink-faint uppercase">When</p>
            <p className="mt-0.5 text-ink">{dateTime(anomaly.occurred_at)}</p>
          </div>
          <div>
            <p className="text-[11px] tracking-wide text-ink-faint uppercase">Where</p>
            <p className="mt-0.5 text-ink">{anomaly.warehouse_name}</p>
          </div>
          <div>
            <p className="text-[11px] tracking-wide text-ink-faint uppercase">Product</p>
            <p className="mt-0.5 text-ink">
              {anomaly.product_name || "Several"}
              {anomaly.sku && (
                <span className="ml-2 font-mono text-[11px] text-ink-faint">
                  {anomaly.sku}
                </span>
              )}
            </p>
          </div>
          <div>
            <p className="text-[11px] tracking-wide text-ink-faint uppercase">Quantity</p>
            <p className="tnum mt-0.5 text-ink">{qty(anomaly.quantity)} units</p>
          </div>
        </div>

        {/*
          The evidence, not a confidence percentage. Someone is going to ask a
          branch manager about this, and "measured against 28 days" is a
          defensible thing to say where "0.94" is not.
        */}
        <div>
          <h4 className="mb-2 text-[13px] font-semibold text-ink">
            What this was measured against
          </h4>
          <div className="divide-y divide-line rounded-lg border border-line">
            {anomaly.score > 0 && (
              <div className="flex items-baseline justify-between gap-3 px-3 py-2">
                <span className="text-[13px] text-ink-soft">Deviation</span>
                <span className="tnum text-[13px] text-ink">
                  {anomaly.score.toFixed(1)}× the normal spread
                </span>
              </div>
            )}
            {Object.entries(anomaly.baseline).map(([key, value]) => (
              <div
                key={key}
                className="flex items-baseline justify-between gap-3 px-3 py-2"
              >
                <span className="text-[13px] text-ink-soft">{fieldLabel(key)}</span>
                <span className="tnum text-[13px] text-ink">
                  {formatBaseline(value)}
                </span>
              </div>
            ))}
          </div>
        </div>

        <div>
          <h4 className="mb-1.5 text-[13px] font-semibold text-ink">
            Ledger rows behind it
          </h4>
          <p className="font-mono text-[12px] leading-relaxed break-all text-ink-soft">
            {anomaly.movement_ids.length
              ? anomaly.movement_ids.map((id) => `#${id}`).join("  ")
              : "—"}
          </p>
          <p className="mt-2 text-[12px] text-ink-faint">
            Nothing here has been changed. Findings are recomputed from the ledger
            each time this page loads — correct or reverse the movement and this
            entry stops appearing.
          </p>
        </div>
      </div>
    </Modal>
  );
}

/* -------------------------------------------------------------------- page */

export function Anomalies() {
  const [lookback, setLookback] = useState(90);
  const [kind, setKind] = useState("");
  const [warehouse, setWarehouse] = useState("");
  const [severity, setSeverity] = useState("low");
  const [selected, setSelected] = useState<Anomaly | null>(null);

  const warehouses = useQuery({
    queryKey: ["warehouses"],
    queryFn: () => api.get<Warehouse[]>("/api/v1/warehouses"),
  });

  const { data, isLoading, error } = useQuery({
    queryKey: ["anomalies", { lookback, kind, warehouse, severity }],
    queryFn: () =>
      api.get<AnomalyReport>(
        `/api/v1/ai/anomalies${qs({
          lookback_days: lookback,
          kind,
          warehouse_id: warehouse,
          min_severity: severity,
        })}`,
      ),
  });

  const summary = data?.summary;

  const columns: Column<Anomaly>[] = [
    {
      key: "severity",
      header: "",
      width: "3rem",
      render: (row) => {
        const Icon = KINDS[row.kind].icon;
        return (
          <div
            className={cn(
              "flex size-7 items-center justify-center rounded-lg",
              row.severity === "high"
                ? "bg-danger-soft text-danger"
                : row.severity === "medium"
                  ? "bg-warn-soft text-warn"
                  : "bg-muted text-ink-faint",
            )}
          >
            <Icon className="size-3.5" />
          </div>
        );
      },
    },
    {
      key: "finding",
      header: "Finding",
      card: "primary",
      render: (row) => (
        <div className="min-w-0">
          <p className="text-[13px] text-ink">{row.explanation}</p>
          <p className="mt-0.5 truncate text-[11px] text-ink-faint">
            {row.warehouse_name}
            {row.product_name && ` · ${row.product_name}`}
          </p>
        </div>
      ),
    },
    {
      key: "kind",
      header: "Type",
      card: "secondary",
      hideBelow: "md",
      render: (row) => (
        <Badge tone={SEVERITY_TONE[row.severity]}>{KINDS[row.kind].label}</Badge>
      ),
    },
    {
      key: "value",
      header: "At cost",
      numeric: true,
      hideBelow: "sm",
      render: (row) =>
        row.value > 0 ? (
          <span>{money(row.value)}</span>
        ) : (
          <span className="text-ink-faint">—</span>
        ),
    },
    {
      key: "when",
      header: "When",
      card: "secondary",
      hideBelow: "lg",
      width: "10rem",
      render: (row) => (
        <span className="text-[13px] whitespace-nowrap text-ink-soft">
          {dateTime(row.occurred_at)}
        </span>
      ),
    },
  ];

  const filter = <T,>(set: (value: T) => void) => (value: T) => set(value);

  return (
    <>
      <PageHeader
        title="Exceptions"
        description="Movements that broke from the pattern around them — found by comparing the ledger against itself, not by a rule someone has to keep updating."
      />

      {error ? (
        <ErrorState error={error} />
      ) : (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-danger-soft p-2">
                <AlertTriangle className="size-4 text-danger" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Needs attention
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {summary?.high ?? 0}
                </p>
                <p className="text-[11px] text-ink-faint">
                  {summary?.medium ?? 0} worth a look, {summary?.low ?? 0} minor
                </p>
              </div>
            </Card>
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-muted p-2">
                <Banknote className="size-4 text-ink-faint" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Value in losses
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {money(summary?.value_at_risk ?? 0)}
                </p>
                <p className="text-[11px] text-ink-faint">
                  Write-offs and shrinkage, at cost
                </p>
              </div>
            </Card>
            <Card className="flex items-center gap-3 px-4 py-3">
              <div className="rounded-lg bg-muted p-2">
                <Clock className="size-4 text-ink-faint" />
              </div>
              <div>
                <p className="text-[11px] tracking-wide text-ink-faint uppercase">
                  Findings in window
                </p>
                <p className="tnum mt-0.5 text-lg font-semibold text-ink">
                  {summary?.total ?? 0}
                </p>
                <p className="text-[11px] text-ink-faint">
                  {Object.entries(summary?.by_kind ?? {})
                    .map(([k, n]) => `${n} ${KINDS[k as Anomaly["kind"]]?.label.toLowerCase() ?? k}`)
                    .join(", ") || "Nothing flagged"}
                </p>
              </div>
            </Card>
          </div>

          <Card>
            <CardHeader
              title="Findings"
              description="Most serious first. Open one to see what it was measured against."
              actions={
                <div className="flex flex-wrap items-center gap-2">
                  <Select
                    value={severity}
                    onChange={(e) => filter(setSeverity)(e.target.value)}
                    className="sm:w-auto"
                    aria-label="Minimum severity"
                  >
                    <option value="low">All severities</option>
                    <option value="medium">Medium and above</option>
                    <option value="high">High only</option>
                  </Select>
                  <Select
                    value={kind}
                    onChange={(e) => filter(setKind)(e.target.value)}
                    className="sm:w-auto"
                    aria-label="Finding type"
                  >
                    <option value="">All types</option>
                    {Object.entries(KINDS).map(([value, meta]) => (
                      <option key={value} value={value}>
                        {meta.label}
                      </option>
                    ))}
                  </Select>
                  <Select
                    value={warehouse}
                    onChange={(e) => filter(setWarehouse)(e.target.value)}
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
                    value={lookback}
                    onChange={(e) => filter(setLookback)(Number(e.target.value))}
                    className="sm:w-auto"
                    aria-label="Window"
                  >
                    {WINDOWS.map((w) => (
                      <option key={w.value} value={w.value}>
                        {w.label}
                      </option>
                    ))}
                  </Select>
                </div>
              }
            />
            <DataTable
              columns={columns}
              rows={data?.anomalies ?? []}
              rowKey={(row) => row.key}
              loading={isLoading}
              onRowClick={(row) => setSelected(row)}
              emptyTitle="Nothing out of the ordinary"
              emptyDescription="No movement in this window broke from the pattern around it. Widen the window or lower the severity filter to see more."
              emptyAction={
                <span className="inline-flex items-center gap-1.5 text-[13px] text-ok">
                  <ShieldCheck className="size-4" />
                  Ledger looks clean
                </span>
              }
            />
          </Card>
        </div>
      )}

      {selected && <Detail anomaly={selected} onClose={() => setSelected(null)} />}
    </>
  );
}
