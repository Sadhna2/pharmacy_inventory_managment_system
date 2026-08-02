"""Stock allocation: FEFO for expiry-tracked products, FIFO otherwise.

FEFO (First Expiring, First Out) is not an optimisation in a pharmacy — you
cannot legally dispense expired medicine, so the earliest-expiring saleable
batch must go first or it becomes a write-off (ARCHITECTURE.md §6.6).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.core.errors import InsufficientStock, ValidationError
from app.models.enums import ReservationStatus, StockStatus, TrackingMode
from app.models.masters import Product, Warehouse
from app.models.stock import Lot, StockBalance, StockReservation

# Refuse to ship stock that will expire before the customer can reasonably use
# it. Configurable per call; this is the sane default.
DEFAULT_MIN_SHELF_LIFE_DAYS = 30


@dataclass
class Allocation:
    """One slice of an allocation: take `quantity` from this lot/bin."""

    lot_id: int | None
    bin_id: int | None
    quantity: Decimal
    expiry_date: date | None = None


def fmt_qty(value: Decimal | int | float) -> str:
    """A quantity as a person would write it: 25, not 25.0000.

    Quantities are Numeric(18,4) because some products are dispensed in
    fractions of a pack. Almost none are, so every message that interpolated
    one raw read as machine output — "need 30.0000, only 25.0000 available".
    """
    dec = Decimal(str(value)).normalize()
    if dec == dec.to_integral_value():
        dec = dec.quantize(Decimal(1))
    return f"{dec:f}"


def available(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    lot_id: int | None = None,
    min_shelf_life_days: int = 0,
) -> Decimal:
    """How much could be allocated right now, in total.

    `allocate()` is the authority and takes row locks because it is about to
    move stock. This is the plain read a form needs in order to say "you only
    have 25 of that batch" *before* anything is written — same filters, no
    locks, no side effects.
    """
    product = db.get(Product, product_id)
    if product is None:
        return Decimal("0")

    stmt = (
        select(StockBalance, Lot)
        .outerjoin(Lot, Lot.id == StockBalance.lot_id)
        .where(
            and_(
                StockBalance.product_id == product_id,
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.status == StockStatus.AVAILABLE,
                StockBalance.qty_on_hand > StockBalance.qty_reserved,
            )
        )
    )
    if lot_id is not None:
        stmt = stmt.where(StockBalance.lot_id == lot_id)
    if product.tracking_mode == TrackingMode.LOT_EXPIRY and min_shelf_life_days > 0:
        stmt = stmt.where(
            Lot.expiry_date > date.today() + timedelta(days=min_shelf_life_days)
        )

    return sum(
        (b.qty_on_hand - b.qty_reserved for b, _ in db.execute(stmt).all()),
        Decimal("0"),
    )


def allocate(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    quantity: Decimal,
    min_shelf_life_days: int = DEFAULT_MIN_SHELF_LIFE_DAYS,
    lot_id: int | None = None,
) -> list[Allocation]:
    """Decide which batches AND BINS fulfil a demand, without reserving them.

    Ordering:
      LOT_EXPIRY -> earliest expiry first (FEFO)
      LOT        -> earliest received first (FIFO)
      NONE       -> whatever is available

    Returning `bin_id` matters as much as `lot_id`: the balance grain includes
    the bin, so an outbound movement that does not name the bin holding the
    stock would look at an empty row and fail. Every outbound path resolves
    its source through here.

    Args:
        min_shelf_life_days: pass 0 for adjustments and write-offs, which must
            be able to touch near-expiry and expired stock.
        lot_id: pin to a specific batch (used by transfers and recalls).

    Rows are locked FOR UPDATE so two concurrent pickers cannot both claim the
    last pack (ARCHITECTURE.md §6.7).
    """
    if quantity <= 0:
        raise ValidationError("Allocation quantity must be positive")

    product = db.get(Product, product_id)
    if product is None:
        raise ValidationError(f"Product {product_id} not found")

    stmt = (
        select(StockBalance, Lot)
        .outerjoin(Lot, Lot.id == StockBalance.lot_id)
        .where(
            and_(
                StockBalance.product_id == product_id,
                StockBalance.warehouse_id == warehouse_id,
                # Only AVAILABLE stock is sellable — never quarantined,
                # damaged or in-transit.
                StockBalance.status == StockStatus.AVAILABLE,
                StockBalance.qty_on_hand > StockBalance.qty_reserved,
            )
        )
        .with_for_update(of=StockBalance)
    )

    if lot_id is not None:
        stmt = stmt.where(StockBalance.lot_id == lot_id)

    if product.tracking_mode == TrackingMode.LOT_EXPIRY:
        if min_shelf_life_days > 0:
            # Never ship stock that expires before the customer can use it.
            stmt = stmt.where(
                Lot.expiry_date > date.today() + timedelta(days=min_shelf_life_days)
            )
        stmt = stmt.order_by(Lot.expiry_date.asc(), Lot.received_at.asc())
    elif product.tracking_mode == TrackingMode.LOT:
        stmt = stmt.order_by(Lot.received_at.asc())
    else:
        stmt = stmt.order_by(StockBalance.id.asc())

    remaining = Decimal(quantity)
    allocations: list[Allocation] = []

    for balance, lot in db.execute(stmt).all():
        if remaining <= 0:
            break
        available = balance.qty_on_hand - balance.qty_reserved
        if available <= 0:
            continue

        take = min(available, remaining)
        allocations.append(
            Allocation(
                lot_id=balance.lot_id,
                bin_id=balance.bin_id,
                quantity=take,
                expiry_date=lot.expiry_date if lot else None,
            )
        )
        remaining -= take

    if remaining > 0:
        allocated = Decimal(quantity) - remaining
        where = db.get(Warehouse, warehouse_id)
        shelf = (
            f" with at least {min_shelf_life_days} days of shelf life left"
            if min_shelf_life_days > 0
            else ""
        )
        batch = ""
        if lot_id is not None:
            lot = db.get(Lot, lot_id)
            batch = f" in batch {lot.lot_code}" if lot else ""
        raise InsufficientStock(
            f"{product.sku} — {product.name}: need {fmt_qty(quantity)}, "
            f"only {fmt_qty(allocated)} available{batch} at "
            f"{where.name if where else f'warehouse {warehouse_id}'}{shelf}."
        )

    return allocations


def reserve(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    quantity: Decimal,
    sales_order_line_id: int | None = None,
    ttl_hours: int = 24,
    min_shelf_life_days: int = DEFAULT_MIN_SHELF_LIFE_DAYS,
) -> list[StockReservation]:
    """Allocate AND hold the stock, so nobody else can take it.

    Increments `qty_reserved` on the balance rows. The balance CHECK constraint
    guarantees reserved can never exceed on-hand.
    """
    allocations = allocate(
        db,
        product_id=product_id,
        warehouse_id=warehouse_id,
        quantity=quantity,
        min_shelf_life_days=min_shelf_life_days,
    )

    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    reservations: list[StockReservation] = []

    for alloc in allocations:
        balance = _balance_row(
            db,
            product_id=product_id,
            warehouse_id=warehouse_id,
            lot_id=alloc.lot_id,
            bin_id=alloc.bin_id,
        )
        if balance is None:
            raise InsufficientStock("Stock disappeared during reservation")

        balance.qty_reserved += alloc.quantity

        reservation = StockReservation(
            product_id=product_id,
            warehouse_id=warehouse_id,
            lot_id=alloc.lot_id,
            bin_id=alloc.bin_id,
            quantity=alloc.quantity,
            sales_order_line_id=sales_order_line_id,
            status=ReservationStatus.ACTIVE,
            expires_at=expires_at,
        )
        db.add(reservation)
        reservations.append(reservation)

    db.flush()
    return reservations


def _balance_row(
    db: Session,
    *,
    product_id: int,
    warehouse_id: int,
    lot_id: int | None,
    bin_id: int | None,
) -> StockBalance | None:
    """Fetch and lock the exact balance row for a grain.

    Matching on bin as well as lot matters: the same product+lot can sit in
    several bins, and updating the wrong row would corrupt qty_reserved.
    """
    return db.scalar(
        select(StockBalance)
        .where(
            and_(
                StockBalance.product_id == product_id,
                StockBalance.warehouse_id == warehouse_id,
                StockBalance.status == StockStatus.AVAILABLE,
                StockBalance.lot_id.is_(None)
                if lot_id is None
                else StockBalance.lot_id == lot_id,
                StockBalance.bin_id.is_(None)
                if bin_id is None
                else StockBalance.bin_id == bin_id,
            )
        )
        .with_for_update()
    )


def default_bin(db: Session, warehouse_id: int, product: Product) -> int | None:
    """Where inbound stock lands when the caller does not name a bin.

    Cold-chain products must go to a cold-chain bin — that rule is enforced in
    ledger.post_movement(), so picking the right one here avoids a needless
    rejection on receipt.
    """
    from app.models.masters import Bin, StorageCondition

    needs_cold = product.storage_condition == StorageCondition.COLD_CHAIN
    return db.scalar(
        select(Bin.id)
        .where(
            Bin.warehouse_id == warehouse_id,
            Bin.is_active.is_(True),
            Bin.is_quarantine.is_(False),
            Bin.is_cold_chain.is_(needs_cold),
        )
        .order_by(Bin.id)
        .limit(1)
    )


def release(db: Session, reservation: StockReservation) -> None:
    """Give reserved stock back without shipping it."""
    if reservation.status != ReservationStatus.ACTIVE:
        return

    balance = _balance_row(
        db,
        product_id=reservation.product_id,
        warehouse_id=reservation.warehouse_id,
        lot_id=reservation.lot_id,
        bin_id=reservation.bin_id,
    )
    if balance is not None:
        balance.qty_reserved -= reservation.quantity

    reservation.status = ReservationStatus.RELEASED
    db.flush()


def consume(db: Session, reservation: StockReservation) -> None:
    """Mark a reservation as fulfilled.

    Called immediately before the SALE_ISSUE movement: the reserved quantity is
    dropped so the ledger write reduces on-hand without leaving a dangling hold.
    """
    if reservation.status != ReservationStatus.ACTIVE:
        raise ValidationError("Reservation is no longer active")

    balance = _balance_row(
        db,
        product_id=reservation.product_id,
        warehouse_id=reservation.warehouse_id,
        lot_id=reservation.lot_id,
        bin_id=reservation.bin_id,
    )
    if balance is not None:
        balance.qty_reserved -= reservation.quantity

    reservation.status = ReservationStatus.CONSUMED
    db.flush()
