/**
 * Read a supplier invoice into the receipt form.
 *
 * Photograph the paper, and the batch numbers, expiries, quantities and rates
 * arrive filled in. That is the whole feature: the typing on this screen is
 * the slowest part of receiving a delivery, and it is the part where a
 * mistyped batch code quietly breaks a recall trace months later.
 *
 * WHAT THIS COMPONENT DOES NOT DO
 * -------------------------------
 * It does not receive stock. It calls a read-only endpoint that returns a
 * proposal, hands the proposal to the form, and gets out of the way. The
 * person still reads the rows against the carton in front of them and presses
 * the same button they pressed before. There is no path from here to the
 * ledger, which is why a misread digit costs a correction rather than an
 * inventory discrepancy nobody notices for a month.
 *
 * WHY THE FINDINGS ARE SHOWN RATHER THAN SILENTLY APPLIED
 * -------------------------------------------------------
 * Every extraction is checked against the invoice's own arithmetic before it
 * gets here — the line amounts against quantity times rate, the tax against
 * the footer, the batch codes against what this supplier has shipped before.
 * Those findings are the honest part of the feature, so they are listed with
 * the line number to look at, and a blocking one keeps the row visible until
 * somebody deals with it.
 *
 * Showing the model's reading as editable state, rather than as an answer, is
 * the difference between a tool and a guess.
 */

import { useRef, useState } from "react";
import {
  AlertTriangle,
  Check,
  Info,
  OctagonAlert,
  ScanLine,
  Sparkles,
} from "lucide-react";
import { ApiError, api, getAccessToken } from "@/lib/api";
import type { InvoiceIntake, IntakeFlag } from "@/lib/types";
import { Button } from "@/components/ui";
import { cn } from "@/lib/format";

/** Formats accepted by the endpoint, mirrored so the file picker matches. */
const ACCEPT = "image/png,image/jpeg,image/webp,application/pdf";

const SEVERITY = {
  BLOCK: {
    icon: OctagonAlert,
    tone: "text-rose-700 dark:text-rose-300",
    dot: "bg-rose-500",
    label: "Must be checked",
  },
  REVIEW: {
    icon: AlertTriangle,
    tone: "text-amber-700 dark:text-amber-300",
    dot: "bg-amber-500",
    label: "Worth a look",
  },
  INFO: {
    icon: Info,
    tone: "text-slate-600 dark:text-slate-400",
    dot: "bg-slate-400",
    label: "Noted",
  },
} as const;

type Severity = keyof typeof SEVERITY;

/**
 * Upload happens here rather than through `api.post`.
 *
 * The shared client sends JSON and sets its own content type; multipart needs
 * the browser to set the boundary itself, which means passing a FormData body
 * and deliberately not setting a Content-Type header.
 */
async function upload(
  file: File,
  fields: { warehouseId: string; poId: string; supplierId?: string },
): Promise<InvoiceIntake> {
  const body = new FormData();
  body.append("file", file);
  // All three are optional. Sending an empty string would reach the server as
  // the string "", which is not an integer and not absent either — it is a
  // 422 on a field nobody filled in on purpose.
  if (fields.warehouseId) body.append("warehouse_id", fields.warehouseId);
  if (fields.poId) body.append("purchase_order_id", fields.poId);
  if (fields.supplierId) body.append("supplier_id", fields.supplierId);

  const token = getAccessToken();
  const response = await fetch("/api/v1/ai/intake/invoice", {
    method: "POST",
    body,
    credentials: "include",
    headers: token ? { Authorization: `Bearer ${token}` } : undefined,
  });

  if (!response.ok) {
    const problem = await response.json().catch(() => null);
    throw new ApiError(response.status, {
      type: "about:blank",
      title: "Scan failed",
      status: response.status,
      detail: "The invoice could not be read.",
      ...problem,
    });
  }
  return response.json();
}

/** One product, as this distributor prints it. */
export interface Alias {
  product_id: number;
  printed_name: string;
  /**
   * The rate off the line this was resolved from. The server only uses it when
   * it has no record of buying this product from this distributor before, in
   * which case it becomes the link's cost — the price actually charged, rather
   * than one worked backwards from the MRP.
   */
  unit_cost?: number;
}

