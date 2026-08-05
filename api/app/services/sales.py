"""Sales orders: create -> allocate (FEFO) -> ship.

Allocation reserves stock. Shipping is where stock actually leaves, and it
records WHICH BATCH went to WHICH CUSTOMER — that link is what makes recall
traceability possible (ARCHITECTURE.md §6.10).
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core import clock
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.documents import (
    DocumentStatus,
    SalesOrder,
    SalesOrderLine,
    Shipment,
    ShipmentLine,
)
from app.models.enums import MovementType, ReservationStatus, StockStatus
from app.models.masters import Customer, Product, Warehouse
from app.models.stock import StockReservation
from app.services import allocation, gst, ledger, numbering


def create_sales_order(
    db: Session,
    *,
    customer_id: int,
    warehouse_id: int,
    lines: list[dict],
    user_id: int,
    order_date: date | None = None,
    notes: str | None = None,
) -> SalesOrder:
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError(f"Customer {customer_id} not found")
    warehouse = db.get(Warehouse, warehouse_id)
    if warehouse is None:
        raise NotFoundError(f"Warehouse {warehouse_id} not found")
    if not lines:
        raise ValidationError("A sales order needs at least one line")

    # Supply is from the warehouse; the customer's state is the destination.
    interstate = gst.is_interstate(warehouse.state_code, customer.state_code)

    so = SalesOrder(
        so_number=numbering.next_number(db, "SO"),
        customer_id=customer_id,
        warehouse_id=warehouse_id,
        status=DocumentStatus.DRAFT,
        order_date=order_date or clock.today(),
        notes=notes,
        created_by=user_id,
        is_interstate=interstate,
        place_of_supply=customer.state_code,
    )
    db.add(so)
    db.flush()

    breakdowns = []
    for line in lines:
        product = db.get(Product, line["product_id"])
        if product is None:
            raise NotFoundError(f"Product {line['product_id']} not found")

        unit_price = Decimal(
            line.get("unit_price") or product.mrp or Decimal("0")
        )
        tax = gst.compute_line_tax(
            quantity=Decimal(line["qty_ordered"]),
            unit_price=unit_price,
            gst_rate=product.gst_rate,
            interstate=interstate,
        )
        breakdowns.append(tax)
        db.add(
            SalesOrderLine(
                sales_order_id=so.id,
                product_id=product.id,
                qty_ordered=Decimal(line["qty_ordered"]),
                unit_price=unit_price,
                taxable_value=tax.taxable_value,
                gst_rate=tax.gst_rate,
                # Frozen onto the line beside the rate it was taxed at, not
                # read off the product later — see TaxLineMixin.
                hsn_code=product.hsn_code,
                cgst_amount=tax.cgst_amount,
                sgst_amount=tax.sgst_amount,
                igst_amount=tax.igst_amount,
                line_total=tax.line_total,
            )
        )

    totals = gst.compute_document_totals(breakdowns)
    so.subtotal = totals.subtotal
    so.tax_total = totals.tax_total
    so.round_off = totals.round_off
    so.grand_total = totals.grand_total

    # Last, because the check needs the order's own total, and the whole thing
    # is one transaction — a refusal here rolls the document back with it.
    _enforce_credit_limit(db, customer, so)

    db.flush()
    return so


def allocate_order(db: Session, so_id: int) -> list[StockReservation]:
    """Reserve stock for every line, choosing batches by FEFO."""
    so = _get_so(db, so_id)
    if so.status not in (DocumentStatus.DRAFT, DocumentStatus.APPROVED):
        raise ConflictError(f"Cannot allocate an order in state {so.status.value}")

    reservations: list[StockReservation] = []
    # Deterministic lock order (by product id) to avoid deadlocks between two
    # concurrent multi-line allocations.
    for line in sorted(so.lines, key=lambda line_: line_.product_id):
        outstanding = line.qty_ordered - line.qty_shipped
        if outstanding <= 0:
            continue
        reservations.extend(
            allocation.reserve(
                db,
                product_id=line.product_id,
                warehouse_id=so.warehouse_id,
                quantity=outstanding,
                sales_order_line_id=line.id,
            )
        )

    so.status = DocumentStatus.ALLOCATED
    db.flush()
    return reservations


def ship_order(db: Session, so_id: int, *, user_id: int) -> Shipment:
    """Consume the reservations and issue the stock.

    Records lot -> customer on every shipment line, which is what a recall
    later walks backwards.
    """
    so = _get_so(db, so_id)
    if so.status != DocumentStatus.ALLOCATED:
        raise ConflictError(
            f"Order must be allocated before shipping (currently {so.status.value})"
        )

    shipment = Shipment(
        shipment_number=numbering.next_number(db, "SHP"),
        sales_order_id=so.id,
        shipped_by=user_id,
    )
    db.add(shipment)
    db.flush()

    line_by_id = {line.id: line for line in so.lines}
    reservations = db.scalars(
        select(StockReservation).where(
            StockReservation.sales_order_line_id.in_(list(line_by_id)),
            StockReservation.status == ReservationStatus.ACTIVE,
        )
    ).all()

    if not reservations:
        raise ConflictError("No active reservations for this order")

    for reservation in reservations:
        line = line_by_id[reservation.sales_order_line_id]

        # Drop the hold first, then issue — otherwise the balance CHECK sees
        # reserved > on_hand momentarily.
        allocation.consume(db, reservation)

        ledger.post_movement(
            db,
            product_id=reservation.product_id,
            warehouse_id=reservation.warehouse_id,
            quantity=-reservation.quantity,
            movement_type=MovementType.SALE_ISSUE,
            user_id=user_id,
            bin_id=reservation.bin_id,
            lot_id=reservation.lot_id,
            status=StockStatus.AVAILABLE,
            reference_type="SHIPMENT",
            reference_id=shipment.id,
            notes=f"Shipment {shipment.shipment_number}",
        )

        db.add(
            ShipmentLine(
                shipment_id=shipment.id,
                sales_order_line_id=line.id,
                product_id=reservation.product_id,
                lot_id=reservation.lot_id,
                quantity=reservation.quantity,
            )
        )
        line.qty_shipped += reservation.quantity

    so.status = (
        DocumentStatus.COMPLETED
        if all(line.qty_shipped >= line.qty_ordered for line in so.lines)
        else DocumentStatus.SHIPPED
    )
    db.flush()
    return shipment


def cancel_order(db: Session, so_id: int) -> SalesOrder:
    """Release any held stock and close the order."""
    so = _get_so(db, so_id)
    if so.status in (DocumentStatus.COMPLETED, DocumentStatus.CANCELLED):
        raise ConflictError(f"Cannot cancel an order in state {so.status.value}")

    reservations = db.scalars(
        select(StockReservation).where(
            StockReservation.sales_order_line_id.in_([line.id for line in so.lines]),
            StockReservation.status == ReservationStatus.ACTIVE,
        )
    ).all()
    for reservation in reservations:
        allocation.release(db, reservation)

    so.status = DocumentStatus.CANCELLED
    db.flush()
    return so


def _enforce_credit_limit(
    db: Session, customer: Customer, order: SalesOrder
) -> None:
    """Refuse an order that takes a customer past their credit limit.

    Be honest about what this measures: OPEN ORDER EXPOSURE, not a receivable.
    Payments are out of scope for this system (SRS §9), so nothing here knows
    what has been invoiced, what has been paid, or what is overdue — there is
    no unpaid balance to compute and pretending otherwise would put a number
    on screen that no one could reconcile. What the system does know is what
    it has promised and not yet closed out, so that is what is capped: every
    order for this customer that is neither cancelled nor completed, plus the
    one being raised.

    Two consequences worth stating, because they will surprise someone. A
    customer's headroom frees up when their order is delivered, not when they
    pay — so a delivered-but-unpaid order stops counting against them, which a
    real credit control would never allow. And goods still sitting on our own
    shelf, promised but not yet picked, do count against them, which is
    stricter than a receivable. It is a brake on how much is out on order at
    once, and it should be described that way to whoever asks.

    A limit of zero means NO limit, which is what the seed means by it: only
    institutional customers are given a figure and the walk-in counter is left
    at zero. Reading zero as "refuse everything" would close the shop.
    """
    if customer.credit_limit <= 0:
        return

    # The new order already has a row of its own by now, so it is excluded
    # here and added back from the object — the row still holds the zero it
    # was inserted with, which would quietly understate the exposure.
    open_orders = db.scalar(
        select(func.coalesce(func.sum(SalesOrder.grand_total), 0)).where(
            SalesOrder.customer_id == customer.id,
            SalesOrder.id != order.id,
            SalesOrder.status.notin_(
                [DocumentStatus.CANCELLED, DocumentStatus.COMPLETED]
            ),
        )
    )
    committed = Decimal(open_orders or 0)
    exposure = committed + order.grand_total
    if exposure <= customer.credit_limit:
        return

    over = exposure - customer.credit_limit
    headroom = max(customer.credit_limit - committed, Decimal("0"))
    raise ValidationError(
        f"{customer.name} has ₹{committed:,.2f} committed on open orders "
        f"against a credit limit of ₹{customer.credit_limit:,.2f} — this "
        f"order of ₹{order.grand_total:,.2f} would put them ₹{over:,.2f} "
        f"over it",
        [{"field": "customer_id", "message": (
            f"₹{headroom:,.2f} of the ₹{customer.credit_limit:,.2f} limit is left."
        )}],
    )


def _get_so(db: Session, so_id: int) -> SalesOrder:
    so = db.scalar(
        select(SalesOrder)
        .options(selectinload(SalesOrder.lines))
        .where(SalesOrder.id == so_id)
    )
    if so is None:
        raise NotFoundError(f"Sales order {so_id} not found")
    return so


# --- what this customer last paid --------------------------------------------


@dataclass
class SuggestedPrice:
    product_id: int
    unit_price: Decimal
    #: "last_charged", "mrp", or "none" — the screen says which, because the
    #: two mean different things to whoever is about to accept the number.
    source: str
    last_charged_on: date | None = None


def suggest_price(
    db: Session, *, customer_id: int, product_id: int
) -> SuggestedPrice:
    """What to put in the price box before anyone types.

    An empty box with MRP as placeholder was accurate and useless: the price
    every institutional buyer actually pays is the one agreed with them, and
    the person taking the order was expected to remember it per product. Left
    blank, the order silently went out at MRP — the list price, which is the
    one number an institutional customer is certainly not paying.

    So the last price this customer was actually charged for this product,
    falling back to MRP when there is no history. Cancelled orders are
    excluded: a price on an order that was withdrawn was never agreed to.

    A suggestion, not a rule. The field stays editable, and nothing here is
    written — the price that counts is whatever is on the order when it is
    raised.
    """
    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")

    last = db.execute(
        select(SalesOrderLine.unit_price, SalesOrder.order_date)
        .join(SalesOrder, SalesOrder.id == SalesOrderLine.sales_order_id)
        .where(
            SalesOrder.customer_id == customer_id,
            SalesOrderLine.product_id == product_id,
            SalesOrder.status != DocumentStatus.CANCELLED,
        )
        # By date first, because that is what "last" means to a person; by id
        # to break a tie within a day, which the seed produces in quantity.
        .order_by(SalesOrder.order_date.desc(), SalesOrder.id.desc())
        .limit(1)
    ).first()

    if last is not None:
        return SuggestedPrice(
            product_id=product_id,
            unit_price=Decimal(last[0]),
            source="last_charged",
            last_charged_on=last[1],
        )
    if product.mrp:
        return SuggestedPrice(
            product_id=product_id, unit_price=Decimal(product.mrp), source="mrp"
        )
    return SuggestedPrice(
        product_id=product_id, unit_price=Decimal("0"), source="none"
    )


# --- planning a split across branches ----------------------------------------


@dataclass
class PlannedLine:
    product_id: int
    product_name: str
    sku: str
    quantity: Decimal
    unit_price: Decimal


@dataclass
class Alternative:
    """Another branch that could supply this order's lines instead.

    Carries its own tax split and total, because switching branch can change
    both: the same lines out of Ahmedabad rather than Mumbai are IGST rather
    than CGST + SGST, and the rounding follows.
    """

    warehouse_id: int
    warehouse_name: str
    state_code: str
    is_interstate: bool
    subtotal: Decimal
    tax_total: Decimal
    grand_total: Decimal


@dataclass
class PlannedOrder:
    """One warehouse's share of a request: exactly one ordinary sales order."""

    warehouse_id: int
    warehouse_name: str
    state_code: str
    is_interstate: bool
    lines: list[PlannedLine]
    subtotal: Decimal
    tax_total: Decimal
    grand_total: Decimal
    #: Branches that could take this order's lines in their entirety instead.
    #: A recommendation is not a ruling — someone who knows a branch is short
    #: staffed, or that a customer collects from one, overrides it here.
    alternatives: list[Alternative]


