"""THE single write path into stock_movements.

Nothing else in this codebase may insert into the ledger. That rule is what
makes the audit trail trustworthy, and it is enforced two ways:
  - by convention here (one function), and
  - by the database, which rejects UPDATE and DELETE outright.

Corrections are reversing entries, never edits (ARCHITECTURE.md §6.4).
"""

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import (
    ConflictError,
    InsufficientStock,
    NotFoundError,
    ValidationError,
)
from app.models.enums import MovementType, StockStatus, TrackingMode
from app.models.masters import Bin, Product, StorageCondition
from app.models.stock import Lot, StockMovement

# Movement types that must not be issued directly by an API caller; they are
# produced only as the paired legs of a higher-level operation.
_PAIRED_ONLY = {MovementType.STATUS_CHANGE}

# reference_type marking a row as the correction of an earlier one.
_REVERSAL = "REVERSAL"


def post_movement(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    quantity: Decimal,
    movement_type: MovementType,
    user_id: int,
    bin_id: int | None = None,
    lot_id: int | None = None,
    serial_id: int | None = None,
    status: StockStatus = StockStatus.AVAILABLE,
    unit_cost: Decimal | None = None,
    reference_type: str | None = None,
    reference_id: int | None = None,
    idempotency_key: str | None = None,
    occurred_at: datetime | None = None,
    notes: str | None = None,
) -> StockMovement:
    """Append one row to the ledger.

    The balance projection is updated by a database trigger inside this same
    transaction, so on return the balance is already correct.

    Raises:
        ValidationError: tracking-mode or cold-chain rules violated.
        InsufficientStock: the movement would drive the balance negative.
        NotFoundError: product, bin or lot does not exist.
    """
    if quantity == 0:
        raise ValidationError("Movement quantity cannot be zero")

    product = db.get(Product, product_id)
    if product is None:
        raise NotFoundError(f"Product {product_id} not found")

    _validate_tracking(db, product, lot_id, serial_id, quantity)
    _validate_storage(db, product, bin_id)

    movement = StockMovement(
        movement_type=movement_type,
        product_id=product_id,
        warehouse_id=warehouse_id,
        bin_id=bin_id,
        status=status,
        # The DB trigger overwrites this from the product; set it so the
        # NOT NULL constraint is satisfied on the way in.
        tracking_mode=product.tracking_mode,
        lot_id=lot_id,
        serial_id=serial_id,
        quantity=Decimal(quantity),
        unit_cost=unit_cost,
        reference_type=reference_type,
        reference_id=reference_id,
        idempotency_key=idempotency_key,
        occurred_at=occurred_at or datetime.now(UTC),
        created_by=user_id,
        notes=notes,
    )
    db.add(movement)

    try:
        db.flush()
    except IntegrityError as exc:
        db.rollback()
        message = str(exc.orig)
        if "insufficient stock" in message or "chk_no_negative" in message:
            raise InsufficientStock(
                f"Not enough stock of {product.sku} at warehouse {warehouse_id}"
            ) from exc
        if "chk_tracking" in message:
            raise ValidationError(
                f"{product.sku} is tracked as {product.tracking_mode.value}; "
                "the required lot or serial was missing"
            ) from exc
        if "chk_serial_qty" in message:
            raise ValidationError(
                "Serial-tracked products move exactly one unit at a time"
            ) from exc
        if "idempotency_key" in message:
            existing = db.scalar(
                select(StockMovement).where(
                    StockMovement.idempotency_key == idempotency_key
                )
            )
            if existing:
                return existing
        raise

    return movement


def post_status_change(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    quantity: Decimal,
    from_status: StockStatus,
    to_status: StockStatus,
    user_id: int,
    lot_id: int | None = None,
    bin_id: int | None = None,
    movement_type: MovementType = MovementType.STATUS_CHANGE,
    reference_type: str | None = None,
    reference_id: int | None = None,
    notes: str | None = None,
) -> tuple[StockMovement, StockMovement]:
    """Move stock between statuses as TWO balanced rows.

    Quantity is conserved: nothing is created or destroyed, it just becomes
    unsellable (or sellable again). See ARCHITECTURE.md §21.2.
    """
    if quantity <= 0:
        raise ValidationError("Status-change quantity must be positive")

    out_leg = post_movement(
        db,
        product_id=product_id,
        warehouse_id=warehouse_id,
        quantity=-Decimal(quantity),
        movement_type=movement_type,
        user_id=user_id,
        bin_id=bin_id,
        lot_id=lot_id,
        status=from_status,
        reference_type=reference_type,
        reference_id=reference_id,
        notes=notes,
    )
    in_leg = post_movement(
        db,
        product_id=product_id,
        warehouse_id=warehouse_id,
        quantity=Decimal(quantity),
        movement_type=movement_type,
        user_id=user_id,
        bin_id=bin_id,
        lot_id=lot_id,
        status=to_status,
        reference_type=reference_type,
        reference_id=reference_id,
        notes=notes,
    )
    return out_leg, in_leg


