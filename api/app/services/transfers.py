"""Stock transfers between locations, and stock adjustments.

A transfer is where the IN_TRANSIT status earns its place: between dispatch
and receipt the stock is on a truck, and it must remain visible and countable
rather than vanishing from the system (ARCHITECTURE.md §21.2).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.models.documents import (
    DocumentStatus,
    StockAdjustment,
    StockAdjustmentLine,
    StockTransfer,
    StockTransferLine,
)
from app.models.enums import MovementType, StockStatus
from app.models.masters import Product, Warehouse
from app.services import allocation, ledger, numbering


def create_transfer(
    db: Session,
    *,
    from_warehouse_id: int,
    to_warehouse_id: int,
    lines: list[dict],
    user_id: int,
    notes: str | None = None,
) -> StockTransfer:
    if from_warehouse_id == to_warehouse_id:
        raise ValidationError("Source and destination must be different")
    for wid in (from_warehouse_id, to_warehouse_id):
        if db.get(Warehouse, wid) is None:
            raise NotFoundError(f"Warehouse {wid} not found")
    if not lines:
        raise ValidationError("A transfer needs at least one line")

    # Check every line against what is actually on the shelf BEFORE creating
    # anything. A draft that cannot possibly dispatch is not a useful record —
    # it is a typo the user finds out about two screens later, after approving
    # it. Validating here also avoids burning a transfer number on it.
    #
    # This is a check, not a hold: a draft reserves nothing, so stock can still
    # be gone by dispatch time. Dispatch re-checks under a row lock and remains
    # the authority. This one exists to catch the mistake while the person who
    # made it is still looking at the form.
    products: list[Product] = []
    errors: list[dict[str, str]] = []
    for index, line in enumerate(lines):
        product = db.get(Product, line["product_id"])
        if product is None:
            raise NotFoundError(f"Product {line['product_id']} not found")
        products.append(product)

        quantity = Decimal(str(line["quantity"]))
        field = f"lines.{index}.quantity"
        if quantity <= 0:
            errors.append({"field": field, "message": "Quantity must be more than zero."})
            continue

        lot_id = line.get("lot_id")
        on_hand = allocation.available(
            db,
            product_id=product.id,
            warehouse_id=from_warehouse_id,
            lot_id=lot_id,
        )
        if quantity > on_hand:
            where = "in that batch" if lot_id else "at the source location"
            errors.append(
                {
                    "field": field,
                    "message": (
                        f"Only {allocation.fmt_qty(on_hand)} available {where} — "
                        f"asked for {allocation.fmt_qty(quantity)}."
                    ),
                }
            )

    if errors:
        raise ValidationError("Some lines ask for more stock than is available", errors)

    transfer = StockTransfer(
        transfer_number=numbering.next_number(db, "TRF"),
        from_warehouse_id=from_warehouse_id,
        to_warehouse_id=to_warehouse_id,
        status=DocumentStatus.DRAFT,
        created_by=user_id,
        notes=notes,
    )
    db.add(transfer)
    db.flush()

    for product, line in zip(products, lines, strict=True):
        db.add(
            StockTransferLine(
                stock_transfer_id=transfer.id,
                product_id=product.id,
                lot_id=line.get("lot_id"),
                quantity=Decimal(str(line["quantity"])),
            )
        )
    db.flush()
    return transfer


def approve_transfer(db: Session, transfer_id: int, *, user_id: int) -> StockTransfer:
    transfer = _get_transfer(db, transfer_id)
    if transfer.status not in (DocumentStatus.DRAFT, DocumentStatus.PENDING_APPROVAL):
        raise ConflictError(f"Cannot approve a transfer in state {transfer.status.value}")
    transfer.status = DocumentStatus.APPROVED
    transfer.approved_by = user_id
    db.flush()
    return transfer


def dispatch_transfer(db: Session, transfer_id: int, *, user_id: int) -> StockTransfer:
    """Leg 1: stock leaves the source and appears as IN_TRANSIT at the destination.

    Two balanced ledger rows per line, so quantity is conserved — nothing is
    briefly invisible while it is on the road.
    """
    transfer = _get_transfer(db, transfer_id)
    if transfer.status != DocumentStatus.APPROVED:
        raise ConflictError(
            f"Transfer must be approved before dispatch (currently {transfer.status.value})"
        )

    for line in sorted(transfer.lines, key=lambda line_: line_.product_id):
        product = db.get(Product, line.product_id)

        # Resolve WHERE the stock physically is. The balance grain includes the
        # bin, so an outbound movement must name the bin holding the stock or
        # it would look at an empty row.
        slices = allocation.allocate(
            db,
            product_id=line.product_id,
            warehouse_id=transfer.from_warehouse_id,
            quantity=line.quantity,
            min_shelf_life_days=0,
            lot_id=line.lot_id,
        )
        lots = {s.lot_id for s in slices}
        if len(lots) > 1:
            raise ValidationError(
                f"{product.sku}: quantity spans multiple batches. "
                "Create one transfer line per batch."
            )
        line.lot_id = slices[0].lot_id

        # One outbound movement per source bin...
        for slice_ in slices:
            ledger.post_movement(
                db,
                product_id=line.product_id,
                warehouse_id=transfer.from_warehouse_id,
                quantity=-slice_.quantity,
                movement_type=MovementType.TRANSFER_DISPATCH,
                user_id=user_id,
                bin_id=slice_.bin_id,
                lot_id=slice_.lot_id,
                status=StockStatus.AVAILABLE,
                reference_type="TRANSFER",
                reference_id=transfer.id,
                notes=f"Dispatch {transfer.transfer_number}",
            )

        # ...and one IN_TRANSIT row at the destination. Stock on a truck is in
        # no bin, which is exactly what bin_id=None means here.
        ledger.post_movement(
            db,
            product_id=line.product_id,
            warehouse_id=transfer.to_warehouse_id,
            quantity=line.quantity,
            movement_type=MovementType.TRANSFER_DISPATCH,
            user_id=user_id,
            lot_id=line.lot_id,
            status=StockStatus.IN_TRANSIT,
            reference_type="TRANSFER",
            reference_id=transfer.id,
            notes=f"In transit from {transfer.from_warehouse.name}",
        )

    transfer.status = DocumentStatus.IN_TRANSIT
    transfer.dispatched_at = datetime.now(UTC)
    db.flush()
    return transfer


def receive_transfer(db: Session, transfer_id: int, *, user_id: int) -> StockTransfer:
    """Leg 2: IN_TRANSIT becomes AVAILABLE at the destination."""
    transfer = _get_transfer(db, transfer_id)
    if transfer.status != DocumentStatus.IN_TRANSIT:
        raise ConflictError(
            f"Only in-transit transfers can be received (currently {transfer.status.value})"
        )

    for line in sorted(transfer.lines, key=lambda line_: line_.product_id):
        product = db.get(Product, line.product_id)
        put_away_bin = allocation.default_bin(db, transfer.to_warehouse_id, product)

        # Two legs: leave IN_TRANSIT (bin-less), arrive AVAILABLE in a bin.
        ledger.post_movement(
            db,
            product_id=line.product_id,
            warehouse_id=transfer.to_warehouse_id,
            quantity=-line.quantity,
            movement_type=MovementType.TRANSFER_RECEIPT,
            user_id=user_id,
            lot_id=line.lot_id,
            status=StockStatus.IN_TRANSIT,
            reference_type="TRANSFER",
            reference_id=transfer.id,
            notes=f"Received {transfer.transfer_number}",
        )
        ledger.post_movement(
            db,
            product_id=line.product_id,
            warehouse_id=transfer.to_warehouse_id,
            quantity=line.quantity,
            movement_type=MovementType.TRANSFER_RECEIPT,
            user_id=user_id,
            bin_id=put_away_bin,
            lot_id=line.lot_id,
            status=StockStatus.AVAILABLE,
            reference_type="TRANSFER",
            reference_id=transfer.id,
            notes=f"Put away from {transfer.transfer_number}",
        )
        line.qty_received = line.quantity

    transfer.status = DocumentStatus.COMPLETED
    transfer.received_at = datetime.now(UTC)
    db.flush()
    return transfer


# --- adjustments ------------------------------------------------------------


def create_adjustment(
    db: Session,
    *,
    warehouse_id: int,
    reason_code: str,
    lines: list[dict],
    user_id: int,
    notes: str | None = None,
) -> StockAdjustment:
    if not lines:
        raise ValidationError("An adjustment needs at least one line")

    adjustment = StockAdjustment(
        adjustment_number=numbering.next_number(db, "ADJ"),
        warehouse_id=warehouse_id,
        reason_code=reason_code,
        status=DocumentStatus.PENDING_APPROVAL,
        created_by=user_id,
        notes=notes,
    )
    db.add(adjustment)
    db.flush()

    for line in lines:
        if Decimal(line["quantity"]) == 0:
            raise ValidationError("Adjustment quantity cannot be zero")
        db.add(
            StockAdjustmentLine(
                stock_adjustment_id=adjustment.id,
                product_id=line["product_id"],
                lot_id=line.get("lot_id"),
                bin_id=line.get("bin_id"),
                quantity=Decimal(line["quantity"]),
            )
        )
    db.flush()
    return adjustment


def approve_adjustment(
    db: Session, adjustment_id: int, *, user_id: int
) -> StockAdjustment:
    """Approval is what posts the adjustment to the ledger.

    Separation of duties: whoever raised it cannot approve it.
    """
    adjustment = db.scalar(
        select(StockAdjustment)
        .options(selectinload(StockAdjustment.lines))
        .where(StockAdjustment.id == adjustment_id)
    )
    if adjustment is None:
        raise NotFoundError(f"Adjustment {adjustment_id} not found")
    if adjustment.status != DocumentStatus.PENDING_APPROVAL:
        raise ConflictError(f"Cannot approve in state {adjustment.status.value}")
    if adjustment.created_by == user_id:
        raise ValidationError(
            "A stock adjustment must be approved by someone other than its creator"
        )

    for line in sorted(adjustment.lines, key=lambda line_: line_.product_id):
        product = db.get(Product, line.product_id)
        note = f"{adjustment.adjustment_number}: {adjustment.reason_code}"

        if line.quantity < 0 and line.bin_id is None:
            # Outbound with no bin named: find where the stock actually sits.
            # min_shelf_life_days=0 because a write-off must be able to reach
            # near-expiry and expired stock.
            for slice_ in allocation.allocate(
                db,
                product_id=line.product_id,
                warehouse_id=adjustment.warehouse_id,
                quantity=-line.quantity,
                min_shelf_life_days=0,
                lot_id=line.lot_id,
            ):
                ledger.post_movement(
                    db,
                    product_id=line.product_id,
                    warehouse_id=adjustment.warehouse_id,
                    quantity=-slice_.quantity,
                    movement_type=MovementType.ADJUSTMENT,
                    user_id=user_id,
                    bin_id=slice_.bin_id,
                    lot_id=slice_.lot_id,
                    reference_type="ADJUSTMENT",
                    reference_id=adjustment.id,
                    notes=note,
                )
            continue

        ledger.post_movement(
            db,
            product_id=line.product_id,
            warehouse_id=adjustment.warehouse_id,
            quantity=line.quantity,
            movement_type=MovementType.ADJUSTMENT,
            user_id=user_id,
            bin_id=line.bin_id
            or allocation.default_bin(db, adjustment.warehouse_id, product),
            lot_id=line.lot_id,
            reference_type="ADJUSTMENT",
            reference_id=adjustment.id,
            notes=note,
        )

    adjustment.status = DocumentStatus.COMPLETED
    adjustment.approved_by = user_id
    db.flush()
    return adjustment


def _get_transfer(db: Session, transfer_id: int) -> StockTransfer:
    transfer = db.scalar(
        select(StockTransfer)
        .options(
            selectinload(StockTransfer.lines),
            selectinload(StockTransfer.from_warehouse),
            selectinload(StockTransfer.to_warehouse),
        )
        .where(StockTransfer.id == transfer_id)
    )
    if transfer is None:
        raise NotFoundError(f"Transfer {transfer_id} not found")
    return transfer
