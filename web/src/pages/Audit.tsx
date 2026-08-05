/**
 * Audit trail.
 *
 * Every write path in the API already records who did what, to which record,
 * and the field values either side of the change. This is the only screen that
 * reads it back. There is no create, edit or delete here by design — the table
 * revokes UPDATE and DELETE at the database level, so a viewer is all it can
 * ever have.
 *
 * The filter options come from `/audit/facets` rather than a hardcoded list,
 * so the action dropdown grows on its own as Layer 2 adds audited actions.
 */

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowRight, ScrollText } from "lucide-react";
import { api, qs } from "@/lib/api";
import { dateTime, titleCase } from "@/lib/format";
import type { AuditEntry, AuditFacets, Page } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { DataTable, type Column } from "@/components/DataTable";
import { Badge, Button, Card, Input, Modal, Select } from "@/components/ui";

type Tone = "neutral" | "ok" | "warn" | "danger" | "info";

/**
 * Colour by verb, not by an exhaustive list — a new `shipment.cancel` should
 * read as destructive on day one without anyone remembering to register it.
 */
function actionTone(action: string): Tone {
  const verb = action.split(".").pop() ?? "";
  if (["cancel", "delete", "reverse", "deactivate", "recall"].includes(verb)) return "danger";
  if (["approve", "create", "receive", "ship"].includes(verb)) return "ok";
  if (["update", "adjust"].includes(verb)) return "warn";
  return "neutral";
}

/** "po.approve" → "PO approve"; "product.update" → "Product update". */
function actionLabel(action: string): string {
  const [entity, verb] = action.split(".");
  const noun = entity.length <= 3 ? entity.toUpperCase() : titleCase(entity);
  return verb ? `${noun} ${verb.replace(/_/g, " ")}` : noun;
}

function fieldLabel(key: string): string {
  return titleCase(key.replace(/_/g, " "));
}

/**
 * Values arrive as whatever JSON the service serialised — strings, numbers,
 * nulls, and occasionally a nested object for a line item.
 */
function formatValue(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

interface Change {
  key: string;
  before: unknown;
  after: unknown;
}

/**
 * The union of both sides, so a field added by the change and a field cleared
 * by it are both visible. Unchanged fields are dropped — a diff that lists
 * everything is a diff nobody reads.
 */
function diff(entry: AuditEntry): Change[] {
  const before = entry.before_json ?? {};
  const after = entry.after_json ?? {};
  const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])];
  return keys
    .map((key) => ({ key, before: before[key], after: after[key] }))
    .filter((c) => formatValue(c.before) !== formatValue(c.after))
    .sort((a, b) => a.key.localeCompare(b.key));
}

/** Rendered both inline in the modal and, abbreviated, in the table. */
function ChangeRow({ change }: { change: Change }) {
  return (
    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-3 py-2">
      <span className="w-40 shrink-0 text-[13px] font-medium text-ink">
        {fieldLabel(change.key)}
      </span>
      <span className="text-[13px] text-ink-faint line-through decoration-ink-faint/50">
        {formatValue(change.before)}
      </span>
      <ArrowRight className="size-3 shrink-0 text-ink-faint" />
      <span className="text-[13px] font-medium text-ink">
        {formatValue(change.after)}
      </span>
    </div>
  );
}

