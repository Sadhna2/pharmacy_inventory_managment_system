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
from app.models.stock import StockMovement
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
        # A quantity that spans two batches is ordinary, not exceptional: FEFO
        # empties the oldest lot and rolls into the next, and asking for 150
        # when the front lot holds 100 is exactly what a branch does. This used
        # to refuse — after approval, with no way to cancel, so the transfer
        # stuck permanently and the message told the raiser to split lines by
        # batches they had no screen to look up. The ledger already carries a
        # lot per row, so the split is simply recorded rather than rejected.
        lots = {s.lot_id for s in slices}
        # The line's own lot names the batch only when there is exactly one.
        # None means "see the ledger" — writing one of several here would
        # label the whole transfer with a batch that was part of it.
        line.lot_id = slices[0].lot_id if len(lots) == 1 else None

        for slice_ in slices:
            # Out of the source bin...
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
            # ...and onto the road, batch by batch. Paired one-for-one with
            # the row above so quantity is conserved per batch and not merely
            # in total, which is what lets receipt put each batch away as
            # itself. Stock on a truck is in no bin — that is `bin_id=None`.
            ledger.post_movement(
                db,
                product_id=line.product_id,
                warehouse_id=transfer.to_warehouse_id,
                quantity=slice_.quantity,
                movement_type=MovementType.TRANSFER_DISPATCH,
                user_id=user_id,
                lot_id=slice_.lot_id,
                status=StockStatus.IN_TRANSIT,
                reference_type="TRANSFER",
                reference_id=transfer.id,
                notes=f"In transit from {transfer.from_warehouse.name}",
            )

    transfer.status = DocumentStatus.IN_TRANSIT
    transfer.dispatched_at = datetime.now(UTC)
    db.flush()
    return transfer


#: The states a transfer can still be abandoned from — everything before the
#: stock has physically left. Once it is IN_TRANSIT the goods are on a road and
#: the document cannot be wished away; that lorry has to arrive somewhere, so
#: the answer there is to receive it and transfer it back, which leaves both
#: movements in the ledger where an auditor can see them.
CANCELLABLE = (
    DocumentStatus.DRAFT,
    DocumentStatus.PENDING_APPROVAL,
    DocumentStatus.APPROVED,
)


def cancel_transfer(db: Session, transfer_id: int, *, user_id: int) -> StockTransfer:
    """Abandon a transfer that has not shipped.

    APPROVED is included deliberately, and it is the case that mattered: a
    transfer could be approved and then fail every dispatch attempt, with
    nothing on any screen able to close it. It sat in the list as a permanent
    piece of work nobody could finish.

    Nothing is posted or unposted here. A transfer reserves no stock before
    dispatch, so cancelling one moves nothing and frees nothing — it only stops
    the document being offered for dispatch again.
    """
    transfer = _get_transfer(db, transfer_id)
    if transfer.status not in CANCELLABLE:
        raise ConflictError(
            f"Cannot cancel a transfer in state {transfer.status.value}"
        )
    transfer.status = DocumentStatus.CANCELLED
    db.flush()
    return transfer


def _in_transit_legs(db: Session, transfer: StockTransfer) -> list[StockMovement]:
    """The rows dispatch put on the road for this transfer, one per batch.

    The ledger is the record of what shipped, so it is also the record of what
    can be received. Reading it back means a transfer whose quantity was drawn
    from two batches arrives as those two batches, and a receipt can never post
    more than a dispatch sent.

    Filtered to positive IN_TRANSIT dispatch rows: dispatch writes a matching
    negative row at the source, and receipt writes a negative IN_TRANSIT row
    here, so only the positive ones are the outstanding load.
    """
    return list(
        db.scalars(
            select(StockMovement)
            .where(
                StockMovement.reference_type == "TRANSFER",
                StockMovement.reference_id == transfer.id,
                StockMovement.movement_type == MovementType.TRANSFER_DISPATCH,
                StockMovement.status == StockStatus.IN_TRANSIT,
                StockMovement.quantity > 0,
            )
            .order_by(StockMovement.id)
        )
    )


