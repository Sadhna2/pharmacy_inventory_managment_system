/**
 * Turning a SQL result set into English.
 *
 * A question answered by SQL comes back as raw column names and driver values:
 * `grand_total`, `"1240.50"`, `"2026-08-05T09:14:22"`. Every one of those is a
 * leak of how the system is built, and the person reading the answer runs a
 * pharmacy counter. This module is the translation layer, kept apart from the
 * rendering so each rule is one small pure function that can be reasoned about
 * on its own.
 *
 * Nothing here decides what is *true* — the sentence above the result and the
 * rows themselves come from the server, which built them from the rows and
 * only the rows. All this file decides is wording and units.
 *
 * Two shapes of knowledge live here, and they are separate on purpose:
 *
 *   `PHRASES`  columns whose word-by-word reading is wrong. `qty_on_hand` is
 *              "On hand", not "Quantity on hand" — the noun is already implied
 *              by the column beside it. This list is short and specific.
 *   `WORDS`    a per-word substitution applied to everything else, so a column
 *              nobody anticipated still comes out readable rather than raw.
 */

import { date, dateTime, money, qty } from "@/lib/format";

/**
 * What a column holds, as far as *writing it down* is concerned.
 *
 * Deliberately not the same taxonomy the server uses to pick a chart. That one
 * asks "can this be plotted"; this one asks "does this get a rupee sign, a
 * thousands separator, or neither" — and `code` exists only for this question:
 * a batch number and a rupee amount are both digits, and only one of them may
 * be given commas.
 */
export type CellKind =
  | "money"
  | "quantity"
  | "percent"
  | "date"
  | "timestamp"
  | "code"
  | "text";

/* ------------------------------------------------------------ column names */

/**
 * Words that keep their own casing, and words this domain says differently.
 *
 * A value with a capital in it is stamped in verbatim wherever it appears; an
 * all-lowercase one is re-cased by its position in the phrase. That one rule
 * is what lets `gst` be "GST" in the middle of a header while `warehouse` is
 * "branch" in the middle and "Branch" at the front.
 *
 * `uom` is expanded rather than shouted, because it is the only one of these
 * that is a database word rather than a trade word. GST, MRP, HSN and SKU are
 * printed on the invoices and the strips; "UOM" is printed nowhere.
 */
const WORDS: Record<string, string> = {
  cgst: "CGST",
  gst: "GST",
  gstin: "GSTIN",
  grn: "GRN",
  hsn: "HSN",
  id: "ID",
  igst: "IGST",
  lot: "batch",
  mrp: "MRP",
  no: "number",
  po: "PO",
  qty: "quantity",
  sgst: "SGST",
  sku: "SKU",
  so: "SO",
  uom: "unit",
  warehouse: "branch",
};

/** Columns whose word-by-word reading is clumsy, wrong, or just too long. */
const PHRASES: Record<string, string> = {
  avg_daily_demand: "Sold per day",
  created_at: "Created",
  days_of_cover: "Days of cover",
  expiry_date: "Expires",
  grand_total: "Total",
  gst_rate: "GST",
  hsn_code: "HSN code",
  lead_time_days: "Lead time",
  lot_code: "Batch",
  mfg_date: "Made on",
  moq: "Minimum order",
  qty_available: "Available",
  qty_incoming: "On order",
  qty_on_hand: "On hand",
  qty_ordered: "Ordered",
  qty_quarantined: "Quarantined",
  qty_received: "Received",
  qty_reserved: "Reserved",
  qty_shipped: "Shipped",
  round_off: "Rounding",
  subtotal: "Before tax",
  tax_total: "Tax",
  unit_cost: "Cost each",
  unit_price: "Price each",
  updated_at: "Updated",
  // The stored forecast columns. `yhat` is the notation of the paper the model
  // came from and means nothing at a counter.
  yhat: "Forecast",
  yhat_lower: "Forecast, low",
  yhat_upper: "Forecast, high",
};

/**
 * A column name as a person would say it. `grand_total` → "Total".
 *
 * Sentence case rather than Title Case, because every header already written
 * by hand in this app is sentence case — "Per day", "Off by", "On hand" — and
 * a lone "Per Day" among them reads as a different product. The table styles
 * its headers uppercase anyway; this casing is what shows under the big number
 * on a stat and inside a chart tooltip, where it is read as prose.
 */
