/**
 * The answer to a plain-English question, drawn for a plain-English reader.
 *
 * What arrives here is a SQL result: an ordered list of column names and a
 * list of rows. What has to leave is something a pharmacist can act on without
 * knowing that a database was involved — a sentence, then a shape.
 *
 * Three rules run through the whole file.
 *
 * A single number is an answer; a one-cell table is not. The `stat` case gets
 * the number at the size of a headline with a plain label under it, because
 * "how much stock is expiring this month" deserves to be answered rather than
 * tabulated.
 *
 * The chart hint is a hint. The server decides it from the shape and types of
 * the result and is careful about it, but every branch below re-checks the
 * shape it needs and falls back to the table if the result does not fit. A
 * wrong picture is a second confident claim on the same screen as the number.
 *
 * This component asserts nothing about the data. The sentence above the result
 * is the server's, built from the rows and only the rows; the fallback here can
 * do no more than count them. Totalling a column client-side would look like
 * the same kind of sentence and would silently be a claim about a capped list.
 */

import { useMemo, type ReactElement } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  LabelList,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { SearchX } from "lucide-react";
import { cn, moneyShort } from "@/lib/format";
import { DataTable, type Column } from "./DataTable";
import { Card, EmptyState, TableSkeleton } from "./ui";
import {
  cellKind,
  formatCell,
  humaniseColumn,
  humaniseSummary,
  isNumericKind,
  statUnit,
  toNumber,
  type CellKind,
} from "./answerFormat";

export type ChartHint = "stat" | "bar" | "line" | "table";

export interface AnswerViewProps {
  /** Column names exactly as the database returned them. Never shown raw. */
  columns: string[];
  /** Row-major, in the column order above. Values keep their driver types. */
  rows: unknown[][];
  chartHint: ChartHint;
  /** One sentence about the rows, written by the server from the rows. */
  summary: string;
  loading?: boolean;
}

/**
 * Every colour is a design token, so a chart follows the theme the way the
 * rest of the app does and neither mode needs a second set of hex codes. The
 * background is set explicitly because Recharts defaults its tooltip to white,
 * which is invisible text on a dark surface.
 */
const TOOLTIP_STYLE = {
  borderRadius: 8,
  border: "1px solid var(--color-line)",
  background: "var(--color-surface)",
  fontSize: 12,
} as const;

const TOOLTIP_LABEL = { color: "var(--color-ink-soft)" } as const;
const TOOLTIP_ITEM = { color: "var(--color-ink)" } as const;
const AXIS_TICK = { fontSize: 11, fill: "var(--color-ink-faint)" } as const;

/** Kinds that can name a bar, and kinds that can sit on a time axis. */
const NAMING: CellKind[] = ["text", "code"];
const TEMPORAL: CellKind[] = ["date", "timestamp"];

/* --------------------------------------------------------------- plotting */

interface Point {
  label: string;
  value: number;
  /** Short enough to sit at the end of a bar — "₹1.2L" rather than ₹1,20,000. */
  display: string;
  /** The full figure, for the tooltip and for the spoken description. */
  exact: string;
}

/**
 * Full precision at the end of every bar and beside every axis tick is noise;
 * it is also what pushes a chart off the side of a phone. The exact figure is
 * one hover away and is in the table underneath either way.
 */
const compact = (value: unknown, kind: CellKind) =>
  kind === "money" ? moneyShort(value as string | number) : formatCell(value, kind);

/**
 * The naming column and the measure column, in whichever order they arrived.
 *
 * `SELECT sum(qty), name` and `SELECT name, sum(qty)` are the same answer, and
 * a model that writes the columns the other way round should not cost the
 * reader their chart.
 */
function pairing(kinds: CellKind[], naming: CellKind[]) {
  const labelIndex = kinds.findIndex((kind) => naming.includes(kind));
  const valueIndex = kinds.findIndex(isNumericKind);
  if (labelIndex < 0 || valueIndex < 0 || labelIndex === valueIndex) return null;
  return { labelIndex, valueIndex };
}

function barPoints(
  rows: unknown[][],
  labelIndex: number,
  valueIndex: number,
  labelKind: CellKind,
  valueKind: CellKind,
): Point[] {
  const points: Point[] = [];
  for (const row of rows) {
    const value = toNumber(row[valueIndex]);
    // A null measure gets no bar. Drawing it as zero would invent a reading
    // the query never returned — "we sold none" instead of "we did not ask".
    if (value === null) continue;
    points.push({
      label: formatCell(row[labelIndex], labelKind),
      value,
      display: compact(row[valueIndex], valueKind),
      exact: formatCell(row[valueIndex], valueKind),
    });
  }
  return points.sort((a, b) => b.value - a.value);
}