export function Audit() {
  const [action, setAction] = useState("");
  const [entityType, setEntityType] = useState("");
  const [actorId, setActorId] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [page, setPage] = useState(1);
  const [detail, setDetail] = useState<AuditEntry | null>(null);

  const facets = useQuery({
    queryKey: ["audit-facets"],
    queryFn: () => api.get<AuditFacets>("/api/v1/audit/facets"),
  });

  const { data, isPending, error, refetch } = useQuery({
    queryKey: ["audit", { action, entityType, actorId, from, to, page }],
    queryFn: () =>
      api.get<Page<AuditEntry>>(
        `/api/v1/audit${qs({
          action,
          entity_type: entityType,
          actor_user_id: actorId,
          date_from: from,
          date_to: to,
          page,
          size: 25,
        })}`,
      ),
  });

  // Any filter change invalidates the current page number — page 7 of an
  // unfiltered log is rarely page 7 of a filtered one.
  const filter = <T,>(set: (value: T) => void) => (value: T) => {
    set(value);
    setPage(1);
  };

  const columns: Column<AuditEntry>[] = [
    {
      key: "when",
      header: "When",
      card: "secondary",
      width: "11rem",
      render: (row) => (
        <span className="text-[13px] whitespace-nowrap text-ink-soft">
          {dateTime(row.created_at)}
        </span>
      ),
    },
    {
      key: "action",
      header: "Action",
      card: "primary",
      render: (row) => (
        <Badge tone={actionTone(row.action)}>{actionLabel(row.action)}</Badge>
      ),
    },
    {
      key: "record",
      header: "Record",
      card: "secondary",
      render: (row) => (
        <span className="text-[13px] text-ink-soft">
          {fieldLabel(row.entity_type)}
          {row.entity_id !== null && (
            <span className="ml-1 font-mono text-ink-faint">#{row.entity_id}</span>
          )}
        </span>
      ),
    },
    {
      key: "actor",
      header: "By",
      hideBelow: "md",
      render: (row) => (
        <div className="min-w-0">
          <p className="truncate text-[13px] text-ink">{row.actor_name ?? "System"}</p>
          {row.actor_email && (
            <p className="truncate text-[11px] text-ink-faint">{row.actor_email}</p>
          )}
        </div>
      ),
    },
    {
      key: "changes",
      header: "Changed",
      hideBelow: "lg",
      render: (row) => {
        const changes = diff(row);
        if (changes.length === 0) {
          return <span className="text-[13px] text-ink-faint">—</span>;
        }
        return (
          <span className="text-[13px] text-ink-soft">
            {changes
              .slice(0, 2)
              .map((c) => fieldLabel(c.key))
              .join(", ")}
            {changes.length > 2 && (
              <span className="text-ink-faint"> +{changes.length - 2}</span>
            )}
          </span>
        );
      },
    },
  ];

  const changes = detail ? diff(detail) : [];

  return (
    <>
      <PageHeader
        title="Audit Trail"
        description="Every state change, who made it, and what the record looked like before"
      />

      <Card>
        <div className="grid grid-cols-2 gap-2 border-b border-line p-3 sm:flex sm:flex-wrap sm:items-center">
          <Select
            value={action}
            onChange={(e) => filter(setAction)(e.target.value)}
            className="min-w-0 sm:w-auto sm:min-w-[11rem] sm:flex-none"
          >
            <option value="">All actions</option>
            {facets.data?.actions.map((a) => (
              <option key={a} value={a}>
                {actionLabel(a)}
              </option>
            ))}
          </Select>

          <Select
            value={entityType}
            onChange={(e) => filter(setEntityType)(e.target.value)}
            className="min-w-0 sm:w-auto sm:min-w-[10rem] sm:flex-none"
          >
            <option value="">All records</option>
            {facets.data?.entity_types.map((t) => (
              <option key={t} value={t}>
                {fieldLabel(t)}
              </option>
            ))}
          </Select>

          <Select
            value={actorId}
            onChange={(e) => filter(setActorId)(e.target.value)}
            className="min-w-0 sm:w-auto sm:min-w-[11rem] sm:flex-none"
          >
            <option value="">Anyone</option>
            {facets.data?.actors.map((a) => (
              <option key={a.id} value={a.id}>
                {a.full_name}
              </option>
            ))}
          </Select>

          <Input
            type="date"
            value={from}
            max={to || undefined}
            onChange={(e) => filter(setFrom)(e.target.value)}
            aria-label="From date"
            className="min-w-0 sm:w-[9.5rem] sm:flex-none"
          />
          <Input
            type="date"
            value={to}
            min={from || undefined}
            onChange={(e) => filter(setTo)(e.target.value)}
            aria-label="To date"
            className="min-w-0 sm:w-[9.5rem] sm:flex-none"
          />

          {(action || entityType || actorId || from || to) && (
            <Button
              size="sm"
              className="col-span-2 sm:col-span-1"
              onClick={() => {
                setAction("");
                setEntityType("");
                setActorId("");
                setFrom("");
                setTo("");
                setPage(1);
              }}
            >
              Clear
            </Button>
          )}
        </div>

        <DataTable
          columns={columns}
          rows={data?.items ?? []}
          rowKey={(row) => row.id}
          loading={isPending}
          error={error}
          onRetry={refetch}
          onRowClick={setDetail}
          page={data?.page}
          pages={data?.pages}
          total={data?.total}
          onPageChange={setPage}
          emptyTitle="Nothing recorded"
          emptyDescription="No activity matches these filters."
        />
      </Card>

      {/* ----------------------------------------------------- entry detail */}
      <Modal
        open={detail !== null}
        onClose={() => setDetail(null)}
        title={detail ? actionLabel(detail.action) : ""}
        description={
          detail
            ? `${detail.actor_name ?? "System"} · ${dateTime(detail.created_at)}`
            : undefined
        }
        wide
        footer={<Button onClick={() => setDetail(null)}>Close</Button>}
      >
        {detail && (
          <div className="space-y-5">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-3 sm:grid-cols-4">
              {[
                ["Record", `${fieldLabel(detail.entity_type)} #${detail.entity_id ?? "—"}`],
                ["Action", detail.action],
                ["IP address", detail.ip ?? "—"],
                ["Request id", detail.request_id ?? "—"],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0">
                  <dt className="text-[11px] tracking-wide text-ink-faint uppercase">
                    {label}
                  </dt>
                  <dd className="truncate font-mono text-[13px] text-ink" title={value}>
                    {value}
                  </dd>
                </div>
              ))}
            </dl>

            <section>
              <h4 className="mb-2 flex items-center gap-1.5 text-[13px] font-semibold text-ink">
                <ScrollText className="size-3.5 text-ink-faint" />
                What changed
              </h4>
              {changes.length === 0 ? (
                <p className="text-[13px] text-ink-soft">
                  {/* Logins and approvals are events, not edits — there is no
                      field-level diff to show and that is not a gap. */}
                  No field-level changes were recorded for this action.
                </p>
              ) : (
                <div className="divide-y divide-line rounded-lg border border-line">
                  {changes.map((c) => (
                    <ChangeRow key={c.key} change={c} />
                  ))}
                </div>
              )}
            </section>
          </div>
        )}
      </Modal>
    </>
  );
}