export function humaniseColumn(column: string): string {
  const key = column.trim().toLowerCase();
  // Postgres names an unaliased expression `?column?`. It is the one name that
  // means the database had nothing to offer, so neither do we.
  if (!key || key === "?column?") return "Value";

  const phrase = PHRASES[key];
  if (phrase) return phrase;

  // Every boolean in this schema is named `is_something`, and the "Is" adds
  // nothing once the cell beneath it says Yes or No — "Cold chain: Yes" reads
  // as English, "Is cold chain: Yes" reads as a column name.
  const words = key.replace(/^is_/, "").split(/[_\s]+/).filter(Boolean);
  if (words.length === 0) return column;

  return words
    .map((word, index) => {
      const spoken = WORDS[word] ?? word;
      // Already carrying a capital means it is an abbreviation that owns its
      // own casing — GST stays GST at the end of a header, not Gst.
      if (/[A-Z]/.test(spoken)) return spoken;
      return index === 0 ? spoken.charAt(0).toUpperCase() + spoken.slice(1) : spoken;
    })
    .join(" ");
}

/* ------------------------------------------------------------ column kinds */

/**
 * Names whose digits are an identity, not a measurement.
 *
 * Without this, a phone number is rendered "98,76,54,321" and a batch id of
 * 1204 becomes "1,204". Both are the same failure: a separator inserted into
 * something that was never a quantity, which makes it wrong to read out and
 * impossible to search for.
 */
const IDENTIFIER = /(^|_)(id|no|number|code|sku|gstin|barcode|phone|hash|key|ref)$/;

/** Head nouns that mean rupees. English puts the thing being measured last. */
const MONEY_HEADS = new Set([
  "amount",
  "cost",
  "mrp",
  "price",
  "revenue",
  "sales",
  "spend",
  "subtotal",
  "total",
  "value",
  "worth",
]);

/** Head nouns that mean a count of things, in no unit but their own. */
const QUANTITY_HEADS = new Set([
  "count",
  "days",
  "hand",
  "months",
  "qty",
  "quantity",
  "stock",
  "units",
  "weeks",
  "years",
]);

/**
 * `_rate` in this schema means whole percentage points — `gst_rate` is 12.00,
 * not 0.12. A fraction here would render as "0.12%", so if a rate ever starts
 * arriving as a fraction this set is the line to change.
 */
const PERCENT_HEADS = new Set(["rate", "pct", "percent", "percentage"]);

/** Money columns whose head noun gives nothing away. */
const MONEY_EXACT = new Set(["credit_limit", "round_off"]);

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;
const ISO_TIMESTAMP = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}/;
/** A `Numeric` column: JSON has no decimal type, so it crosses as a string. */
const DECIMAL_STRING = /^-?\d+(\.\d+)?$/;

/** The first value in a column that is not NULL, or `null` if there is none. */
function firstPresent(rows: unknown[][], index: number): unknown {
  for (const row of rows) {
    const value = row[index];
    if (value !== null && value !== undefined) return value;
  }
  return null;
}

/**
 * What a numeric column's *name* claims it is, or `null` if it says nothing.
 *
 * The last word decides outright, and the others only get a vote when the last
 * word belongs to none of the sets. English puts the thing being measured at
 * the end, and that ordering is the whole reason `stock_value` is rupees while
 * `total_qty` is units — each contains a word belonging to the other's set, and
 * only the head noun separates them.
 */
function namedKind(column: string): "money" | "quantity" | "percent" | null {
  const words = column.split(/[_\s]+/).filter(Boolean);
  if (words.length === 0) return null;

  const head = words[words.length - 1];
  if (PERCENT_HEADS.has(head)) return "percent";
  if (MONEY_HEADS.has(head)) return "money";
  if (QUANTITY_HEADS.has(head)) return "quantity";

  if (words.some((word) => PERCENT_HEADS.has(word))) return "percent";
  if (words.some((word) => MONEY_HEADS.has(word))) return "money";
  if (words.some((word) => QUANTITY_HEADS.has(word))) return "quantity";
  return null;
}

/**
 * How one column should be written, from its name and its first real value.
 *
 * Reading the first *non-null* value rather than the first row matters: under
 * an outer join the top row is very often null, and a measure demoted to text
 * loses its alignment, its separators and its rupee sign for the whole column.
 *
 * The rupee sign additionally requires the value to have arrived as a string.
 * `SELECT COUNT(*) AS total` and `SELECT SUM(grand_total) AS total` have the
 * same column name and mean utterly different things; the first crosses JSON
 * as the integer 18 and the second as the string "124300.00", because only the
 * `Numeric` column is serialised as text. Insisting on the string is what
 * stops a row count being announced as ₹18.
 */
