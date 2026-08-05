import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import {
  AlertTriangle,
  ArrowRight,
  CalendarClock,
  Package,
  ShieldAlert,
  Truck,
} from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { cn, moneyShort, num, qty, relativeDays } from "@/lib/format";
import type { ExpiringStock, Movement, Page, StockSummary } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import {
  Badge,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Spinner,
} from "@/components/ui";

function Tile({
  label,
  value,
  sub,
  icon: Icon,
  tone = "neutral",
  to,
}: {
  label: string;
  value: string;
  sub?: string;
  icon: React.ComponentType<{ className?: string }>;
  tone?: "neutral" | "warn" | "danger" | "info";
  to?: string;
}) {
  const toneClasses = {
    neutral: "text-ink-faint bg-muted",
    warn: "text-warn bg-warn-soft",
    danger: "text-danger bg-danger-soft",
    info: "text-info bg-info-soft",
  }[tone];

  const body = (
    <div
      className={cn(
        "card h-full p-4 transition-shadow",
        to && "hover:border-line-strong hover:shadow-sm",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <p className="text-[13px] font-medium text-ink-soft">{label}</p>
        <div className={cn("rounded-lg p-1.5", toneClasses)}>
          <Icon className="size-4" />
        </div>
      </div>
      <p className="mt-2 text-2xl font-semibold tracking-tight tnum text-ink">
        {value}
      </p>
      {sub && <p className="mt-0.5 text-xs text-ink-faint">{sub}</p>}
    </div>
  );

  return to ? (
    <Link to={to} className="block">
      {body}
    </Link>
  ) : (
    body
  );
}

export function Dashboard() {
  const { user, can } = useAuth();

  const summary = useQuery({
    queryKey: ["stock", "summary"],
    queryFn: () => api.get<StockSummary>("/api/v1/stock/summary"),
  });

  const expiring = useQuery({
    queryKey: ["stock", "expiring", 30],
    queryFn: () => api.get<ExpiringStock[]>("/api/v1/stock/expiring?days=30"),
  });

  const recent = useQuery({
    queryKey: ["movements", "recent"],
    queryFn: () => api.get<Page<Movement>>("/api/v1/stock/movements?size=8"),
  });

  const data = summary.data;

  return (
    <>
      <PageHeader
        title={`Good ${greeting()}, ${user?.full_name.split(" ")[0] ?? ""}`}
        description={
          user?.warehouse_name
            ? `Showing ${user.warehouse_name}`
            : "Across the whole chain"
        }
      />

      {/* The tiles below print an em dash when `data` is missing, which is at
          least not a false zero — but on its own it looks like the chain holds
          no stock. This says which it is. It matters most for the CUSTOMER
          role, whose account is refused all three of these reads: without it
          their landing page is a wall of dashes and two cheerful empty states
          claiming nothing is expiring and nothing has moved. */}
      {summary.error && (
        <div className="mt-4 rounded-xl border border-danger/20 bg-danger-soft px-4 py-3 text-[13px] text-ink-soft">
          {summary.error instanceof ApiError && summary.error.status === 403
            ? "Your role does not include the chain-wide stock figures, so the totals below are blank."
            : "The stock totals could not be loaded, so the figures below are blank."}
        </div>
      )}

      {/* ------------------------------------------------------------ tiles */}
      <div className="grid grid-cols-2 gap-3 sm:gap-4 lg:grid-cols-4">
        <Tile
          label="Stock on hand"
          value={data ? qty(data.total_units) : "—"}
          sub={data ? `${data.total_skus} SKUs` : undefined}
          icon={Package}
          to="/stock"
        />
        <Tile
          label="Below reorder"
          value={data ? String(data.below_reorder_point) : "—"}
          sub="Needs replenishment"
          icon={AlertTriangle}
          tone={data && data.below_reorder_point > 0 ? "warn" : "neutral"}
          to="/products?below_reorder=true"
        />
        <Tile
          label="Expiring soon"
          value={data ? String(data.expiring_30_days) : "—"}
          sub={
            data && data.expired_on_hand > 0
              ? `${data.expired_on_hand} already expired`
              : "Batches within 30 days"
          }
          icon={CalendarClock}
          tone={data && data.expiring_30_days > 0 ? "danger" : "neutral"}
        />
        {can("stock.view_cost") ? (
          <Tile
            label="Stock value"
            value={data ? moneyShort(data.stock_value) : "—"}
            sub="Weighted average"
            icon={Package}
          />
        ) : (
          <Tile
            label="In transit"
            value={data ? qty(data.in_transit_units) : "—"}
            sub="Between locations"
            icon={Truck}
            tone="info"
          />
        )}
      </div>

      {/* -------------------------------------------- quarantine highlight */}
      {data && num(data.quarantined_units) > 0 && (
        <div className="mt-4 flex items-start gap-3 rounded-xl border border-danger/20 bg-danger-soft px-4 py-3">
          <ShieldAlert className="mt-0.5 size-4 shrink-0 text-danger" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium text-danger">
              {qty(data.quarantined_units)} units quarantined
            </p>
            <p className="mt-0.5 text-[13px] text-ink-soft">
              Held under recall or pending QC. Not available for dispensing.
            </p>
          </div>
          <Link
            to="/recalls"
            className="shrink-0 self-center text-[13px] font-medium text-danger hover:underline"
          >
            Review
          </Link>
        </div>
      )}

      <div className="mt-4 grid gap-4 lg:grid-cols-5">
        {/* ------------------------------------------------ expiry watchlist */}
        <Card className="lg:col-span-3">
          <CardHeader
            title="Expiry watchlist"
            description="Earliest-expiring batches still on the shelf"
            actions={
              <Link
                to="/stock"
                className="inline-flex items-center gap-1 text-[13px] font-medium text-brand hover:underline"
              >
                All stock <ArrowRight className="size-3.5" />
              </Link>
            }
          />
          {expiring.isPending ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : expiring.error ? (
            <ErrorState error={expiring.error} onRetry={expiring.refetch} />
          ) : !expiring.data?.length ? (
            <EmptyState
              icon={CalendarClock}
              title="Nothing expiring soon"
              description="No batches fall due within 30 days."
            />
          ) : (
            <ul className="divide-y divide-line">
              {expiring.data.slice(0, 7).map((row) => (
                <li
                  key={`${row.lot_id}-${row.warehouse_id}`}
                  className="flex items-center gap-3 px-4 py-2.5"
                >
                  <div className="min-w-0 flex-1">
                    <p className="truncate text-[13px] font-medium text-ink">
                      {row.product_name}
                    </p>
                    <p className="truncate text-[11px] text-ink-faint">
                      <span className="font-mono">{row.lot_code}</span> ·{" "}
                      {row.warehouse_name}
                    </p>
                  </div>
                  <span className="shrink-0 text-[13px] tnum text-ink-soft">
                    {qty(row.qty_on_hand)}
                  </span>
                  <Badge
                    tone={
                      row.days_to_expiry < 0
                        ? "danger"
                        : row.days_to_expiry <= 30
                          ? "warn"
                          : "neutral"
                    }
                    className="shrink-0"
                  >
                    {relativeDays(row.days_to_expiry)}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </Card>

        {/* ---------------------------------------------------- recent moves */}
        <Card className="lg:col-span-2">
          <CardHeader
            title="Recent activity"
            description="Every movement, immutably logged"
          />
          {recent.isPending ? (
            <div className="flex justify-center py-10">
              <Spinner />
            </div>
          ) : recent.error ? (
            <ErrorState error={recent.error} onRetry={recent.refetch} />
          ) : !recent.data?.items.length ? (
            <EmptyState icon={Package} title="No movements yet" />
          ) : (
            <ul className="divide-y divide-line">
              {recent.data.items.map((movement) => {
                const inbound = num(movement.quantity) > 0;
                return (
                  <li key={movement.id} className="flex items-center gap-3 px-4 py-2.5">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[13px] font-medium text-ink">
                        {movement.product_name}
                      </p>
                      <p className="truncate text-[11px] text-ink-faint">
                        {movement.movement_type
                          .replace(/_/g, " ")
                          .toLowerCase()
                          .replace(/^./, (c) => c.toUpperCase())}
                        {movement.warehouse_name && ` · ${movement.warehouse_name}`}
                      </p>
                    </div>
                    <span
                      className={cn(
                        "shrink-0 text-[13px] font-medium tnum",
                        inbound ? "text-ok" : "text-ink-soft",
                      )}
                    >
                      {inbound ? "+" : ""}
                      {qty(movement.quantity)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </Card>
      </div>
    </>
  );
}

function greeting(): string {
  const hour = new Date().getHours();
  if (hour < 12) return "morning";
  if (hour < 17) return "afternoon";
  return "evening";
}