/**
 * Teach the catalogue what a distributor calls things.
 *
 * A trade name cannot be derived from a generic one by any rule worth
 * trusting — `OMEZ-20` is not a truncation of `OMEPRAZOLE`, and a matcher that
 * reached it would reach a dozen wrong things with it. So the matcher refuses,
 * a person answers once, and the answer is stored against the supplier. Every
 * later invoice from them matches it outright.
 *
 * Best-effort, and deliberately so. The server declines to overwrite a
 * supplier code somebody set by hand, which is not a reason to fail a delivery
 * that has physically arrived. Returns how many were stored, so the caller can
 * say what happened rather than imply it all worked.
 */
export async function rememberAliases(
  supplierId: string,
  aliases: Alias[],
): Promise<number> {
  const results = await Promise.all(
    aliases.map((alias) =>
      api
        .post("/api/v1/ai/intake/alias", {
          supplier_id: Number(supplierId),
          ...alias,
        })
        .then(() => true)
        .catch(() => false),
    ),
  );
  return results.filter(Boolean).length;
}

export function InvoiceScanButton({
  warehouseId,
  poId,
  supplierId,
  disabled,
  onScanned,
}: {
  warehouseId: string;
  poId: string;
  /**
   * Narrows matching to what this distributor supplies, and is what makes an
   * unresolved line teachable afterwards — an alias belongs to a supplier, so
   * without one there is nobody to remember it for.
   */
  supplierId?: string;
  disabled?: boolean;
  /** Handed the proposal. The form decides what to do with it. */
  onScanned: (result: InvoiceIntake) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const choose = async (file: File | undefined) => {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      onScanned(await upload(file, { warehouseId, poId, supplierId }));
    } catch (err) {
      setError(
        err instanceof ApiError
          ? err.problem.detail
          : "The invoice could not be read.",
      );
    } finally {
      setBusy(false);
      // Clearing lets the same file be chosen twice, which is what somebody
      // does after fixing the lighting and taking the photograph again.
      if (input.current) input.current.value = "";
    }
  };

  return (
    <div className="space-y-1.5">
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        className="hidden"
        onChange={(e) => void choose(e.target.files?.[0])}
      />
      <Button
        type="button"
        size="sm"
        loading={busy}
        disabled={disabled || busy}
        onClick={() => input.current?.click()}
        title="Photograph or upload the supplier's invoice"
      >
        <ScanLine className="size-4" /> Scan invoice
      </Button>
      {error && (
        <p className="text-xs text-rose-600 dark:text-rose-400">{error}</p>
      )}
    </div>
  );
}

/** What a row on the form holds now, for reconciling findings against. */
export interface RowState {
  /** The invoice line this row was filled from. */
  lineNo: number;
  hasProduct: boolean;
  batch: string;
  expiry: string;
}

/** Longest batch code the server will accept — `validate.MAX_BATCH_LENGTH`. */
const MAX_BATCH = 24;

/** A finding has no id, so it is identified by what it says about where. */
const flagKey = (f: IntakeFlag) =>
  `${f.line_no ?? ""}|${f.field}|${f.message}`;

const isMonthEnd = (iso: string): boolean => {
  const d = new Date(`${iso}T00:00:00`);
  if (Number.isNaN(d.getTime())) return false;
  return d.getUTCDate() === new Date(
    Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0),
  ).getUTCDate();
};