def reverse_movement(
    db: Session, movement_id: int, *, user_id: int, reason: str
) -> StockMovement:
    """Correct a mistake by posting the opposite entry.

    This is the ONLY way to undo something. The original row stays in the
    ledger forever, which is the point.
    """
    original = db.get(StockMovement, movement_id)
    if original is None:
        raise NotFoundError(f"Movement {movement_id} not found")

    # A reversal is itself an ordinary ledger row, so without these two guards
    # a second click would post a second opposing entry and the "correction"
    # would overshoot into a fresh error.
    if original.reference_type == _REVERSAL:
        raise ConflictError(
            f"Movement {movement_id} is itself a reversal. Reverse the entry it "
            f"corrects, or post an adjustment."
        )

    already = db.scalar(
        select(StockMovement.id).where(
            StockMovement.reference_type == _REVERSAL,
            StockMovement.reference_id == original.id,
        )
    )
    if already is not None:
        raise ConflictError(
            f"Movement {movement_id} was already reversed by movement {already}"
        )

    return post_movement(
        db,
        product_id=original.product_id,
        warehouse_id=original.warehouse_id,
        quantity=-original.quantity,
        movement_type=original.movement_type,
        user_id=user_id,
        bin_id=original.bin_id,
        lot_id=original.lot_id,
        serial_id=original.serial_id,
        status=original.status,
        unit_cost=original.unit_cost,
        reference_type=_REVERSAL,
        reference_id=original.id,
        notes=f"Reversal of movement {original.id}: {reason}",
    )


# --- validation -------------------------------------------------------------


def _validate_tracking(
    db: Session,
    product: Product,
    lot_id: int | None,
    serial_id: int | None,
    quantity: Decimal,
) -> None:
    """Application-level mirror of the DB CHECK, for friendly error messages."""
    mode = product.tracking_mode

    if mode == TrackingMode.NONE:
        if lot_id is not None or serial_id is not None:
            raise ValidationError(
                f"{product.sku} is not batch-tracked; do not supply a lot or serial"
            )
    elif mode in (TrackingMode.LOT, TrackingMode.LOT_EXPIRY):
        if lot_id is None:
            raise ValidationError(
                f"{product.sku} is batch-tracked — a lot is required"
            )
        if serial_id is not None:
            raise ValidationError(f"{product.sku} does not use serial numbers")

        lot = db.get(Lot, lot_id)
        if lot is None:
            raise NotFoundError(f"Lot {lot_id} not found")
        if lot.product_id != product.id:
            raise ValidationError(
                f"Lot {lot.lot_code} belongs to a different product"
            )
        if mode == TrackingMode.LOT_EXPIRY and lot.expiry_date is None:
            raise ValidationError(
                f"{product.sku} requires an expiry date on every batch"
            )
    elif mode == TrackingMode.SERIAL:
        if serial_id is None:
            raise ValidationError(f"{product.sku} requires a serial number")
        if abs(quantity) != 1:
            raise ValidationError(
                "Serial-tracked products move exactly one unit at a time"
            )


def _validate_storage(db: Session, product: Product, bin_id: int | None) -> None:
    """Cold-chain products may only be binned in cold-chain locations."""
    if bin_id is None or product.storage_condition != StorageCondition.COLD_CHAIN:
        return

    bin_ = db.get(Bin, bin_id)
    if bin_ is None:
        raise NotFoundError(f"Bin {bin_id} not found")
    if not bin_.is_cold_chain:
        raise ValidationError(
            f"{product.sku} requires cold-chain storage (2-8 C) and cannot be "
            f"placed in ambient bin {bin_.code}"
        )