@dataclass
class Shortfall:
    product_id: int
    product_name: str
    requested: Decimal
    planned: Decimal


@dataclass
class FulfilmentPlan:
    orders: list[PlannedOrder]
    shortfalls: list[Shortfall]


def _totals_from(
    branch: Warehouse,
    customer: Customer,
    lines: list[PlannedLine],
    products: dict[int, Product],
) -> tuple[bool, gst.DocumentTotals]:
    """What these lines would come to, shipped from this branch.

    The branch decides the tax split, so the same lines are a different total
    depending where they leave from — which is exactly why an alternative has
    to carry its own figures rather than reuse the recommendation's.
    """
    interstate = gst.is_interstate(branch.state_code, customer.state_code)
    breakdowns = [
        gst.compute_line_tax(
            quantity=line.quantity,
            unit_price=line.unit_price,
            gst_rate=products[line.product_id].gst_rate,
            interstate=interstate,
        )
        for line in lines
    ]
    return interstate, gst.compute_document_totals(breakdowns)


def plan_fulfilment(
    db: Session,
    *,
    customer_id: int,
    lines: list[dict],
    warehouse_ids: list[int] | None = None,
) -> FulfilmentPlan:
    """Work out which branches, together, can supply a request.

    Why this exists. A sales order ships from one warehouse, and until now the
    person taking the order had to name it before they knew what was in it.
    Pick wrong and nothing said so: `create_sales_order` checks no stock, so
    the document saved happily and the refusal arrived at *allocation*, by
    which point a document existed for an order that could never ship.

    So this reads availability first and proposes a set of ordinary orders,
    one per branch. Nothing is written and nothing is reserved — it is a plan,
    and stock can move between reading it and acting on it, which is why the
    orders it proposes are still created through the ordinary route and are
    still refused by the ordinary checks.

    One order per branch, deliberately, rather than one order drawing on
    several. GST registers per state, so a branch in another state is a
    separately registered person: a single document covering both would need
    two supplier GSTINs and two tax splits, and Rule 46 allows one of each.
    Splitting is not a workaround for that — it is what the statute describes.

    How the branches are chosen. Greedily, taking the branch that settles the
    most at each step, because the count of orders is what the person raising
    them pays for — three documents to approve, three to invoice, three to
    deliver. Whole lines first, then quantity, so a branch that closes two
    products outright is preferred to one that half-fills four. Ties go to a
    branch in the customer's own state, which keeps the supply intra-state and
    the invoice CGST + SGST; the tie-break never costs an extra order, because
    it is only consulted between branches that settle the same amount.

    A line too large for any single branch is split across several. A line the
    whole chain cannot cover is planned as far as it goes and reported as a
    shortfall — a distributor ships what it holds and chases the rest, and
    refusing the other nine lines because of the tenth helps nobody.
    """
    customer = db.get(Customer, customer_id)
    if customer is None:
        raise NotFoundError(f"Customer {customer_id} not found")
    if not lines:
        raise ValidationError("A sales order needs at least one line")

    wanted: dict[int, Decimal] = {}
    price: dict[int, Decimal] = {}
    products: dict[int, Product] = {}
    for line in lines:
        product = db.get(Product, line["product_id"])
        if product is None:
            raise NotFoundError(f"Product {line['product_id']} not found")
        products[product.id] = product
        # A product named twice is one line for planning purposes; otherwise
        # the two halves would be planned against the same stock twice over.
        wanted[product.id] = wanted.get(product.id, Decimal("0")) + Decimal(
            line["qty_ordered"]
        )
        price[product.id] = (
            Decimal(line["unit_price"])
            if line.get("unit_price")
            else suggest_price(
                db, customer_id=customer_id, product_id=product.id
            ).unit_price
        )

    requested = dict(wanted)

    branches = db.scalars(
        select(Warehouse).where(Warehouse.is_active).order_by(Warehouse.id)
    ).all()
    if warehouse_ids is not None:
        branches = [w for w in branches if w.id in set(warehouse_ids)]

    # Read once. `available` is a query per product per branch, and the loop
    # below revisits every remaining branch on every pass.
    on_hand: dict[int, dict[int, Decimal]] = {
        w.id: {
            product_id: allocation.available(
                db, product_id=product_id, warehouse_id=w.id
            )
            for product_id in wanted
        }
        for w in branches
    }

    orders: list[PlannedOrder] = []
    remaining = {w.id: w for w in branches}

    while any(qty > 0 for qty in wanted.values()) and remaining:
        best: Warehouse | None = None
        best_score: tuple[int, Decimal, int, int] | None = None

        for branch in remaining.values():
            takeable = {
                product_id: min(qty, on_hand[branch.id][product_id])
                for product_id, qty in wanted.items()
                if qty > 0 and on_hand[branch.id][product_id] > 0
            }
            if not takeable:
                continue
            whole = sum(
                1 for pid, qty in takeable.items() if qty >= wanted[pid]
            )
            score = (
                whole,
                sum(takeable.values(), Decimal("0")),
                # Only ever a tie-break: same state keeps the supply
                # intra-state, so the invoice splits CGST + SGST.
                1 if branch.state_code == customer.state_code else 0,
                -branch.id,  # deterministic, so the same request plans alike
            )
            if best_score is None or score > best_score:
                best, best_score = branch, score

        if best is None:
            break

        taken: list[PlannedLine] = []
        for product_id, qty in wanted.items():
            share = min(qty, on_hand[best.id][product_id])
            if share <= 0:
                continue
            wanted[product_id] = qty - share
            taken.append(
                PlannedLine(
                    product_id=product_id,
                    product_name=products[product_id].name,
                    sku=products[product_id].sku,
                    quantity=share,
                    unit_price=price[product_id],
                )
            )

        interstate, totals = _totals_from(best, customer, taken, products)

        orders.append(
            PlannedOrder(
                warehouse_id=best.id,
                warehouse_name=best.name,
                state_code=best.state_code,
                is_interstate=interstate,
                lines=taken,
                subtotal=totals.subtotal,
                tax_total=totals.tax_total,
                grand_total=totals.grand_total,
                alternatives=[],  # filled once the whole plan is known
            )
        )
        del remaining[best.id]

    # Offered only from branches the plan did not use.
    #
    # A branch already carrying one of these orders still has stock left over,
    # and offering it as an alternative for a *different* order would promise
    # the same shelf twice — both orders would pass this check and one would
    # fail at allocation, which is the failure this whole route exists to
    # prevent. Restricting the offer to untouched branches makes that
    # impossible rather than merely unlikely.
    spare = [w for w in branches if w.id in remaining]
    for order in orders:
        order.alternatives = [
            Alternative(
                warehouse_id=branch.id,
                warehouse_name=branch.name,
                state_code=branch.state_code,
                is_interstate=other_interstate,
                subtotal=other.subtotal,
                tax_total=other.tax_total,
                grand_total=other.grand_total,
            )
            for branch in spare
            # In full, or not at all. An alternative that could take four of
            # five lines is not an alternative to this order, it is a
            # different plan.
            if all(
                on_hand[branch.id][line.product_id] >= line.quantity
                for line in order.lines
            )
            for other_interstate, other in [
                _totals_from(branch, customer, order.lines, products)
            ]
        ]

    shortfalls = [
        Shortfall(
            product_id=product_id,
            product_name=products[product_id].name,
            requested=requested[product_id],
            planned=requested[product_id] - short,
        )
        for product_id, short in wanted.items()
        if short > 0
    ]
    return FulfilmentPlan(orders=orders, shortfalls=shortfalls)
