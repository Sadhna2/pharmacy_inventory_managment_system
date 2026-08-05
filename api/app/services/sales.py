"""Sales orders: create -> allocate (FEFO) -> ship.

Allocation reserves stock. Shipping is where stock actually leaves, and it
records WHICH BATCH went to WHICH CUSTOMER — that link is what makes recall
traceability possible (ARCHITECTURE.md §6.10).
"""

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