/**
 * The findings the form has since answered.
 *
 * A scan is a snapshot, the rows underneath it are live, and the two used to
 * drift apart the moment somebody started fixing things: choose the product for
 * an unmatched line and the row goes green while the panel above still reports
 * it as unmatched. That is worse than a stale count. It teaches a receiver that
 * the panel is not to be believed, which is the one thing it has to be.
 *
 * DELIBERATELY NOT A SECOND VALIDATOR
 * -----------------------------------
 * This does not re-run the checks. Reimplementing the server's rules in the
 * browser gives two copies that agree today and disagree after the next change
 * to either, and the browser's copy would be the one nobody tests. So the
 * question here is narrower and answerable without any rules: *the finding said
 * this row lacked a thing — does the row still lack it?*
 *
 * That is why only four kinds settle. Each names something missing or malformed
 * that a person can supply in the form:
 *
 *   an unmatched product      settled by choosing one
 *   a missing batch code      settled by typing it off the carton
 *   a missing expiry          settled by typing it
 *   an expiry off month end   settled by moving it to the month end
 *
 * Everything else stands. A GSTIN checksum, a tax split, arithmetic that does
 * not add up — those are facts about the *paper*, and no amount of typing into
 * this form makes the supplier's invoice say something different.
 *
 * Two that look settleable and are not, on purpose:
 *
 *   `'OMEZ-20' read as 'Omeprazole 20mg'; check it against the carton` asks for
 *   a physical check nothing on screen can witness. The row having a product is
 *   what prompted the question, so treating it as the answer would dismiss
 *   every one of them the instant it was raised.
 *
 *   A batch in an unfamiliar format is a claim about what this distributor has
 *   shipped before. Re-deciding it needs the vocabulary, which lives on the
 *   server.
 */
export function settledFindings(
  result: InvoiceIntake,
  rows: RowState[],
): Set<string> {
  const byLine = new Map(rows.map((r) => [r.lineNo, r]));
  const invoiceDate = result.supplier_invoice_date ?? "";
  const settled = new Set<string>();

  for (const line of result.lines) {
    for (const flag of line.flags ?? []) {
      const row = byLine.get(line.line_no);

      // The row was deleted. Whatever was wrong with it is no longer part of
      // what is about to be received, so it stops counting against the draft —
      // but it is still shown, struck through, because "I removed the line I
      // could not answer" is a decision worth seeing on screen.
      if (!row) {
        settled.add(flagKey(flag));
        continue;
      }

      const batch = row.batch.trim();
      const blocking = flag.severity === "BLOCK";

      if (flag.field === "product_name" && blocking && row.hasProduct) {
        settled.add(flagKey(flag));
      } else if (
        flag.field === "batch_no" &&
        blocking &&
        batch &&
        batch.length <= MAX_BATCH
      ) {
        settled.add(flagKey(flag));
      } else if (flag.field === "expiry_date" && blocking) {
        if (row.expiry && (!invoiceDate || row.expiry > invoiceDate)) {
          settled.add(flagKey(flag));
        }
      } else if (
        flag.field === "expiry_date" &&
        flag.suggestion &&
        row.expiry &&
        isMonthEnd(row.expiry)
      ) {
        settled.add(flagKey(flag));
      }
    }
  }
  return settled;
}

function FlagRow({ flag, settled }: { flag: IntakeFlag; settled?: boolean }) {
  const style = SEVERITY[(flag.severity as Severity) ?? "INFO"] ?? SEVERITY.INFO;
  const Icon = style.icon;
  if (settled) {
    return (
      <li className="flex gap-2 py-1 opacity-55">
        <Check className="mt-0.5 size-3.5 shrink-0 text-emerald-600" />
        <span className="text-xs leading-relaxed line-through decoration-slate-400">
          <span className="font-medium">
            {flag.line_no ? `Line ${flag.line_no}` : "Invoice"}
          </span>{" "}
          <span className="text-slate-500">{flag.message}</span>
        </span>
      </li>
    );
  }
  return (
    <li className="flex gap-2 py-1">
      <Icon className={cn("mt-0.5 size-3.5 shrink-0", style.tone)} />
      <span className="text-xs leading-relaxed">
        <span className="font-medium">
          {flag.line_no ? `Line ${flag.line_no}` : "Invoice"}
        </span>{" "}
        <span className="text-slate-600 dark:text-slate-400">
          {flag.message}
        </span>
        {flag.suggestion && (
          <>
            {" "}
            <span className="text-slate-500">did you mean</span>{" "}
            <code className="rounded bg-slate-100 px-1 py-0.5 font-mono text-[11px] dark:bg-slate-800">
              {flag.suggestion}
            </code>
            ?
          </>
        )}
      </span>
    </li>
  );
}

/**
 * What the scan found, above the rows it filled in.
 *
 * Ordered by severity rather than by line, because the question being answered
 * is "is there anything here I must not miss", and a blocking finding on line
 * twelve matters more than a note on line one.
 */