/** "05 Aug", or "05 Aug 26" once the series crosses a new year. */
function axisDate(iso: string, withYear: boolean): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return iso;
  return when.toLocaleDateString("en-IN", {
    day: "2-digit",
    month: "short",
    ...(withYear ? { year: "2-digit" } : {}),
  });
}

function linePoints(
  rows: unknown[][],
  timeIndex: number,
  valueIndex: number,
  valueKind: CellKind,
): Point[] {
  const ordered = rows
    .map((row) => ({ iso: String(row[timeIndex] ?? ""), cell: row[valueIndex] }))
    .filter((row) => row.iso !== "" && toNumber(row.cell) !== null)
    // ISO timestamps sort chronologically as plain text, so the axis still
    // reads left to right when the query came back without an ORDER BY.
    .sort((a, b) => a.iso.localeCompare(b.iso));

  const spansYears =
    ordered.length > 1 &&
    ordered[0].iso.slice(0, 4) !== ordered[ordered.length - 1].iso.slice(0, 4);

  return ordered.map((row) => ({
    label: axisDate(row.iso, spansYears),
    value: toNumber(row.cell) as number,
    display: compact(row.cell, valueKind),
    exact: formatCell(row.cell, valueKind),
  }));
}

/**
 * One bar's name: short, and on a single line.
 *
 * Recharts breaks a tick at its spaces to fit the axis, so "Bandra Kurla
 * Complex" arrives as two lines while "Dadar TT Circle" stays as one, and a
 * chart where some names wrap and others do not looks broken rather than full.
 * Non-breaking spaces leave it nowhere to break; the clipping is then this
 * function's decision alone, and the whole name is in the tooltip and in the
 * table underneath.
 */
const truncate = (text: string) =>
  (text.length > 16 ? `${text.slice(0, 15)}…` : text).replace(/ /g, "\u00a0");

function barAlternative(measure: string, category: string, points: Point[]): string {
  const [first] = points;
  const last = points[points.length - 1];
  if (points.length === 1) {
    return `A bar chart with a single bar. ${first.label}: ${first.exact}.`;
  }
  return (
    `A bar chart of ${measure} by ${category}, ${points.length} bars, largest first. ` +
    `Highest is ${first.label} at ${first.exact}; lowest is ${last.label} at ` +
    `${last.exact}. The same figures are in the table below.`
  );
}

function lineAlternative(measure: string, points: Point[]): string {
  const [first] = points;
  const last = points[points.length - 1];
  if (points.length === 1) {
    return `A chart with a single reading. ${first.label}: ${first.exact}.`;
  }
  const peak = points.reduce((best, point) => (point.value > best.value ? point : best));
  return (
    `A line chart of ${measure} over time, ${points.length} readings from ` +
    `${first.label} to ${last.label}. It starts at ${first.exact} and ends at ` +
    `${last.exact}, and its highest point is ${peak.exact} on ${peak.label}. ` +
    `The same figures are in the table below.`
  );
}

/* ------------------------------------------------------------ chart frames */

/**
 * The chart, its spoken description, and the numbers it was drawn from.
 *
 * The plot itself is hidden from assistive technology — an SVG of forty tick
 * marks announced one at a time is worse than silence — and the `figcaption`
 * carries the description in its place. The table underneath is not only for
 * that reader: "show me the actual numbers" is the first thing anyone asks of
 * a chart they are about to act on.
 */
function ChartCard({
  alternative,
  height,
  category,
  measure,
  points,
  children,
}: {
  alternative: string;
  height: number;
  category: string;
  measure: string;
  points: Point[];
  children: ReactElement;
}) {
  return (
    <Card className="px-2 pt-4 pb-3 sm:px-4">
      <figure className="m-0">
        <div className="w-full" style={{ height }} aria-hidden>
          <ResponsiveContainer width="100%" height="100%">
            {children}
          </ResponsiveContainer>
        </div>
        <figcaption className="sr-only">{alternative}</figcaption>
      </figure>
      <NumbersBehind category={category} measure={measure} points={points} />
    </Card>
  );
}

