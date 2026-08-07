"""Everything the model is told about this database before it proposes SQL.

The model has no memory between calls, so every question is the first time it
has ever seen this schema. What it is given here is the whole of its knowledge,
and the quality of that briefing is the difference between a query that answers
the question and one that answers a different question convincingly.

WHY THE SCHEMA IS GENERATED AND THE SEMANTICS ARE NOT
-----------------------------------------------------
The table listing and the enum values are read out of SQLAlchemy metadata at
runtime. A hand-written schema is correct on the day it is written and quietly
wrong from the first migration afterwards — and the failure is silent, because
a model told about a column that no longer exists writes SQL that fails, while
a model *not* told about a column that does exist writes SQL that succeeds and
omits it. Generating it means the briefing cannot drift from the database.

The rest cannot be generated, because it is not in the schema. That corrections
are new opposing rows rather than edits, that stock on a truck is counted at
the branch it has not reached yet, that the two years of trading history are
ledger rows and not sales orders — none of that is expressible as a column
type, and all of it changes the answer. Those facts are written by hand, each
one checked against the file named beside it. When one of those files changes
its mind, this text must change with it; the citations are there so the next
person can tell.

SIZE
----
The whole briefing is a prompt prefix paid for on every question, so it is
written dense rather than chatty and held under `TOKEN_BUDGET`. Room bought by
abbreviating a type name is room spent on a trap that would otherwise produce a
confident wrong number.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import INET, JSONB

import app.models  # noqa: F401 — importing binds every table to Base.metadata
from app.db.base import Base

#: Roughly what the finished briefing may cost, counted as the usual four
#: characters to a token. Not enforced at runtime — failing a question because
#: a paragraph was added would be a worse outcome than a slightly longer
#: prompt — but asserted in the tests, so growth stays a decision.
#:
#: Raised from 6000 after a run of twenty real questions against the seeded
#: database: four of them came back with a confident wrong number, and every
#: one was a fact the briefing had not been told (which statuses a stock
#: adjustment actually reaches, that unit_cost is cost rather than price, that
#: nothing here records a payment, that a recall trace must be a LEFT JOIN).
#: Traps 15 to 17 are what that run bought, along with the rolling-window and
#: best-seller paragraphs a second run asked for. The ceiling is a reminder to
#: keep the prose dense, not a reason to leave a wrong answer in.
TOKEN_BUDGET = 8500

#: Columns are packed several to a line up to this width. One column per line
#: is easier to read and costs about two and a half times as much; the packing
#: is the single biggest saving in the generated half.
_LINE_WIDTH = 88

#: Every line inside a table block is indented by this, and it counts against
#: `_LINE_WIDTH`.
_INDENT = "  "


# --- generated: tables ------------------------------------------------------


def _render_type(column: Column[Any]) -> str:
    """A column type, as short as it can be while still answering questions.

    Precision survives only where it changes what a query may assume: a
    NUMERIC's scale says quantities can be fractional, a VARCHAR's length says
    how much room a code has. Everything else collapses to one word.
    """
    type_ = column.type
    if isinstance(type_, SAEnum):
        # The Postgres type name, which is the key into the ENUMS block.
        return type_.name or "enum"
    if isinstance(type_, JSONB):
        return "jsonb"
    if isinstance(type_, INET):
        return "inet"
    if isinstance(type_, Boolean):
        return "bool"
    if isinstance(type_, DateTime):
        return "timestamptz" if type_.timezone else "timestamp"
    if isinstance(type_, Date):
        return "date"
    if isinstance(type_, Numeric):
        if type_.precision is None:
            return "numeric"
        return f"numeric({type_.precision},{type_.scale})"
    # Text and Enum both subclass String, so they are tested before it.
    if isinstance(type_, Text):
        return "text"
    if isinstance(type_, String):
        return f"varchar({type_.length})" if type_.length else "varchar"
    if isinstance(type_, Integer):
        return "int"
    return str(type_).lower()


def _render_column(column: Column[Any]) -> str:
    """One column as `name type [PK|NN] [UQ] [->target]`."""
    parts = [column.name, _render_type(column)]
    if column.primary_key:
        parts.append("PK")  # implies NOT NULL, so NN would be noise
    elif not column.nullable:
        parts.append("NN")
    if column.unique:
        parts.append("UQ")
    parts.extend(
        f"->{fk.target_fullname}"
        for fk in sorted(column.foreign_keys, key=lambda fk: fk.target_fullname)
    )
    return " ".join(parts)


def _render_composite_uniques(table: Table) -> list[str]:
    """Multi-column UNIQUE constraints, which change how a table may be joined.

    Single-column ones already show as UQ beside the column. The composite ones
    are the load-bearing ones here: `lots` is unique on (product_id, lot_code),
    not on lot_code, so two manufacturers' batches can share a code and a join
    on the code alone silently mixes them.
    """
    return [
        f"UNIQUE({', '.join(column.name for column in constraint.columns)})"
        for constraint in sorted(table.constraints, key=lambda c: str(c.name))
        if isinstance(constraint, UniqueConstraint) and len(constraint.columns) > 1
    ]


def _pack(cells: list[str]) -> list[str]:
    """Greedily fill lines with `; `-separated cells, indent included."""
    lines: list[str] = []
    current = ""
    for cell in cells:
        candidate = f"{current}; {cell}" if current else cell
        if current and len(_INDENT) + len(candidate) > _LINE_WIDTH:
            lines.append(current)
            current = cell
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _render_tables() -> str:
    """Every mapped table, in dependency order.

    `sorted_tables` puts a table after everything it references, which means
    the model meets products and warehouses before the documents that point at
    them — the same order a person would read the schema in, and computed
    rather than curated so a new table lands in the right place by itself.
    """
    blocks: list[str] = []
    for table in Base.metadata.sorted_tables:
        rows = _pack([_render_column(column) for column in table.columns])
        rows.extend(_render_composite_uniques(table))
        body = "\n".join(f"{_INDENT}{row}" for row in rows)
        blocks.append(f"TABLE {table.name}\n{body}")
    return "\n".join(blocks)


# --- generated: enums -------------------------------------------------------


def _render_enums() -> str:
    """Every Postgres enum type that some column actually uses.

    Driven off the columns rather than off `models/enums.py` on purpose: an
    enum no column is typed with is a value the model can never legitimately
    write, and listing it would only invite a WHERE clause that matches nothing.
    """
    values: dict[str, tuple[str, ...]] = {}
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if isinstance(column.type, SAEnum) and column.type.name:
                values.setdefault(column.type.name, tuple(column.type.enums))
    return "\n".join(
        f"  {name}: {' | '.join(members)}" for name, members in sorted(values.items())
    )


# --- hand-written: what the schema cannot say -------------------------------

_PREAMBLE = """\
PHARMACY INVENTORY CHAIN — PostgreSQL. One central warehouse, several branch
pharmacies. Four sections follow: the tables, the enum values, how the business
works, and the joins worth knowing. Read all of it before writing a query. The
schema is the easy half; the third section is where the wrong answers come from.