export function ScanFindings({
  result,
  rows,
  onDismiss,
}: {
  result: InvoiceIntake;
  /** The form's rows as they stand now. See `settledFindings`. */
  rows: RowState[];
  onDismiss: () => void;
}) {
  const settled = settledFindings(result, rows);
  // Both lists carry a server-side default, so the generated types mark them
  // optional even though the endpoint always sends them.
  const flags: IntakeFlag[] = [
    ...(result.flags ?? []),
    ...result.lines.flatMap((line) => line.flags ?? []),
  ];
  const order: Severity[] = ["BLOCK", "REVIEW", "INFO"];
  const open = flags.filter((f) => !settled.has(flagKey(f)));
  const done = flags.filter((f) => settled.has(flagKey(f)));
  const sorted = [...open].sort(
    (a, b) =>
      order.indexOf(a.severity as Severity) -
      order.indexOf(b.severity as Severity),
  );
  // An unmatched line raises a blocking finding of its own, and it is already
  // counted as "needs a product choosing". Counting it twice made a scan with
  // six unresolved names read "6 need a product choosing · 6 to check", as
  // though there were twelve things wrong with it.
  const blocking = open.filter(
    (f) => f.severity === "BLOCK" && f.field !== "product_name",
  ).length;
  const { lines } = result.summary;
  // Counted from the rows rather than from the scan, so choosing a product for
  // an unmatched line moves it from one side of this tally to the other. The
  // server's numbers describe the moment the page was read and never change.
  const unmatched = rows.filter((r) => !r.hasProduct).length;
  const resolved = rows.length - unmatched;

  return (
    <div
      className={cn(
        "rounded-lg border p-3",
        blocking || unmatched
          ? "border-amber-300 bg-amber-50/60 dark:border-amber-900 dark:bg-amber-950/20"
          : "border-emerald-300 bg-emerald-50/60 dark:border-emerald-900 dark:bg-emerald-950/20",
      )}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-2">
          <Sparkles className="size-4 text-slate-500" />
          <div>
            <p className="text-sm font-medium">
              Read {lines} {lines === 1 ? "line" : "lines"} from the invoice
            </p>
            <p className="text-xs text-slate-600 dark:text-slate-400">
              {resolved} matched to products
              {unmatched > 0 && (
                <>
                  {" · "}
                  <span className="font-medium text-amber-700 dark:text-amber-300">
                    {unmatched} need{unmatched === 1 ? "s" : ""} a product
                    choosing
                  </span>
                </>
              )}
              {blocking > 0 && (
                <>
                  {" · "}
                  <span className="font-medium text-rose-700 dark:text-rose-300">
                    {blocking} to check
                  </span>
                </>
              )}
            </p>
          </div>
        </div>
        <Button size="sm" onClick={onDismiss}>
          Dismiss
        </Button>
      </div>

      {sorted.length > 0 && (
        <ul className="mt-2 divide-y divide-black/5 border-t border-black/5 pt-1 dark:divide-white/5 dark:border-white/5">
          {sorted.map((flag, i) => (
            <FlagRow key={`${flag.field}-${flag.line_no}-${i}`} flag={flag} />
          ))}
        </ul>
      )}

      {/*
        Answered findings stay on the page, struck through, rather than
        vanishing. A count that silently drops looks the same whether the thing
        was fixed or quietly dropped, and this panel's only job is to be
        believed. Kept behind a summary so it never crowds what is still open.
      */}
      {done.length > 0 && (
        <details className="mt-2 border-t border-black/5 pt-1 dark:border-white/5">
          <summary className="cursor-pointer text-xs font-medium text-emerald-700 dark:text-emerald-300">
            {done.length} answered on the form
          </summary>
          <ul className="divide-y divide-black/5 dark:divide-white/5">
            {done.map((flag, i) => (
              <FlagRow
                key={`${flag.field}-${flag.line_no}-${i}`}
                flag={flag}
                settled
              />
            ))}
          </ul>
        </details>
      )}

      <p className="mt-2 text-[11px] text-slate-500">
        Nothing has been received yet. Check the rows against the cartons, then
        press Receive into stock as usual.
      </p>
    </div>
  );
}