def receive_transfer(db: Session, transfer_id: int, *, user_id: int) -> StockTransfer:
    """Leg 2: IN_TRANSIT becomes AVAILABLE at the destination."""
    transfer = _get_transfer(db, transfer_id)
    if transfer.status != DocumentStatus.IN_TRANSIT:
        raise ConflictError(
            f"Only in-transit transfers can be received (currently {transfer.status.value})"
        )

    # What to put away is read back from the ledger rather than from the line,
    # because a line's `lot_id` cannot express a quantity that travelled as two
    # batches. These are the exact rows dispatch wrote, so receipt lands each
    # batch as itself and the totals cannot drift from what actually shipped.
    legs = _in_transit_legs(db, transfer)
    if not legs:
        raise ConflictError(
            f"{transfer.transfer_number} has no dispatched stock to receive"
        )

    received: dict[int, Decimal] = {}
    for leg in legs:
        product = db.get(Product, leg.product_id)
        put_away_bin = allocation.default_bin(db, transfer.to_warehouse_id, product)

        # Two legs: leave IN_TRANSIT (bin-less), arrive AVAILABLE in a bin.
        ledger.post_movement(
            db,
            product_id=leg.product_id,
            warehouse_id=transfer.to_warehouse_id,
            quantity=-leg.quantity,
            movement_type=MovementType.TRANSFER_RECEIPT,
            user_id=user_id,
            lot_id=leg.lot_id,
            status=StockStatus.IN_TRANSIT,
            reference_type="TRANSFER",
            reference_id=transfer.id,
            notes=f"Received {transfer.transfer_number}",
        )
        ledger.post_movement(
            db,
            product_id=leg.product_id,
            warehouse_id=transfer.to_warehouse_id,
            quantity=leg.quantity,
            movement_type=MovementType.TRANSFER_RECEIPT,
            user_id=user_id,
            bin_id=put_away_bin,
            lot_id=leg.lot_id,
            status=StockStatus.AVAILABLE,
            reference_type="TRANSFER",
            reference_id=transfer.id,
            notes=f"Put away from {transfer.transfer_number}",
        )
        received[leg.product_id] = received.get(
            leg.product_id, Decimal("0")
        ) + leg.quantity

    for line in transfer.lines:
        line.qty_received = received.get(line.product_id, Decimal("0"))

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
        # Refuse now what approval would refuse later. A batch-tracked product
        # with no lot named used to be accepted here and then rejected by every
        # attempt to approve it, leaving a document that could be neither
        # posted nor withdrawn sitting at the top of the approver's queue.
        ledger.validate_line(
            db,
            product_id=line["product_id"],
            quantity=Decimal(line["quantity"]),
            lot_id=line.get("lot_id"),
            bin_id=line.get("bin_id"),
        )
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


def cancel_adjustment(
    db: Session, adjustment_id: int, *, user_id: int
) -> StockAdjustment:
    """Withdraw an adjustment that has not posted.

    The way out for a document an approver will not pass — and the way out for
    the raiser who has spotted their own mistake. Only PENDING_APPROVAL can be
    cancelled: once approved, the adjustment is in the ledger, and the ledger
    is corrected by a reversing entry, never by changing what it says happened.

    Unlike approval, the raiser may cancel their own: separation of duties
    exists to stop one person moving stock unwatched, and withdrawing a
    document moves nothing.
    """
    adjustment = db.scalar(
        select(StockAdjustment)
        .options(selectinload(StockAdjustment.lines))
        .where(StockAdjustment.id == adjustment_id)
    )
    if adjustment is None:
        raise NotFoundError(f"Adjustment {adjustment_id} not found")
    if adjustment.status != DocumentStatus.PENDING_APPROVAL:
        raise ConflictError(
            f"Cannot cancel an adjustment in state {adjustment.status.value}"
        )

    adjustment.status = DocumentStatus.CANCELLED
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