NOTATION: PK primary key | NN not null | UQ unique | ->table.column foreign key.
Timestamps are stored in UTC. Money and quantities are NUMERIC, never float."""

_ENUM_WARNING = """\
ENUMS — Postgres enum types. Use these spellings exactly. A near miss such as
'CANCELED' for 'CANCELLED' or 'IN_TRANSIT' spelled 'TRANSIT' parses fine and
returns zero rows, which reads as "there were none" rather than as a typo."""

_SEMANTICS = """\
HOW THIS BUSINESS WORKS — none of this is visible in the schema, and each one
turns a query that runs into a number that lies.

1. CORRECTIONS ARE NEW OPPOSING ROWS, NEVER EDITS (services/ledger.py::
   reverse_movement — "the original row stays in the ledger forever"). A
   mistake is undone by posting its mirror image: same product, same warehouse,
   same movement_type, negated quantity, reference_type='REVERSAL' and
   reference_id pointing at the row it corrects. THE MOST DANGEROUS TRAP HERE.
   A signed SUM(quantity) nets correctly and is safe — that is the design. Any
   aggregate that discards the sign does not: COUNT(*), SUM(ABS(quantity)),
   AVG, MAX and "how many times did X happen" all see a mistake and its
   correction as two real events. To count events rather than rows, exclude
   both halves of every correction:
     WHERE m.reference_type IS DISTINCT FROM 'REVERSAL'
       AND NOT EXISTS (SELECT 1 FROM stock_movements r
                       WHERE r.reference_type = 'REVERSAL'
                         AND r.reference_id = m.id)

2. BALANCES ARE NOW, MOVEMENTS ARE HISTORY (models/stock.py). stock_balances is
   a projection of the ledger maintained by a database trigger, at the grain
   (product, warehouse, bin, lot, status). Answer "how much do we have" from
   stock_balances. Use stock_movements only for questions about events over
   time — what moved, when, who posted it. Never re-derive a current position
   by summing the ledger; it is slower and it walks straight into trap 1.

