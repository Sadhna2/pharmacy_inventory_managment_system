/**
 * Settings.
 *
 * The whole page renders itself from what the API describes — label, help
 * text, type, bounds and default all come down with each setting. Adding a
 * tunable on the server puts a correctly validated field here with no change
 * to this file, which is the only way a settings screen stays honest as the
 * thing it configures grows.
 *
 * Two decisions worth knowing:
 *
 *  - Nothing saves as you type. A settings screen that applies each keystroke
 *    means an administrator halfway through a change has already changed it,
 *    and there are cross-field rules that only make sense on a complete edit.
 *  - Switching a capability off closes its API, not just its menu item. That
 *    is the point: turning off something that is misbehaving in front of an
 *    audience has to actually stop it.
 */

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, TriangleAlert } from "lucide-react";
import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/format";
import type { AppSettings, Tunable } from "@/lib/types";
import { PageHeader } from "@/components/Shell";
import { ConfirmDialog } from "@/components/confirm";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  ErrorState,
  Input,
  TableSkeleton,
} from "@/components/ui";

type Draft = Record<string, unknown>;

/** A single setting's control, chosen from the type the server declared. */
function Control({
  tunable,
  value,
  error,
  onChange,
}: {
  tunable: Tunable;
  value: unknown;
  error?: string;
  onChange: (value: unknown) => void;
}) {
  const common = {
    "aria-label": tunable.label,
    "aria-invalid": error ? true : undefined,
    className: cn("sm:w-44", error && "border-danger focus:border-danger"),
  };

  if (tunable.kind === "time") {
    return (
      <Input
        {...common}
        type="time"
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
      />
    );
  }

  return (
    <Input
      {...common}
      type="number"
      inputMode="decimal"
      step={tunable.kind === "int" ? 1 : 0.01}
      min={tunable.minimum ?? undefined}
      max={tunable.maximum ?? undefined}
      value={String(value ?? "")}
      onChange={(e) => onChange(e.target.value)}
    />
  );
}

function Row({
  tunable,
  value,
  error,
  onChange,
  onReset,
}: {
  tunable: Tunable;
  value: unknown;
  error?: string;
  onChange: (value: unknown) => void;
  onReset: () => void;
}) {
  const dirty = String(value) !== String(tunable.value);
  const offDefault = String(value) !== String(tunable.default);

  return (
    <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 px-4 py-3.5 sm:px-5">
      <div className="min-w-0 flex-1 sm:max-w-xl">
        <div className="flex flex-wrap items-center gap-2">
          <p className="text-[13px] font-medium text-ink">{tunable.label}</p>
          {dirty && <Badge tone="warn">Unsaved</Badge>}
          {!dirty && tunable.is_overridden && <Badge tone="info">Changed</Badge>}
        </div>
        <p className="mt-0.5 text-[12px] leading-relaxed text-ink-soft">
          {tunable.help}
        </p>
        {error && <p className="mt-1 text-xs text-danger">{error}</p>}
      </div>

      <div className="flex shrink-0 items-center gap-2">
        <Control
          tunable={tunable}
          value={value}
          error={error}
          onChange={onChange}
        />
        {tunable.unit && (
          <span className="w-16 text-[12px] text-ink-faint">{tunable.unit}</span>
        )}
        <button
          type="button"
          onClick={onReset}
          disabled={!offDefault}
          title={`Back to the default (${String(tunable.default)})`}
          aria-label={`Reset ${tunable.label} to default`}
          className={cn(
            "rounded-md p-1.5 transition-colors",
            offDefault
              ? "text-ink-faint hover:bg-muted hover:text-ink"
              : "cursor-default text-transparent",
          )}
        >
          <RotateCcw className="size-3.5" />
        </button>
      </div>
    </div>
  );
}