export function cellKind(column: string, rows: unknown[][], index: number): CellKind {
  const sample = firstPresent(rows, index);
  if (sample === null) return "text";
  if (typeof sample === "boolean") return "text";

  if (typeof sample === "string") {
    if (ISO_TIMESTAMP.test(sample)) return "timestamp";
    if (ISO_DATE.test(sample)) return "date";
  }

  const isDecimal = typeof sample === "string" && DECIMAL_STRING.test(sample);
  const isNumber = typeof sample === "number" || isDecimal;
  if (!isNumber) return "text";

  const key = column.trim().toLowerCase();
  if (IDENTIFIER.test(key)) return "code";

  const named = MONEY_EXACT.has(key) ? "money" : namedKind(key);
  if (named === "percent") return "percent";
  if (named === "money" && isDecimal) return "money";
  // Anything else numeric is a plain count: separators, no unit, no claim.
  return "quantity";
}

/** Whether a column of this kind belongs on the right, in tabular figures. */
export const isNumericKind = (kind: CellKind) =>
  kind === "money" || kind === "quantity" || kind === "percent";

/* ----------------------------------------------------------------- values */

/** All-capital, underscored values are database enums — `PARTIALLY_RECEIVED`. */
const ENUM_VALUE = /^[A-Z][A-Z_]{3,}$/;

/**
 * One cell, written out.
 *
 * Product names, batch codes and free text are returned untouched. Only two
 * kinds of string are rewritten: an ISO date, which nobody outside a terminal
 * reads, and an all-caps enum, which is re-worded exactly the way `StatusBadge`
 * words it so the same status reads the same on every screen.
 */
export function formatCell(value: unknown, kind: CellKind): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "boolean") return value ? "Yes" : "No";

  switch (kind) {
    case "money":
      return money(value as string | number);
    case "quantity":
      return qty(value as string | number);
    case "percent":
      return `${qty(value as string | number)}%`;
    case "date":
      return date(String(value));
    case "timestamp":
      return dateTime(String(value));
    case "code":
      return String(value);
    default: {
      const text = String(value);
      return ENUM_VALUE.test(text)
        ? text.replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase())
        : text;
    }
  }
}

/**
 * The server's sentence about the rows, with the database words taken out.
 *
 * `summarise()` quotes the columns it describes, and it can only quote them as
 * SQL returned them — "warehouse_name Andheri West, expiry_date 2026-11-30".
 * Rewriting those here rather than there is what keeps that function honest:
 * it goes on building its sentence from the rows and nothing else, and no
 * column name a person did not choose reaches the screen.
 *
 * Only two things are rewritten, and both are unambiguous whatever wording the
 * sentence uses: a name from this result's own column list, and an ISO date,
 * which in prose is never anything but a date. The figures are left as the
 * server wrote them — reformatting a number would mean guessing from sentence
 * structure whether it is rupees or units, and the formatted version of every
 * one of them is in the result directly below.
 */
export function humaniseSummary(summary: string, columns: string[]): string {
  const spoken = new Map<string, string>();
  for (const column of columns) {
    // Anything stranger than a plain identifier is left alone rather than
    // interpolated into a regular expression.
    if (/^[a-z_][a-z0-9_]*$/i.test(column)) spoken.set(column, humaniseColumn(column));
  }

  let text = summary;
  if (spoken.size > 0) {
    // One pass over an alternation rather than a replace per column, so a
    // substituted word can never be matched again by a later column. The word
    // boundaries do the rest: `\bqty\b` cannot match inside `qty_on_hand`,
    // because an underscore is a word character.
    const names = [...spoken.keys()].join("|");
    text = text.replace(
      new RegExp(`\\b(${names})\\b`, "g"),
      (match) => spoken.get(match) ?? match,
    );
  }

  return text.replace(
    /\b\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)?\b/g,
    (stamp) => (stamp.length > 10 ? dateTime(stamp) : date(stamp)),
  );
}

/** A cell as a plottable number, or `null` when it is not one. */
export function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean") return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * The unit that belongs beside a single large number.
 *
 * Money and percentages carry their own mark, so this only has to speak for
 * counts — and only when the column name says what is being counted. Guessing
 * "units" for an unnamed number would be inventing a fact about the answer.
 */
export function statUnit(column: string, kind: CellKind): string {
  if (kind !== "quantity") return "";
  const words = column.toLowerCase().split(/[_\s]+/);
  if (words.includes("days")) return "days";
  if (words.some((word) => word === "qty" || word === "quantity" || word === "units"))
    return "units";
  return "";
}