3. SELLABLE IS NARROWER THAN PRESENT (services/allocation.py::allocate). Only
   status='AVAILABLE' stock can be sold, and only the part of it that is not
   already spoken for: qty_available = qty_on_hand - qty_reserved, computed,
   not stored. QUARANTINE, DAMAGED, IN_TRANSIT and RETURNED_PENDING stock is
   real, countable and on the books, and none of it can be dispensed. "How much
   do we have" almost always means AVAILABLE; "what is the stock worth" means
   all statuses.

4. IN_TRANSIT IS STOCK ON A TRUCK (services/transfers.py::dispatch_transfer).
   Dispatch writes two rows: AVAILABLE goes negative at the source, and an
   IN_TRANSIT row appears AT THE DESTINATION with bin_id NULL, because the
   goods are in no bin. So a branch's total on-hand includes cartons that have
   not arrived. The application special-cases this — api/v1/masters.py::
   retire_warehouse excludes IN_TRANSIT when checking a location is empty —
   and a per-branch query usually should too.

5. A STATUS CHANGE IS TWO BALANCED ROWS (services/ledger.py::
   post_status_change). Quarantining, releasing and damaging stock post a
   negative row in the old status and a positive row in the new one, same
   movement_type, same quantity. Nothing is created or destroyed. Counting
   those rows doubles the number of events.

6. MRP IS PER BATCH, NOT PER PRODUCT (models/stock.py::Lot). lots.mrp is the
   price printed on that physical pack and a legal ceiling for it. Two batches
   bought a year apart sit on the shelf at once at different printed prices.
   products.mrp is only the default offered for the next receipt (services/
   procurement.py). For what a specific pack may be sold at, read lots.mrp and
   fall back to products.mrp when it is null.

7. CGST+SGST OR IGST, NEVER BOTH (services/gst.py). is_interstate on the
   document decides: same state gets CGST and SGST at half the rate each,
   different states get IGST at the full rate. Total tax is always
   cgst_amount + sgst_amount + igst_amount — the two unused columns are 0.00,
   so summing all three is correct and adding a rate to it is not.

8. THE SALES HISTORY IS LEDGER ROWS, NOT DOCUMENTS (app/seed/history.py::
   _sell). Two years of counter trade were generated as bare SALE_ISSUE
   movements — one per product, per branch, per day, notes 'Counter sales' —
   with no sales order and no shipment behind them. sales_orders, shipments and
   shipment_lines hold only a handful of demonstration documents (app/seed/
   showcase.py). Anything about volume, revenue, trend or seasonality must come
   from stock_movements WHERE movement_type='SALE_ISSUE'. A query over
   sales_orders will return a real-looking total that is a rounding error of
   the truth.

9. reference_type/reference_id IS A POLYMORPHIC POINTER, NOT A FOREIGN KEY.
   reference_type says which table reference_id names:
     'GRN'->goods_receipts, 'SHIPMENT'->shipments, 'TRANSFER'->stock_transfers,
     'ADJUSTMENT'->stock_adjustments, 'RECALL'->recalls,
     'REVERSAL'->stock_movements (the row being corrected),
     'MANUAL' (posted by hand from the stock screen, no document),
     'SYNTH' (a row from the seeded two years of history).
   Always constrain reference_type as well as reference_id; ids collide across
   tables and a join on the id alone will match unrelated rows.

   AND JOIN reference_id TO THE TABLE IT NAMES, NOT THE ONE YOU WANT. This is
   the single most common way to get a plausible, empty answer here. A
   'SHIPMENT' movement's reference_id is a shipments.id — it is NOT a
   sales_orders.id, even though both are integers and the join runs happily
   and returns nothing. A sales order reaches the ledger only through its
   shipments:

     stock_movements.reference_id -> shipments.id -> shipments.sales_order_id
       -> sales_orders.id

   so "what did we sell on completed orders at Ahmedabad" is

     FROM stock_movements sm
     JOIN shipments s   ON s.id = sm.reference_id AND sm.reference_type = 'SHIPMENT'
     JOIN sales_orders so ON so.id = s.sales_order_id
     JOIN warehouses w  ON w.id = so.warehouse_id
     WHERE sm.movement_type = 'SALE_ISSUE' AND so.status = 'COMPLETED'
       AND w.name ILIKE '%ahmedabad%'

   The same hop applies to every reference_type: 'GRN' -> goods_receipts.id
   before purchase_orders, 'TRANSFER' -> stock_transfers.id, 'RECALL' ->
   recalls.id. Never join reference_id straight to the document one table
   further out.