/** Theme-matched switch. Not a checkbox — this is a power switch, not a form field. */
function Toggle({
  checked,
  disabled,
  onChange,
  label,
}: {
  checked: boolean;
  disabled?: boolean;
  onChange: (next: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={cn(
        "relative h-6 w-11 shrink-0 rounded-full transition-colors",
        "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-brand",
        checked ? "bg-brand" : "bg-line-strong",
        disabled && "cursor-not-allowed opacity-40",
      )}
    >
      {/*
        `left-0` is load-bearing. A button centres its inline content, so an
        absolutely-positioned child with no `left` takes its static position
        from that centre — the knob then sits half outside the track, and the
        translate that was supposed to move it just makes it worse.
      */}
      <span
        className={cn(
          "absolute top-0.5 left-0 size-5 rounded-full bg-surface shadow-sm",
          "transition-transform",
          checked ? "translate-x-[1.375rem]" : "translate-x-0.5",
        )}
      />
    </button>
  );
}

export function Settings() {
  const queryClient = useQueryClient();
  const [draft, setDraft] = useState<Draft>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [notice, setNotice] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["settings"],
    queryFn: () => api.get<AppSettings>("/api/v1/settings"),
  });

  // Seed the draft from the server, and re-seed whenever the server's own
  // view changes — a save can normalise a value (or delete an override), and
  // the form has to show what is actually stored rather than what was typed.
  useEffect(() => {
    if (!data) return;
    setDraft(
      Object.fromEntries(
        data.groups.flatMap((g) => g.tunables.map((t) => [t.key, t.value])),
      ),
    );
  }, [data]);

  const save = useMutation({
    mutationFn: (body: { values?: Draft; features?: Record<string, boolean> }) =>
      api.patch<AppSettings>("/api/v1/settings", body),
    onSuccess: (fresh) => {
      queryClient.setQueryData(["settings"], fresh);
      queryClient.invalidateQueries({ queryKey: ["features"] });
      // Everything downstream was computed under the old numbers.
      for (const key of ["anomalies", "forecast", "reorder", "lead-times"]) {
        queryClient.invalidateQueries({ queryKey: [key] });
      }
      setErrors({});
      setFailure(null);
      setNotice("Saved. Every screen recomputes from the new values.");
    },
    onError: (err) => {
      setNotice(null);
      if (err instanceof ApiError) {
        setErrors(err.fieldErrors);
        setFailure(
          Object.keys(err.fieldErrors).length ? null : err.message,
        );
      } else {
        setFailure("Could not save");
      }
    },
  });

  const tunables = data?.groups.flatMap((g) => g.tunables) ?? [];
  const dirtyKeys = tunables
    .filter((t) => String(draft[t.key]) !== String(t.value))
    .map((t) => t.key);

  if (error) return <ErrorState error={error} />;

  return (
    <>
      <PageHeader
        title="Settings"
        description="Which capabilities are live, and the judgement calls behind them. Nothing here can change what the ledger records — only how cautiously it is read."
        actions={
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => setResetting(true)}>
              <RotateCcw className="size-3.5" />
              Reset to defaults
            </Button>
            <Button
              variant="primary"
              size="sm"
              disabled={dirtyKeys.length === 0}
              loading={save.isPending}
              onClick={() =>
                save.mutate({
                  values: Object.fromEntries(
                    dirtyKeys.map((key) => [key, draft[key]]),
                  ),
                })
              }
            >
              {dirtyKeys.length
                ? `Save ${dirtyKeys.length} change${dirtyKeys.length > 1 ? "s" : ""}`
                : "Saved"}
            </Button>
          </div>
        }
      />

      {notice && (
        <div className="mb-4 rounded-lg border border-ok/20 bg-ok-soft px-4 py-3">
          <p className="text-[13px] font-medium text-ok">{notice}</p>
        </div>
      )}
      {failure && (
        <div className="mb-4 rounded-lg border border-danger/20 bg-danger-soft px-4 py-3">
          <p className="text-[13px] font-medium text-danger">{failure}</p>
        </div>
      )}
      {Object.keys(errors).length > 0 && (
        <div className="mb-4 flex items-start gap-2 rounded-lg border border-danger/20 bg-danger-soft px-4 py-3">
          <TriangleAlert className="mt-0.5 size-4 shrink-0 text-danger" />
          <p className="text-[13px] text-danger">
            Nothing was saved — the whole batch is rejected if any field is
            wrong, so what you see is still what is in effect.
          </p>
        </div>
      )}

      {isLoading ? (
        <Card>
          <TableSkeleton rows={8} />
        </Card>
      ) : (
        <div className="space-y-4">
          <Card>
            <CardHeader
              title="Capabilities"
              description="Switching one off closes its API as well as hiding its screen."
            />
            <div className="divide-y divide-line">
              {(data?.features ?? []).map((feature) => (
                <div
                  key={feature.key}
                  className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 px-4 py-3.5 sm:px-5"
                >
                  <div className="min-w-0 flex-1 sm:max-w-2xl">
                    <div className="flex flex-wrap items-center gap-2">
                      <p className="text-[13px] font-medium text-ink">
                        {feature.label}
                      </p>
                      {!feature.is_implemented && (
                        <Badge tone="neutral">Not built yet</Badge>
                      )}
                    </div>
                    <p className="mt-0.5 text-[12px] leading-relaxed text-ink-soft">
                      {feature.description}
                    </p>
                  </div>
                  <Toggle
                    label={feature.label}
                    checked={feature.is_enabled}
                    disabled={!feature.is_implemented || save.isPending}
                    onChange={(next) =>
                      save.mutate({ features: { [feature.key]: next } })
                    }
                  />
                </div>
              ))}
            </div>
          </Card>

          {(data?.groups ?? []).map((group) => (
            <Card key={group.key}>
              <CardHeader title={group.label} />
              <div className="divide-y divide-line">
                {group.tunables.map((tunable) => (
                  <Row
                    key={tunable.key}
                    tunable={tunable}
                    value={draft[tunable.key]}
                    error={errors[tunable.key]}
                    onChange={(value) =>
                      setDraft((d) => ({ ...d, [tunable.key]: value }))
                    }
                    onReset={() =>
                      setDraft((d) => ({ ...d, [tunable.key]: tunable.default }))
                    }
                  />
                ))}
              </div>
            </Card>
          ))}
        </div>
      )}

      <ConfirmDialog
        open={resetting}
        onClose={() => setResetting(false)}
        danger
        title="Reset every setting?"
        description="All the numbers go back to the values the system ships with. Feature switches are left alone — resetting the maths should not quietly turn something back on."
        confirmLabel="Reset to defaults"
        path="/api/v1/settings/reset"
        method="post"
        invalidate={["settings", "features", "anomalies", "forecast", "reorder", "lead-times"]}
      />
    </>
  );
}