/**
 * The plotted figures as a table, folded away under the chart.
 *
 * Built from the points rather than from the rows, so it is the same data in
 * the same order as the picture above it — sorted largest first for a bar
 * chart, earliest first for a line. A table underneath a chart that quietly
 * disagreed with it about the order would be worse than no table at all.
 *
 * Written out here rather than handed to `DataTable` because this one has to
 * stay small and quiet beneath a picture, where the card-per-row layout that
 * makes `DataTable` good on a phone would be louder than the chart it explains.
 */
function NumbersBehind({
  category,
  measure,
  points,
}: {
  category: string;
  measure: string;
  points: Point[];
}) {
  return (
    <details className="mt-3 border-t border-line px-2 pt-2">
      <summary className="cursor-pointer text-[13px] font-medium text-ink-soft hover:text-ink">
        Show the numbers behind this chart
      </summary>
      <div className="scroll-x mt-2 rounded-lg border border-line">
        <table className="w-full text-[13px]">
          <thead className="bg-muted/50 text-[11px] tracking-wide text-ink-faint uppercase">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-medium">
                {category}
              </th>
              <th scope="col" className="px-3 py-2 text-right font-medium">
                {measure}
              </th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {points.map((point, index) => (
              // Keyed by position: two branches can share a name, and two
              // readings can share a day once a date is shown without its year.
              <tr key={index}>
                <td className="px-3 py-1.5">{point.label}</td>
                <td className="tnum px-3 py-1.5 text-right">{point.exact}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </details>
  );
}

/* ------------------------------------------------------------- the shapes */

/**
 * How large a single number may be set.
 *
 * Two things decide it, and a CSS clamp on its own only knows one of them.
 * The viewport is the obvious one; the other is that ₹4,200 and
 * ₹12,45,83,002.75 are the same element and the second is three times as wide.
 * Left to one clamp, the long one runs to both edges of its card on a phone,
 * so the longest values step down a size and then scale from there.
 */
function statSize(text: string): string {
  if (text.length > 13) return "text-[clamp(1.5rem,6.5vw,2.5rem)]";
  if (text.length > 9) return "text-[clamp(1.9rem,8vw,3rem)]";
  return "text-[clamp(2.25rem,9vw,3.5rem)]";
}

/**
 * `column` is the raw name and `label` the readable one, and both are needed.
 * The unit comes off the name the database used — `qty_on_hand` is what says
 * "units", and by the time it reads "On hand" that word is gone.
 */
function StatAnswer({
  column,
  label,
  kind,
  value,
}: {
  column: string;
  label: string;
  kind: CellKind;
  value: unknown;
}) {
  const unit = statUnit(column, kind);
  const text = formatCell(value, kind);

  return (
    <Card className="px-6 py-10 text-center">
      <p
        className={cn(
          "tnum leading-none font-semibold tracking-tight text-ink",
          statSize(text),
        )}
      >
        {text}
        {unit && (
          <span className="ml-2 text-[0.34em] font-medium tracking-normal text-ink-faint">
            {unit}
          </span>
        )}
      </p>
      <p className="mt-3 text-sm text-ink-soft">{label}</p>
    </Card>
  );
}

function BarAnswer({
  labels,
  kinds,
  rows,
  labelIndex,
  valueIndex,
}: {
  labels: string[];
  kinds: CellKind[];
  rows: unknown[][];
  labelIndex: number;
  valueIndex: number;
}) {
  const valueKind = kinds[valueIndex];
  const points = barPoints(rows, labelIndex, valueIndex, kinds[labelIndex], valueKind);
  // Enough room per bar to stay tappable and legible, and a floor so that a
  // two-bar answer is still a chart rather than a stripe.
  const height = Math.max(168, points.length * 34 + 16);

  return (
    <ChartCard
      alternative={barAlternative(labels[valueIndex], labels[labelIndex], points)}
      height={height}
      category={labels[labelIndex]}
      measure={labels[valueIndex]}
      points={points}
    >
      <BarChart
        data={points}
        layout="vertical"
        // The right margin is the room the value labels sit in.
        margin={{ top: 0, right: 64, bottom: 0, left: 0 }}
      >
        <CartesianGrid stroke="var(--color-line)" horizontal={false} />
        {/* The axis is hidden because every bar is labelled with its own value,
            which is easier to read than tracing a bar back to a gridline. */}
        <XAxis type="number" hide />
        <YAxis
          type="category"
          dataKey="label"
          width={132}
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          tickFormatter={truncate}
        />
        <Tooltip
          cursor={{ fill: "var(--color-muted)" }}
          contentStyle={TOOLTIP_STYLE}
          labelStyle={TOOLTIP_LABEL}
          itemStyle={TOOLTIP_ITEM}
          formatter={(value) => [formatCell(value, valueKind), labels[valueIndex]]}
        />
        <Bar
          dataKey="value"
          fill="var(--color-brand)"
          radius={[0, 4, 4, 0]}
          barSize={18}
          isAnimationActive={false}
        >
          <LabelList
            dataKey="display"
            position="right"
            fill="var(--color-ink-soft)"
            fontSize={11}
          />
        </Bar>
      </BarChart>
    </ChartCard>
  );
}

function LineAnswer({
  labels,
  kinds,
  rows,
  timeIndex,
  valueIndex,
}: {
  labels: string[];
  kinds: CellKind[];
  rows: unknown[][];
  timeIndex: number;
  valueIndex: number;
}) {
  const valueKind = kinds[valueIndex];
  const points = linePoints(rows, timeIndex, valueIndex, valueKind);

  return (
    <ChartCard
      alternative={lineAlternative(labels[valueIndex], points)}
      height={224}
      category={labels[timeIndex]}
      measure={labels[valueIndex]}
      points={points}
    >
      <LineChart data={points} margin={{ top: 4, right: 12, bottom: 0, left: -8 }}>
        <CartesianGrid stroke="var(--color-line)" vertical={false} />
        <XAxis
          dataKey="label"
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={{ stroke: "var(--color-line)" }}
          // A year of daily readings is 365 ticks in the space of twelve. Keep
          // the ends, which are the dates the reader asked about, and thin the
          // middle until the labels stop colliding.
          interval="preserveStartEnd"
          minTickGap={28}
        />
        <YAxis
          tick={AXIS_TICK}
          tickLine={false}
          axisLine={false}
          width={56}
          tickFormatter={(value: number) => compact(value, valueKind)}
        />
        <Tooltip
          contentStyle={TOOLTIP_STYLE}
          labelStyle={TOOLTIP_LABEL}
          itemStyle={TOOLTIP_ITEM}
          formatter={(value) => [formatCell(value, valueKind), labels[valueIndex]]}
        />
        <Line
          dataKey="value"
          stroke="var(--color-brand)"
          strokeWidth={2}
          dot={false}
          isAnimationActive={false}
        />
      </LineChart>
    </ChartCard>
  );
}

interface ResultRow {
  index: number;
  cells: unknown[];
}

type CardSlot = Column<ResultRow>["card"];

/**
 * Which columns survive on a phone, and where each one lands on the card.
 *
 * `DataTable` hides any column with no slot, so this is the decision about
 * what a nine-column answer looks like at 375px: the first thing with a name
 * leads, up to two more sit under it, and the numbers stack down the right.
 */
function cardSlots(kinds: CellKind[]): CardSlot[] {
  let named = 0;
  let numbers = 0;

  const slots = kinds.map<CardSlot>((kind) => {
    if (isNumericKind(kind)) {
      numbers += 1;
      // Three figures down the right of a card is already dense; the fourth
      // turns a row into a paragraph.
      return numbers <= 3 ? "meta" : undefined;
    }
    named += 1;
    if (named === 1) return "primary";
    return named <= 3 ? "secondary" : undefined;
  });

  // An answer that is all numbers still needs something to lead its card.
  if (slots.length > 0 && !slots.includes("primary")) slots[0] = "primary";
  return slots;
}

function TableAnswer({
  labels,
  kinds,
  rows,
}: {
  labels: string[];
  kinds: CellKind[];
  rows: unknown[][];
}) {
  const slots = cardSlots(kinds);

  const columns: Column<ResultRow>[] = labels.map((label, index) => ({
    // Position, not name: a join can return `name` twice and React needs the
    // two of them to be different columns.
    key: String(index),
    header: label,
    numeric: isNumericKind(kinds[index]),
    card: slots[index],
    render: (row) => {
      const text = formatCell(row.cells[index], kinds[index]);
      return isNumericKind(kinds[index]) ? (
        <span>{text}</span>
      ) : (
        // Composition strings and supplier addresses run to hundreds of
        // characters. Clamping the column keeps the table readable; the title
        // keeps the rest reachable.
        <span className="block max-w-[20rem] truncate" title={text}>
          {text}
        </span>
      );
    },
  }));

  return (
    <Card>
      <DataTable
        columns={columns}
        rows={rows.map((cells, index) => ({ index, cells }))}
        rowKey={(row) => row.index}
      />
    </Card>
  );
}

/* --------------------------------------------------------- the other states */

function Working() {
  return (
    <section aria-busy className="space-y-3">
      <span className="sr-only">Working out the answer.</span>
      <div className="h-4 w-2/3 animate-pulse rounded bg-muted" aria-hidden />
      <Card>
        <TableSkeleton rows={4} />
      </Card>
    </section>
  );
}

/**
 * Nothing came back — a designed state, not an accident.
 *
 * It says what to do next, because "no rows" is almost always a question that
 * was narrower than the asker realised, and it repeats the server's careful
 * wording: an empty result is not evidence that the answer is zero.
 */
/** Named ways to widen a question, rather than "try something else". */
const WIDEN =
  "Try widening it — a longer stretch of time, every branch rather than one, " +
  "or one condition fewer.";

function NothingMatched({ summary, columns }: { summary: string; columns: string[] }) {
  const said =
    humaniseSummary(summary, columns).trim() ||
    "The question ran and matched nothing, which is not the same as the answer being zero.";

  return (
    <section aria-label="Answer">
      <Card>
        <EmptyState
          icon={SearchX}
          title="Nothing matched that question"
          description={`${said} ${WIDEN}`}
        />
      </Card>
    </section>
  );
}

/* ------------------------------------------------------------------ answer */

/**
 * Whether the result really is the single number the hint says it is.
 *
 * The hint is decided server-side from the shape, so this should always agree
 * with it — but "should" is doing the work in that sentence, and a one-line
 * check is cheaper than a headline number pulled out of a result that had
 * three more columns nobody saw.
 */
const isStat = (chartHint: ChartHint, columns: string[], rows: unknown[][]) =>
  chartHint === "stat" && rows.length === 1 && columns.length === 1;

function AnswerBody({
  columns,
  labels,
  kinds,
  rows,
  chartHint,
}: {
  columns: string[];
  labels: string[];
  kinds: CellKind[];
  rows: unknown[][];
  chartHint: ChartHint;
}) {
  if (isStat(chartHint, columns, rows)) {
    return (
      <StatAnswer
        column={columns[0]}
        label={labels[0]}
        kind={kinds[0]}
        value={rows[0][0]}
      />
    );
  }

  if (chartHint === "bar") {
    const pair = pairing(kinds, NAMING);
    if (pair) {
      return (
        <BarAnswer
          labels={labels}
          kinds={kinds}
          rows={rows}
          labelIndex={pair.labelIndex}
          valueIndex={pair.valueIndex}
        />
      );
    }
  }

  if (chartHint === "line") {
    const pair = pairing(kinds, TEMPORAL);
    if (pair) {
      return (
        <LineAnswer
          labels={labels}
          kinds={kinds}
          rows={rows}
          timeIndex={pair.labelIndex}
          valueIndex={pair.valueIndex}
        />
      );
    }
  }

  return <TableAnswer labels={labels} kinds={kinds} rows={rows} />;
}

export function AnswerView({
  columns,
  rows,
  chartHint,
  summary,
  loading = false,
}: AnswerViewProps) {
  const labels = useMemo(() => columns.map(humaniseColumn), [columns]);
  const kinds = useMemo(
    () => columns.map((column, index) => cellKind(column, rows, index)),
    [columns, rows],
  );

  if (loading) return <Working />;
  if (rows.length === 0) return <NothingMatched summary={summary} columns={columns} />;

  // A stat already says everything the sentence would, and says it better:
  // "Total: 1243000" above ₹12,43,000.00 is one fact twice, in the worse of the
  // two formats. Every other shape gets the sentence, because a table is a
  // thing to read and a sentence is an answer.
  const lead = isStat(chartHint, columns, rows)
    ? ""
    : humaniseSummary(summary, columns).trim() ||
      (rows.length === 1 ? "One result." : `${rows.length} results.`);

  return (
    <section aria-label="Answer" className="space-y-3">
      {lead && <p className="text-[15px] leading-relaxed text-pretty text-ink">{lead}</p>}
      <AnswerBody
        columns={columns}
        labels={labels}
        kinds={kinds}
        rows={rows}
        chartHint={chartHint}
      />
    </section>
  );
}

// Named like every other component in this folder, and default as well because
// the Ask screen imports it that way.
export default AnswerView;