10. THE BUSINESS DAY IS ASIA/KOLKATA (core/clock.py::today). occurred_at is
    UTC, so occurred_at::date is a UTC day and files an evening's trade in a
    Mumbai branch under tomorrow. For anything a shopkeeper would call a day,
    group by (occurred_at AT TIME ZONE 'Asia/Kolkata')::date. Use that same
    expression for "today" and "this month" rather than CURRENT_DATE.

    AND A ROLLING WINDOW ENDS TODAY, NOT YESTERDAY. "Last week", "the last 30
    days", "recently" mean up to and including today, so the upper bound is
    open: `>= today - 7` with nothing above it. An exclusive `< today` drops
    this morning's rows, and on a small day that is the whole answer. Only a
    named calendar period — "in July", "last quarter" — ends before today.

11. NEAR-EXPIRY STOCK IS AVAILABLE BUT UNSHIPPABLE (services/allocation.py).
    Batches are picked first-expiring-first, and anything with fewer than 30
    days of shelf life left is refused by default even though its balance rows
    still say AVAILABLE. So "what can we actually ship" is narrower than
    AVAILABLE: join lots and require
    expiry_date > (now() AT TIME ZONE 'Asia/Kolkata')::date + 30.

12. NOTHING IS DELETED (api/v1/masters.py). Retiring a product, supplier,
    customer, warehouse or bin sets is_active = false. Counts of "how many
    suppliers do we have" should filter is_active unless the question is
    explicitly historical.

13. FOUR TABLES EXIST AND ARE ALWAYS EMPTY: forecasts, forecast_runs,
    forecast_accuracy and reorder_suggestions. Nothing in this application
    writes a row to any of them. The forecasting, accuracy and replenishment
    figures are computed from the movement ledger on demand and returned by
    /api/v1/ai/... endpoints; they are never stored (see the Layer 2 note in
    app/main.py, and that no code anywhere constructs these models).

    So do NOT answer a question about a forecast, forecast accuracy, or what
    to reorder by selecting from these tables. The query would be valid, would
    run, and would return nothing — and "no rows" reads as "there is nothing to
    reorder", which is the opposite of the truth and the most dangerous wrong
    answer this system can give. Refuse instead, and say the figures live on
    the Demand forecast and Replenishment screens, which compute them.

    Their columns and foreign keys are still described above because they are
    really in the schema. Present in the schema is not the same as populated,
    and only this note carries that difference.

14. NOBODY SAYS A PLACE'S FULL NAME. The warehouses are stored as "Ahmedabad
    Branch", "Bandra Branch (Residential)", "Andheri Branch (Hospital)",
    "Pune Branch (Commercial)" and "Central Warehouse - Mumbai", and a person
    asking about them says "Ahmedabad", "Bandra", "the central warehouse".

    So match a place, a supplier or a customer the person named in words with
        WHERE w.name ILIKE '%ahmedabad%'
    never with `= 'Ahmedabad'`. Equality returns zero rows, and zero rows for a
    branch that exists reads as "nothing was sold there" rather than "you typed
    the name differently" — a wrong answer with no sign that it is wrong. The
    same goes for suppliers and customers, whose names carry "Pvt Ltd" and
    "Distributors" that nobody says out loud.

    A code — a SKU, a PO number, a lot code — is different: those are exact by
    construction, and a person quoting one is reading it off something. Match
    codes with `=`.

    AND A LOOSE NAME IS NOT A REASON TO ASK BACK. "Insulin" covering two
    products in the catalogue is not an ambiguity — it is how people talk, and
    the answer is one ILIKE that covers both, with "all products whose name
    contains insulin" in the assumptions. Ask back when the MEASURE is unclear
    (units or rupees, which of three definitions of "late"), never when the
    only question is how wide a name should reach. A clarification the data
    could have settled costs the asker a whole round trip to be told what they
    already meant.

