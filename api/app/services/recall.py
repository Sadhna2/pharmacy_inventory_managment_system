"""Batch recall (ARCHITECTURE.md §6.10).

Two things happen, and both are ordinary SQL on top of the status dimension
and the lot-aware ledger:

  1. UPSTREAM FREEZE   — every unit of the batch still on our shelves, at every
                          branch, moves AVAILABLE -> QUARANTINE in one
                          transaction.
  2. DOWNSTREAM TRACE  — every customer who already received that batch, read
                          off shipment_lines.

This is roughly thirty lines of service code because the data model was built
to answer it. That is the whole argument for the append-only lot-aware ledger.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import and_, func, select
from sqlalchemy.orm import Session

from app.core.errors import ConflictError, NotFoundError
from app.models.documents import (
    Recall,
    SalesOrder,
    Shipment,
    ShipmentLine,
)
from app.models.enums import MovementType, RecallStatus, StockStatus
from app.models.masters import Customer, Warehouse
from app.models.stock import Lot, StockBalance
from app.services import ledger


@dataclass
class LocationImpact:
    warehouse_id: int
    warehouse_name: str
    qty_quarantined: Decimal


@dataclass
class CustomerImpact:
    customer_id: int
    customer_name: str
    quantity: Decimal
    shipment_numbers: list[str]


@dataclass
class RecallImpact:
    recall_id: int
    lot_code: str
    product_sku: str
    product_name: str
    expiry_date: str | None
    total_quarantined: Decimal
    locations: list[LocationImpact]
    customers: list[CustomerImpact]


def initiate_recall(
    db: Session,
    *,
    lot_id: int,
    reason: str,
    user_id: int,
    regulator_ref: str | None = None,
) -> RecallImpact:
    lot = db.get(Lot, lot_id)
    if lot is None:
        raise NotFoundError(f"Batch {lot_id} not found")

    existing = db.scalar(
        select(Recall).where(
            Recall.lot_id == lot_id, Recall.status != RecallStatus.CLOSED
        )
    )
    if existing is not None:
        raise ConflictError(
            f"Batch {lot.lot_code} is already under recall (#{existing.id})"
        )

    recall = Recall(
        lot_id=lot_id,
        reason=reason,
        regulator_ref=regulator_ref,
        status=RecallStatus.INITIATED,
        initiated_by=user_id,
    )
    db.add(recall)
    db.flush()

    # --- 1. upstream freeze -------------------------------------------------
    holdings = db.execute(
        select(StockBalance, Warehouse)
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .where(
            and_(
                StockBalance.lot_id == lot_id,
                StockBalance.status == StockStatus.AVAILABLE,
                StockBalance.qty_on_hand > 0,
            )
        )
        .with_for_update(of=StockBalance)
    ).all()

    locations: list[LocationImpact] = []
    total = Decimal("0")

    for balance, warehouse in holdings:
        # Release any holds first: reserved stock cannot change status while
        # the balance CHECK requires reserved <= on_hand for AVAILABLE rows.
        quantity = balance.qty_on_hand
        if balance.qty_reserved > 0:
            balance.qty_reserved = Decimal("0")
            db.flush()

        ledger.post_status_change(
            db,
            product_id=balance.product_id,
            warehouse_id=balance.warehouse_id,
            quantity=quantity,
            from_status=StockStatus.AVAILABLE,
            to_status=StockStatus.QUARANTINE,
            user_id=user_id,
            lot_id=lot_id,
            bin_id=balance.bin_id,
            movement_type=MovementType.STATUS_CHANGE,
            reference_type="RECALL",
            reference_id=recall.id,
            notes=f"Recall of batch {lot.lot_code}: {reason}",
        )
        locations.append(
            LocationImpact(
                warehouse_id=warehouse.id,
                warehouse_name=warehouse.name,
                qty_quarantined=quantity,
            )
        )
        total += quantity

    recall.qty_quarantined = total
    recall.status = RecallStatus.QUARANTINED
    db.flush()

    return RecallImpact(
        recall_id=recall.id,
        lot_code=lot.lot_code,
        product_sku=lot.product.sku,
        product_name=lot.product.name,
        expiry_date=lot.expiry_date.isoformat() if lot.expiry_date else None,
        total_quarantined=total,
        locations=locations,
        customers=trace_customers(db, lot_id),
    )


def trace_customers(db: Session, lot_id: int) -> list[CustomerImpact]:
    """Who already received this batch? Read straight off shipment_lines."""
    rows = db.execute(
        select(
            Customer.id,
            Customer.name,
            func.sum(ShipmentLine.quantity),
            func.array_agg(func.distinct(Shipment.shipment_number)),
        )
        .join(Shipment, Shipment.id == ShipmentLine.shipment_id)
        .join(SalesOrder, SalesOrder.id == Shipment.sales_order_id)
        .join(Customer, Customer.id == SalesOrder.customer_id)
        .where(ShipmentLine.lot_id == lot_id)
        .group_by(Customer.id, Customer.name)
        .order_by(func.sum(ShipmentLine.quantity).desc())
    ).all()

    return [
        CustomerImpact(
            customer_id=cid,
            customer_name=name,
            quantity=qty,
            shipment_numbers=list(shipments),
        )
        for cid, name, qty, shipments in rows
    ]


def get_impact(db: Session, recall_id: int) -> RecallImpact:
    recall = db.get(Recall, recall_id)
    if recall is None:
        raise NotFoundError(f"Recall {recall_id} not found")

    lot = recall.lot
    quarantined = db.execute(
        select(StockBalance, Warehouse)
        .join(Warehouse, Warehouse.id == StockBalance.warehouse_id)
        .where(
            StockBalance.lot_id == recall.lot_id,
            StockBalance.status == StockStatus.QUARANTINE,
            StockBalance.qty_on_hand > 0,
        )
    ).all()

    return RecallImpact(
        recall_id=recall.id,
        lot_code=lot.lot_code,
        product_sku=lot.product.sku,
        product_name=lot.product.name,
        expiry_date=lot.expiry_date.isoformat() if lot.expiry_date else None,
        total_quarantined=recall.qty_quarantined,
        locations=[
            LocationImpact(
                warehouse_id=w.id, warehouse_name=w.name, qty_quarantined=b.qty_on_hand
            )
            for b, w in quarantined
        ],
        customers=trace_customers(db, recall.lot_id),
    )


def close_recall(db: Session, recall_id: int, *, user_id: int) -> Recall:
    """Scrap the quarantined stock and close the recall."""
    recall = db.get(Recall, recall_id)
    if recall is None:
        raise NotFoundError(f"Recall {recall_id} not found")
    if recall.status == RecallStatus.CLOSED:
        raise ConflictError("This recall is already closed")

    quarantined = db.scalars(
        select(StockBalance).where(
            StockBalance.lot_id == recall.lot_id,
            StockBalance.status == StockStatus.QUARANTINE,
            StockBalance.qty_on_hand > 0,
        )
    ).all()

    for balance in quarantined:
        ledger.post_movement(
            db,
            product_id=balance.product_id,
            warehouse_id=balance.warehouse_id,
            quantity=-balance.qty_on_hand,
            movement_type=MovementType.SCRAP,
            user_id=user_id,
            bin_id=balance.bin_id,
            lot_id=recall.lot_id,
            status=StockStatus.QUARANTINE,
            reference_type="RECALL",
            reference_id=recall.id,
            notes=f"Scrapped under recall #{recall.id}",
        )

    recall.status = RecallStatus.CLOSED
    recall.closed_at = datetime.now(UTC)
    db.flush()
    return recall