15. ONE STATUS ENUM, FOUR DIFFERENT LIFECYCLES. document_status is a single
    Postgres type shared by purchase_orders, sales_orders, stock_transfers and
    stock_adjustments, and each of them walks only part of it. A value that is
    legal for the type but unreachable for that document parses, runs, and
    matches nothing — the empty answer with no sign that it is wrong again.
      purchase_orders    DRAFT -> PENDING_APPROVAL -> APPROVED ->
                         PARTIALLY_RECEIVED -> RECEIVED | CANCELLED
      sales_orders       DRAFT -> APPROVED -> ALLOCATED -> SHIPPED (part gone)
                         -> COMPLETED (all gone) | CANCELLED. Never
                         PENDING_APPROVAL — a sales order needs no second
                         signature (services/sales.py).
      stock_transfers    DRAFT -> PENDING_APPROVAL -> APPROVED -> IN_TRANSIT ->
                         COMPLETED | CANCELLED
      stock_adjustments  PENDING_APPROVAL -> COMPLETED | CANCELLED, and NEVER
                         'APPROVED'. Approving an adjustment is what posts it
                         to the ledger, and it lands on COMPLETED inside that
                         same call (services/transfers.py::approve_adjustment).
                         status='APPROVED' on this table returns nothing, ever.
      PICKED             is in the enum and no code path anywhere sets it.
    "Still open", "outstanding", "not done yet" means every status before the
    terminal one, DRAFT included: a purchase order nobody has approved yet is
    still an order that has not arrived.

16. NO SELLING PRICE, AND NO RECORD OF WHO HAS PAID. Two separate absences,
    and each of them turns an unanswerable question into a confident figure.

    (a) stock_movements.unit_cost IS WHAT THE BATCH COST US, copied from
    lots.purchase_cost (services/ledger.py, seed/history.py). It is never a
    selling price. quantity * unit_cost is cost of goods sold, and returning
    that column under the heading 'revenue' produces a large, plausible number
    that is wrong by the whole margin. Rupee revenue lives only on
    sales_order_lines.unit_price and sales_orders.grand_total, and by trap 8
    those cover a dozen demonstration documents rather than the trading
    history. So "top selling", "best seller", "how much did we sell" over any
    real period is answered in UNITS — -SUM(quantity) — with that said out
    loud in the assumptions. Cost is for valuing what is on the shelf.

    THE HARD RULE, because the word "revenue" in a question is very persuasive
    and this has been got wrong twice: if a query reads SALE_ISSUE movements,
    then unit_cost, purchase_cost and mrp MUST NOT appear anywhere in its
    SELECT list. Not multiplied by quantity, not under an honest-looking alias
    like 'estimated_revenue', not as a COALESCE between the two. Ranking sales
    by cost puts the products with the dearest batches on top and calls them
    the best sellers, and nothing on the screen says the column is not money
    taken. The columns are the product and -SUM(quantity), and the assumption
    that says so is "this database records no selling price, so this ranks by
    units sold". Those three columns are also why the other shape fails:
    joining the ledger to sales_order_lines to hunt for a price is correct SQL
    that matches almost nothing, and returns an empty best-seller list.

    But do NOT refuse the question merely because it said "revenue". "Top 5 by
    revenue" has an answerable question inside it — which products moved the
    most — so answer that one, in units, and put the missing price in the
    assumptions. Refuse only when nothing answerable is left: "what did we take
    last month" is about money and has no ranking hiding in it.

    (b) THERE IS NO PAYMENTS TABLE. Nothing in this database records a rupee
    received or a bill settled; suppliers.payment_terms_days is a contract
    term, not a balance. "Who owes us money", "outstanding amount", "overdue",
    "receivables", "unpaid invoices" and every ageing question therefore have
    no answer here, and SUM(grand_total) is not one: that is what was ordered,
    paid or not, and it counts the walk-in customer who paid at the counter.
    Refuse, and say this system tracks stock and documents, not money received.

17. WHAT STOCK IS WORTH HAS EXACTLY ONE BASIS, AND mrp IS NOT IT (api/v1/
    stock.py, the `stock.view_cost` block). Most balance rows here have no
    batch cost — the lot is null, or its purchase_cost is — so whatever the
    fallback is decides most of the answer, and falling back to mrp values four
    fifths of the shelf at retail. It overstates the holding by about a third
    and disagrees with the figure the Stock screen shows for the same day.
    The basis is the batch's own cost, and failing that the weighted average
    the ledger has already paid for that product:
      COALESCE(l.purchase_cost,
               (SELECT SUM(m.quantity * m.unit_cost) / NULLIF(SUM(m.quantity),0)
                  FROM stock_movements m
                 WHERE m.product_id = sb.product_id
                   AND m.quantity > 0 AND m.unit_cost IS NOT NULL))
    A row with neither contributes nothing, rather than a price it never had.

18. AN AGGREGATE OVER NO ROWS IS NULL, AND NULL ARRIVES AS A BLANK CELL. A
    single-row SUM or AVG that matched nothing comes back empty rather than as
    zero, which reads as a broken query rather than as "there were none". Wrap
    scalar aggregates: COALESCE(SUM(...), 0). Inside a GROUP BY, leave them —
    there the absent row is itself the honest answer.

SMALLER TRAPS
  * Supplier lead time is purchase_orders.order_date to MIN(goods_receipts.
    received_at) per order, not to the last receipt — a split delivery has
    several GRNs and only the first one is the supplier's speed (ai/leadtime/
    service.py).
  * "Late", though, is measured against purchase_orders.expected_date, the date
    the supplier promised. received_at - order_date is lead time, is always the
    larger number, and reported as days late it slanders every supplier.
  * products.reorder_point is one number for the whole chain, not one per
    branch (api/v1/stock.py compares it against total on-hand). Held up against
    a single branch's stock it flags most of the catalogue.
  * A movement's warehouse is where the stock is, not where the document was
    raised: read stock_movements.warehouse_id for "at which branch", and only
    reach for the document's warehouse when the question is about the document.
  * users.warehouse_id NULL means chain-wide access, not "no branch"
    (models/identity.py).
  * goods_receipt_lines.cross_dock_warehouse_id means the line was sent
    straight on to a branch, but the stock was still legally received at the
    GRN's warehouse first, so it appears in both places (services/
    procurement.py::_cross_dock).
  * Closing a recall SCRAPs the quarantined stock, so it leaves the balances
    (services/recall.py::close_recall).
  * stock_movements.quantity is signed: positive in, negative out. Sales and
    write-offs are negative, so "units sold" is -SUM(quantity)."""

_JOIN_PATHS = """\
JOIN PATHS worth knowing

  Which customers received a batch (the recall question — services/recall.py::
  trace_customers):
    lots -> shipment_lines.lot_id -> shipments.id -> sales_orders.id ->
    customers.id
    LEFT JOIN, every hop. By trap 8 most stock left as bare SALE_ISSUE rows
    with no shipment behind them, so a recalled batch usually traces to nobody
    — and inner joins turn "we cannot say where it went" into "this batch was
    never recalled", which is the answer a recall must never give. List the
    recalled lot first and let the customer columns come back null.

  Who posted a movement, and from which branch:
    stock_movements.created_by -> users.id -> roles.id; users.warehouse_id ->
    warehouses.id

  Current stock with names attached:
    stock_balances -> products.id, warehouses.id, bins.id, lots.id (lot_id and
    bin_id are nullable — use LEFT JOIN or untracked products disappear)

  Expiry exposure:
    lots -> stock_balances.lot_id, filtering qty_on_hand > 0. Never read lots
    alone: a batch row survives long after its last pack was sold.

  Supplier delivery performance:
    purchase_orders -> goods_receipts.purchase_order_id -> goods_receipt_lines
    -> products

  What a product costs and from whom:
    products -> product_suppliers -> suppliers (is_preferred marks the default;
    moq and pack_qty constrain what may be ordered)

  A movement back to its document:
    stock_movements.reference_id -> the table named by reference_type (see
    trap 9)

  Held stock back to the order holding it:
    stock_reservations.sales_order_line_id -> sales_order_lines ->
    sales_orders -> customers

  A forecast or a recommendation back to the run that produced it:
    forecasts.run_id / forecast_accuracy.run_id / reorder_suggestions.run_id ->
    forecast_runs.id (seed and cutoff_date make a run reproducible). Listed for
    completeness only — all four tables are empty, always. See trap 13; a join
    along this path returns nothing and must not be used to answer anything."""


# --- assembly ---------------------------------------------------------------


@lru_cache(maxsize=1)
def build_schema_context() -> str:
    """The full briefing, built once and reused.

    Cached because the metadata cannot change while the process is running —
    it is fixed at import time — and rebuilding it per question would pay the
    same reflection cost on every request. Tests that mutate the metadata to
    prove the output is genuinely derived from it must call
    `invalidate_schema_context()` first.
    """
    return "\n\n".join(
        (
            _PREAMBLE,
            _render_tables(),
            f"{_ENUM_WARNING}\n{_render_enums()}",
            _SEMANTICS,
            _JOIN_PATHS,
        )
    )


def invalidate_schema_context() -> None:
    """Drop the cached briefing so the next call rebuilds it from metadata."""
    build_schema_context.cache_clear()
